"""U6a: network helpers — model-endpoint preflight + network-failure classification.

Three lost runs in one day: a dead model endpoint killed the stage, and
workers could hang silently until the 1h hard timeout. These helpers make
the engine (1) check the endpoint BEFORE spending a worker's context, and
(2) recognize "the network died" in a worker log so the stage can retry
with backoff instead of escalating.

No new dependencies — stdlib ``urllib`` only.

CARRY IMP1: the module also owns the ONE provider-failure classifier
(``classify_failure`` — network / auth / transient) that both the
``worker-exit:`` diagnostic and the engine's retry decision read. Auth is
not a network problem, but it is the same "why did the worker die" question
and the two readers must never drift.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

# Automation defaults (config keys under ``automation:`` in config.yaml).
PREFLIGHT_TIMEOUT_DEFAULT = 600   # automation.preflight_timeout_seconds
NO_OUTPUT_TIMEOUT_DEFAULT = 900   # automation.no_output_timeout_seconds
NET_RETRY_LIMIT_DEFAULT = 3       # automation.net_retry_limit
PREFLIGHT_HTTP_TIMEOUT = 5.0      # seconds per GET {baseURL}/models

# Backoff between network retries: 1st 30s, 2nd 60s, 3rd 120s.
NET_RETRY_BACKOFFS: tuple[float, ...] = (30, 60, 120)

# Marker strings that mean "the model endpoint / network is down" in a
# worker (opencode) log tail. Any ONE match classifies the death as
# network — these strings come from opencode's own error output.
NETWORK_MARKERS: tuple[str, ...] = (
    "Cannot connect to API",
    "ECONNREFUSED",
    "fetch failed",
    "Connection reset",
    "ETIMEDOUT",
)

# CARRY IMP1 (R12): connect/reset/timeout are network even without the
# exact vendor strings above. ``_`` is a word char, so ``\btimeout\b``
# does not match a config key like ``no_output_timeout``. AUD3: the
# provider-side phrasings below are network too — they mean the request
# never reached the model.
_NETWORK_RE = re.compile(
    r"(?i)\b(?:connect(?:ion)?|reset|timeout|timed\s+out"
    r"|upstream\s+request\s+failed|all\s+workers\s+are\s+unavailable)\b"
)

# Auth is a separate class: a dead credential is NOT fixed by a backoff
# retry — it must stop the stage with a BLOCKED note (R12). AUD1: a bare
# word is not auth — ``unauthorized`` alone is ordinary log text (a failing
# test name, a rejected request to an auth endpoint).
# AUD1 (extends the bare-word fix): the code must be its own TOKEN, not an
# identifier fragment. ``test_401_credential`` / ``assert_403`` in a project's
# own log would otherwise match the word AND the code and block the stage.
# PA1/AUD1: the code alone is not enough either — it needs an HTTP/status
# context (``HTTP 401``, ``statusCode: 403``, ``code: 403``) or to lead the
# line (a server's bare reply). AUD1 narrows it once more: the context must
# ALSO carry a credential phrase. ``HTTP 401`` / ``GET /login -> HTTP 401``
# name a rejected request, not a dead key; K12 requires 401/403 *with* the
# text about credential/key/unauthorized. A bare ``assert 401 == 200`` under
# a test named ``test_unauthorized_access_is_rejected`` is a project's own
# failing test, not a dead key.
_AUTH_PHRASE_RE = re.compile(
    r"(?i)(?:invalid[ \t_-]+)?"
    r"(?:credential|api[ _-]?key|unauthorized|forbidden)\b"
)
_AUTH_CODE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:http(?:/[0-9.]+)?|statuscode|status|code|error)\b"
    r"[ \t]*[:=]?[ \t]*(?:401|403)(?!\w)"
    r"|^[ \t]*(?:401|403)(?!\w)"
    r")"
)

# Transient: HTTP 5xx and rate limiting (R12, K12). A bare number is
# ordinary text — ``1500 passed``, ``500 passed`` and ``took 429ms`` are
# NOT failures; the code needs an HTTP/status/code context or the
# canonical reason phrase. AUD3: the provider's generic ``5xx`` token
# (``statusCode: 5xx``) is a server error too — the audit named that
# literal, not only the concrete codes.
_TRANSIENT_RE = re.compile(
    r"(?i)(?:"
    r"\bhttp(?:/[0-9.]+|\s+status)?\s*[:=]?\s*(?:429|5\d\d|5xx)\b"
    r"|\b(?:status|code)[\"']?\s*[:=]\s*(?:429|5\d\d|5xx)\b"
    r"|\bstatuscode\b[\"']?\s*[:=]\s*(?:429|5\d\d|5xx)\b"
    r"|\b(?:429|500|502|503|504)\s+(?:internal\s+server\s+error|"
    r"service\s+unavailable|bad\s+gateway|gateway\s+timeout|"
    r"too\s+many\s+requests)\b"
    r")"
)


# R12: the classes that share the backoff retry. ``transient`` is a provider
# overload — the requirement calls it network. ONE tuple for the diagnostic
# (``is_network_failure``), ``_worker_exit`` and both engine branches: a
# later class added here cannot be missed at a retry decision written inline.
RETRY_FAILURE_CLASSES: tuple[str, ...] = ("network", "transient")


def _auth_lines(text: str) -> list[str]:
    """The ONE auth rule: a line with a 401/403 status AND a credential word.

    PA1: an auth context is an HTTP/status/code/error anchor or a code that
    leads the line — a bare number in ``assert 401 == 200`` or in a URL is
    not one. AUD1: the context alone is still not auth — the line must also
    name the credential (K12), so ``HTTP 401`` on a rejected login stays
    ordinary output. Same-line keeps the class and the evidence
    (``failure_evidence``) in agreement by construction: if no single line
    names the failure, the note could not quote it either.
    """
    return [
        ln for ln in (text or "").splitlines()
        if _AUTH_CODE_RE.search(ln) and _AUTH_PHRASE_RE.search(ln)
    ]


def _is_network_text(text: str) -> bool:
    """The original marker set plus the generic connect/reset/timeout words."""
    return any(marker in text for marker in NETWORK_MARKERS) or bool(
        _NETWORK_RE.search(text)
    )


def failure_evidence(text: str, limit: int = 3) -> list[str]:
    """AUD4: the log lines that name an auth failure, else the last few.

    The BLOCKED note quotes evidence instead of a generic sentence — the
    owner must see WHY the stage stopped without opening the log. Each
    line is stripped and capped so one 10 KiB line cannot flood the note.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    hits = [ln.strip() for ln in _auth_lines(text)]
    picked = hits or lines[-limit:]
    return [ln[:300] for ln in picked[-limit:]]


