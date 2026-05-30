"""Verify Thinker no longer registers a global floating component."""
import pytest
from paperreadagent.core import create_core


def test_register_does_not_register_global_component():
    """After register(), frontend should NOT contain thinker-panel component."""
    core = create_core(config_path="config.yaml", db_path=":memory:")
    from paperreadagent.modules.thinker import register
    register(core)

    thinker_comps = [c for c in core.frontend._components if c.name == "thinker-panel"]
    assert len(thinker_comps) == 0, "thinker-panel global component should NOT exist after register()"


def test_register_returns_valid_module_info():
    """register() should still return valid ModuleInfo with updated version."""
    core = create_core(config_path="config.yaml", db_path=":memory:")
    from paperreadagent.modules.thinker import register
    from paperreadagent.core import ModuleInfo

    info = register(core)
    assert isinstance(info, ModuleInfo)
    assert info.name == "thinker"
    assert info.version == "0.2.0"
    assert info.schema_version == 3
    assert info.routes is not None
