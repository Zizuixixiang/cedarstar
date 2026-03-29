"""
项目根入口：python import_memes.py <清单.txt>

实际执行 scripts/import_memes.py。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "import_memes.py"


def main() -> None:
    if not SCRIPT.is_file():
        print(f"错误：找不到 {SCRIPT}", file=sys.stderr)
        sys.exit(1)
    if len(sys.argv) < 2:
        print(
            "用法: python import_memes.py <清单.txt>\n"
            "  完整说明: python import_memes.py -h",
            file=sys.stderr,
        )
        sys.exit(1)
    rc = subprocess.call([sys.executable, str(SCRIPT)] + sys.argv[1:])
    sys.exit(rc)


if __name__ == "__main__":
    main()
