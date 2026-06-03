"""Thinker 模块测试 fixtures。"""

import pytest
from pathlib import Path
import sys

# 确保项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

from core import create_core, ModuleInfo


@pytest.fixture
def thinker_core():
    """创建带 Thinker 模块注册的 Core 实例（内存数据库）。"""
    core = create_core(config_path="config.yaml", db_path=":memory:")
    from modules.thinker import register
    register(core)
    return core