def parse_model_from_cmd(cmd: list[str]) -> str | None:
    """Return the value after ``--model`` in a command list, if present."""
    for i, arg in enumerate(cmd):
        if arg == "--model" and i + 1 < len(cmd):
            return str(cmd[i + 1])
    return None


def model_endpoint_url(model_spec: str, opencode_path: Path) -> str | None:
    """Resolve ``provider/model`` → the provider's ``options.baseURL``.

    Returns None (preflight skipped) when: no ``/`` in the spec, the
    opencode.json is missing/unreadable, the provider is unknown, or the
    provider has no ``baseURL`` (cloud provider — nothing to probe).
    """
    if not model_spec or "/" not in model_spec:
        return None
    provider_name = str(model_spec).split("/", 1)[0]

    # Lazy import: keeps this module light and avoids loading the whole
    # awf.api package for callers that only need the classifier.
    from .api.model_check import _load_opencode_config

    providers, _agents = _load_opencode_config(Path(opencode_path))
    provider_cfg = providers.get(provider_name)
    if not isinstance(provider_cfg, dict):
        return None
    options = provider_cfg.get("options")
    if not isinstance(options, dict):
        return None
    base_url = options.get("baseURL")
    if not isinstance(base_url, str) or not base_url:
        return None
    return base_url


