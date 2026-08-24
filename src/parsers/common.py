from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import BinaryIO


BinarySource = bytes | bytearray | BinaryIO | Path | str


def source_name(source: BinarySource, default: str) -> str:
    if isinstance(source, (str, Path)):
        return Path(source).name
    return str(getattr(source, "name", default))


def source_bytes(source: BinarySource) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, bytearray):
        return bytes(source)
    if isinstance(source, (str, Path)):
        return Path(source).read_bytes()
    if hasattr(source, "getvalue"):
        return bytes(source.getvalue())
    position = source.tell() if hasattr(source, "tell") else None
    data = source.read()
    if position is not None and hasattr(source, "seek"):
        source.seek(position)
    return bytes(data)


def source_buffer(source: BinarySource) -> BytesIO:
    return BytesIO(source_bytes(source))

