"""Thinker 模块测试 fixtures。"""

import pytest

from paperreadagent.core import create_core, ModuleInfo


@pytest.fixture
def thinker_core():
    """创建带 Thinker 模块注册的 Core 实例（内存数据库）。"""
    core = create_core(config_path="config.yaml", db_path=":memory:")
    from paperreadagent.modules.thinker import register
    register(core)
    return core
