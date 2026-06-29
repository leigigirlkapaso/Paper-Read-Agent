"""
modules/thinker/tests/test_register.py
契约测试：register(core) 返回合法的 ModuleInfo。
"""

import pytest

from paperreadagent.core import create_core, ModuleInfo


@pytest.fixture
def core():
    return create_core(config_path="config.yaml", db_path=":memory:")


def test_register_returns_valid_module_info(core):
    from paperreadagent.modules.thinker import register

    info = register(core)
    assert isinstance(info, ModuleInfo)
    assert info.name == "thinker"
    assert info.version == "0.2.0"
    assert info.schema_version >= 1
    assert info.routes is not None


def test_register_is_idempotent(core):
    """重复调用 register 不报错。"""
    from paperreadagent.modules.thinker import register

    register(core)
    register(core)
    mods = core.list_modules()
    assert len(mods) == 1


def test_module_appears_in_core_registry(core):
    from paperreadagent.modules.thinker import register

    register(core)
    mod = core.get_module("thinker")
    assert mod is not None
    assert mod.name == "thinker"
    assert mod.version == "0.2.0"