def endpoint_reachable(url: str, timeout: float = PREFLIGHT_HTTP_TIMEOUT) -> bool:
    """GET ``{url}/models`` — any HTTP response means the endpoint is alive.

    Network-level failures (DNS, refused, reset, timeout) mean dead.
    HTTP error codes (404, 500, ...) still mean the server answered, so
    they count as alive: the worker would have reached it too.
    """
    probe_url = url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(probe_url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        # The endpoint answered (with an error) — alive for our purposes.
        return True
    except Exception:
        return False


def is_oom_exit(rc: int | None, text: str) -> bool:
    """PA1: the exit looks like a memory kill, not a provider refusal.

    ``rc`` 137 (128+SIGKILL) / -9 (a raw SIGKILL) or the shell's ``Killed``
    line means the process was killed before it could act on a credential.
    The rule lives here so the diagnostic (``classify_worker_exit``) and the
    engine's decision use ONE spelling.
    """
    return rc in (137, -9) or bool(re.search(r"(?m)^Killed\b", text or ""))


def classify_failure(text: str, rc: int | None = None) -> str:
    """CARRY IMP1 (R12): one of ``network`` / ``auth`` / ``transient`` / "".

    Priority network → auth → transient, with TWO deliberate exceptions:
    D6 — a 5xx/429 next to a 401/403 is an endpoint problem, not a dead key,
    so the mixture returns ``network`` and takes the backoff retry instead
    of stopping the stage as BLOCKED. PA1 — an OOM/signal exit outranks the
    auth text (``is_oom_exit``): the process died before it could act on a
    credential, so the log does not speak for it. ``transient`` alone is the
    5xx/429/overloaded family; the engine retries it like ``network``
    (``is_network_failure``).
    """
    if not text:
        return ""
    if _is_network_text(text):
        return "network"
    has_auth = bool(_auth_lines(text)) and not is_oom_exit(rc, text)
    has_transient = bool(
        _TRANSIENT_RE.search(text) or "overloaded" in text.lower()
    )
    if has_auth and has_transient:
        return "network"  # D6: never BLOCK on a 5xx+401 mixture
    if has_auth:
        return "auth"
    if has_transient:
        return "transient"
    return ""


def is_network_failure(text: str) -> bool:
    """True when a worker log tail is a network-class death (R12).

    ``transient`` (HTTP 5xx / 429 / overloaded) shares the backoff retry,
    so it counts as a network failure too — the retry policy must not
    differ from the diagnostic line.
    """
    return classify_failure(text) in RETRY_FAILURE_CLASSES


def is_preflight_timeout(text: str) -> bool:
    """True when an exception text is the U6a preflight timeout (U6c).

    The preflight dies BEFORE the worker starts, so there is no worker log
    to classify — the exception text is the only evidence. The message
    ("Model endpoint <url> unreachable for <n>s — worker not started")
    embeds the URL between "endpoint" and "unreachable", so the match
    requires BOTH words (plus a literal "preflight" fallback for
    rewordings). Watchdog and hard-timeout messages contain neither, so
    they stay non-network.
    """
    if not text:
        return False
    lowered = str(text).lower()
    if "preflight" in lowered:
        return True
    return "unreachable" in lowered and "endpoint" in lowered


def read_log_tail(path: Path, max_bytes: int = 65536, start_offset: int = 0) -> str:
    """Last ``max_bytes`` of a file (from ``start_offset``) as text.

    ``start_offset`` skips earlier content — the worker log is append-mode
    (dogfood-11), so without it a previous run's network marker would leak
    into the next run's death classification. ``""`` when missing/unreadable.
    """
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(start_offset, size - max_bytes))
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_log_from(path: Path, start_offset: int = 0) -> str:
    """Everything in the file after ``start_offset`` (K13).

    The suite gate needs the whole current attempt, not just the tail: a
    full-suite invocation may sit anywhere between ``log_start_offset`` and
    the end. ``""`` when missing/unreadable.
    """
    try:
        with open(path, "rb") as f:
            f.seek(max(start_offset, 0))
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


__all__ = [
    "NET_RETRY_BACKOFFS",
    "NET_RETRY_LIMIT_DEFAULT",
    "NETWORK_MARKERS",
    "NO_OUTPUT_TIMEOUT_DEFAULT",
    "PREFLIGHT_HTTP_TIMEOUT",
    "PREFLIGHT_TIMEOUT_DEFAULT",
    "RETRY_FAILURE_CLASSES",
    "endpoint_reachable",
    "classify_failure",
    "failure_evidence",
    "is_network_failure",
    "is_oom_exit",
    "is_preflight_timeout",
    "model_endpoint_url",
    "parse_model_from_cmd",
    "read_log_from",
    "read_log_tail",
]
