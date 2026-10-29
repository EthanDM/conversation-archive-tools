"""Read plain and Zstandard-compressed Codex session files without modifying them."""

from __future__ import annotations

import codecs
from pathlib import Path
from typing import Iterator

import zstandard


def is_session_file(path: Path) -> bool:
    return path.name.endswith((".jsonl", ".jsonl.zst"))


def session_lines(path: Path) -> Iterator[str]:
    """Yield UTF-8 lines; reject corrupt or incomplete compressed frames.

    Validation completes only when the iterator is exhausted. The file is never
    expanded on disk, and concatenated Zstandard frames are supported.
    """
    if not path.name.endswith(".jsonl.zst"):
        with path.open(encoding="utf-8", errors="replace") as source:
            yield from source
        return

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decompressor = zstandard.ZstdDecompressor().decompressobj()
    pending = ""
    completed_frame = False
    with path.open("rb") as source:
        while chunk := source.read(131072):
            while chunk:
                if completed_frame:
                    decompressor = zstandard.ZstdDecompressor().decompressobj()
                    completed_frame = False
                try:
                    data = decompressor.decompress(chunk)
                except zstandard.ZstdError as error:
                    raise ValueError(f"Invalid compressed Codex session: {path}") from error
                chunk = decompressor.unused_data
                pending += decoder.decode(data)
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    yield line + "\n"
                completed_frame = decompressor.eof
        if not completed_frame:
            raise ValueError(f"Incomplete compressed Codex session: {path}")
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending
