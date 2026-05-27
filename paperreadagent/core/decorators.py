"""
core/decorators.py
API 稳定性标注装饰器，用于标记核心层公共方法的兼容性承诺。
"""

from __future__ import annotations


def stable(func):
    """签名冻结，不会变。模块可以放心依赖。"""
    func._stability = "stable"
    return func


def evolving(func):
    """可能加可选参数，向后兼容。模块可以使用，但需容忍参数变化。"""
    func._stability = "evolving"
    return func


def internal(func):
    """模块禁止直接调用。仅供核心层内部使用。"""
    func._stability = "internal"
    return func
