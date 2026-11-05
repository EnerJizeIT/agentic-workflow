"""TODO-0323 (A22): a socks proxy never reaches the worker.

opencode 2.x uses Bun's fetch, which rejects the socks scheme: a worker that
inherits ``http_proxy=socks5h://...`` dies on its first network call. The
worker env is assembled in ``awf._env`` — this file pins what it does with
the proxy (default: strip socks) and what ``worker.network.proxy`` adds
(``http://host:port`` or ``none``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from awf._env import (
    PROXY_VARS,
    apply_worker_proxy,
    awf_subprocess_env,
    worker_proxy_setting,
)


def _project(base: Path, monkeypatch, name: str, config: str = "") -> Path:
    proj = base / name
    (proj / ".agentic").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        config or "project:\n  name: test\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg" / name))
    return proj


def _any_socks(env: dict[str, str]) -> list[str]:
    return [
        name for name in PROXY_VARS
        if (env.get(name) or "").lower().startswith("socks")
    ]


class TestDefaultStripsSocks:
    def test_socks_variables_are_gone_from_the_worker_env(
        self, tmp_path, monkeypatch
    ):
        proj = _project(tmp_path, monkeypatch, "socks")
        monkeypatch.setenv("http_proxy", "socks5h://127.0.0.1:10808")
        monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:10808")
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:10809")

        env = awf_subprocess_env(project_dir=proj)

        assert _any_socks(env) == []
        # A non-socks proxy is not the worker's problem: it stays.
        assert env["https_proxy"] == "http://127.0.0.1:10809"

    def test_a_clean_env_keeps_its_other_variables(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "clean")
        monkeypatch.setenv("HOME", "/home/tester")

        env = awf_subprocess_env(project_dir=proj)

        assert env["HOME"] == "/home/tester"
        assert _any_socks(env) == []


class TestConfiguredProxy:
    def test_http_proxy_from_config_is_handed_to_the_worker(
        self, tmp_path, monkeypatch
    ):
        config = "worker:\n  network:\n    proxy: http://127.0.0.1:10809\n"
        proj = _project(tmp_path, monkeypatch, "http", config)
        monkeypatch.setenv("http_proxy", "socks5h://127.0.0.1:10808")
        monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:10808")

        env = awf_subprocess_env(project_dir=proj)

        assert env["http_proxy"] == "http://127.0.0.1:10809"
        assert env["https_proxy"] == "http://127.0.0.1:10809"
        assert _any_socks(env) == []

    def test_none_strips_every_proxy_variable(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "none", "worker:\n  network:\n    proxy: none\n")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:10809")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:10809")

        env = awf_subprocess_env(project_dir=proj)

        assert [name for name in PROXY_VARS if name in env] == []

    def test_setting_is_read_from_the_project_config(self, tmp_path, monkeypatch):
        proj = _project(
            tmp_path, monkeypatch, "read",
            "worker:\n  network:\n    proxy: '  http://127.0.0.1:10809  '\n",
        )
        assert worker_proxy_setting(proj) == "http://127.0.0.1:10809"

    def test_wrong_setting_type_degrades_to_the_default(self, tmp_path, monkeypatch):
        config = "worker:\n  network:\n    proxy: 42\n"
        proj = _project(tmp_path, monkeypatch, "bad", config)
        monkeypatch.setenv("http_proxy", "socks5h://127.0.0.1:10808")

        env = awf_subprocess_env(project_dir=proj)

        assert worker_proxy_setting(proj) == ""
        assert _any_socks(env) == []


class TestPayloadRegression:
    def test_opencode_config_content_survives_the_proxy_step(
        self, tmp_path, monkeypatch
    ):
        """The regression the unit names: proxy handling touches env, not config."""
        proj = _project(tmp_path, monkeypatch, "payload")
        monkeypatch.setenv("http_proxy", "socks5h://127.0.0.1:10808")

        env = awf_subprocess_env(project_dir=proj)

        payload = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert payload["permissions"]
        assert "http_proxy" not in payload


class TestFactLine:
    """The stage log says what happened — scheme and names, never values.

    The fact is the return value of ``apply_worker_proxy`` (the same call the
    env builder makes), so the log line cannot be a second opinion.
    """

    def test_default_with_socks_names_the_variable_only(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "socks5h://127.0.0.1:10808")

        fact = apply_worker_proxy(os.environ.copy(), "")

        assert fact == "strip socks (HTTPS_PROXY)"
        assert "10808" not in fact

    def test_configured_http_reports_the_scheme(self, monkeypatch):
        fact = apply_worker_proxy(
            os.environ.copy(), "http://user:pass@127.0.0.1:10809"
        )

        assert fact == "http (http_proxy, https_proxy)"
        assert "10809" not in fact
        assert "pass" not in fact

    def test_none_reports_none(self, monkeypatch):
        for name in PROXY_VARS:
            monkeypatch.delenv(name, raising=False)

        assert apply_worker_proxy(os.environ.copy(), "none") == "none"

    def test_a_clean_env_has_no_fact(self, monkeypatch):
        for name in PROXY_VARS:
            monkeypatch.delenv(name, raising=False)

        assert apply_worker_proxy(os.environ.copy(), "") == ""


class TestSocksInTheConfigKey:
    """A socks value in the key is as unusable as an inherited one."""

    def test_a_socks_setting_is_stripped_instead_of_handed_over(
        self, tmp_path, monkeypatch
    ):
        config = "worker:\n  network:\n    proxy: socks5h://127.0.0.1:10808\n"
        proj = _project(tmp_path, monkeypatch, "cfg-socks", config)

        env = awf_subprocess_env(project_dir=proj)

        assert _any_socks(env) == []
        assert "http_proxy" not in env
        assert "https_proxy" not in env


class TestEnvReportsItsOwnFact:
    def test_the_env_builder_fills_the_collector(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "sink")
        monkeypatch.setenv("HTTPS_PROXY", "socks5h://127.0.0.1:10808")
        facts: list[str] = []

        env = awf_subprocess_env(project_dir=proj, proxy_facts=facts)

        assert facts == ["strip socks (HTTPS_PROXY)"]
        assert "HTTPS_PROXY" not in env

    def test_a_clean_env_leaves_the_collector_empty(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "sink-clean")
        for name in PROXY_VARS:
            monkeypatch.delenv(name, raising=False)
        facts: list[str] = []

        awf_subprocess_env(project_dir=proj, proxy_facts=facts)

        assert facts == []


class TestNoValueInTheFact:
    """The fact carries a scheme and variable names — never a URL.

    ``worker.network.proxy`` accepts ``host:port`` by accident as well as
    ``http://host:port`` on purpose. Echoing the first form would put a
    credential into the stage log and into ``awf doctor``.
    """

    def test_a_value_without_a_scheme_is_never_echoed(self):
        env: dict[str, str] = {}

        fact = apply_worker_proxy(env, "alice:s3cret@proxy:3128")

        assert fact == "value without a scheme (http_proxy, https_proxy)"
        assert "s3cret" not in fact
        assert "3128" not in fact
        # The user meant something: the value is still handed over as written.
        assert env["http_proxy"] == "alice:s3cret@proxy:3128"

    def test_a_scheme_carrying_a_credential_is_not_echoed(self):
        fact = apply_worker_proxy({}, "alice:s3cret@http://proxy:3128")

        assert "s3cret" not in fact
        assert "3128" not in fact


class TestStageLogLine:
    """TODO item 4: the spawn log says what happened to the proxy.

    The line is the fact returned by the same ``awf_subprocess_env`` call
    that built the env, so it cannot describe a different decision.
    """

    def _project(self, base: Path) -> Path:
        proj = base / "stage-proxy"
        for sub in ("inbox", "outbox", "logs", "roles"):
            (proj / ".agentic" / sub).mkdir(parents=True)
        (proj / ".agentic" / "roles" / "developer.md").write_text(
            "# Developer role\n", encoding="utf-8"
        )
        (proj / ".agentic" / "config.yaml").write_text(
            "project:\n  name: test\n", encoding="utf-8"
        )
        return proj

    def _run(self, monkeypatch, proj: Path, socks: bool = True) -> list[str]:
        from awf import agent_stage
        from awf.pipeline import Stage

        logs: list[str] = []

        class _R:
            returncode = 0

        def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                     logs_dir=None, hard_timeout=None, env=None, **kwargs):
            logs.append("ENV " + str(sorted(k for k in env if "proxy" in k.lower())))
            return _R()

        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
        )
        monkeypatch.setattr(
            agent_stage, "_log", lambda logs_dir, msg: logs.append(str(msg))
        )
        monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
        monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
        if socks:
            monkeypatch.setenv("HTTPS_PROXY", "socks5h://127.0.0.1:10808")

        agent_stage.run_agent_stage(
            Stage(name="agent-dev", role="developer", kind="execute"),
            "TODO-0001", proj,
            {"models": {"developer": {"model": "p/test"}}},
            proj / ".agentic" / "logs",
        )
        return logs

    def test_the_log_repeats_the_env_builder_fact(self, tmp_path, monkeypatch):
        logs = self._run(monkeypatch, self._project(tmp_path))

        proxy_lines = [line for line in logs if line.startswith("proxy: ")]
        assert proxy_lines == ["proxy: strip socks (HTTPS_PROXY)"], logs
        assert "10808" not in "\n".join(logs)

    def test_a_clean_env_says_the_environment_was_inherited(
        self, tmp_path, monkeypatch
    ):
        """Nothing was rewritten, so the line must not claim otherwise."""
        logs = self._run(monkeypatch, self._project(tmp_path), socks=False)

        proxy_lines = [line for line in logs if line.startswith("proxy: ")]
        assert proxy_lines == ["proxy: inherited from the parent env"], logs
