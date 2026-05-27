"""
core/tests/test_config.py
测试配置加载与合并逻辑。
"""

from core.config import merge_configs, apply_env_overrides, load_module_defaults


def test_merge_shallow():
    a = {"key1": "a", "key2": 1}
    b = {"key2": 2, "key3": "c"}
    result = merge_configs(a, b)
    assert result["key1"] == "a"
    assert result["key2"] == 2
    assert result["key3"] == "c"


def test_merge_deep():
    a = {"outer": {"inner_a": 1, "inner_b": 2}}
    b = {"outer": {"inner_b": 99, "inner_c": 3}}
    result = merge_configs(a, b)
    assert result["outer"]["inner_a"] == 1
    assert result["outer"]["inner_b"] == 99
    assert result["outer"]["inner_c"] == 3


def test_merge_three_levels():
    default = {"modules": {"thinker": {"timeout": 10, "mode": "gentle"}}}
    user = {"modules": {"thinker": {"timeout": 20}}}
    result = merge_configs(default, user)
    assert result["modules"]["thinker"]["timeout"] == 20
    assert result["modules"]["thinker"]["mode"] == "gentle"


def test_merge_does_not_mutate_originals():
    a = {"key": {"sub": 1}}
    b = {"key": {"sub": 2}}
    merge_configs(a, b)
    assert a["key"]["sub"] == 1


def test_apply_env_overrides(monkeypatch):
    monkeypatch.setenv("PRA_MODULES__THINKER__TIMEOUT", "30")
    monkeypatch.setenv("PRA_MODULES__THINKER__ENABLED", "true")
    config = {"modules": {"thinker": {"timeout": 10}}}
    result = apply_env_overrides(config, prefix="PRA_")
    assert result["modules"]["thinker"]["timeout"] == 30
    assert result["modules"]["thinker"]["enabled"] is True


def test_apply_env_overrides_no_match(monkeypatch):
    monkeypatch.setenv("OTHER_VAR", "hello")
    config = {"key": "val"}
    result = apply_env_overrides(config)
    assert result["key"] == "val"
    assert "other_var" not in result
