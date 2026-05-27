"""
core/config.py
配置加载与合并：模块默认值 < config.yaml < 环境变量。
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

import yaml

from .decorators import stable


@stable
def load_config(config_path: str | Path = "config.yaml") -> dict:
    """加载根 config.yaml，返回完整配置字典。"""
    path = Path(config_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@stable
def load_module_defaults(module_name: str, default_path: str | Path) -> dict:
    """加载模块默认配置。"""
    path = Path(default_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@stable
def merge_configs(*configs: dict) -> dict:
    """
    深度合并多个配置字典，后面的覆盖前面的。
    合并顺序：模块默认值 < config.yaml < 环境变量覆盖。
    """
    result: dict = {}
    for cfg in configs:
        _deep_merge(result, cfg)
    return result


@stable
def apply_env_overrides(config: dict, prefix: str = "PRA_") -> dict:
    """
    用环境变量覆盖配置。
    例：PRA_MODULES_THINKER_INACTIVITY_TIMEOUT_MINUTES=30
      → config["modules"]["thinker"]["inactivity_timeout_minutes"] = "30"
    """
    for key, val in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = key[len(prefix):].lower().split("__")
        _set_nested(config, path, _coerce_env(val))
    return config


def _deep_merge(base: dict, overlay: dict) -> None:
    for k, v in overlay.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = deepcopy(v)


def _set_nested(d: dict, keys: list[str], value) -> None:
    for key in keys[:-1]:
        if key not in d:
            d[key] = {}
        d = d[key]
    d[keys[-1]] = value


def _coerce_env(raw: str):
    """尝试将环境变量字符串转为合理的 Python 类型。"""
    if raw.lower() in ("true", "yes"):
        return True
    if raw.lower() in ("false", "no"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw
