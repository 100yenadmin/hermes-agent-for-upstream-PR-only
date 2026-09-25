import pytest
import hermes_yaml as yaml


@pytest.fixture
def policy_homes(tmp_path, monkeypatch):
    home, managed = tmp_path / "home", tmp_path / "managed"
    for path in (home, managed):
        path.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as config
    from hermes_cli import config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config_effective._EFFECTIVE_CACHE, config_effective._LAST_GOOD_USER_RAW):
        cache.clear()
    managed_scope.invalidate_managed_cache()
    return home, managed


@pytest.mark.parametrize(("strict", "expected"), [(False, {"grants": ["user"]}), (True, None)])
def test_strict_overlay_preserves_explicit_null(policy_homes, strict, expected):
    from hermes_cli.config_effective import load_user_config_effective
    home, managed = policy_homes
    (home / "config.yaml").write_text("authorization: {nested: {grants: [user]}}\n")
    (managed / "config.yaml").write_text("authorization: {nested: null}\n")
    result = load_user_config_effective(fail_closed=strict)
    assert result["authorization"]["nested"] == expected


@pytest.mark.parametrize(
    ("managed_body", "error"),
    [("kanban: [unterminated", yaml.YAMLError), ("- kanban\n", ValueError)],
)
def test_strict_managed_root_never_recovers_user_grant(policy_homes, managed_body, error):
    from hermes_cli.config_effective import load_user_config_effective
    home, managed = policy_homes
    (home / "config.yaml").write_text("kanban: {dispatch_profiles: [default]}\n")
    (managed / "config.yaml").write_text(managed_body)
    with pytest.raises(error):
        load_user_config_effective(fail_closed=True)


@pytest.mark.parametrize("managed_body", ["", "null\n"])
def test_strict_read_treats_empty_managed_root_as_no_policy(policy_homes, managed_body):
    """An empty or ``null`` managed file carries no policy, like an absent one."""
    from hermes_cli.config_effective import load_user_config_effective
    home, managed = policy_homes
    (home / "config.yaml").write_text("kanban: {dispatch_profiles: [default]}\n")
    (managed / "config.yaml").write_text(managed_body)
    assert load_user_config_effective(fail_closed=True)["kanban"] == {"dispatch_profiles": ["default"]}
