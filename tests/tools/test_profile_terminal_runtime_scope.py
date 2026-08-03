"""Regression coverage for profile-scoped terminal execution state (#68559)."""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
import os
import threading

import pytest

from profile_runtime_context import (
    current_profile_runtime_context,
    use_profile_runtime_context,
    use_terminal_env_overrides,
)
from tools import terminal_tool


def _profile(tmp_path, name: str, terminal_yaml: str):
    home = tmp_path / name
    home.mkdir()
    (home / "config.yaml").write_text(
        f"terminal:\n{terminal_yaml}",
        encoding="utf-8",
    )
    return home


def test_profile_terminal_config_is_context_local_and_process_env_is_unchanged(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_TIMEOUT", "999")
    profile_a = _profile(
        tmp_path,
        "a",
        "  backend: docker\n  timeout: 11\n  docker_image: a/image\n",
    )
    profile_b = _profile(
        tmp_path,
        "b",
        "  backend: local\n  timeout: 22\n",
    )

    with use_profile_runtime_context(profile_a):
        config_a = terminal_tool._get_env_config()
        assert config_a["env_type"] == "docker"
        assert config_a["timeout"] == 11
        assert config_a["docker_image"] == "a/image"
        assert os.environ["TERMINAL_ENV"] == "ssh"
        assert os.environ["TERMINAL_TIMEOUT"] == "999"

        with use_profile_runtime_context(profile_b):
            config_b = terminal_tool._get_env_config()
            assert config_b["env_type"] == "local"
            assert config_b["timeout"] == 22

        assert terminal_tool._get_env_config()["env_type"] == "docker"

    assert current_profile_runtime_context() is None
    assert os.environ["TERMINAL_ENV"] == "ssh"
    assert os.environ["TERMINAL_TIMEOUT"] == "999"


def test_profile_terminal_refs_use_secret_scope_without_process_fallback(
    tmp_path, monkeypatch
):
    from agent.secret_scope import reset_secret_scope, set_secret_scope

    monkeypatch.setenv("PROFILE_TERMINAL_BACKEND", "local")
    profile = _profile(
        tmp_path,
        "scoped-ref",
        "  backend: ${env:PROFILE_TERMINAL_BACKEND}\n",
    )

    token = set_secret_scope({"PROFILE_TERMINAL_BACKEND": "docker"})
    try:
        with use_profile_runtime_context(profile):
            assert terminal_tool._get_env_config()["env_type"] == "docker"
    finally:
        reset_secret_scope(token)

    with use_profile_runtime_context(profile):
        # Missing profile value stays unresolved instead of inheriting the
        # launch profile's `local` backend. Environment creation will reject
        # this unknown backend rather than execute on the host.
        assert terminal_tool._get_env_config()["env_type"] == "${env:PROFILE_TERMINAL_BACKEND}"
    assert os.environ["PROFILE_TERMINAL_BACKEND"] == "local"


def test_profile_dotenv_terminal_values_win_defaults_but_not_explicit_config(
    tmp_path, monkeypatch
):
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )

    dotenv_only = tmp_path / "dotenv-only"
    dotenv_only.mkdir()
    (dotenv_only / "config.yaml").write_text("{}\n", encoding="utf-8")
    (dotenv_only / ".env").write_text(
        "TERMINAL_ENV=docker\nTERMINAL_TIMEOUT=77\n",
        encoding="utf-8",
    )
    explicit = _profile(
        tmp_path,
        "explicit",
        "  backend: local\n  timeout: 22\n",
    )
    (explicit / ".env").write_text(
        "TERMINAL_ENV=docker\nTERMINAL_TIMEOUT=77\n",
        encoding="utf-8",
    )

    for home, expected in ((dotenv_only, ("docker", 77)), (explicit, ("local", 22))):
        token = set_secret_scope(build_profile_secret_scope(home))
        try:
            with use_profile_runtime_context(home):
                config = terminal_tool._get_env_config()
                assert (config["env_type"], config["timeout"]) == expected
        finally:
            reset_secret_scope(token)


