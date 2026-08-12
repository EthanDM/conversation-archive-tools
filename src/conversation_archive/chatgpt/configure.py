from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .paths import (
    DEFAULT_DB_PATH,
    DEFAULT_HISTORICAL_DB_PATH,
    write_config,
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Configure canonical ChatGPT export paths.")
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--current-export", required=True, help="Export directory name, such as 2026-07-25")
    parser.add_argument("--runtime-db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--historical-db", default=str(DEFAULT_HISTORICAL_DB_PATH))
    args = parser.parse_args(argv)

    archive_root = Path(args.archive_root).expanduser().resolve()
    export_path = archive_root / "exports" / "full" / args.current_export
    if not export_path.is_dir():
        parser.error(f"Current export directory does not exist: {export_path}")

    config = {
        "archive_root": str(archive_root),
        "current_export": args.current_export,
        "runtime_db": str(Path(args.runtime_db).expanduser()),
        "historical_db": str(Path(args.historical_db).expanduser()),
    }
    path = write_config(config)
    print(f"configured {path}")
    print(f"current export: {export_path}")
    print(f"runtime database: {config['runtime_db']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
