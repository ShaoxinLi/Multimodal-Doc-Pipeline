import os
import stat
import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unicodedata import normalize
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from doc_pipeline.contracts.config import Limits
from doc_pipeline.contracts.core import PipelineError

_LOCAL_HEADER = struct.Struct("<4s5H3I2H")


_READ_SIZE = 1024 * 1024


def archive_member_parts(name: str) -> tuple[str, ...]:
    parts = tuple(name.removesuffix("/").split("/"))
    if (
        any(part in {"", ".", ".."} for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 or char in '\\:*?"<>|' for char in name)
        or any(part.endswith((" ", ".")) for part in parts)
    ):
        raise ValueError("unsafe member path")
    return parts


@contextmanager
def open_validated_archive(path: Path, limits: Limits) -> Iterator[ZipFile]:
    try:
        stream = path.open("rb")
    except OSError as error:
        raise PipelineError("parse", "invalid_provider_archive") from error

    with stream:
        try:
            if os.fstat(stream.fileno()).st_size > limits.max_zip_bytes:
                raise PipelineError("parse", "provider_archive_limit_exceeded")
            archive = ZipFile(stream)
        except (BadZipFile, OSError, ValueError, struct.error) as error:
            raise PipelineError("parse", "invalid_provider_archive") from error

        with archive:
            try:
                _check_members(archive, limits)
            except (
                BadZipFile,
                OSError,
                RuntimeError,
                ValueError,
                EOFError,
                struct.error,
                zlib.error,
            ) as error:
                raise PipelineError("parse", "invalid_provider_archive") from error
            yield archive


def _check_members(archive: ZipFile, limits: Limits) -> None:
    members = archive.infolist()
    if len(members) > limits.max_zip_members:
        raise PipelineError("parse", "provider_archive_limit_exceeded")
    seen: dict[tuple[str, ...], bool] = {}
    expanded_size = 0
    for member in members:
        parts = archive_member_parts(member.orig_filename)
        path_key = tuple(normalize("NFC", part).casefold() for part in parts)
        is_dir = member.is_dir()
        mode = stat.S_IFMT(member.external_attr >> 16)
        if (
            path_key in seen
            or mode not in (0, stat.S_IFDIR if is_dir else stat.S_IFREG)
            or member.external_attr & 0x408
            or member.flag_bits & 0x41
            or member.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
            or (member.compress_type == ZIP_STORED and member.compress_size != member.file_size)
            or (is_dir and member.file_size != 0)
        ):
            raise ValueError("unsupported ZIP member")
        seen[path_key] = is_dir
        expanded_size += member.file_size
        if member.file_size > limits.max_member_bytes or expanded_size > limits.max_expanded_bytes:
            raise PipelineError("parse", "provider_archive_limit_exceeded")

    for parts in seen:
        if any(seen.get(parts[:index]) is False for index in range(1, len(parts))):
            raise ValueError("ZIP member has a file ancestor")

    for member in members:
        _verify_member(archive, member)


def _verify_member(archive: ZipFile, member: ZipInfo) -> None:
    # ZipExtFile checks overlap and the member name. Its reader stops at the declared output size.
    with archive.open(member):
        pass
    stream = archive.fp
    stream.seek(member.header_offset)
    header = _LOCAL_HEADER.unpack(stream.read(_LOCAL_HEADER.size))
    _, _, flags, method, _, _, crc_header, compressed_header, size_header, name_size, extra_size = header
    if flags != member.flag_bits or method != member.compress_type:
        raise ValueError("inconsistent ZIP member header")
    if not flags & 8 and (
        crc_header != member.CRC
        or compressed_header not in (member.compress_size, 0xFFFFFFFF)
        or size_header not in (member.file_size, 0xFFFFFFFF)
    ):
        raise ValueError("inconsistent ZIP member size or CRC")
    stream.seek(name_size + extra_size, 1)
    decompressor = zlib.decompressobj(-zlib.MAX_WBITS) if method == ZIP_DEFLATED else None
    remaining = member.compress_size
    size = crc = 0
    while remaining:
        compressed = stream.read(min(remaining, _READ_SIZE))
        if not compressed:
            raise ValueError("truncated ZIP member")
        remaining -= len(compressed)
        if decompressor is None:
            size += len(compressed)
            crc = zlib.crc32(compressed, crc)
            continue
        while compressed:
            expanded = decompressor.decompress(compressed, min(_READ_SIZE, member.file_size - size + 1))
            size += len(expanded)
            if size > member.file_size or decompressor.unused_data:
                raise ValueError("ZIP member exceeds its declared size")
            crc = zlib.crc32(expanded, crc)
            compressed = decompressor.unconsumed_tail

    if (decompressor is not None and not decompressor.eof) or size != member.file_size or crc != member.CRC:
        raise ValueError("ZIP member size or CRC mismatch")
