"""U6a: network helpers — model-endpoint preflight + network-failure classification.

Three lost runs in one day: a dead model endpoint killed the stage, and
workers could hang silently until the 1h hard timeout. These helpers make
the engine (1) check the endpoint BEFORE spending a worker's context, and
(2) recognize "the network died" in a worker log so the stage can retry
with backoff instead of escalating.

No new dependencies — stdlib ``urllib`` only.
"""
from __future__ import annotations

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


def is_network_failure(text: str) -> bool:
    """True when a worker log tail contains a known network-failure marker."""
    if not text:
        return False
    return any(marker in text for marker in NETWORK_MARKERS)


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


__all__ = [
    "NET_RETRY_BACKOFFS",
    "NET_RETRY_LIMIT_DEFAULT",
    "NETWORK_MARKERS",
    "NO_OUTPUT_TIMEOUT_DEFAULT",
    "PREFLIGHT_HTTP_TIMEOUT",
    "PREFLIGHT_TIMEOUT_DEFAULT",
    "endpoint_reachable",
    "is_network_failure",
    "model_endpoint_url",
    "parse_model_from_cmd",
    "read_log_tail",
]