def test_load_config_uses_profile_scope_and_invalidates_on_secret_rotation(
    tmp_path, monkeypatch
):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_cli.config import load_config

    home = tmp_path / "config-profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "profile_value: ${env:PROFILE_CONFIG_VALUE}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROFILE_CONFIG_VALUE", "launch-value")

    for scoped_value in ("routed-a", "routed-b"):
        token = set_secret_scope({"PROFILE_CONFIG_VALUE": scoped_value})
        try:
            with use_profile_runtime_context(home):
                assert load_config()["profile_value"] == scoped_value
        finally:
            reset_secret_scope(token)

    assert os.environ["PROFILE_CONFIG_VALUE"] == "launch-value"


def test_managed_config_refs_keep_process_env_precedence(tmp_path, monkeypatch):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_cli import managed_scope
    from hermes_cli.config import load_config

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "managed_probe: ${env:MANAGED_PROBE}\n",
        encoding="utf-8",
    )
    managed = tmp_path / "managed-config"
    managed.mkdir()
    (managed / "config.yaml").write_text(
        "managed_probe: ${env:MANAGED_PROBE}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("MANAGED_PROBE", "managed-process")
    managed_scope.invalidate_managed_cache()

    token = set_secret_scope({"MANAGED_PROBE": "profile-secret"})
    try:
        with use_profile_runtime_context(home):
            assert load_config()["managed_probe"] == "managed-process"
    finally:
        reset_secret_scope(token)


def test_singularity_scratch_dir_uses_profile_secret_scope(tmp_path, monkeypatch):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from tools.environments.singularity import _get_scratch_dir

    profile = _profile(
        tmp_path,
        "profile",
        f"  backend: singularity\n  cwd: {tmp_path / 'profile-cwd'}\n",
    )
    launch_scratch = tmp_path / "launch-scratch"
    profile_scratch = tmp_path / "profile-scratch"
    monkeypatch.setenv("TERMINAL_SCRATCH_DIR", str(launch_scratch))

    token = set_secret_scope({"TERMINAL_SCRATCH_DIR": str(profile_scratch)})
    try:
        with use_profile_runtime_context(profile):
            assert _get_scratch_dir() == profile_scratch
            assert os.environ["TERMINAL_SCRATCH_DIR"] == str(launch_scratch)
    finally:
        reset_secret_scope(token)

    assert _get_scratch_dir() == launch_scratch


def test_managed_terminal_policy_wins_over_profile_config(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    profile = _profile(tmp_path, "profile", "  backend: docker\n")
    (profile / ".env").write_text("TERMINAL_ENV=local\n", encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  ssh_host: managed.example\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()

    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )

    token = set_secret_scope(build_profile_secret_scope(profile))
    try:
        with use_profile_runtime_context(profile):
            config = terminal_tool._get_env_config()
            assert config["env_type"] == "ssh"
            assert config["ssh_host"] == "managed.example"
    finally:
        reset_secret_scope(token)


def test_malformed_profile_config_fails_closed(tmp_path):
    profile = tmp_path / "malformed"
    profile.mkdir()
    (profile / "config.yaml").write_text("terminal: [", encoding="utf-8")

    with pytest.raises(Exception):
        with use_profile_runtime_context(profile):
            pytest.fail("malformed profile config must not enter runtime scope")


def test_profile_scope_propagates_only_when_context_is_copied(tmp_path):
    profile = _profile(tmp_path, "threaded", "  backend: docker\n")
    seen = []

    with use_profile_runtime_context(profile):
        copied = contextvars.copy_context()
        thread = threading.Thread(
            target=lambda: seen.append(
                copied.run(lambda: terminal_tool._get_env_config()["env_type"])
            )
        )
        thread.start()
        thread.join(timeout=5)

    assert seen == ["docker"]


def test_concurrent_profiles_do_not_cross_terminal_config_or_cache_keys(tmp_path):
    profile_a = _profile(tmp_path, "a", "  backend: docker\n  timeout: 11\n")
    profile_b = _profile(tmp_path, "b", "  backend: local\n  timeout: 22\n")
    barrier = threading.Barrier(2, timeout=5)

    def read_profile(home):
        from hermes_cli.config import load_config
        from hermes_constants import get_hermes_home

        with use_profile_runtime_context(home):
            barrier.wait()
            config = terminal_tool._get_env_config()
            return (
                config["env_type"],
                config["timeout"],
                terminal_tool._resolve_container_task_id(None),
                str(get_hermes_home()),
                load_config()["terminal"]["backend"],
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        result_a = pool.submit(read_profile, profile_a)
        result_b = pool.submit(read_profile, profile_b)

    backend_a, timeout_a, key_a, home_a, loaded_a = result_a.result()
    backend_b, timeout_b, key_b, home_b, loaded_b = result_b.result()
    assert (backend_a, timeout_a) == ("docker", 11)
    assert (backend_b, timeout_b) == ("local", 22)
    assert key_a != key_b
    assert home_a == str(profile_a.resolve())
    assert home_b == str(profile_b.resolve())
    assert loaded_a == "docker"
    assert loaded_b == "local"


def test_profile_keys_partition_environment_overrides_and_cwd(tmp_path):
    profile_a = _profile(tmp_path, "a", "  backend: local\n")
    profile_b = _profile(tmp_path, "b", "  backend: local\n")

    with use_profile_runtime_context(profile_a):
        key_a = terminal_tool._resolve_container_task_id("same-task")
        terminal_tool.register_task_env_overrides("same-task", {"cwd": "/profile-a"})
        terminal_tool.record_session_cwd("same-task", "/profile-a")

    with use_profile_runtime_context(profile_b):
        key_b = terminal_tool._resolve_container_task_id("same-task")
        terminal_tool.register_task_env_overrides("same-task", {"cwd": "/profile-b"})
        terminal_tool.record_session_cwd("same-task", "/profile-b")

    assert key_a != key_b
    try:
        with use_profile_runtime_context(profile_a):
            assert terminal_tool.resolve_task_overrides("same-task")["cwd"] == "/profile-a"
            assert terminal_tool.get_session_cwd("same-task") == "/profile-a"
        with use_profile_runtime_context(profile_b):
            assert terminal_tool.resolve_task_overrides("same-task")["cwd"] == "/profile-b"
            assert terminal_tool.get_session_cwd("same-task") == "/profile-b"
    finally:
        with use_profile_runtime_context(profile_a):
            terminal_tool.clear_task_env_overrides("same-task")
        with use_profile_runtime_context(profile_b):
            terminal_tool.clear_task_env_overrides("same-task")


def test_profile_keys_prevent_cached_environment_reuse(tmp_path):
    profile_a = _profile(tmp_path, "a", "  backend: local\n")
    profile_b = _profile(tmp_path, "b", "  backend: local\n")
    env_a = object()
    env_b = object()

    with use_profile_runtime_context(profile_a):
        key_a = terminal_tool._resolve_container_task_id(None)
    with use_profile_runtime_context(profile_b):
        key_b = terminal_tool._resolve_container_task_id(None)

    with terminal_tool._env_lock:
        terminal_tool._active_environments[key_a] = env_a
        terminal_tool._active_environments[key_b] = env_b
    try:
        with use_profile_runtime_context(profile_a):
            assert terminal_tool.get_active_env("default") is env_a
        with use_profile_runtime_context(profile_b):
            assert terminal_tool.get_active_env("default") is env_b
    finally:
        with terminal_tool._env_lock:
            terminal_tool._active_environments.pop(key_a, None)
            terminal_tool._active_environments.pop(key_b, None)


def test_file_state_caches_are_profile_partitioned(tmp_path):
    from tools.file_tools import (
        _check_not_found_cache,
        _patch_failure_tracker,
        _read_tracker,
        _record_not_found,
        _record_patch_failure,
    )

    profile_a = _profile(tmp_path, "a-cache", "  backend: local\n")
    profile_b = _profile(tmp_path, "b-cache", "  backend: local\n")
    missing = str(tmp_path / "missing")

    try:
        with use_profile_runtime_context(profile_a):
            _record_not_found("read", missing, "shared-task", "A-NOTFOUND")
            assert _record_patch_failure("shared-task", missing) == 1
            assert _check_not_found_cache("read", missing, "shared-task") == "A-NOTFOUND"

        with use_profile_runtime_context(profile_b):
            assert _check_not_found_cache("read", missing, "shared-task") is None
            assert _record_patch_failure("shared-task", missing) == 1
    finally:
        _read_tracker.clear()
        _patch_failure_tracker.clear()


def test_file_read_limit_cache_is_profile_partitioned(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import file_tools

    profiles = []
    for name, limit in (("a", 111_111), ("b", 222_222)):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(
            f"file_read_max_chars: {limit}\nterminal:\n  backend: local\n",
            encoding="utf-8",
        )
        profiles.append((home, limit))

    file_tools._max_read_chars_cached = {}
    seen = []
    for home, expected in profiles:
        token = set_hermes_home_override(str(home))
        try:
            with use_profile_runtime_context(home):
                seen.append(file_tools._get_max_read_chars())
        finally:
            reset_hermes_home_override(token)
        assert seen[-1] == expected

    assert seen == [111_111, 222_222]


def test_cleanup_preserves_legacy_raw_fallback_but_not_inside_profile_scope(tmp_path):
    cleaned = []

    class LegacyEnv:
        def cleanup(self):
            cleaned.append(True)

    raw_key = "legacy-raw-task"
    with terminal_tool._env_lock:
        terminal_tool._active_environments[raw_key] = LegacyEnv()
    terminal_tool.cleanup_vm(raw_key)
    assert cleaned == [True]
    assert raw_key not in terminal_tool._active_environments

    profile = _profile(tmp_path, "profile", "  backend: local\n")
    with terminal_tool._env_lock:
        terminal_tool._active_environments[raw_key] = LegacyEnv()
    try:
        with use_profile_runtime_context(profile):
            terminal_tool.cleanup_vm(raw_key)
        assert raw_key in terminal_tool._active_environments
        assert cleaned == [True]
    finally:
        with terminal_tool._env_lock:
            terminal_tool._active_environments.pop(raw_key, None)


def test_all_runtime_cwd_readers_follow_profile_scope(tmp_path, monkeypatch):
    from agent.runtime_cwd import resolve_agent_cwd, resolve_context_cwd
    from tools.code_execution_tool import _resolve_child_cwd
    from tools.file_tools import _configured_terminal_cwd

    launch_cwd = tmp_path / "launch"
    profile_cwd = tmp_path / "profile"
    launch_cwd.mkdir()
    profile_cwd.mkdir()
    profile = _profile(
        tmp_path,
        "cwd-profile",
        f"  backend: local\n  cwd: {profile_cwd}\n",
    )
    monkeypatch.setenv("TERMINAL_CWD", str(launch_cwd))

    with use_profile_runtime_context(profile):
        assert _configured_terminal_cwd() == str(profile_cwd)
        assert _resolve_child_cwd("project", str(tmp_path / "staging")) == str(
            profile_cwd
        )
        session_cwd = tmp_path / "session-cwd"
        session_cwd.mkdir()
        terminal_tool.register_task_env_overrides(
            "execute-session", {"cwd": str(session_cwd)}
        )
        try:
            assert _resolve_child_cwd(
                "project", str(tmp_path / "staging"), task_id="execute-session"
            ) == str(session_cwd)
        finally:
            terminal_tool.clear_task_env_overrides("execute-session")
        assert resolve_agent_cwd() == profile_cwd
        assert resolve_context_cwd() == profile_cwd


def test_nested_terminal_overrides_do_not_mutate_process_env(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home

    profile = _profile(
        tmp_path,
        "override-profile",
        "  backend: local\n  cwd: /profile\n",
    )
    monkeypatch.setenv("TERMINAL_CWD", "/launch")
    foreign = _profile(
        tmp_path,
        "foreign-profile",
        "  backend: local\n  cwd: /foreign\n",
    )

    with use_profile_runtime_context(profile):
        assert str(get_hermes_home()) == str(profile.resolve())
        assert terminal_tool._get_env_config()["cwd"] == "/profile"
        with use_terminal_env_overrides({"TERMINAL_CWD": "/cron-job"}):
            assert terminal_tool._get_env_config()["cwd"] == "/cron-job"
            with use_profile_runtime_context(foreign):
                assert str(get_hermes_home()) == str(foreign.resolve())
                assert terminal_tool._get_env_config()["cwd"] == "/foreign"
            assert str(get_hermes_home()) == str(profile.resolve())
            assert terminal_tool._get_env_config()["cwd"] == "/cron-job"
            assert os.environ["TERMINAL_CWD"] == "/launch"
        assert terminal_tool._get_env_config()["cwd"] == "/profile"

    assert os.environ["TERMINAL_CWD"] == "/launch"


def test_process_reader_thread_keeps_profile_checkpoint_context(tmp_path):
    import json
    import shlex
    import sys

    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from profile_runtime_context import profile_scoped_key
    from tools.process_registry import ProcessRegistry

    launch_home = tmp_path / "launch-home"
    profile_home = tmp_path / "profile-home"
    launch_home.mkdir()
    _profile(tmp_path, "profile-home", "  backend: local\n")

    home_token = set_hermes_home_override(str(launch_home))
    try:
        registry = ProcessRegistry()
        code = 'print("done")'
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        with use_profile_runtime_context(profile_home):
            session = registry.spawn_local(
                command,
                task_id=profile_scoped_key("checkpoint-test"),
            )
            assert session._reader_thread is not None
            session._reader_thread.join(timeout=5)
            assert not session._reader_thread.is_alive()

        profile_checkpoint = profile_home / "processes.json"
        assert profile_checkpoint.exists()
        assert json.loads(profile_checkpoint.read_text(encoding="utf-8")) == []
        assert not (launch_home / "processes.json").exists()
    finally:
        reset_hermes_home_override(home_token)


def test_scoped_runtime_policy_reaches_safe_child_env_and_checkpoint(
    tmp_path, monkeypatch
):
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )
    from hermes_constants import (
        apply_subprocess_home_env,
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from tools.code_execution_tool import _scrub_child_env
    from tools.process_registry import (
        ProcessSession,
        _checkpoint_path,
        _process_visible_to_active_profile,
    )
    from profile_runtime_context import profile_scoped_key
    from tools.terminal_tool import (
        _disk_warning_threshold_gb,
        _foreground_max_timeout,
    )

    profile = _profile(
        tmp_path,
        "child-policy",
        "  backend: ssh\n"
        "  cwd: /remote/workspace\n"
        "  timeout: 777\n"
        "  home_mode: profile\n",
    )
    (profile / "home").mkdir()
    (profile / ".env").write_text(
        "TERMINAL_MAX_FOREGROUND_TIMEOUT=333\n"
        "TERMINAL_DISK_WARNING_GB=12.5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_TIMEOUT", "11")
    monkeypatch.setenv("TERMINAL_DOCKER_ENV", "SECRET=launch")

    home_token = set_hermes_home_override(str(profile))
    secret_token = set_secret_scope(build_profile_secret_scope(profile))
    try:
        with use_profile_runtime_context(profile):
            child = _scrub_child_env(
                dict(os.environ),
                is_passthrough=lambda _name: False,
                is_windows=False,
            )
            assert child["TERMINAL_ENV"] == "ssh"
            assert child["TERMINAL_TIMEOUT"] == "777"
            assert child["TERMINAL_MAX_FOREGROUND_TIMEOUT"] == "333"
            assert child["TERMINAL_HOME_MODE"] == "profile"
            assert "TERMINAL_DOCKER_ENV" not in child
            assert _foreground_max_timeout() == 333
            assert _disk_warning_threshold_gb() == 12.5
            apply_subprocess_home_env(child)
            assert child["HOME"] == str(profile / "home")
            assert _checkpoint_path() == profile / "processes.json"
            own = ProcessSession(id="own", command="x", task_id=profile_scoped_key("default"))
            foreign = ProcessSession(
                id="foreign", command="x", task_id="profile:other-profile:default"
            )
            assert _process_visible_to_active_profile(own) is True
            assert _process_visible_to_active_profile(foreign) is False
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)
