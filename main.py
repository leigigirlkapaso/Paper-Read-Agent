"""
PaperReadAgent — 根入口
实际逻辑在 paperreadagent/ 包中。
"""

import sys
from pathlib import Path

# 将 paperreadagent 加入 sys.path，保持原有 import 路径兼容
sys.path.insert(0, str(Path(__file__).parent / "paperreadagent"))

if __name__ == "__main__":
    from paperreadagent.main import main
    main()
