"""Publish one Mac's Codex session JSONL files into a shared private archive."""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .paths import (
    default_input_path,
    default_machine_id,
    default_shared_input_path,
    validate_machine_id,
)


@dataclass(frozen=True)
class PublishResult:
    copied: int
    skipped: int
    destination: Path


def iter_session_files(input_path: Path) -> list[tuple[Path, Path]]:
    if input_path.is_file():
        if input_path.suffix != ".jsonl":
            raise ValueError(f"Codex session input is not a JSONL file: {input_path}")
        return [(input_path, Path(input_path.name))]
    return [
        (path, path.relative_to(input_path))
        for path in sorted(input_path.rglob("*.jsonl"))
        if path.is_file() and not path.is_symlink()
    ]


def ensure_destination_parent(archive_root: Path, destination: Path) -> None:
    """Create a destination parent without following an existing symlink."""
    directory = archive_root
    for component in destination.parent.relative_to(archive_root).parts:
        directory = directory / component
        if directory.is_symlink():
            raise ValueError(f"Codex session archive destination contains a symlink: {directory}")
        directory.mkdir(exist_ok=True)


def contents_match(source: Path, destination: Path) -> bool:
    """Compare content without retaining stale results for preserved file metadata."""
    filecmp.clear_cache()
    return filecmp.cmp(source, destination, shallow=False)


def publish_sessions(input_path: Path, archive_root: Path, machine_id: str) -> PublishResult:
    machine_id = validate_machine_id(machine_id)
    source_root = input_path.expanduser().resolve()
    archive_root = archive_root.expanduser().resolve()
    destination_root = archive_root / machine_id

    if source_root == destination_root or source_root.is_relative_to(destination_root):
        raise ValueError("Codex session input must not be inside its archive destination.")
    if destination_root.is_relative_to(source_root):
        raise ValueError("Codex session archive destination must not be inside its input directory.")
    if not source_root.exists():
        raise FileNotFoundError(f"Codex session input does not exist: {source_root}")

    archive_root.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for source, relative_path in iter_session_files(source_root):
        destination = destination_root / relative_path
        ensure_destination_parent(archive_root, destination)
        if destination.is_symlink():
            raise ValueError(f"Codex session archive destination is a symlink: {destination}")
        source_mode = source.stat().st_mode & 0o777
        if input_path.is_file() and destination.exists() and not contents_match(source, destination):
            raise ValueError(
                "Codex session archive destination already contains a different single-file session: "
                f"{destination}"
            )
        if (
            destination.exists()
            and not os.path.samestat(source.stat(), destination.stat())
            and destination.stat().st_nlink == 1
            and destination.stat().st_mode & 0o777 == source_mode
            and contents_match(source, destination)
        ):
            skipped += 1
            continue

        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                source_mode | 0o200,
            )
            os.close(descriptor)
            shutil.copyfile(source, temporary)
            temporary.chmod(source_mode)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        copied += 1

    return PublishResult(copied=copied, skipped=skipped, destination=destination_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish one Mac's Codex session JSONL files to its private archive namespace."
    )
    parser.add_argument("--input", default=default_input_path(), help="Local Codex sessions directory or one JSONL session")
    parser.add_argument(
        "--archive-root",
        help="Shared Codex-session archive root; defaults to the configured ChatGPT Archive/codex-sessions",
    )
    parser.add_argument(
        "--machine-id",
        default=default_machine_id(),
        required=default_machine_id() is None,
        help="Stable lowercase ID for this Mac",
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    archive_root = (
        Path(args.archive_root).expanduser()
        if args.archive_root
        else Path(default_shared_input_path()).expanduser()
    )
    result = publish_sessions(Path(args.input).expanduser(), archive_root, args.machine_id)
    print(f"done: copied {result.copied} sessions, skipped {result.skipped} unchanged sessions")
    print(f"shared archive: {result.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
