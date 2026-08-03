"""Characterization + unit tests for the `run_one_job` shared helper (Phase 4A).

`tick`'s per-job body (`_process_job`) is the execute → save → deliver → mark
sequence that fires ONE due job. Phase 4A extracts it into a module-level
`run_one_job(job, *, adapters=None, loop=None, verbose=False)` so the external
Chronos provider's `fire_due` can reuse the IDENTICAL body — no duplicated
correctness.

The first test characterizes the sequence as driven through `tick()` (proving
the extraction didn't change `tick`'s behavior); the rest unit-test the
extracted helper directly.
"""
import cron.scheduler as s


def _patch_pipeline(monkeypatch, *, success=True, output="out", final="final response",
                    error=None, silent_marker_in=None):
    """Patch the job pipeline primitives and record the call order."""
    calls = []

    def fake_run_job(job, *, defer_agent_teardown=None):
        calls.append(("run_job", job["id"]))
        fr = final if silent_marker_in is None else silent_marker_in
        return (success, output, fr, error)

    def fake_save(jid, out):
        calls.append(("save", jid))
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None):
        calls.append(("deliver", job["id"]))
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None):
        calls.append(("mark", jid, ok))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    return calls


def test_tick_process_job_sequence(monkeypatch):
    """Characterization: a single due job driven through tick() runs the
    sequence run_job → save → deliver → mark, in that order."""
    calls = _patch_pipeline(monkeypatch)
    monkeypatch.setattr(s, "get_due_jobs", lambda: [{"id": "j1", "name": "t"}])
    monkeypatch.setattr(s, "advance_next_runs", lambda ids: 1)

    s.tick(verbose=False, sync=True)

    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j1", True)


def test_run_one_job_success_sequence(monkeypatch):
    """The extracted helper runs the same execute→save→deliver→mark sequence
    for a successful job."""
    calls = _patch_pipeline(monkeypatch)

    ok = s.run_one_job({"id": "j2", "name": "t"})

    assert ok is True
    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j2", True)


def test_run_one_job_installs_secret_scope_under_multiplex(monkeypatch, tmp_path):
    """Regression: under profile isolation (multiplex active), run_one_job must
    execute run_job inside a profile secret scope so credential reads
    (resolve_runtime_provider -> get_secret) don't fail-close with
    UnscopedSecretError, and must tear the scope down afterward.

    Behavior contract: a scope is present during run_job and absent after,
    regardless of the concrete secret values.
    """
    from agent import secret_scope as ss

    # Point cron's home resolution at a profile whose .env carries a secret.
    (tmp_path / ".env").write_text("OPENROUTER_BASE_URL=https://openrouter.ai/api/v1\n")
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)

    scope_during_run = {}

    def fake_run_job(job, *, defer_agent_teardown=None):
        # This is where resolve_runtime_provider() would read a secret. Prove a
        # scope is installed and the profile's secret resolves without raising.
        scope_during_run["scope"] = ss.current_secret_scope()
        scope_during_run["base_url"] = ss.get_secret("OPENROUTER_BASE_URL")
        return (True, "out", "final", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", lambda *a, **k: None)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)

    ss.set_multiplex_active(True)
    try:
        ok = s.run_one_job({"id": "j7", "name": "t"})
    finally:
        ss.set_multiplex_active(False)

    assert ok is True
    # Scope was installed during run_job and the profile secret resolved.
    assert scope_during_run["scope"] is not None
    assert scope_during_run["base_url"] == "https://openrouter.ai/api/v1"
    # And it was torn down after run_one_job returned (no leak).
    assert ss.current_secret_scope() is None


def test_run_one_job_scopes_terminal_config_and_deferred_teardown(monkeypatch, tmp_path):
    from profile_runtime_context import current_profile_runtime_context
    from agent import secret_scope as ss
    from hermes_cli import env_loader
    from tools.terminal_tool import _get_env_config

    (tmp_path / "config.yaml").write_text(
        "terminal:\n  backend: docker\n  timeout: 17\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "TELEGRAM_HOME_CHANNEL=profile-home\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)
    seen = {}
    deferred_agent = object()

    def fake_run_job(job, *, defer_agent_teardown=None):
        seen["run_context"] = current_profile_runtime_context()
        seen["run_backend"] = _get_env_config()["env_type"]
        defer_agent_teardown.append(deferred_agent)
        return (True, "out", "final", None)

    def fake_teardown(agent, job_id):
        seen["teardown_context"] = current_profile_runtime_context()
        seen["teardown_backend"] = _get_env_config()["env_type"]
        assert agent is deferred_agent
        assert job_id == "j8"

    def fake_delivery(*_args, **_kwargs):
        from agent.secret_scope import current_secret_scope
        from hermes_constants import get_hermes_home

        seen["delivery_context"] = (
            str(get_hermes_home()),
            current_secret_scope().get("TELEGRAM_HOME_CHANNEL"),
            _get_env_config()["env_type"],
        )
        return None

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(
        env_loader,
        "refresh_profile_secret_sources",
        lambda home: seen.__setitem__("refreshed_home", home) or {},
    )
    monkeypatch.setattr(s, "_teardown_cron_agent", fake_teardown)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", fake_delivery)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)

    ss.set_multiplex_active(True)
    try:
        assert s.run_one_job({"id": "j8", "name": "t"}) is True
    finally:
        ss.set_multiplex_active(False)
    assert seen["run_context"] is not None
    assert seen["refreshed_home"] == tmp_path
    assert seen["run_backend"] == "docker"
    assert seen["teardown_context"] is not None
    assert seen["teardown_backend"] == "docker"
    assert seen["delivery_context"] == (str(tmp_path), "profile-home", "docker")
    assert current_profile_runtime_context() is None


def test_cron_home_targets_read_profile_secret_scope(monkeypatch):
    from agent.secret_scope import reset_secret_scope, set_secret_scope

    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "launch-home")
    monkeypatch.setenv("TELEGRAM_CRON_THREAD_ID", "launch-thread")
    token = set_secret_scope(
        {
            "TELEGRAM_HOME_CHANNEL": "profile-home",
            "TELEGRAM_CRON_THREAD_ID": "profile-thread",
        }
    )
    try:
        assert s._get_home_target_chat_id("telegram") == "profile-home"
        assert s._get_home_target_thread_id("telegram") == "profile-thread"
    finally:
        reset_secret_scope(token)


def test_two_profiles_keep_cron_delivery_scope_isolated(monkeypatch, tmp_path):
    from agent import secret_scope as ss
    from hermes_constants import get_hermes_home
    from profile_runtime_context import current_profile_runtime_context
    from tools.terminal_tool import _get_env_config

    homes = []
    for name in ("a", "b"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(
            "terminal:\n  backend: local\n",
            encoding="utf-8",
        )
        (home / ".env").write_text(
            f"TELEGRAM_HOME_CHANNEL=profile-{name}-home\n",
            encoding="utf-8",
        )
        homes.append(home)

    active = {"home": homes[0]}
    records = []

    def record(stage):
        scope = ss.current_secret_scope()
        records.append(
            (
                active["home"].name,
                stage,
                str(get_hermes_home()),
                None if scope is None else scope.get("TELEGRAM_HOME_CHANNEL"),
                _get_env_config()["env_type"],
                current_profile_runtime_context() is not None,
            )
        )

    def fake_run_job(_job, *, defer_agent_teardown=None):
        record("run")
        assert s._resolve_delivery_target(_job)["chat_id"] == (
            f"profile-{active['home'].name}-home"
        )
        record("target")
        defer_agent_teardown.append(object())
        return True, "output", "final", None

    monkeypatch.setattr(s, "_get_hermes_home", lambda: active["home"])
    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda *_args: record("save") or "/tmp/x")
    def fake_delivery(*_args, **_kwargs):
        record("deliver")
        if active["home"].name == "b":
            raise RuntimeError("synthetic delivery failure")
        return None

    monkeypatch.setattr(s, "_deliver_result", fake_delivery)
    monkeypatch.setattr(s, "_teardown_cron_agent", lambda *_args: record("teardown"))
    monkeypatch.setattr(s, "mark_job_run", lambda *_args, **_kwargs: None)

    ss.set_multiplex_active(True)
    try:
        for home in homes:
            active["home"] = home
            assert s.run_one_job(
                {"id": f"job-{home.name}", "name": home.name, "deliver": "telegram"}
            ) is True
    finally:
        ss.set_multiplex_active(False)

    for name, stage, home, chat, backend, runtime_active in records:
        assert stage in {"run", "target", "save", "deliver", "teardown"}
        assert home == str((tmp_path / name).resolve()), records
        assert chat == f"profile-{name}-home", records
        assert backend == "local"
        assert runtime_active is True


def test_run_one_job_preserves_unscoped_terminal_behavior_outside_multiplex(
    monkeypatch, tmp_path
):
    from profile_runtime_context import current_profile_runtime_context
    from agent import secret_scope as ss

    (tmp_path / "config.yaml").write_text(
        "terminal:\n  backend: docker\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)
    seen = {}

    def fake_run_job(job, *, defer_agent_teardown=None):
        seen["context"] = current_profile_runtime_context()
        return (True, "out", "final", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", lambda *a, **k: None)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)

    ss.set_multiplex_active(False)
    assert s.run_one_job({"id": "j9", "name": "t"}) is True
    assert seen["context"] is None


