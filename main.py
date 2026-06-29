"""
PaperReadAgent — 根入口
实际逻辑在 paperreadagent/ 包中。

注意：本文件同时作为顶层 `main` 模块的兼容入口。pytest 的 rootdir 会
被插到 sys.path 最前，使得 `import main` 命中本文件而非 paperreadagent/main.py，
因此这里通过 PEP 562 的 `__getattr__` 惰性转发到包内 main 的公开符号，
保证 `from main import build_final_report` 等导入在测试与运行时均可用，
同时避免在 import 阶段触发包内 main 的副作用（stdout 重配置、日志 handler）。
"""

import sys
from pathlib import Path

# 将 paperreadagent 加入 sys.path，保持原有 import 路径兼容
sys.path.insert(0, str(Path(__file__).parent / "paperreadagent"))


def __getattr__(name: str):
    """惰性转发到 paperreadagent.main 的公开符号（仅在访问时导入）。"""
    from paperreadagent import main as _pkg_main
    try:
        return getattr(_pkg_main, name)
    except AttributeError as exc:  # pragma: no cover
        raise AttributeError(
            f"module 'main' has no attribute '{name}'"
        ) from exc


if __name__ == "__main__":
    from paperreadagent.main import main
    main()
