"""M8, доборы R2 (TODO-0232): git-хуки (версия/prepare-commit-msg), PA-A,
PA-B, UX-щели ACK/blocked, мелочи фактов публикации.

На базовом дереве КРАСНЫЙ: `publish._hook_run_supported` не существует
(AttributeError), prepare-commit-msg не запускается, APPROVE с
инвалидированным candidate — «valid» (PA-A), candidate check — shell вместо
argv (PA-B), noop-continue с ACK молчит, blocked-текст без рецепта реплана,
publication_facts доверяет чужому todo_id. Репозитории disposable.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from awf import api, commit_gate, commit_plan, git_utils

TODO = "TODO-0001"


def _publish():
    """Ленивый импорт: провал — утверждение, а не import-ошибка."""
    try:
        from awf import publish

        assert hasattr(publish, "_hook_run_supported"), (
            "publish must own a hook-version probe (M8: git >= 2.36 for "
            "`git hook run`)"
        )
        return publish
    except ImportError as e:
        raise AssertionError(f"publish module is missing: {e}") from e


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _plan(files) -> commit_plan.CommitPlan:
    return commit_plan.CommitPlan(
        todo_id=TODO, generation=0, verified_sha="", files=tuple(files)
    )


def _logs(repo: Path) -> Path:
    d = repo / ".agentic" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _commit_unit(repo: Path, files, crash_at: str = ""):
    _publish()
    return commit_gate._commit_via_isolated_index(
        repo, _plan(files), f"awf(verify): {TODO}", _logs(repo), crash_at=crash_at
    )


def _write_hook(repo: Path, name: str, body: str) -> Path:
    hook = repo / ".git" / "hooks" / name
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(body)
    hook.chmod(0o755)
    return hook


def _touch_work(repo: Path) -> None:
    """Работа юнита в дереве: план-файл отличается от HEAD."""
    (repo / "README.md").write_text("v2\n", encoding="utf-8")


def _affect_post_commit(repo: Path, counter: Path) -> None:
    _write_hook(repo, "post-commit", f'#!/bin/sh\nprintf \'x\\n\' >> "{counter}"\n')


# ─── (a) git-хуки: версия, fallback, prepare-commit-msg ──────────────────


class TestGitHooks:
    def test_older_git_falls_back_to_direct_hook_run(self, tmp_path, monkeypatch):
        """Старый git: хук запускается напрямую, а не «'hook' is not a git
        command»."""
        publish = _publish()
        repo = _repo(tmp_path)
        flag = tmp_path / "hook-ran.flag"
        monkeypatch.setenv("GAWF_HOOK_RAN_FLAG", str(flag))
        _write_hook(repo, "pre-commit", '#!/bin/sh\necho ran > "$GAWF_HOOK_RAN_FLAG"\n')
        monkeypatch.setattr(publish, "_hook_run_supported", lambda _p: False)
        flag.unlink(missing_ok=True)
        _touch_work(repo)
        ok = _commit_unit(repo, ("README.md",))
        assert ok.status == "committed", (
            f"older git must fall back to direct hook run, got {ok.status}: {ok.reason}"
        )
        assert flag.is_file(), "the pre-commit hook must run through the fallback"

    def test_older_git_hook_failure_still_blocks_commit(self, tmp_path, monkeypatch):
        publish = _publish()
        repo = _repo(tmp_path)
        flag = tmp_path / "hook-block.flag"
        _write_hook(
            repo, "pre-commit", f'#!/bin/sh\necho blocked > "{flag}"\nexit 1\n'
        )
        monkeypatch.setattr(publish, "_hook_run_supported", lambda _p: False)
        _touch_work(repo)
        bad = _commit_unit(repo, ("README.md",))
        assert bad.status != "committed", (
            f"a failed hook must block the commit on older git too: {bad}")
        assert flag.read_text(encoding="utf-8").strip() == "blocked"

    def test_prepare_commit_msg_rewrite_lands_in_commit(self, tmp_path):
        """Правки message хуком prepare-commit-msg попадают в коммит."""
        _publish()
        repo = _repo(tmp_path)
        _write_hook(
            repo, "prepare-commit-msg", '#!/bin/sh\nprintf \'X-PC(M8)\\n\' >> "$1"\n'
        )
        _touch_work(repo)
        outcome = _commit_unit(repo, ("README.md",))
        assert outcome.status == "committed", f"hook-policy repo must commit: {outcome}"
        msg = _git(repo, "log", "-1", "--format=%B").stdout
        assert "X-PC(M8)" in msg, f"the rewrite must reach the commit: {msg!r}"

    def test_prepare_commit_msg_failure_aborts_before_object(self, tmp_path):
        _publish()
        repo = _repo(tmp_path)
        first = _head(repo)
        _write_hook(repo, "prepare-commit-msg", "#!/bin/sh\nexit 1\n")
        _touch_work(repo)
        outcome = _commit_unit(repo, ("README.md",))
        assert outcome.status == commit_plan.OUTCOME_ERROR, (
            f"a failed prepare-commit-msg must abort: {outcome}"
        )
        assert "prepare-commit-msg hook failed" in outcome.reason, (
            f"the diagnosis must name the hook: {outcome.reason!r}"
        )
        assert _head(repo) == first, (
            "a rejected message must not move the ref"
        )


class TestHookVersionProbe:
    """QA (stage agent-qa-review): the version probe ITSELF, not its seam.

    Every other fallback test patches ``_hook_run_supported``, so a broken
    banner parse or a wrong threshold would leave them all green: the
    porcelain backend would always be chosen and the direct fallback would
    silently stop being reachable on an old git (the regress the unit
    exists to prevent). These tests feed real banners through ``_git``.
    """

    def _with_banner(self, monkeypatch, banner: str):
        publish = _publish()
        monkeypatch.setattr(
            publish,
            "_git",
            lambda *_a, **_k: subprocess.CompletedProcess(
                ["git", "--version"], 0, banner, ""
            ),
        )
        return publish

    @pytest.mark.parametrize(
        "banner,expected",
        [
            ("git version 2.35.1\n", False),
            ("git version 2.36.0\n", True),
            ("git version 2.39.3 (Apple Git-146)\n", True),
            ("git version 2.9.5\n", False),
        ],
    )
    def test_banner_decides_the_backend(self, monkeypatch, banner, expected):
        publish = self._with_banner(monkeypatch, banner)
        assert publish._hook_run_supported(Path(".")) is expected, banner

    def test_unreadable_banner_keeps_the_porcelain_backend(self, monkeypatch):
        """An unreadable banner keeps the old posture (``git hook run``), so
        the failure text stays precise instead of guessing a fallback."""
        publish = self._with_banner(monkeypatch, "no version here")
        assert publish._hook_run_supported(Path(".")) is True


# ─── (b) PA-A: пред-существующий APPROVE и candidate-привязка R2.4 ───────


def _project(proj: Path) -> str:
    """Живой забег: baseline в HEAD, работа юнита в дереве, BASELINE-файл."""
    api.init_project(proj, project_name="M8")
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text("# Task\n", encoding="utf-8")
    (proj / "file.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "baseline"], cwd=proj, check=True, capture_output=True
    )
    baseline_sha = _head(proj)
    (proj / "file.txt").write_text("v2 good\n", encoding="utf-8")
    ctx = proj / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{TODO}.sha").write_text(baseline_sha + "\n", encoding="utf-8")
    return baseline_sha


class TestPreexistingApproveCandidateBinding:
    def test_head_moved_after_approve_is_stale_not_valid(
        self, tmp_git_repo, monkeypatch
    ):
        """PA-A: старый APPROVE, у которого candidate-привязка R2.4
        инвалидирована (HEAD двинулся после одобрения), — stale, не valid."""
        proj = tmp_git_repo
        _project(proj)
        api.run_start(proj, queue=[TODO])
        api.approve_commit(
            proj,
            TODO,
            evidence="pytest -q → 1 passed; verdict: approve",
            verified_sha=git_utils.tree_fingerprint(proj),
        )
        # Чужой актёр сдвинул HEAD после одобрения (пустой коммит): M2.1
        # (поколение/fingerprint/дайджест файлов) жив, R2.4 — нет.
        _git(proj, "commit", "--allow-empty", "-q", "-m", "other actor")
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        status, reason = commit_plan.preexisting_approval_status(
            proj, TODO, commit_plan.read_baseline_sha(proj, TODO)
        )
        assert status == "stale", (
            f"a candidate-invalidated pre-existing APPROVE must be stale, "
            f"got {status!r}: {reason!r}"
        )
        assert "HEAD moved" in reason, (
            f"the reason must name the candidate invalidation: {reason!r}"
        )

    def test_valid_preexisting_approve_stays_valid(self, tmp_git_repo):
        proj = tmp_git_repo
        _project(proj)
        api.run_start(proj, queue=[TODO])
        api.approve_commit(
            proj,
            TODO,
            evidence="pytest -q → 1 passed; verdict: approve",
            verified_sha=git_utils.tree_fingerprint(proj),
        )
        status, reason = commit_plan.preexisting_approval_status(
            proj, TODO, commit_plan.read_baseline_sha(proj, TODO)
        )
        assert status == "valid", f"a binding approval must stay valid: {reason!r}"


# ─── (c) PA-B: candidate check = тот же механизм запуска, что verify ─────


class TestCandidateCheckRunner:
    def test_run_check_is_argv_not_shell(self, tmp_path):
        """PA-B + I1: mandatory check — тот же argv-механизм, что verify.py.
        Shell-операторы больше не «тихий литерал» (это давало ложный
        зелёный): и candidate check, и verify-pack их типизированно
        отбивают одним предикатом, shell не запускается."""
        try:
            from awf import candidate_verify as cv
        except ImportError as e:
            raise AssertionError(f"candidate_verify is missing: {e}") from e

        for cmd in ["true && false", "echo hit > struck.txt"]:
            st, out = cv.run_check([cmd], tmp_path)
            assert st == cv.CHECK_FAILED, (cmd, st, out)
            assert "ABORTED" in out and "shell operator" in out, (cmd, out)
        assert not (tmp_path / "struck.txt").exists(), (
            "shell semantics created a file — the runner used the shell"
        )

    def test_bad_quoting_fails_typed_not_crashes(self, tmp_path):
        from awf import candidate_verify as cv

        status, out = cv.run_check(["echo 'unterminated"], tmp_path)
        assert status == cv.CHECK_FAILED, f"got {status}: {out!r}"
        assert "ABORTED: bad quoting" in out


# ─── (d) UX-щели: ACK на noop-continue, рецепт блокированного юнита ──────


class TestNoopAckHint:
    def test_continue_noop_with_armed_ack_names_the_recipe(
        self, tmp_git_repo, monkeypatch
    ):
        proj = tmp_git_repo
        _project(proj)
        from awf.api import pipeline as pl

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )

        res = pl.continue_pipeline(proj, ack=TODO)
        assert res.run_mode == "noop"
        armed = proj / ".agentic" / "inbox" / f"ACK-{TODO}.ready"
        assert armed.is_file(), "the ack must be written even on a noop"
        assert "ACK" in res.message and "kill" in res.message, res.message
        assert "continue" in res.message.lower(), res.message

    def test_continue_noop_without_ack_stays_quiet(self, tmp_git_repo, monkeypatch):
        proj = tmp_git_repo
        _project(proj)
        from awf.api import pipeline as pl

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )
        res = pl.continue_pipeline(proj)
        assert res.run_mode == "noop"
        assert "ACK is armed" not in res.message, res.message

    def test_noop_ack_recipe_pins_the_ack_it_names(self, tmp_git_repo, monkeypatch):
        """L1 (REVIEW виток 9): рецепт noop-подсказки ПИНИТ названный ACK.

        Голый ``continue`` резолвит сначала явный todo_id, потом state, и
        только потом ждущий ACK — рекомендованный шаг оставил бы названный
        ACK в inbox (ровно тот «stranded ACK», который закрывает пункт 4а).
        Рецепт обязан нести пин ``ack=<id>``, и этот шаг применяет ACK.
        """
        import re

        from awf.api import pipeline as pl

        proj = tmp_git_repo
        _project(proj)
        inbox = proj / ".agentic" / "inbox"
        (inbox / "TODO-0002.md").write_text("# second\n", encoding="utf-8")
        (inbox / "TODO-0002.ready").touch()
        (inbox / "ACK-TODO-0001.ready").touch()

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )
        res = pl.continue_pipeline(proj)
        assert res.run_mode == "noop", res
        named = "TODO-0001"
        assert f"ACK is armed for {named}" in res.message, res.message

        pin = re.search(r"ack\s*=\s*['\"]?(TODO-\d{4,})", res.message)
        assert pin is not None and pin.group(1) == named, (
            "the recipe must pin the ACK it names:\n" + res.message
        )

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (False, None, None)
        )
        monkeypatch.setattr(pl, "read_state", lambda _p: {"todo_id": "TODO-0002"})
        captured: dict = {}

        class _Stop(Exception):
            pass

        def _fake_start(project_dir, **kwargs):
            captured.update(kwargs)
            raise _Stop()

        monkeypatch.setattr(pl, "start_in_background", _fake_start)
        try:
            pl.continue_pipeline(proj, ack=pin.group(1))
        except _Stop:
            pass
        else:
            raise AssertionError("the launch did not reach start_in_background")
        assert captured.get("todo_id") == named, (
            "the recommended action did not apply the ACK it named: "
            f"resumed {captured.get('todo_id')!r}, hint named {named!r}"
        )
        assert not (inbox / f"ACK-{named}.ready").exists(), (
            "the applied ACK must be consumed"
        )


class TestBlockedRecipes:
    def test_blocked_event_names_replan_recipe(self, tmp_git_repo, monkeypatch):
        """blocked-текст: рецепт реплана «kill → continue(from_stage=…)»,
        НЕ нерабочий awf_retry_stage."""
        from awf.api import wait_event as we

        proj = tmp_git_repo
        api.init_project(proj, project_name="M8")

        state = {
            "stage_name": "mystery",
            "stage_kind": "work",
            "todo_id": TODO,
            "last_signal": f"BLOCKED-{TODO}",
        }
        monkeypatch.setattr(we, "read_state", lambda *_a, **_kw: dict(state))
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
        res = we.wait_for_event(proj, timeout=1)
        assert res.event_type == "blocked", f"{res.event_type}: {res.message!r}"
        assert "kill" in res.message.lower(), res.message
        assert "from_stage" in res.message or "from-stage" in res.message, res.message
        assert "awf_retry_stage" not in res.message, res.message

    def test_next_action_recipes(self):
        """next_action (run-active): blocked — рецепт реплана kill →
        continue(from_stage); salvage — рецепт retry_stage сохранён."""
        from agent_workflow_ui.tools import awf as tool

        src = Path(tool.__file__).read_text(encoding="utf-8")
        blocked = [ln for ln in src.splitlines() if '"blocked"' in ln]
        assert blocked, "the run-active blocked recipe is missing entirely"
        text = "\n".join(blocked)
        assert (
            "kill" in text.lower()
            and ("from_stage" in text or "from-stage" in text)
        ), f"blocked next_action must carry the replan recipe:\n{text}"
        assert "awf_retry_stage" not in text, (
            "the blocked recipe must not advertise awf_retry_stage:\n" + text
        )
        salvage = [ln for ln in src.splitlines() if '"salvage"' in ln]
        assert any("awf_retry_stage" in ln for ln in salvage), (
            "the salvage recipe must KEEP the awf_retry_stage advice:\n"
            + "\n".join(salvage)
        )


# ─── (e) мелочи фактов: идентичность фактов, post-commit семантика ──────


class TestPublicationFactsIdentity:
    def test_foreign_archive_and_superseded_facts_are_dropped(self, tmp_path):
        publish = _publish()
        repo = _repo(tmp_path)
        _touch_work(repo)
        outcome = _commit_unit(repo, ("README.md",))
        assert outcome.status == "committed", outcome
        facts_dir = publish.archive_fact_file(repo, TODO).parent
        facts_dir.mkdir(parents=True, exist_ok=True)
        (facts_dir / f"ARCHIVE-{TODO}.json").write_text(
            json.dumps({"todo_id": "TODO-9999", "archived": "done/TODO-9999"}),
            encoding="utf-8",
        )
        (facts_dir / f"PUBLISH-SUPERSEDED-{TODO}.json").write_text(
            json.dumps({"todo_id": "TODO-9999", "reason": "moved somewhere else"}),
            encoding="utf-8",
        )
        facts = publish.publication_facts(repo, TODO)
        assert facts["archived"] != "done/TODO-9999", facts
        assert facts["superseded"] is False and facts["superseded_reason"] == "", facts


class TestPostCommitSemantics:
    def test_post_commit_runs_once_per_ref_move(self, tmp_path):
        publish = _publish()
        repo = _repo(tmp_path)
        counter = tmp_path / "post-commit.log"
        _affect_post_commit(repo, counter)
        _touch_work(repo)
        outcome = _commit_unit(repo, ("README.md",))
        assert outcome.status == "committed", outcome.reason
        assert counter.read_text(encoding="utf-8").count("x") == 1

    def test_resume_that_finds_ref_moved_does_not_repeat_post_commit(self, tmp_path):
        _publish()
        repo = _repo(tmp_path)
        counter = tmp_path / "post-commit2.log"
        _affect_post_commit(repo, counter)
        _touch_work(repo)
        with pytest.raises(Exception) as exc:
            _commit_unit(repo, ("README.md",), crash_at="update_ref")
        assert "update_ref" in str(exc.value), exc
        resume = commit_gate.maybe_commit(
            "verify", TODO, "commit_and_report", repo, _logs(repo), auto=False
        )
        assert resume.status == "committed", resume
        ran = counter.read_text(encoding="utf-8").count("x") if counter.is_file() else 0
        assert ran == 0, (
            f"the resume must not repeat post-commit (at-least-once); ran {ran}"
        )


# ─── (REVIEW виток 2) A1: ACK-подсказка не врёт при pin ──────────────────


class TestAckPinOverridden:
    def test_continue_noop_ack_overridden_by_pin_says_so(
        self, tmp_git_repo, monkeypatch
    ):
        """A1+F4: явный todo_id перекрыл ack — текст «ACK not applied
        (todo_id pin wins); no ACK for TODO-0001»: подтверждение НЕ
        записано и для запиненного TODO его нет (строго, без лжи про
        «armed/nothing armed»)."""
        proj = tmp_git_repo
        _project(proj)
        inbox = proj / ".agentic" / "inbox"
        (inbox / "TODO-0002.md").write_text("# second\n", encoding="utf-8")
        from awf.api import pipeline as pl

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )
        res = pl.continue_pipeline(proj, ack="TODO-0002", todo_id="TODO-0001")
        assert res.run_mode == "noop"
        assert "ACK is armed" not in res.message, res.message
        assert "ack not applied" in res.message.lower(), res.message
        assert "no ACK for TODO-0001" in res.message, res.message
        assert "nothing is armed" not in res.message, res.message
        assert not (inbox / "ACK-TODO-0002.ready").is_file(), (
            "an ack that lost to the pin must NOT be written"
        )

    def test_pin_override_names_a_waiting_ack_instead_of_denying_it(
        self, tmp_git_repo, monkeypatch
    ):
        """architect-review M8: если для запиненного TODO уже лежит
        потребляемый ACK, подсказка его НАЗЫВАЕТ. Старый текст «no ACK for
        TODO-0001» утверждал факт, который код не проверял, — ложь в
        подсказке, ведущая супервизора к неверному шагу."""
        proj = tmp_git_repo
        _project(proj)
        inbox = proj / ".agentic" / "inbox"
        (inbox / "TODO-0002.md").write_text("# second\n", encoding="utf-8")
        (inbox / "ACK-TODO-0001.ready").touch()
        from awf.api import pipeline as pl

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )
        res = pl.continue_pipeline(proj, ack="TODO-0002", todo_id="TODO-0001")
        assert res.run_mode == "noop"
        assert "no ACK for TODO-0001" not in res.message, res.message
        assert "ACK-TODO-0001.ready is already waiting" in res.message, res.message
        assert "kill" in res.message.lower(), res.message
        assert "ack not applied" in res.message.lower(), res.message
        assert not (inbox / "ACK-TODO-0002.ready").is_file(), (
            "an ack that lost to the pin must NOT be written"
        )


    def test_pin_override_finds_a_legacy_named_ack(self, tmp_git_repo, monkeypatch):
        """architect-review M8: the hint asks the consumer's mapping
        (``_signal_todo_id``), so a legacy ``ACK-0001.ready`` that the
        consumer WILL consume is named — not denied — and named by its
        real file, not the canonical spelling."""
        proj = tmp_git_repo
        _project(proj)
        inbox = proj / ".agentic" / "inbox"
        (inbox / "TODO-0002.md").write_text("# second\n", encoding="utf-8")
        (inbox / "TODO-0001.ready").touch()
        (inbox / "ACK-0001.ready").touch()
        from awf.api import pipeline as pl

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )
        res = pl.continue_pipeline(proj, ack="TODO-0002", todo_id="TODO-0001")
        assert res.run_mode == "noop"
        assert "no ACK for TODO-0001" not in res.message, res.message
        assert "ACK-0001.ready is already waiting" in res.message, res.message
        assert not (inbox / "ACK-TODO-0002.ready").is_file(), (
            "an ack that lost to the pin must NOT be written"
        )

    def test_pin_without_its_own_ack_does_not_advertise_a_foreign_one(
        self, tmp_git_repo, monkeypatch
    ):
        """I3 (architect-review M8): при пине подсказка описывает ACK именно
        пина — consumable ACK другой единицы не рекламируется, иначе рецепт
        зовёт убить живой пайплайн ради ответа, который пин не съест."""
        proj = tmp_git_repo
        _project(proj)
        inbox = proj / ".agentic" / "inbox"
        (inbox / "TODO-0002.md").write_text("# second\n", encoding="utf-8")
        (inbox / "ACK-TODO-0002.ready").touch()
        from awf.api import pipeline as pl

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )
        res = pl.continue_pipeline(proj, todo_id="TODO-0001")
        assert res.run_mode == "noop"
        assert "ACK is armed for TODO-0002" not in res.message, res.message
        assert "No ACK is waiting for TODO-0001" in res.message, res.message
        assert "drop the todo_id pin" in res.message, res.message


# ─── (REVIEW виток 2) A2: неисполняемый хук = SKIP, как `git hook run` ───


class TestNonExecutableHookSkipped:
    def test_fallback_nonexecutable_hint_names_the_skip(self, tmp_path):
        """Матрица `хук есть, но не исполняем`: fallback → rc 0 SKIP c hint
        `rc=126 ignored` — как git, не отказ."""
        publish = _publish()
        repo = _repo(tmp_path)
        hook = _write_hook(repo, "pre-commit", "#!/bin/sh\nexit 0\n")
        hook.chmod(0o644)
        proc = publish._run_hook_direct(repo, "pre-commit", {})
        assert proc.returncode == 0, proc
        assert "not set as executable" in proc.stderr, proc
        assert "ignored" in proc.stderr, proc

    def test_fallback_commits_through_nonexecutable_hook(self, tmp_path, monkeypatch):
        publish = _publish()
        repo = _repo(tmp_path)
        hook = _write_hook(repo, "pre-commit", "#!/bin/sh\necho nope\n")
        hook.chmod(0o644)
        monkeypatch.setattr(publish, "_hook_run_supported", lambda _p: False)
        _touch_work(repo)
        ok = _commit_unit(repo, ("README.md",))
        assert ok.status == "committed", (
            f"a non-executable hook is skipped (rc=126 ignored), not a "
            f"refusal: got {ok.status}: {ok.reason}"
        )

    def test_backend_and_fallback_same_outcome_on_nonexecutable(
        self, tmp_path, monkeypatch
    ):
        """«Бэкенд и fallback дают одинаковый исход на одном дереве»:
        идентичные репо с неисполняемым хуком — оба committed."""
        publish = _publish()
        repo_backend = _repo(tmp_path / "backend")
        hook = _write_hook(repo_backend, "pre-commit", "#!/bin/sh\necho nope\n")
        hook.chmod(0o644)
        _touch_work(repo_backend)
        out_backend = _commit_unit(repo_backend, ("README.md",))

        repo_fallback = _repo(tmp_path / "fallback")
        hook2 = _write_hook(repo_fallback, "pre-commit", "#!/bin/sh\necho nope\n")
        hook2.chmod(0o644)
        monkeypatch.setattr(publish, "_hook_run_supported", lambda _p: False)
        _touch_work(repo_fallback)
        out_fallback = _commit_unit(repo_fallback, ("README.md",))

        assert out_backend.status == out_fallback.status == "committed", (
            f"backend: {out_backend} / fallback: {out_fallback}"
        )

    def test_fallback_hung_hook_raises_like_the_backend(
        self, tmp_path, monkeypatch
    ):
        """Зависший хук: `git hook run` уходит в `subprocess.run(timeout=
        GIT_TIMEOUT)` и поднимает ``TimeoutExpired``; прямой бэкенд не
        выдумывает свой исход («launch failed» rc 127) — исключение то же,
        а группу процессов уже убил ``run_tree``."""
        publish = _publish()
        repo = _repo(tmp_path)
        _write_hook(repo, "pre-commit", "#!/bin/sh\nsleep 5\n")
        monkeypatch.setattr(publish.commit_delta, "GIT_TIMEOUT", 0.2)
        with pytest.raises(subprocess.TimeoutExpired):
            publish._run_hook_direct(repo, "pre-commit", {})


# ─── (REVIEW виток 7) J1: «есть, но не файл» ≠ «нет хука» ────────────────


class TestHookPathNotAFile:
    def test_directory_hook_is_refused_not_skipped(self, tmp_path):
        """Каталог на месте хука: git пробует exec и падает EACCES —
        `git commit` «cannot exec …: Permission denied» (rc=1). Fallback не
        имеет права тихо пропустить хук (это обошло бы политику репо)."""
        publish = _publish()
        repo = _repo(tmp_path)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.mkdir(parents=True)
        proc = publish._run_hook_direct(repo, "pre-commit", os.environ.copy())
        assert proc.returncode == 1, proc
        assert "cannot exec" in proc.stderr, proc.stderr
        assert "Permission denied" in proc.stderr, proc.stderr

    def test_fifo_hook_is_refused_not_skipped(self, tmp_path):
        """FIFO с exec-битом: exec(2) не принимает его так же, как каталог."""
        publish = _publish()
        repo = _repo(tmp_path)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(hook)
        hook.chmod(0o755)
        proc = publish._run_hook_direct(repo, "pre-commit", os.environ.copy())
        assert proc.returncode == 1, proc
        assert "cannot exec" in proc.stderr, proc.stderr

    def test_dangling_symlink_is_still_noop(self, tmp_path):
        """Висячий симлинк — «нет хука»: exists() False, git его не видит."""
        publish = _publish()
        repo = _repo(tmp_path)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.symlink_to(tmp_path / "nowhere")
        proc = publish._run_hook_direct(repo, "pre-commit", os.environ.copy())
        assert proc.returncode == 0, proc
        assert proc.stderr == "", proc.stderr

    def test_backend_and_fallback_same_outcome_on_directory(
        self, tmp_path, monkeypatch
    ):
        """Один и тот же исход на одном дереве: каталог-хук — отказ в обоих
        бэкендах (сравниваем статус, диагностика у git своя)."""
        publish = _publish()
        repo_backend = _repo(tmp_path / "backend")
        (repo_backend / ".git" / "hooks" / "pre-commit").mkdir(parents=True)
        _touch_work(repo_backend)
        out_backend = _commit_unit(repo_backend, ("README.md",))

        repo_fallback = _repo(tmp_path / "fallback")
        (repo_fallback / ".git" / "hooks" / "pre-commit").mkdir(parents=True)
        monkeypatch.setattr(publish, "_hook_run_supported", lambda _p: False)
        _touch_work(repo_fallback)
        out_fallback = _commit_unit(repo_fallback, ("README.md",))

        assert out_backend.status == out_fallback.status != "committed", (
            f"backend: {out_backend} / fallback: {out_fallback}"
        )


# ─── (REVIEW виток 2) A3: `~` в core.hooksPath ───────────────────────────


class TestHooksPathTilde:
    def test_tilde_hooks_path_hook_found_and_run(self, tmp_path, monkeypatch):
        """core.hooksPath=~/hk → хук найден (expanduser) и исполняется, не
        молчаливый bypass по пути named `~`."""
        publish = _publish()
        repo = _repo(tmp_path / "tilde")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        _git(repo, "config", "core.hooksPath", "~/hklist")
        flag = tmp_path / "hook-tilde.flag"
        # NOTE: NOT _write_hook — it appends `.git/hooks/`, and here the hook
        # must sit DIRECTLY in the configured hooks directory.
        hklist = home / "hklist"
        hklist.mkdir(parents=True, exist_ok=True)
        tilde_hook = hklist / "pre-commit"
        tilde_hook.write_text(f'#!/bin/sh\necho x > "{flag}"\n', encoding="utf-8")
        tilde_hook.chmod(0o755)
        monkeypatch.setattr(publish, "_hook_run_supported", lambda _p: False)
        _touch_work(repo)
        ok = _commit_unit(repo, ("README.md",))
        assert ok.status == "committed", ok.reason
        assert flag.is_file(), (
            "the hook registered via a ~/ hooksPath must be found and run, "
            "not silently bypassed"
        )


class TestHooksDirAsksGit:
    def test_linked_worktree_hook_lives_in_the_common_dir(self, tmp_path, monkeypatch):
        """A linked worktree's hooks live in the COMMON dir, not
        ``$GIT_DIR/hooks`` (that is the per-worktree dir). The direct runner
        must ask git for the path instead of rebuilding it
        (architect-review M8), or it would silently skip a hook the backend
        runs."""
        publish = _publish()
        repo = _repo(tmp_path)
        flag = tmp_path / "worktree-hook.flag"
        _write_hook(repo, "pre-commit", f'#!/bin/sh\necho x > "{flag}"\n')
        wt = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", str(wt), "-b", "wt-branch")
        monkeypatch.setattr(publish, "_hook_run_supported", lambda _p: False)
        proc = publish._run_hook_direct(wt, "pre-commit", {})
        assert proc.returncode == 0, proc
        assert flag.is_file(), (
            "the common-dir hook must run from a linked worktree, not be "
            "silently skipped"
        )


# ─── (REVIEW виток 2) A4: verify без shell-операторов ────────────────────


class TestVerifyNoShellOperators:
    def test_verify_shell_operator_refused_by_contract(self):
        from awf.unit_contract import parse_todo_contract

        content = "---\nverify:\n  - true && false\n---\n# T\n"
        with pytest.raises(ValueError) as e:
            parse_todo_contract(content)
        assert "use a script or separate commands" in str(e.value), str(e.value)

    def test_quoted_operator_stays_legal(self):
        from awf.unit_contract import parse_todo_contract

        content = (
            "---\nverify:\n  - grep -n \"pid > 1\" awf/_proc.py\n---\n# T\n"
        )
        contract, _unknown = parse_todo_contract(content)
        assert contract and contract["verify"], contract

    def test_escaped_operator_outside_quotes_stays_legal(self):
        """Backslash outside quotes is a literal escape for shlex too: an
        escaped ``;`` is ONE argument, not a shell separator — refusing it
        would be a false positive (architect-review M8)."""
        from awf.unit_contract import parse_todo_contract

        content = "---\nverify:\n  - grep a\\;b file\n---\n# T\n"
        contract, _unknown = parse_todo_contract(content)
        assert contract and contract["verify"], contract

    def test_run_cmd_argv_not_shell(self, tmp_path):
        """I1: verify-pack отбивает оператор тем же предикатом (rc 127 +
        ABORTED), shell не запускается и файл не создаётся."""
        from awf import verify_pack

        rc, out, timed_out = verify_pack._run_cmd(
            "echo hit > struck.txt", tmp_path, 60
        )
        assert rc == 127 and "shell operator" in out, (rc, out)
        assert not timed_out
        assert not (tmp_path / "struck.txt").is_file(), (
            "shell semantics created a file — _run_cmd still uses the shell"
        )

    def test_candidate_and_verify_pack_same_rc(self, tmp_path):
        """Эквивалентная argv-команда: candidate-проверка и verify-pack дают
        одинаковый rc (ОДИН механизм запуска)."""
        from awf import candidate_verify as cv
        from awf import verify_pack

        st, _out = cv.run_check(["echo ok"], tmp_path)
        rc, _out2, timed_out = verify_pack._run_cmd("echo ok", tmp_path, 60)
        assert st == cv.CHECK_OK and rc == 0 and not timed_out
        st2, _out3 = cv.run_check(["false"], tmp_path)
        rc2, _out4, _t2 = verify_pack._run_cmd("false", tmp_path, 60)
        assert st2 == cv.CHECK_FAILED and rc2 == 1

    def test_missing_executable_is_typed_not_traceback(self, tmp_path):
        """PA-B parity edge: with argv the old ``shell=True`` rc 127 for a
        missing executable became a ``FileNotFoundError`` out of
        ``verify_pack._run_cmd``. Both launchers must report the same typed
        failure, not crash the pack."""
        from awf import candidate_verify as cv
        from awf import verify_pack

        rc, out, timed_out = verify_pack._run_cmd(
            "awf-no-such-executable-xyz", tmp_path, 60
        )
        assert rc == 127 and not timed_out and "ABORTED" in out, (rc, out)
        status, cv_out = cv.run_check(["awf-no-such-executable-xyz"], tmp_path)
        assert status == cv.CHECK_FAILED and "ABORTED" in cv_out, cv_out


# ─── (REVIEW виток 2) A5: _newest_armed_ack фильтрует закрытые/чужие ─────


class TestNewestArmedAckFilter:
    def test_closed_and_foreign_acks_are_not_armed(self, tmp_git_repo):
        proj = tmp_git_repo
        _project(proj)
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        from awf.api import pipeline as pl

        # живой/текущий — САМЫЙ старый из трёх
        (inbox / "ACK-TODO-0001.ready").touch()
        time.sleep(0.02)
        # завершённый: DONE в outbox
        (inbox / "TODO-0003.md").write_text("# done\n", encoding="utf-8")
        (inbox / "ACK-TODO-0003.ready").touch()
        (outbox / "DONE-TODO-0003.ready").touch()
        time.sleep(0.02)
        # чужой: нет TODO-файла — САМЫЙ новый: жадный по mtime сказал бы его
        (inbox / "ACK-TODO-0042.ready").touch()
        armed = pl._newest_armed_ack(proj)
        assert armed == "TODO-0001", armed

    def test_only_closed_acks_arm_nothing(self, tmp_git_repo):
        proj = tmp_git_repo
        _project(proj)
        (proj / ".agentic" / "inbox" / "ACK-TODO-0042.ready").touch()
        from awf.api import pipeline as pl

        assert pl._newest_armed_ack(proj) == ""


# ─── architect-review M8: REVIEW-читатель через тот же предикат цикла ────


class TestPendingReviewCyclePredicate:
    def test_legacy_done_suppresses_a_pending_review(self, tmp_git_repo):
        """REVIEW-0001 (legacy short) + DONE-0001.ready: цикл завершён.

        Инлайн-проверка ``DONE-TODO-0001.ready`` этот файл не видела и
        поднимала завершённый юнит как «pending review».
        """
        proj = tmp_git_repo
        _project(proj)
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{TODO}.ready").touch()
        (outbox / "REVIEW-0001.md").write_text("rejected\n", encoding="utf-8")
        (outbox / "DONE-0001.ready").touch()
        from awf.api import pipeline as pl

        assert pl._pending_review_todo(proj) == ""

    def test_live_review_stays_pending(self, tmp_git_repo):
        proj = tmp_git_repo
        _project(proj)
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{TODO}.ready").touch()
        (outbox / f"REVIEW-{TODO}.md").write_text("rejected\n", encoding="utf-8")
        from awf.api import pipeline as pl

        assert pl._pending_review_todo(proj) == TODO


# ─── (REVIEW виток 3) F1: исполняемый хук без shebang = shell-fallback ───


class TestShebanglessHook:
    def test_fallback_runs_shebangless_hook_like_git(self, tmp_path, monkeypatch):
        """Матрица `хук есть, исполняем, без shebang`: git на ENOEXEC уходит
        в `sh <hook> <args>` (run-command.c) и ВЫПОЛНЯЕТ хук; прямой
        запуск fallback'а должен повторить это, а не превращать
        `[Errno 8] Exec format error` в rc 127 (отказ коммита, который
        бэкенд бы пропустил)."""
        publish = _publish()
        repo = _repo(tmp_path)
        flag = tmp_path / "shebangless.flag"
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(f'echo x > "{flag}"\n', encoding="utf-8")  # БЕЗ #!
        hook.chmod(0o755)
        proc = publish._run_hook_direct(repo, "pre-commit", os.environ.copy())
        assert proc.returncode == 0, proc
        assert flag.is_file(), (
            f"a shebang-less executable hook must run through the sh "
            f"fallback like git does, got rc={proc.returncode}: {proc.stderr}"
        )

    def test_backend_and_fallback_commit_through_shebangless_hook(
        self, tmp_path, monkeypatch
    ):
        """Бэкенд (`git hook run`) и fallback дают одинаковый исход на
        одинаковых репо с without-shebang хуком: оба committed."""
        repo_backend = _repo(tmp_path / "backend")
        body = f'printf ok >> "{tmp_path / "b.flag"}"\n'
        _write_hook(repo_backend, "pre-commit", body)
        _touch_work(repo_backend)
        out_backend = _commit_unit(repo_backend, ("README.md",))

        repo_fallback = _repo(tmp_path / "fallback")
        body2 = f'printf ok >> "{tmp_path / "f.flag"}"\n'
        _write_hook(repo_fallback, "pre-commit", body2)
        publish = _publish()
        monkeypatch.setattr(publish, "_hook_run_supported", lambda _p: False)
        _touch_work(repo_fallback)
        out_fallback = _commit_unit(repo_fallback, ("README.md",))

        assert out_backend.status == out_fallback.status == "committed", (
            f"backend: {out_backend} / fallback: {out_fallback}"
        )
        assert (tmp_path / "b.flag").is_file()
        assert (tmp_path / "f.flag").is_file()


# ─── (REVIEW виток 4) G1: git завершает строку буфера перед хуками ───────


class TestMessageBufferCompletedLine:
    def test_commit_msg_trailer_lands_on_its_own_line(self, tmp_path):
        """git перед `commit-msg` завершает строку буфера
        (`strbuf_complete_line`): аппендящий трейлер — отдельная строка,
        а не склейка с последней строкой тела."""
        repo = _repo(tmp_path)
        _write_hook(repo, "commit-msg", "#!/bin/sh\nprintf 'X-TRAILER\\n' >> \"$1\"\n")
        _touch_work(repo)
        outcome = _commit_unit(repo, ("README.md",))
        assert outcome.status == "committed", f"hook repo must commit: {outcome}"
        msg = _git(repo, "log", "-1", "--format=%B").stdout
        assert "\nX-TRAILER" in msg, f"a trailer must be its own line: {msg!r}"

    def test_backend_and_fallback_trailer_on_the_same_buffer(
        self, tmp_path, monkeypatch
    ):
        """Паритет бэкенда (`git hook run`) и fallback на одном дереве и
        одном аппендящем хуке: сообщение в коммите — буфер с завершённой
        строкой, трейлер отдельной строкой."""
        hook_body = "#!/bin/sh\nprintf 'X-TRAILER\\n' >> \"$1\"\n"
        base = f"awf(verify): {TODO}"

        repo_backend = _repo(tmp_path / "backend")
        _write_hook(repo_backend, "prepare-commit-msg", hook_body)
        _touch_work(repo_backend)
        out_backend = _commit_unit(repo_backend, ("README.md",))
        msg_backend = _git(repo_backend, "log", "-1", "--format=%B").stdout

        publish = _publish()
        monkeypatch.setattr(publish, "_hook_run_supported", lambda _p: False)
        repo_fallback = _repo(tmp_path / "fallback")
        _write_hook(repo_fallback, "prepare-commit-msg", hook_body)
        _touch_work(repo_fallback)
        out_fallback = _commit_unit(repo_fallback, ("README.md",))
        msg_fallback = _git(repo_fallback, "log", "-1", "--format=%B").stdout

        assert out_backend.status == out_fallback.status == "committed", (
            f"backend: {out_backend} / fallback: {out_fallback}"
        )
        for name, out, msg in (
            ("backend", out_backend, msg_backend),
            ("fallback", out_fallback, msg_fallback),
        ):
            assert out.status == "committed", f"{name}: {out}"
            assert "\nX-TRAILER" in msg, f"{name}: trailer glued: {msg!r}"
            assert msg.rstrip("\n") == base + "\nX-TRAILER", f"{name}: {msg!r}"


# ─── (REVIEW виток 3) F2: битый контракт ≠ отсутствие контракта ──────────


class TestBrokenContractSection:
    def _make_proj(self, tmp_path: Path, todo_text: str) -> Path:
        proj = tmp_path / "proj"
        inbox = proj / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text(todo_text, encoding="utf-8")
        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True)
        return proj

    def test_unparsed_contract_block_fails_not_silent(self, tmp_path):
        """TODO содержит блок `verify:`, но он не парсится → секция
        `contract_tests` FAIL с причиной, а не «skipped (нет блока)» —
        иначе контрольно-командный гейт молча пропадает."""
        from awf import verify_pack

        proj = self._make_proj(tmp_path, "---\nverify:\n  - true && false\n---\n# T\n")
        contract = verify_pack._load_todo_contract(
            proj, "TODO-0001", proj / ".agentic" / "logs"
        )
        assert contract is not None, "a broken block must not look absent"
        section = verify_pack._contract_tests_section(proj, contract, 60)
        assert section.status == "fail", section
        joined = "\n".join(section.lines)
        assert "does not parse" in joined, joined

    def test_missing_block_is_still_skipped_none(self, tmp_path):
        """Блока нет вовсе → contract is None → skipped (это НЕ fail)."""
        from awf import verify_pack

        proj = self._make_proj(tmp_path, "# no contract block\n")
        contract = verify_pack._load_todo_contract(
            proj, "TODO-0001", proj / ".agentic" / "logs"
        )
        assert contract is None
        section = verify_pack._contract_tests_section(proj, None, 60)
        assert section.status == "skipped", section


# ─── (REVIEW виток 3) F3: `$(...)`, бэктики, фоновый `&` тоже отказ ──────


class TestVerifyShellPolicyRound3:
    def test_extended_operators_refused(self):
        from awf.unit_contract import parse_todo_contract

        for cmd in ["echo $(which git)", "echo `date`", "true & false", "a$(&)"]:
            with pytest.raises(ValueError) as e:
                parse_todo_contract(f'---\nverify:\n  - {cmd}\n---\n# T\n')
            assert "use a script or separate commands" in str(e.value), (cmd, str(e.value))

    def test_var_val_and_globbing_are_literals(self):
        """VAR=val и globbing РАЗРЕШЕНЫ как литералы (без shell-интерпретации
        — argv передаёт их как аргументы), отказ только на операторах."""
        from awf.unit_contract import parse_todo_contract

        content = "---\nverify:\n  - FOO=1 grep a* awf/file.txt\n---\n# T\n"
        contract, _unknown = parse_todo_contract(content)
        assert contract and contract["verify"], contract


# ─── (REVIEW виток 5) H1: внутренние переводы строк — отказ ──────────────


class TestVerifyNewlinePolicy:
    def test_block_scalar_trailing_newline_is_one_command(self):
        """Одиночный блочный скаляр ``- |`` всегда даёт хвостовой ``\\n`` —
        это ОДНА команда, а не мусор: хвост нормализуем, команду принимаем."""
        from awf.unit_contract import parse_todo_contract

        content = "---\nverify:\n  - |\n    pytest -q tests/negative\n---\n# T\n"
        contract, _unknown = parse_todo_contract(content)
        assert contract and contract["verify"], contract
        [cmd] = contract["verify"]
        assert cmd.startswith("pytest -q tests/negative"), contract["verify"]
        assert "\n" not in cmd and "\r" not in cmd, contract["verify"]

    def test_interior_newlines_refused(self):
        """``- |`` с двумя строками — две команды в одной ступени: отказ."""
        from awf.unit_contract import parse_todo_contract

        content = "---\nverify:\n  - |\n    true\n    false\n---\n# T\n"
        with pytest.raises(ValueError) as e:
            parse_todo_contract(content)
        assert "multi-line verify" in str(e.value), str(e.value)
        assert "use a script or separate commands" in str(e.value), str(e.value)

    def test_interior_crlf_refused_too(self):
        """``\\r`` и ``\\r\\n`` внутри команды — тот же отказ, что и ``\\n``."""
        from awf.unit_contract import parse_todo_contract

        for esc in ["true\\rfalse", "true\\r\\nfalse"]:
            content = f'---\nverify:\n  - "{esc}"\n---\n# T\n'
            with pytest.raises(ValueError) as e:
                parse_todo_contract(content)
            assert "multi-line verify" in str(e.value), (esc, str(e.value))

    def test_quoted_operator_still_legal_with_trailing_newline(self):
        """Валидная команда с блочным скаляром проходит и после операторной
        проверки (хвостовой \\n не ломает shlex-поиск операторов)."""
        from awf.unit_contract import parse_todo_contract

        content = "---\nverify:\n  - |\n    grep -n \"pid > 1\" awf/_proc.py\n---\n# T\n"
        contract, _unknown = parse_todo_contract(content)
        assert contract and contract["verify"], contract

    def test_block_scalar_trailing_blank_line_is_normalized(self):
        """Литеральный блок с пустой строкой перед закрывающим ``---`` даёт
        в YAML команду С хвостовым ``\n`` (в отличие от блока в EOF, где
        PyYAML его не добавляет). Нормализация обязана принять её как ОДНУ
        команду — без ``rstrip`` это был бы ложный «multi-line verify»."""
        from awf.unit_contract import parse_todo_contract

        content = (
            "---\nverify:\n  - |\n    pytest -q tests/negative\n\n---\n# T\n"
        )
        contract, _unknown = parse_todo_contract(content)
        assert contract and contract["verify"], contract
        [cmd] = contract["verify"]
        assert cmd.rstrip("\r\n") == "pytest -q tests/negative", contract["verify"]

    def test_normalized_to_empty_command_is_refused(self):
        """Хвостовая нормализация не должна молча превратить запись в
        пустую команду: ``\n``/``\r``/``\r\n`` — отказ «non-empty»."""
        from awf.unit_contract import parse_todo_contract

        for raw in ["\\n", "\\r", "\\r\\n"]:
            content = f'---\nverify: ["{raw}"]\n---\n# T\n'
            with pytest.raises(ValueError) as e:
                parse_todo_contract(content)
            assert "non-empty" in str(e.value), (raw, str(e.value))


# ─── (REVIEW виток 6) I1: config-команды под тем же shell-предикатом ─────


class TestConfigCommandsShellPolicy:
    def test_parse_argv_refuses_operator_outside_quotes(self):
        """``parse_argv`` — один механизм для контракта и config-команд:
        пайп вне кавычек — отказ с текстом A4, а не argv-литерал."""
        from awf.verify import parse_argv

        parts, problem = parse_argv("echo hi | cat")
        assert parts == [], parts
        assert "shell operator" in problem, problem
        assert "use a script or separate commands" in problem, problem

        parts, problem = parse_argv("echo hi")
        assert problem == "" and parts == ["echo", "hi"], (parts, problem)

    def test_quoted_operator_still_runs(self):
        """Оператор В кавычках — легальный аргумент, отказ не срабатывает."""
        from awf.verify import parse_argv

        parts, problem = parse_argv('grep -n "pid > 1" awf/_proc.py')
        assert problem == "", problem
        assert parts[:2] == ["grep", "-n"], parts

    def test_candidate_check_refuses_not_literal(self, tmp_path):
        """Config-команда с пайпом: candidate check — типизированный отказ
        (CHECK_FAILED + ABORTED), а не ложный зелёный с литералом."""
        from awf import candidate_verify as cv

        status, out = cv.run_check(["echo hi | cat"], tmp_path)
        assert status == cv.CHECK_FAILED, (status, out)
        assert "ABORTED" in out and "shell operator" in out, out

    def test_verify_pack_runner_refuses_not_literal(self, tmp_path):
        from awf import verify_pack

        rc, out, timed_out = verify_pack._run_cmd("echo hi | cat", tmp_path, 30)
        assert rc == 127, (rc, out)
        assert not timed_out
        assert "ABORTED" in out and "shell operator" in out, out

    def test_run_verify_commands_config_pipe_is_refused(self, tmp_path):
        """``verification.test_cmd`` с пайпом → verify не зелёный; чистая
        argv-команда → ok (тот же предикат, исход по существу)."""
        from awf import verify

        bad = {"verification": {"test_cmd": "echo hi | cat"}}
        assert verify.run_verify_commands(bad, tmp_path) is False

        good = {"verification": {"test_cmd": "true"}}
        assert verify.run_verify_commands(good, tmp_path) is True


# ─── (REVIEW виток 8) K1: H1-правило на config-поверхности ───────────────


class TestConfigCommandsNewlinePolicy:
    """Внутренний перевод строки в config-команде — тот же отказ, что в
    ``verify:``: иначе ``true\\nfalse`` уходит в argv как ОДНА команда с
    лишним литеральным аргументом (ложный зелёный на гейте юнита)."""

    def _proj(self, tmp_path: Path, verification_yaml: str) -> Path:
        from conftest import _git_init

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentic" / "context").mkdir(parents=True)
        (proj / ".agentic" / "config.yaml").write_text(verification_yaml)
        _git_init(proj)
        return proj

    def test_run_verify_commands_config_interior_newline_is_refused(self, tmp_path):
        from awf import verify

        bad = {"verification": {"test_cmd": "true\nfalse"}}
        assert verify.run_verify_commands(bad, tmp_path) is False

    def test_config_trailing_newline_is_one_command(self, tmp_path):
        from awf import verify

        for cmd in ["true\n", "true\r\n"]:
            assert verify.run_verify_commands(
                {"verification": {"test_cmd": cmd}}, tmp_path
            ) is True, repr(cmd)

    def test_baseline_cmd_interior_newline_is_refused(
        self, tmp_path, monkeypatch, capsys
    ):
        from awf import cmd_baseline

        proj = self._proj(
            tmp_path,
            "verification:\n"
            "  test_cmd: /bin/echo FULL-SUITE-RUN\n"
            '  baseline_cmd: "true\\nfalse"\n',
        )
        monkeypatch.chdir(proj)
        args = type("Args", (), {"todo_id": "TODO-0024"})()
        assert cmd_baseline.run(args) == 1, "interior newline must be refused"
        out = capsys.readouterr().out
        assert "multi-line verify" in out, out
        assert "use a script or separate commands" in out, out

    def test_baseline_cmd_trailing_newline_still_runs(self, tmp_path, monkeypatch):
        from awf import cmd_baseline

        proj = self._proj(
            tmp_path,
            "verification:\n"
            "  test_cmd: /bin/echo FULL-SUITE-RUN\n"
            "  baseline_cmd: |\n"
            "    /bin/echo U1-SMOKE-MARKER\n",
        )
        monkeypatch.chdir(proj)
        args = type("Args", (), {"todo_id": "TODO-0025"})()
        assert cmd_baseline.run(args) == 0
        log = (proj / ".agentic" / "context" / "BASELINE-TODO-0025.tests.log").read_text()
        assert "U1-SMOKE-MARKER" in log, log

    def test_candidate_fallback_config_newline_is_refused(self, tmp_path):
        """Fallback candidate check берёт ``test_cmd`` — тот же отказ, не
        argv-литерал (PA-B-класс на обязательной проверке)."""
        from awf import candidate_verify as cv
        from awf.verify import candidate_check_commands

        config = {"verification": {"test_cmd": "true\nfalse"}}
        commands = candidate_check_commands(config, None)
        assert commands == ["true\nfalse"], commands
        status, out = cv.run_check(commands, tmp_path)
        assert status == cv.CHECK_FAILED, (status, out)
        assert "ABORTED" in out and "multi-line verify" in out, out

        status, out = cv.run_check(
            candidate_check_commands({"verification": {"test_cmd": "true\n"}}, None),
            tmp_path,
        )
        assert status == cv.CHECK_OK, (status, out)


# ─── (REVIEW виток 6) I2: битый контракт — invalid у обоих читателей ─────


class TestBrokenContractReaders:
    def _proj(self, tmp_path: Path, text: str) -> Path:
        proj = tmp_path / "proj"
        inbox = proj / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "TODO-0103.md").write_text(text, encoding="utf-8")
        return proj

    def test_evidence_plan_marks_broken_not_absent(self, tmp_path):
        """api/run reader: блок есть, но не парсится → маркер ``broken``,
        а не пустой план «нет команд»."""
        from awf.api.run import _todo_evidence_plan

        proj = self._proj(tmp_path, "---\nverify:\n  - true && false\n---\n# T\n")
        plan = _todo_evidence_plan(proj / ".agentic" / "inbox" / "TODO-0103.md")
        assert plan.get("contract") == "broken", plan
        assert "broken contract block" in plan["note"], plan

        (proj / ".agentic" / "inbox" / "TODO-0103.md").write_text(
            "# no contract block\n", encoding="utf-8"
        )
        absent = _todo_evidence_plan(proj / ".agentic" / "inbox" / "TODO-0103.md")
        assert "contract" not in absent, absent
        assert "broken" not in absent["note"], absent

    def test_contract_digest_distinguishes_broken_from_absent(self, tmp_path):
        """candidate_verify reader: битый блок — свой digest, не дайджест
        пустого контракта (иначе restore теряет команды молча)."""
        from awf import candidate_verify as cv

        proj = self._proj(tmp_path, "---\nverify:\n  - true && false\n---\n# T\n")
        broken = cv.contract_digest(proj, "TODO-0103")
        assert broken == cv.contract_digest(proj, "TODO-0103"), "must be stable"

        (proj / ".agentic" / "inbox" / "TODO-0103.md").write_text(
            "# no contract block\n", encoding="utf-8"
        )
        absent = cv.contract_digest(proj, "TODO-0103")
        assert broken != absent, "a broken block must not read as absent"

    def test_run_state_keeps_the_marker_on_read(self):
        """Маркер переживает запись/чтение run.yaml — иначе surfaces снова
        увидят «нет команд». Здоровый план историческую форму не меняет."""
        from awf import run_state

        broken = run_state._normalize_evidence_plan(
            {"todo_id": "TODO-0103", "contract": "broken", "note": "x"}
        )
        assert broken["contract"] == "broken", broken

        healthy = run_state._normalize_evidence_plan({"todo_id": "TODO-0001"})
        assert "contract" not in healthy, healthy


# ─── (REVIEW виток 6) I4: доки — git 2.36 и env/glob ────────────────────


class TestDocsPublishRequirements:
    def test_readme_states_git_minimum(self):
        root = Path(__file__).resolve().parents[2]
        readme = (root / "README.md").read_text(encoding="utf-8")
        assert "2.36" in readme, "README must name the git minimum for `git hook run`"
        assert "git hook run" in readme, readme

    def test_unit_contract_docs_env_and_glob(self):
        root = Path(__file__).resolve().parents[2]
        doc = (root / "docs" / "unit-contract.md").read_text(encoding="utf-8")
        assert "env VAR=val" in doc, "environment prefix needs the `env` form"
        assert "литерал" in doc, "globbing must be documented as a literal"


# ─── (REVIEW виток 10) M1: рецепт BLOCKED резюмит ────────────────────────


class TestBlockedReplanResumes:
    def test_recipe_names_unblock_and_reaches_the_launch(
        self, tmp_git_repo, monkeypatch
    ):
        """M1: текст blocked-события несёт `awf_unblock`, и исполнение ровно
        названных шагов (unblock → continue с пином) доходит до запуска."""
        from awf.api import hygiene
        from awf.api import pipeline as pl
        from awf.api import wait_event as we

        proj = tmp_git_repo
        api.init_project(proj, project_name="M8")
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{TODO}.ready").touch()
        (outbox / f"BLOCKED-{TODO}.ready").touch()

        state = {
            "stage_name": "agent-implementer",
            "stage_kind": "work",
            "todo_id": TODO,
            "last_signal": f"BLOCKED-{TODO}",
        }
        monkeypatch.setattr(we, "read_state", lambda *_a, **_k: dict(state))
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        ev = we.wait_for_event(proj, timeout=1)
        assert ev.event_type == "blocked", f"{ev.event_type}: {ev.message!r}"
        assert "awf_unblock" in ev.message, ev.message
        assert "from_stage" in ev.message, ev.message

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (False, None, None)
        )
        monkeypatch.setattr(pl, "read_state", lambda _p: dict(state))

        class _Stop(Exception):
            pass

        captured: dict = {}

        def _fake_start(project_dir, **kwargs):
            captured.update(kwargs)
            raise _Stop()

        monkeypatch.setattr(pl, "start_in_background", _fake_start)

        hygiene.unblock_todo(proj, TODO)
        try:
            pl.continue_pipeline(proj, todo_id=TODO, from_stage="agent-implementer")
        except _Stop:
            pass
        else:
            raise AssertionError("the blocked-replan recipe did not reach the launch")
        assert captured.get("todo_id") == TODO, captured


# ─── (REVIEW виток 10) M2: ACK после reconcile, подсказка по диску ───────


class TestAckOnDiskAfterReconcile:
    def test_noop_hint_is_backed_by_a_file(self, tmp_git_repo, monkeypatch):
        """M2: reconcile deduped the fresh ACK against a waiting APPROVE —
        the noop answer must not claim an armed signal the code just removed."""
        proj = tmp_git_repo
        _project(proj)
        inbox = proj / ".agentic" / "inbox"
        (inbox / f"APPROVE-{TODO}.ready").touch()
        from awf.api import pipeline as pl

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )
        res = pl.continue_pipeline(proj, ack=TODO)
        assert res.run_mode == "noop", res
        if "ACK is armed" in res.message:
            assert (inbox / f"ACK-{TODO}.ready").is_file(), res.message

    def test_legacy_short_ack_is_not_duplicated(self, tmp_git_repo, monkeypatch):
        """M2: the consumer eats ``ACK-0001.ready``; arming the canonical
        spelling next to it would strand a second ACK for a future cycle."""
        proj = tmp_git_repo
        _project(proj)
        inbox = proj / ".agentic" / "inbox"
        (inbox / f"{TODO}.ready").touch()
        (inbox / "ACK-0001.ready").touch()
        from awf.api import pipeline as pl

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )
        res = pl.continue_pipeline(proj, ack=TODO)
        assert res.run_mode == "noop", res
        assert "ACK is armed for TODO-0001" in res.message, res.message
        assert "ACK-0001.ready" in res.message, res.message
        assert not (inbox / f"ACK-{TODO}.ready").exists(), (
            "a legacy ACK for the same unit must not gain a canonical twin"
        )


# ─── (REVIEW виток 10) M3: битый блок — отказ у двух оставшихся читателей ─


class TestBrokenContractRemainingReaders:
    def _proj(self, tmp_path: Path, text: str) -> Path:
        proj = tmp_path / "proj"
        inbox = proj / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "TODO-0103.md").write_text(text, encoding="utf-8")
        return proj

    def test_candidate_commands_refuses_instead_of_falling_back(self, tmp_path):
        from awf import candidate_verify as cv

        proj = self._proj(tmp_path, "---\nverify:\n  - true && false\n---\n# T\n")
        with pytest.raises(cv.BrokenContractError):
            cv.candidate_commands(proj, "TODO-0103")

    def test_prove_red_refuses_instead_of_saying_no_block(self, tmp_path):
        from awf import prove_red as pr

        proj = _repo(tmp_path)
        inbox = proj / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "TODO-0103.md").write_text(
            "---\nverify:\n  - true && false\n---\n# T\n", encoding="utf-8"
        )
        with pytest.raises(Exception) as exc:
            pr.prove_red(proj, "TODO-0103")
        text = str(exc.value)
        assert "does not parse" in text, text
        assert "no prove_red block" not in text, text


# ─── (REVIEW виток 10) M4: три архивных контракта под A4 ─────────────────


class TestArchivedContractsArgvSafe:
    def test_three_archived_contracts_parse(self):
        from awf.unit_contract import parse_todo_contract

        root = Path(__file__).resolve().parents[2]
        paths = [
            root / ".agentic" / "done" / todo / "TODO.md"
            for todo in ("TODO-0103", "TODO-0146", "TODO-0147")
        ]
        present = [path for path in paths if path.is_file()]
        if not present:
            pytest.skip("machine-local archive absent")
        for path in present:
            contract, _unknown = parse_todo_contract(
                path.read_text(encoding="utf-8")
            )
            assert contract and contract.get("verify"), path
            joined = "\n".join(contract["verify"])
            assert "&&" not in joined and "|" not in joined and ";" not in joined, (
                f"{path.parent.name} still carries a shell operator: {joined}"
            )

    def test_synthetic_contract_argv_policy(self, tmp_path):
        """O1: the argv rule is checked on a synthetic contract, so the test
        never depends on the gitignored local archive."""
        from awf.unit_contract import parse_todo_contract

        refused = tmp_path / "refused.md"
        refused.write_text(
            "---\nverify:\n  - true && false\n---\n# T\n", encoding="utf-8"
        )
        with pytest.raises(ValueError) as exc:
            parse_todo_contract(refused.read_text(encoding="utf-8"))
        assert "shell operator" in str(exc.value), str(exc.value)

        accepted = tmp_path / "accepted.md"
        accepted.write_text(
            "---\nverify:\n  - python -m pytest -q\n---\n# T\n",
            encoding="utf-8",
        )
        contract, _unknown = parse_todo_contract(
            accepted.read_text(encoding="utf-8")
        )
        assert contract and contract.get("verify") == ["python -m pytest -q"]

    def test_flake_replay_helper_exists(self):
        root = Path(__file__).resolve().parents[2]
        helper = root / "scripts" / "flake-replay.sh"
        assert helper.is_file(), "TODO-0103 needs its argv-safe replay helper"
        assert os.access(helper, os.X_OK), "the replay helper must be executable"


# ─── (REVIEW виток 10) M5: отказ плана первым, кэш вердикта APPROVE ──────


class TestStalePlanAndHintCache:
    def test_refused_plan_reports_its_own_reason_first(self, tmp_path, monkeypatch):
        from awf import commit_plan

        proj = tmp_path / "proj"
        inbox = proj / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / f"APPROVE-{TODO}.ready").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(
            commit_plan, "read_approval_binding", lambda *_a, **_k: {"generation": 0}
        )
        monkeypatch.setattr(
            commit_plan.run_state,
            "read_run",
            lambda _p: {"active": True, "generation": 0},
        )
        plan = commit_plan.CommitPlan(
            todo_id=TODO,
            generation=0,
            verified_sha="",
            files=(),
            refusal="gitlink is not supported in a unit commit",
        )
        monkeypatch.setattr(commit_plan, "build_commit_plan", lambda *_a, **_k: plan)
        status, reason = commit_plan.preexisting_approval_status(proj, TODO, "sha")
        assert status == "stale"
        assert reason == plan.refusal, reason

    def test_stale_hint_reuses_a_cached_verdict(self, tmp_git_repo, monkeypatch):
        from awf import commit_plan
        from awf.api import wait_event as we

        proj = tmp_git_repo
        inbox = proj / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        sig = inbox / f"APPROVE-{TODO}.ready"
        sig.touch()
        old = time.time() - 100
        os.utime(sig, (old, old))

        calls: list[int] = []

        def _status(*_a, **_k):
            calls.append(1)
            return "stale", "candidate moved"

        monkeypatch.setattr(commit_plan, "preexisting_approval_status", _status)
        cache = getattr(we, "_STALE_STATUS_CACHE", None)
        if cache is not None:
            cache.clear()
        first = we._stale_decision_hint(proj, TODO)
        second = we._stale_decision_hint(proj, TODO)
        assert first, "a stale pre-existing APPROVE must produce a hint"
        assert second == first
        assert len(calls) == 1, "an unchanged tree must reuse the cached verdict"


# ─── (REVIEW виток 11) N1: кэш stale-APPROVE видит рестарт забега ────────


class TestStaleHintCacheSeesRunState:
    def test_stale_hint_cache_sees_a_run_restart(self, tmp_path):
        from awf import commit_delta, run_state
        from awf.api import wait_event as we

        proj = _repo(tmp_path)
        (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "ignore runtime")
        for sub in ("inbox", "context", "state"):
            (proj / ".agentic" / sub).mkdir(parents=True)

        run_state.write_run(
            proj, active=True, generation=5, current=TODO, queue=[TODO], index=0
        )
        verified = "a" * 64
        (proj / ".agentic" / "context" / f"VERIFIED-{TODO}.sha").write_text(
            verified + "\n", encoding="utf-8"
        )
        files = tuple(commit_delta.worktree_file_set(proj))
        approve = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
        approve.write_text(
            json.dumps(
                {
                    "todo_id": TODO,
                    "generation": 5,
                    "verified_sha": verified,
                    "files_digest": commit_plan.files_digest(files),
                }
            ),
            encoding="utf-8",
        )
        old = time.time() - 100
        os.utime(approve, (old, old))

        assert commit_plan.preexisting_approval_status(proj, TODO, "") == (
            "valid",
            "",
        )
        cache = getattr(we, "_STALE_STATUS_CACHE", None)
        if cache is not None:
            cache.clear()
        assert we._stale_decision_hint(proj, TODO) == "", (
            "a valid APPROVE needs no hint"
        )

        # Restart: same tree, same APPROVE bytes/mtime — only the active run
        # generation moves (state lives under the gitignored .agentic/).
        run_state.write_run(proj, generation=6)
        assert approve.stat().st_mtime == old
        assert tuple(commit_delta.worktree_file_set(proj)) == files

        stale_status, stale_reason = commit_plan.preexisting_approval_status(
            proj, TODO, ""
        )
        assert stale_status == "stale" and "generation" in stale_reason, stale_reason

        cached_hint = we._stale_decision_hint(proj, TODO)
        if cache is not None:
            cache.clear()
        fresh_hint = we._stale_decision_hint(proj, TODO)
        assert fresh_hint, "the stale APPROVE must produce a re-approve hint"
        assert cached_hint == fresh_hint, (
            "the cached verdict survived a run restart (tree and APPROVE mtime "
            "unchanged, but the generation moved): the verify wait sheds the "
            "stale APPROVE and the supervisor is never told to re-approve — "
            f"cached={cached_hint!r} fresh={fresh_hint!r}"
        )


# ─── (architect-review M8) кэш stale-APPROVE видит правку TODO-контракта ──


class TestStaleHintCacheSeesTodoBytes:
    def test_stale_hint_cache_sees_a_todo_edit(self, tmp_path, monkeypatch):
        """PA-A binds the TODO bytes (contract + unit revision), and both sit
        under the gitignored ``.agentic/`` — invisible to the tree
        fingerprint. A cache key without them kept the pre-edit verdict after
        the contract moved, the exact invalidation PA-A exists to report."""
        from awf import commit_plan
        from awf.api import wait_event as we

        proj = _repo(tmp_path)
        (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
        _git(proj, "add", "-A")
        _git(proj, "commit", "-qm", "ignore runtime")
        inbox = proj / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        todo_md = inbox / f"{TODO}.md"
        todo_md.write_text('---\nverify: ["true"]\n---\n\n# unit\n', encoding="utf-8")
        sig = inbox / f"APPROVE-{TODO}.ready"
        sig.touch()
        old = time.time() - 100
        os.utime(sig, (old, old))

        calls: list[int] = []

        def _status(*_a, **_k):
            calls.append(1)
            return "stale", "candidate moved"

        monkeypatch.setattr(commit_plan, "preexisting_approval_status", _status)
        cache = getattr(we, "_STALE_STATUS_CACHE", None)
        if cache is not None:
            cache.clear()
        assert we._stale_decision_hint(proj, TODO)
        assert we._stale_decision_hint(proj, TODO)
        assert len(calls) == 1, "an unchanged state must reuse the cached verdict"

        # Edit the TODO contract. The tree fingerprint cannot see it
        # (``.agentic/`` is ignored) and the APPROVE keeps its mtime.
        todo_md.write_text(
            '---\nverify: ["true", "true"]\n---\n\n# unit\n', encoding="utf-8"
        )
        assert we._stale_decision_hint(proj, TODO)
        assert len(calls) == 2, (
            "the cached verdict survived a TODO-contract edit (gitignored "
            "``.agentic/``, APPROVE mtime unchanged): PA-A's contract "
            "invalidation is invisible to the stale-APPROVE hint"
        )
