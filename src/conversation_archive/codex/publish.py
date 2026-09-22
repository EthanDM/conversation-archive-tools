"""Publish one Mac's Codex session JSONL files into a shared private archive."""

from __future__ import annotations

import argparse
import errno
import filecmp
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .paths import (
    INSTALLATION_ID_PATH,
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
    claimed: bool = False


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


def session_id(path: Path) -> str | None:
    """Return the Codex session ID recorded in a JSONL file, if present."""
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload")
            if event.get("type") == "session_meta" and isinstance(payload, dict):
                value = payload.get("id")
                if isinstance(value, str):
                    return value
    return None


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def mkdir_durable(path: Path, *, parents: bool = False) -> None:
    """Create a directory and persist each newly created parent entry."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
        if not parents:
            break
    path.mkdir(parents=parents, exist_ok=True)
    for directory in reversed(missing):
        fsync_directory(directory.parent)


def atomically_create_file(path: Path, contents: str) -> bool:
    """Create a fully written file only when its destination does not exist."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(contents)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        except OSError as error:
            if error.errno in {errno.EOPNOTSUPP, errno.EXDEV, errno.EPERM}:
                raise ValueError(
                    f"Atomic Codex ownership records require hard-link support: {path.parent}"
                ) from error
            raise
        created = True
        return True
    finally:
        temporary.unlink(missing_ok=True)
        if created:
            fsync_directory(path.parent)


def installation_id(path: Path) -> str:
    """Return this installation's persistent UUID without following a symlink."""
    path = path.expanduser()
    existing = existing_installation_id(path)
    if existing is not None:
        return existing

    mkdir_durable(path.parent, parents=True)
    value = str(uuid.uuid4())
    if atomically_create_file(path, f"{value}\n"):
        return value
    return installation_id(path)


def existing_installation_id(path: Path) -> str | None:
    """Return an existing installation UUID without creating or following one."""
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError(f"Codex installation ID is a symlink: {path}")

    if path.exists():
        if not path.is_file():
            raise ValueError(f"Codex installation ID is not a file: {path}")
        value = path.read_text(encoding="utf-8").strip()
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise ValueError(f"Codex installation ID is invalid: {path}") from error
    return None


def rotate_installation_id(path: Path) -> str:
    path = path.expanduser()
    existing_installation_id(path)
    path.unlink(missing_ok=True)
    return installation_id(path)


def restore_installation_id(path: Path, value: str | None) -> None:
    if value is None:
        path.unlink(missing_ok=True)
        return
    path.unlink(missing_ok=True)
    atomically_create_file(path, value)


def reservation_is_owned(archive_root: Path, machine_id: str, installation: str) -> bool:
    """Return whether an already-created reservation belongs to an installation."""
    reservation = archive_root / ".machines" / f"{machine_id}.json"
    if not (reservation.exists() or reservation.is_symlink()):
        return False
    return read_reservation(reservation) == installation


def can_keep_rotated_installation(
    archive_root: Path, machine_id: str, installation: str | None
) -> bool:
    """Keep a rotated ID only when its reservation can be verified safely."""
    if installation is None:
        return False
    try:
        return reservation_is_owned(archive_root, machine_id, installation)
    except (OSError, ValueError):
        return False


def reservation_path(archive_root: Path, machine_id: str) -> Path:
    machines = archive_root / ".machines"
    if machines.is_symlink():
        raise ValueError(f"Codex machine reservation directory is a symlink: {machines}")
    mkdir_durable(machines)
    return machines / f"{machine_id}.json"


def read_reservation(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"Codex machine reservation is a symlink: {path}")
    if not path.is_file():
        raise ValueError(f"Codex machine reservation is not a file: {path}")
    try:
        reservation = json.loads(path.read_text(encoding="utf-8"))
        value = reservation["installation_id"]
        if not isinstance(value, str):
            raise ValueError
        return str(uuid.UUID(value))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Codex machine reservation is corrupt: {path}") from error


def reserve_machine_id(
    archive_root: Path,
    destination_root: Path,
    machine_id: str,
    installation: str,
    claim_existing_machine_id: bool,
) -> bool:
    if destination_root.is_symlink():
        raise ValueError(f"Codex session archive destination contains a symlink: {destination_root}")
    reservation = reservation_path(archive_root, machine_id)
    if reservation.exists() or reservation.is_symlink():
        reserved_installation = read_reservation(reservation)
        if reserved_installation != installation:
            raise ValueError(f"Machine ID is already reserved by another installation: {machine_id}")
        return False

    if destination_root.exists() and any(destination_root.iterdir()) and not claim_existing_machine_id:
        if reservation.exists() or reservation.is_symlink():
            reserved_installation = read_reservation(reservation)
            if reserved_installation == installation:
                return False
            raise ValueError(f"Machine ID is already reserved by another installation: {machine_id}")
        raise ValueError(
            f"Machine ID has an existing archive namespace; rerun with --claim-existing-machine-id: {machine_id}"
        )

    payload = json.dumps({"installation_id": installation}, sort_keys=True) + "\n"
    if atomically_create_file(reservation, payload):
        return True
    reserved_installation = read_reservation(reservation)
    if reserved_installation != installation:
        raise ValueError(f"Machine ID is already reserved by another installation: {machine_id}")
    return False


def publish_sessions(
    input_path: Path,
    archive_root: Path,
    machine_id: str,
    *,
    claim_existing_machine_id: bool = False,
    installation_id_path: Path | None = None,
    wait_for_claim_sync: bool = False,
    confirm_claim_sync: bool = False,
) -> PublishResult:
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

    mkdir_durable(archive_root, parents=True)
    claimed = reserve_machine_id(
        archive_root,
        destination_root,
        machine_id,
        installation_id(installation_id_path or INSTALLATION_ID_PATH),
        claim_existing_machine_id,
    )
    if wait_for_claim_sync and (claimed or not confirm_claim_sync):
        return PublishResult(copied=0, skipped=0, destination=destination_root, claimed=True)
    copied = skipped = 0
    for source, relative_path in iter_session_files(source_root):
        destination = destination_root / relative_path
        ensure_destination_parent(archive_root, destination)
        if destination.is_symlink():
            raise ValueError(f"Codex session archive destination is a symlink: {destination}")
        source_mode = source.stat().st_mode & 0o777
        if input_path.is_file() and destination.exists() and not contents_match(source, destination):
            if session_id(source) != session_id(destination):
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
        "--confirm-machine-id-sync",
        action="store_true",
        help="Confirm the reservation is visible to every publisher before copying sessions",
    )
    parser.add_argument(
        "--rotate-installation-id",
        action="store_true",
        help="Generate a new local installation ID before claiming a new machine ID after migration or restore",
    )
    parser.add_argument(
        "--machine-id",
        default=default_machine_id(),
        required=default_machine_id() is None,
        help="Stable lowercase ID for this Mac",
    )
    parser.add_argument(
        "--claim-existing-machine-id",
        action="store_true",
        help="Claim a populated legacy namespace that has no reservation",
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    archive_root = (
        Path(args.archive_root).expanduser()
        if args.archive_root
        else Path(default_shared_input_path()).expanduser()
    )
    validate_machine_id(args.machine_id)
    installation_path = INSTALLATION_ID_PATH.expanduser()
    previous_installation = existing_installation_id(installation_path) if args.rotate_installation_id else None
    rotated_installation = None
    try:
        if args.rotate_installation_id:
            rotated_installation = rotate_installation_id(installation_path)
        result = publish_sessions(
            Path(args.input).expanduser(),
            archive_root,
            args.machine_id,
            claim_existing_machine_id=args.claim_existing_machine_id,
            wait_for_claim_sync=True,
            confirm_claim_sync=args.confirm_machine_id_sync,
        )
    except Exception:
        if args.rotate_installation_id and not can_keep_rotated_installation(
            archive_root, args.machine_id, rotated_installation
        ):
            restore_installation_id(installation_path, previous_installation)
        raise
    if result.claimed:
        print(f"claimed machine ID: {args.machine_id}")
        print("wait for shared storage to synchronize, then rerun with --confirm-machine-id-sync to publish sessions")
        return 0
    print(f"done: copied {result.copied} sessions, skipped {result.skipped} unchanged sessions")
    print(f"shared archive: {result.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
