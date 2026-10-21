"""便捷入口：python run.py <status|ingest|ask|fill|serve> [...]

等价于 python -m rag.cli。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rag.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
