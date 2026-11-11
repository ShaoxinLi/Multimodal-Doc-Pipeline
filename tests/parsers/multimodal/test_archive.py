import stat
import struct
import zlib
from zipfile import ZipFile, ZipInfo

import pytest

from doc_pipeline.contracts.config import Limits
from doc_pipeline.contracts.core import PipelineError
from doc_pipeline.parsers.multimodal.archive import open_validated_archive
from tests.parsers.multimodal.support import _content_archive, _zip


@pytest.mark.parametrize("member", ["../bad", "/bad", "C:/bad", "nested/../bad", "nested\\bad", "a//b"])
def test_rejects_unsafe_zip_member_even_when_unused(tmp_path, member):
    archive = _content_archive(tmp_path / "result.zip", [[]], [(member, "bad")])
    with pytest.raises(PipelineError) as error, open_validated_archive(archive, Limits()):
        pass
    assert error.value.code == "invalid_provider_archive"


def test_rejects_zip_symlink_and_duplicate_member(tmp_path):
    link = ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = _zip(tmp_path / "link.zip", [(link, "../outside")])
    with pytest.raises(PipelineError) as error, open_validated_archive(archive, Limits()):
        pass
    assert error.value.code == "invalid_provider_archive"

    with pytest.warns(UserWarning, match="Duplicate name"):
        archive = _zip(tmp_path / "duplicate.zip", [("same", "1"), ("same", "2")])
    with pytest.raises(PipelineError) as error, open_validated_archive(archive, Limits()):
        pass
    assert error.value.code == "invalid_provider_archive"


@pytest.mark.parametrize(
    "limits",
    [
        Limits(max_zip_bytes=100),
        Limits(max_zip_members=1),
        Limits(max_member_bytes=50),
        Limits(max_expanded_bytes=150),
    ],
)
def test_rejects_archive_size_and_count_limits(tmp_path, limits):
    archive = _zip(tmp_path / "result.zip", [("one", b"a" * 100), ("two", b"b" * 100)])
    with pytest.raises(PipelineError) as error, open_validated_archive(archive, limits):
        pass
    assert error.value.code == "provider_archive_limit_exceeded"


def test_rejects_corrupt_crc_and_encryption(tmp_path):
    archive_path = tmp_path / "corrupt.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("member", b"plain bytes to corrupt")
    original = archive_path.read_bytes()
    archive_path.write_bytes(original.replace(b"plain bytes to corrupt", b"Plain bytes to corrupt"))
    with pytest.raises(PipelineError) as error, open_validated_archive(archive_path, Limits()):
        pass
    assert error.value.code == "invalid_provider_archive"

    encrypted = bytearray(original)
    struct.pack_into("<H", encrypted, encrypted.index(b"PK\x03\x04") + 6, 1)
    struct.pack_into("<H", encrypted, encrypted.index(b"PK\x01\x02") + 8, 1)
    archive_path.write_bytes(encrypted)
    with pytest.raises(PipelineError) as error, open_validated_archive(archive_path, Limits()):
        pass
    assert error.value.code == "invalid_provider_archive"


def test_rejects_deflate_stream_that_hides_output_beyond_a_crc_valid_prefix(tmp_path):
    path = _zip(tmp_path / "result.zip", [("data", b"a" * 1_500_000)])
    with open_validated_archive(path, Limits()) as archive:
        assert archive.read("data") == b"a" * 1_500_000
    forged = bytearray(path.read_bytes())
    local_header = forged.index(b"PK\x03\x04")
    central_header = forged.index(b"PK\x01\x02")
    prefix = b"a" * 64
    for crc_offset, size_offset in [
        (local_header + 14, local_header + 22),
        (central_header + 16, central_header + 24),
    ]:
        struct.pack_into("<I", forged, crc_offset, zlib.crc32(prefix))
        struct.pack_into("<I", forged, size_offset, len(prefix))
    path.write_bytes(forged)
    with ZipFile(path) as archive:
        assert archive.read("data") == prefix
    with pytest.raises(PipelineError) as error, open_validated_archive(path, Limits(max_member_bytes=100)):
        pass
    assert error.value.code == "invalid_provider_archive"
