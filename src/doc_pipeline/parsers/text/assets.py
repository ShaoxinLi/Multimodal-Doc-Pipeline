from __future__ import annotations

import os
from hashlib import sha256
from html import escape
from io import BytesIO
from pathlib import Path, PurePosixPath
from stat import S_ISREG
from urllib.parse import unquote, urlsplit

from PIL import Image as RasterImage

from doc_pipeline.contracts.core import FileRef
from doc_pipeline.contracts.document import AssetResolution, Image, TextAsset
from doc_pipeline.services.io import write_bytes

_RASTER_EXTENSIONS = {
    "PNG": "png",
    "JPEG": "jpg",
    "GIF": "gif",
    "WEBP": "webp",
    "BMP": "bmp",
    "TIFF": "tiff",
    "ICO": "ico",
}


def asset_html(asset: TextAsset) -> str:
    if asset.file:
        return f'<img src="{escape(asset.file.path, quote=True)}" alt="{escape(asset.alt, quote=True)}">'
    label = f"{asset.alt} [{asset.reference}]" if asset.alt else asset.reference
    return f"<code>{escape(label)}</code>"


def safe_link(reference: str) -> bool:
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return False
    return parsed.scheme in {"", "http", "https", "mailto"} and not any(
        ord(char) < 32 or char == "\\" for char in reference
    )


class AssetResolver:
    def __init__(self, snapshots: dict[str, Path], output_dir: Path, *,
                 source_dir: Path, max_asset_bytes: int, output_root: Path | None = None):
        self.snapshots = snapshots
        self.output_dir = output_dir
        self.source_dir = source_dir.resolve()
        self.output_root = (output_root or output_dir).resolve()
        self.max_asset_bytes = max_asset_bytes
        self.diagnostics: list[str] = []
        self._resolved: dict[str, tuple[AssetResolution, FileRef | None]] = {}

    def __call__(self, reference: str, alt: str) -> TextAsset:
        if reference not in self._resolved:
            self._resolved[reference] = self._resolve(reference)
            status, _ = self._resolved[reference]
            if status != "archived":
                self.diagnostics.append(f"{status}: {reference}")
        status, file = self._resolved[reference]
        return TextAsset(reference, alt, status, file)

    def _resolve(self, reference: str) -> tuple[AssetResolution, FileRef | None]:
        try:
            parsed = urlsplit(reference)
            key = unquote(parsed.path)
        except ValueError:
            return "blocked_reference", None
        if not reference or any(ord(char) < 32 or char == "\\" for char in reference + key):
            return "blocked_reference", None
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            key = reference
            missing = "external_unarchived"
        elif parsed.scheme or parsed.netloc or not key or key.startswith("/") or ":" in key:
            return "blocked_reference", None
        else:
            path = PurePosixPath(key)
            if ".." in path.parts or not path.parts:
                return "blocked_reference", None
            key = path.as_posix()
            missing = "missing_snapshot"
        snapshot = self.snapshots.get(key)
        if snapshot is None and missing == "external_unarchived":
            return missing, None
        try:
            if snapshot is None:
                candidate = self.source_dir / key
                if self.output_root != self.source_dir and candidate.is_relative_to(self.output_root):
                    return "blocked_reference", None
                try:
                    snapshot = candidate.resolve(strict=True)
                except FileNotFoundError:
                    return missing, None
                if not snapshot.is_relative_to(self.source_dir):
                    return "blocked_reference", None
                if self.output_root != self.source_dir and snapshot.is_relative_to(self.output_root):
                    return "blocked_reference", None
            else:
                snapshot = snapshot.resolve(strict=True)
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(snapshot, flags)
            try:
                info = os.fstat(descriptor)
                if not S_ISREG(info.st_mode):
                    return "blocked_reference", None
                if info.st_size > self.max_asset_bytes:
                    self.diagnostics.append(f"asset_too_large: {reference}")
                    return missing, None
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    data = stream.read(self.max_asset_bytes + 1)
            finally:
                os.close(descriptor)
            if len(data) > self.max_asset_bytes:
                self.diagnostics.append(f"asset_too_large: {reference}")
                return missing, None
            with RasterImage.open(BytesIO(data), formats=list(_RASTER_EXTENSIONS)) as raster:
                extension = _RASTER_EXTENSIONS[raster.format]
                raster.verify()
        except (OSError, ValueError, RuntimeError, RasterImage.DecompressionBombError):
            self.diagnostics.append(f"invalid_image_snapshot: {reference}")
            return missing, None
        digest = sha256(data).hexdigest()
        target = self.output_dir / "assets" / f"{digest}.{extension}"
        if not target.exists():
            write_bytes(target, data)
        return "archived", FileRef(path=target.relative_to(self.output_dir).as_posix(), sha256=digest)


def image_payload(asset: TextAsset, captions: list[str] | None = None) -> Image:
    return Image(
        asset=asset.file,
        original_reference=asset.reference,
        alt=asset.alt,
        captions=captions or [],
        resolution=asset.resolution,
    )
