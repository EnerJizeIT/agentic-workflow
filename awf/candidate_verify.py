"""R2.4 (roadmap R2): verify the CANDIDATE and bind the approval.

The commit gate used to trust a verification that ran in the ORIGINAL,
dirty worktree. That success does not prove the candidate commit is good:
a pre-existing dependency may be excluded from the candidate (foreign WIP
is left out by design), a file may be degraded to its full worktree
content, or the tree may have moved between the check and the commit.

This module is the ONE place that materialises the candidate and pins the
evidence an approval is bound to:

* :func:`materialize_candidate` builds the :class:`CandidatePlan` through
  the R2.3 assembler (isolated index, user index untouched) and checks the
  resulting tree into a THROWAWAY worktree — the reproducible artefact the
  mandatory candidate check must run against.
* :func:`build_evidence` records the binding fields: candidate tree OID,
  the TODO contract digest, the environment/setup digest, the run
  generation, the unit revision, the exact file-set digest, the degraded
  (full-file) paths, and the candidate-check outcome. The digest of these
  fields is what the APPROVE signal carries (``evidence_digest``).
* :func:`validate_evidence` re-derives the same fields from the current
  tree and refuses the commit when any of them moved: a blob/mode/path
  change, a contract change, or a moved HEAD (replan, not a commit on the
  new HEAD).

Generated / untracked build dependencies are NOT copied into the candidate
— they are described in the evidence ``manifest`` (path + reason), the way
roadmap R2.4 requires.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import _proc, commit_delta, paths, run_state
from . import config as cfg_mod
from ._atomic import atomic_write_text
from .infra.git_model import GIT_TIMEOUT
from .unit_contract import parse_todo_contract
from .verify import _verify_cmd_timeout, candidate_check_commands, verify_command_keys

#: The evidence file the supervisor's verifier writes and the commit gate
#: reads: ``.agentic/context/CANDIDATE-<todo>.json``.
EVIDENCE_PREFIX = "CANDIDATE"

#: Check statuses (a closed set, not free text).
CHECK_OK = "ok"
CHECK_FAILED = "failed"
CHECK_UNAVAILABLE = "unavailable"


# ─── git helpers (isolated index / throwaway worktree) ───────────────────


def _run_git(
    project_dir: Path, args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT,
        env=env,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()


# ─── binding fields ──────────────────────────────────────────────────────


def current_head(project_dir: Path) -> str:
    return commit_delta.head_sha(project_dir)


def read_todo_text(project_dir: Path, todo_id: str) -> str:
    """The TODO source (inbox, or the archive) — "" when neither exists."""
    for f in (
        paths.inbox(project_dir) / f"{todo_id}.md",
        paths.done_dir(project_dir) / todo_id / "TODO.md",
    ):
        if f.is_file():
            try:
                return f.read_text(encoding="utf-8")
            except OSError:
                return ""
    return ""


def contract_digest(project_dir: Path, todo_id: str) -> str:
    """Digest of the TODO's contract block (verify/gates/prove_red/files).

    The block is what tells the candidate check WHAT to run and what the
    unit declared — a change to it invalidates evidence even when the code
    did not move. No block → the digest of an empty contract (a fixed,
    deterministic marker).
    """
    text = read_todo_text(project_dir, todo_id)
    try:
        contract, unknown = parse_todo_contract(text)
    except ValueError:
        contract, unknown = None, []
    if contract is None:
        return _sha256_text("")
    canonical = json.dumps(
        {"contract": contract, "unknown": sorted(unknown)}, sort_keys=True
    )
    return _sha256_text(canonical)


def environment_digest(project_dir: Path) -> str:
    """Digest of the verification setup the candidate check relies on.

    The configured verify commands are the environment the check runs in;
    a change to them (a new test command, a dropped lint) invalidates the
    evidence the approval was pinned to.
    """
    config = _config(project_dir)
    parts = [f"{key}={cfg_mod.get(config, f'verification.{key}', '')}"
             for key in verify_command_keys()]
    return _sha256_text("\n".join(parts))


def unit_revision(project_dir: Path, todo_id: str, baseline_sha: str) -> str:
    """The revision identity of the unit: TODO bytes + baseline sha."""
    text = read_todo_text(project_dir, todo_id)
    return _sha256_text(f"{todo_id}\0{baseline_sha}\0{text}")


def _config(project_dir: Path) -> dict:
    """The parsed project config, or {} when absent/unreadable."""
    try:
        return cfg_mod.load(project_dir)
    except Exception:
        return {}


def candidate_commands(project_dir: Path, todo_id: str) -> list[str]:
    """The commands the mandatory candidate check runs — contract first.

    Delegates to :func:`awf.verify.candidate_check_commands` (one home for
    the contract/config resolution). No source → ``[]``.
    """
    text = read_todo_text(project_dir, todo_id)
    try:
        contract, _unknown = parse_todo_contract(text)
    except ValueError:
        contract = None
    return candidate_check_commands(_config(project_dir), contract)


# ─── materialization ─────────────────────────────────────────────────────


@dataclass
class Materialization:
    """The candidate artefact: the plan, its tree OID and a throwaway
    worktree the mandatory check runs in.

    ``refusal`` non-empty means there is NO candidate to check: no baseline,
    a model refusal (unsupported state / mixed-ownership rename pair), or a
    candidate build error (``plan.build_error``). It is NEVER "nothing to
    verify" — an empty tree for a unit that HAS files whose build failed is a
    refusal, not a silent skip (F-QA-8). ``degraded`` lists the delta files
    that fell back to their full worktree content.
    """

    plan: commit_delta.CandidatePlan | None
    candidate_tree: str
    worktree: Path | None
    refusal: str = ""
    degraded: tuple[str, ...] = ()


def _build_plan(
    project_dir: Path, todo_id: str, baseline_sha: str, claim: tuple[str, ...]
) -> tuple[commit_delta.CandidatePlan | None, str]:
    """The full CandidatePlan (``build_tree=True``) or (None, refusal).

    R2.4 / добор 0222: the prod path consumes the WHOLE plan — candidate
    tree, typed operations, degraded paths — never the flattened split.

    One home: delegates to :func:`awf.commit_plan._candidate_plan`, the same
    builder the commit gate uses. That keeps the approve-time candidate and
    the gate-time candidate identical by construction — in particular both
    resolve the unit's claim channel (:func:`commit_plan._claimed_paths`) the
    SAME way, so a claimed predecessor change is in both trees or neither.
    An empty ``claim`` means "resolve the claim channel" (``None``).
    """
    from . import commit_plan

    return commit_plan._candidate_plan(
        project_dir, baseline_sha, todo_id, claim=claim or None
    )


def materialize_candidate(
    project_dir: Path,
    todo_id: str,
    baseline_sha: str = "",
    *,
    claim: tuple[str, ...] | list[str] = (),
    worktree_dir: Path | None = None,
    checkout: bool = True,
) -> Materialization:
    """Build the candidate tree and check it into a throwaway worktree.

    The user's ``.git/index`` is NEVER opened: the tree is written through
    an isolated ``GIT_INDEX_FILE`` (R2.3) and the worktree is filled with
    ``git checkout-index`` against that same isolated index. No ref is
    created, no commit object is made, nothing is staged in the user's
    index — the candidate is inert.

    ``worktree_dir`` — an explicit destination (tests pass ``tmp_path``);
    otherwise a fresh ``tempfile.mkdtemp`` under the system temp. The
    caller owns cleanup when it passes a dir; a self-made dir is removed on
    every outcome by the caller that owns the mkdtemp — :func:`build_evidence`
    (``try/finally``).

    ``checkout`` — lay the tree down as files (the mandatory check needs
    them). ``False`` builds only the plan's tree OID and degraded set:
    :func:`validate_evidence` never runs a command, so it must not pay for
    (or clean up) a full repo copy. With ``checkout=False`` no worktree is
    created and ``Materialization.worktree`` is always ``None``.
    """
    if not baseline_sha:
        return Materialization(None, "", None, "no baseline — candidate cannot be built")
    plan, refusal = _build_plan(project_dir, todo_id, baseline_sha, tuple(claim))
    if plan is None:
        return Materialization(None, "", None, refusal)
    tree = plan.candidate_tree
    if not tree:
        # F-QA-8: an empty tree is AMBIGUOUS — it is the legitimate "no
        # files, nothing to verify" state AND the state after a build
        # failure. The plan carries the build error separately; when it is
        # set the candidate could not be assembled at all, so the call is a
        # refusal (the mandatory check must not be skipped for a unit that
        # HAS files — that FAIL-OPEN is what R2.4 exists to close). Only a
        # genuinely file-less candidate stays a valid "nothing to verify".
        if plan.build_error:
            return Materialization(plan, "", None, plan.build_error, tuple(plan.degraded))
        return Materialization(plan, "", None, "", tuple(plan.degraded))
    if not checkout:
        # Validation needs only ``candidate_tree`` + ``degraded``. Checking
        # the tree out here was pure waste (a full copy of the repo per
        # verify pack) and the sole reason the F-QA-6 cleanup branch existed.
        return Materialization(plan, tree, None, "", tuple(plan.degraded))
    self_made = worktree_dir is None
    dest = worktree_dir if worktree_dir is not None else Path(
        tempfile.mkdtemp(prefix=f"awf-candidate-wt-{todo_id}-")
    )
    err = _checkout_tree(project_dir, tree, dest)
    if err:
        # F-QA-7: the checkout failed AFTER the mkdtemp dir was created. The
        # module's cleanup contract is "a self-made dir is removed on EVERY
        # outcome"; returning ``worktree=None`` makes the caller read
        # "nothing to clean" and leaks a full (partial) checkout. Remove the
        # self-made dir here — a caller-supplied ``worktree_dir`` is the
        # caller's to clean.
        if self_made:
            _discard_worktree(dest)
        return Materialization(plan, tree, None, err, tuple(plan.degraded))
    return Materialization(plan, tree, dest, "", tuple(plan.degraded))


def _discard_worktree(dest: Path) -> None:
    """Best-effort removal of a throwaway candidate worktree (self-made)."""
    try:
        shutil.rmtree(dest, ignore_errors=True)
    except OSError:
        pass


def _checkout_tree(project_dir: Path, tree_oid: str, dest: Path) -> str:
    """``git checkout-index`` the tree into ``dest`` via an isolated index.

    Returns "" on success, the error text otherwise. The index file is a
    throwaway: ``git read-tree <tree>`` repopulates it, ``checkout-index``
    lays the files down, and the file is removed in ``finally``.
    """
    dest.mkdir(parents=True, exist_ok=True)
    fd, index_path = tempfile.mkstemp(prefix="awf-candidate-co-", suffix=".idx")
    os.close(fd)
    os.unlink(index_path)  # git read-tree creates the index itself
    env = {**os.environ, "GIT_INDEX_FILE": index_path}
    try:
        read = _run_git(project_dir, ["read-tree", tree_oid], env)
        if read.returncode != 0:
            return f"candidate read-tree failed (rc={read.returncode}): {read.stderr.strip()}"
        co = _run_git(
            project_dir,
            ["checkout-index", "-a", "-f", f"--prefix={dest}{os.sep}"],
            env,
        )
        if co.returncode != 0:
            return f"candidate checkout-index failed (rc={co.returncode}): {co.stderr.strip()}"
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass
    return ""


# ─── candidate check ─────────────────────────────────────────────────────


def run_check(
    commands: list[str],
    worktree: Path,
    *,
    timeout: int | None = None,
) -> tuple[str, str]:
    """Run the candidate check commands in the materialised worktree.

    Returns ``(status, output)``: ``CHECK_OK`` when every command exits 0,
    ``CHECK_FAILED`` on the first non-zero / timeout / launch failure, and
    ``CHECK_UNAVAILABLE`` when there is nothing to run.

    ``timeout`` — per-command seconds; ``None`` (default) resolves the
    project's single mechanism (:func:`awf.verify._verify_cmd_timeout` /
    ``AWF_VERIFY_TIMEOUT``, RE-1) so a raised or disabled verify timeout
    reaches the mandatory candidate check too, not a hardcoded 600. ``0``
    disables the timeout.
    """
    if not commands:
        return CHECK_UNAVAILABLE, ""
    if timeout is None:
        timeout = _verify_cmd_timeout()
    chunks: list[str] = []
    for cmd in commands:
        try:
            # One home for timed shell runs (_proc rule AUD04-06/AUD14-03):
            # ``subprocess.run(timeout=...)`` kills only the direct shell and
            # leaks pytest-xdist / node descendants as orphans. ``run_tree``
            # starts the child in its own session and kills the whole group on
            # timeout — the same runner verify.py / verify_pack.py use.
            result = _proc.run_tree(
                cmd,
                shell=True,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=timeout or None,
            )
        except (subprocess.SubprocessError, OSError) as e:
            chunks.append(f"$ {cmd}\nABORTED: {e}\n")
            return CHECK_FAILED, "".join(chunks)
        chunks.append(
            f"$ {cmd}\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}\n"
        )
        if result.returncode != 0:
            return CHECK_FAILED, "".join(chunks)
    return CHECK_OK, "".join(chunks)


#: Characters that EXTEND a path token: matching an excluded path that is a
#: substring of a longer identifier/path (`lib.py` inside `mylib.py`, `x`
#: inside `max`) is a false binding. A leading `/` is deliberately NOT here —
#: `src/foo.py` inside an absolute traceback path `/tmp/…/src/foo.py` is the
#: SAME dependency and must still bind (RE-2).
_PATH_TAIL = r"A-Za-z0-9_.\-"
_PATH_HEAD = r"A-Za-z0-9_.\-"


def _path_referenced(path: str, output: str) -> bool:
    """True when ``path`` occurs in ``output`` as a standalone path token.

    Substring matches inside a longer path/identifier do not count: only an
    occurrence whose neighbouring characters do not extend the token — the
    char before may be `/` (a parent-directory separator) but not a path
    body char, the char after may be neither a path body char nor `/`.
    """
    if not path:
        return False
    pattern = re.compile(
        rf"(?<![{_PATH_HEAD}]){re.escape(path)}(?![{_PATH_TAIL}/])"
    )
    return pattern.search(output) is not None


def missing_dependency_note(
    plan: commit_delta.CandidatePlan | None, output: str
) -> str:
    """Explain a candidate failure by the plan's excluded dependencies.

    A candidate check can fail because a pre-existing path the unit's
    tests need was left OUT of the candidate (foreign WIP, by design). The
    refusal names the excluded path(s) and offers the fix: claim the path
    explicitly (R2.2) or dispatch it as its own unit. Returns "" when no
    excluded path is referenced.

    A path counts only as a standalone token (RE-2): a short/common path
    (`x`, `lib.py`) must not misattribute an ordinary check failure to an
    excluded dependency just because it appears as a substring of unrelated
    output.
    """
    if plan is None:
        return ""
    hits: list[tuple[str, str]] = []
    for path, reason in plan.excluded:
        if _path_referenced(path, output):
            hits.append((path, reason))
    if not hits:
        return ""
    listed = ", ".join(f"{p!r} ({r})" for p, r in hits)
    return (
        f"the candidate check failed and the plan EXCLUDED path(s) it "
        f"references: {listed}. The candidate does not contain a pre-existing "
        "dependency the check needs — claim it explicitly (carry_over_from / "
        "include_untracked) or dispatch it as its own unit; the approval is "
        "refused until the candidate is complete."
    )


# ─── dependency manifest ─────────────────────────────────────────────────


def dependency_manifest(
    project_dir: Path, plan: commit_delta.CandidatePlan | None
) -> tuple[tuple[str, str], ...]:
    """Untracked / generated worktree paths NOT copied into the candidate.

    Roadmap R2.4: generated build dependencies are DESCRIBED (a manifest),
    not copied wholesale. Every untracked path outside the candidate file
    set is listed with its reason — the check runs against the candidate,
    so its absence is a fact the evidence records, not a silent drop.
    """
    if plan is None:
        return ()
    try:
        untracked = commit_delta.GitRepository(project_dir).untracked_paths()
    except Exception:
        return ()
    in_plan = set(plan.files)
    entries: list[tuple[str, str]] = []
    for raw in untracked:
        shown = commit_delta._display(raw) if isinstance(raw, bytes) else str(raw)
        if shown in in_plan:
            continue
        entries.append((shown, "generated/untracked — described, not copied into the candidate"))
    return tuple(sorted(entries))


# ─── evidence ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CandidateEvidence:
    """R2.4: the binding an approval is pinned to.

    Every field is re-derivable from the current tree; the digest of the
    binding fields (``evidence_digest``) is what the APPROVE signal carries
    and the commit gate re-checks. ``check`` records the mandatory
    candidate-check outcome (status + output digest), ``degraded`` the
    full-file degradation paths bound into the candidate, ``manifest`` the
    generated dependencies described but not copied.
    """

    todo_id: str
    candidate_tree: str
    contract_digest: str
    environment_digest: str
    generation: int
    unit_revision: str
    files_digest: str
    head_expected: str
    degraded: tuple[str, ...] = ()
    check_status: str = CHECK_UNAVAILABLE
    check_output_digest: str = ""
    manifest: tuple[tuple[str, str], ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()

    def binding_fields(self) -> dict:
        return {
            "todo_id": self.todo_id,
            "candidate_tree": self.candidate_tree,
            "contract_digest": self.contract_digest,
            "environment_digest": self.environment_digest,
            "generation": self.generation,
            "unit_revision": self.unit_revision,
            "files_digest": self.files_digest,
            "head_expected": self.head_expected,
            "degraded": list(self.degraded),
            "check_status": self.check_status,
            "check_output_digest": self.check_output_digest,
        }

    def binding_projection(self) -> dict:
        """The candidate fields that ride in the APPROVE signal binding.

        A projection of :meth:`binding_fields` — the run-owned keys
        (``todo_id``, ``generation``, ``files_digest``) stay with the M2.1
        binding, so adding a binding field to the schema cannot silently
        desync the approve signal from ``evidence_digest`` (a hand-copied
        field list would omit the new key and refuse every approve).
        """
        fields = self.binding_fields()
        for owned in ("todo_id", "generation", "files_digest"):
            fields.pop(owned, None)
        return fields

    def evidence_digest(self) -> str:
        canonical = json.dumps(self.binding_fields(), sort_keys=True)
        return _sha256_text(canonical)

    def to_dict(self) -> dict:
        data = self.binding_fields()
        data["manifest"] = [list(x) for x in self.manifest]
        data["excluded"] = [list(x) for x in self.excluded]
        data["evidence_digest"] = self.evidence_digest()
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1) + "\n"

    @classmethod
    def from_dict(cls, data: object) -> CandidateEvidence | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                todo_id=str(data["todo_id"]),
                candidate_tree=str(data.get("candidate_tree", "")),
                contract_digest=str(data.get("contract_digest", "")),
                environment_digest=str(data.get("environment_digest", "")),
                generation=int(data.get("generation", 0)),
                unit_revision=str(data.get("unit_revision", "")),
                files_digest=str(data.get("files_digest", "")),
                head_expected=str(data.get("head_expected", "")),
                degraded=tuple(str(x) for x in data.get("degraded", [])),
                check_status=str(data.get("check_status", CHECK_UNAVAILABLE)),
                check_output_digest=str(data.get("check_output_digest", "")),
                manifest=tuple(
                    (str(a), str(b)) for a, b in data.get("manifest", [])
                ),
                excluded=tuple(
                    (str(a), str(b)) for a, b in data.get("excluded", [])
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


def evidence_path(project_dir: Path, todo_id: str) -> Path:
    return paths.context_dir(project_dir) / f"{EVIDENCE_PREFIX}-{todo_id}.json"


def read_evidence(project_dir: Path, todo_id: str) -> CandidateEvidence | None:
    p = evidence_path(project_dir, todo_id)
    if not p.is_file():
        return None
    try:
        return CandidateEvidence.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def write_evidence(project_dir: Path, ev: CandidateEvidence) -> None:
    atomic_write_text(evidence_path(project_dir, ev.todo_id), ev.to_json())


def _generation(project_dir: Path) -> int:
    try:
        state = run_state.read_run(project_dir)
    except Exception:
        return 0
    if not state or not state.get("active"):
        return 0
    return run_state.generation_of(state)


@dataclass
class BuiltEvidence:
    """The build result: the evidence plus the materialization (so the
    caller can reason about exclusions) and a refusal string."""

    evidence: CandidateEvidence | None = None
    materialization: Materialization | None = None
    refusal: str = ""
    check_output: str = ""


def build_evidence(
    project_dir: Path,
    todo_id: str,
    baseline_sha: str,
    *,
    claim: tuple[str, ...] | list[str] = (),
    commands: list[str] | None = None,
    timeout: int | None = None,
    confirm_degraded: bool = False,
    worktree_dir: Path | None = None,
) -> BuiltEvidence:
    """Materialise the candidate, run the mandatory check, build evidence.

    Refusals (no evidence is returned; the caller must not approve):

    * the candidate could not be built (unsupported state / mixed pair);
    * the candidate is MISSING a pre-existing dependency the check needs
      (a failing check whose output references an excluded path);
    * the plan contains a full-file DEGRADATION and ``confirm_degraded``
      is False (R2.3-4: a full file enters the commit only by an explicit
      confirmation tied to the plan digest).

    An empty ``claim`` resolves the unit's own claim channel (R2.2) inside
    :func:`materialize_candidate` — the candidate the check runs against is
    the same tree the gate will commit.
    """
    # Lazy import: commit_plan is the one home of the file-set digest; a
    # top-level import is avoided only to keep this module's import graph
    # flat for its own importers.
    from .commit_plan import files_digest as _plan_files_digest

    materialization = materialize_candidate(
        project_dir, todo_id, baseline_sha,
        claim=tuple(claim),
        worktree_dir=worktree_dir,
    )
    # Cleanup invariant (F-QA-5): the self-made ``mkdtemp`` worktree is
    # removed on EVERY outcome — a refusal raised after materialisation
    # (unconfirmed degradation, failing check) must not leak a full checkout
    # into the system temp. A caller-supplied ``worktree_dir`` is the
    # caller's to clean (tests inspect it), so only a self-made dir is
    # discarded here.
    self_made = worktree_dir is None and materialization.worktree is not None
    try:
        if materialization.refusal:
            return BuiltEvidence(
                materialization=materialization, refusal=materialization.refusal
            )
        plan = materialization.plan
        if materialization.degraded and not confirm_degraded:
            listed = ", ".join(repr(p) for p in materialization.degraded)
            return BuiltEvidence(
                materialization=materialization,
                refusal=(
                    "the candidate contains full-file degradation event(s) for "
                    f"{listed} — the full worktree content would enter the unit "
                    "commit. Approve with an explicit confirmation "
                    "(confirm_degraded=True) binding the decision to this plan "
                    "digest, or resolve the degradation; a warning alone must "
                    "not admit foreign work."
                ),
            )

        cmds = (
            commands if commands is not None else candidate_commands(project_dir, todo_id)
        )
        if materialization.worktree is not None:
            check_status, output = run_check(
                cmds, materialization.worktree, timeout=timeout
            )
        else:
            # No candidate files → nothing to materialise and nothing to
            # check. An empty candidate is a valid "nothing to verify" state,
            # NOT a failure: the commit gate already skips an empty plan ("No
            # changes to commit"), and a no-op / ignore-only /
            # restart-recovery unit must stay approvable. The check never ran
            # on a missing tree, so the status is UNAVAILABLE — CHECK_FAILED
            # here was a factually wrong refusal that stalled the verify stage
            # (QA finding F-QA-2).
            check_status = CHECK_UNAVAILABLE
            output = "the candidate tree is empty — nothing was materialised to check\n"

        if check_status == CHECK_FAILED:
            note = missing_dependency_note(plan, output)
            return BuiltEvidence(
                materialization=materialization,
                refusal=note or (
                    "the mandatory candidate check FAILED on the reproducible "
                    "candidate (a green run in the original dirty worktree does "
                    "not replace it). Fix the candidate or the check, then "
                    "re-verify."
                ),
                check_output=output,
            )

        ev = CandidateEvidence(
            todo_id=todo_id,
            candidate_tree=materialization.candidate_tree,
            contract_digest=contract_digest(project_dir, todo_id),
            environment_digest=environment_digest(project_dir),
            generation=_generation(project_dir),
            unit_revision=unit_revision(project_dir, todo_id, baseline_sha),
            files_digest=_plan_files_digest(list(plan.files)) if plan else "",
            head_expected=current_head(project_dir),
            degraded=materialization.degraded,
            check_status=check_status,
            check_output_digest=_sha256_text(output),
            manifest=dependency_manifest(project_dir, plan),
            excluded=tuple(plan.excluded) if plan else (),
        )
        return BuiltEvidence(
            evidence=ev,
            materialization=materialization,
            check_output=output,
        )
    finally:
        if self_made and materialization.worktree is not None:
            _discard_worktree(materialization.worktree)


# ─── validation (commit-gate side) ───────────────────────────────────────


def validate_evidence(
    stored: CandidateEvidence,
    project_dir: Path,
    todo_id: str,
    baseline_sha: str,
    *,
    claim: tuple[str, ...] | list[str] = (),
    candidate_tree: str | None = None,
    degraded: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Re-derive the fields; return a refusal, or "" when still valid.

    Fail-closed ordering: a moved HEAD is reported first (the fix is a
    REPLAN, not a commit on the new HEAD); then a contract change; then a
    candidate-content change (blob/mode/path/degradation) — the evidence
    the approval was bound to no longer describes the tree.

    ``candidate_tree`` / ``degraded`` — the two facts a caller may already
    hold (the commit gate built the candidate with ``build_tree=True``):
    when given, they are used instead of materialising a second candidate.
    ``None`` means "re-derive" — the validator materialises and refuses if
    that is impossible (the approve-side and direct-test path).
    """
    current_head_value = current_head(project_dir)
    if stored.head_expected and current_head_value != stored.head_expected:
        return (
            f"HEAD moved after the approval (approved on "
            f"{stored.head_expected[:12]}…, now {current_head_value[:12]}…) — "
            "REPLAN on the current HEAD; the gate must not commit on top of a "
            "new HEAD. Re-dispatch the unit (its baseline changes) or re-run "
            "the verify cycle."
        )
    recomputed_contract = contract_digest(project_dir, todo_id)
    if recomputed_contract != stored.contract_digest:
        return (
            "the TODO contract changed after the approval (the verify/gates/"
            "prove_red/files block moved) — the candidate evidence is "
            "invalidated. Re-verify and re-approve."
        )
    # Requirement 2 (R2.4): the unit revision (TODO bytes + baseline sha) is
    # a BOUND field, so it must be re-derived — a tampered/renamed TODO or a
    # different baseline invalidates the evidence. F-QA-3: the field was
    # declared a binding but never re-checked.
    if stored.unit_revision and (
        unit_revision(project_dir, todo_id, baseline_sha) != stored.unit_revision
    ):
        return (
            "the unit revision changed after the approval (the TODO bytes or "
            "the baseline sha moved) — the candidate evidence is invalidated. "
            "Re-verify the unit and re-approve."
        )
    if candidate_tree is None:
        # Validation only re-derives the plan's tree OID and degraded set —
        # it never runs a command, so no worktree is laid down
        # (``checkout=False``). Nothing to clean, and a verify pack for an
        # approved unit no longer copies the repo into the system temp.
        materialization = materialize_candidate(
            project_dir, todo_id, baseline_sha,
            claim=tuple(claim), checkout=False,
        )
        if materialization.refusal:
            return (
                f"the candidate could not be re-materialised for validation: "
                f"{materialization.refusal}"
            )
        candidate_tree = materialization.candidate_tree
        degraded = tuple(materialization.degraded)
    else:
        degraded = tuple(degraded or ())
    if candidate_tree != stored.candidate_tree:
        return (
            "the candidate content changed after the approval (a blob, mode "
            "or path moved, or the degradation set changed) — the evidence is "
            "invalidated and the commit is refused. Re-verify the candidate "
            "and re-approve with fresh evidence."
        )
    if tuple(degraded) != tuple(stored.degraded):
        return (
            "the plan's full-file degradation set changed after the approval "
            "— the evidence is invalidated. Re-verify and re-approve."
        )
    if (
        stored.environment_digest
        and environment_digest(project_dir) != stored.environment_digest
    ):
        return (
            "the verification environment changed after the approval (a "
            "configured verify command moved) — the evidence is invalidated. "
            "Re-verify and re-approve."
        )
    return ""


__all__ = [
    "CHECK_FAILED",
    "CHECK_OK",
    "CHECK_UNAVAILABLE",
    "BuiltEvidence",
    "CandidateEvidence",
    "EVIDENCE_PREFIX",
    "Materialization",
    "build_evidence",
    "candidate_commands",
    "contract_digest",
    "current_head",
    "dependency_manifest",
    "environment_digest",
    "evidence_path",
    "materialize_candidate",
    "missing_dependency_note",
    "read_evidence",
    "run_check",
    "unit_revision",
    "validate_evidence",
    "write_evidence",
]
