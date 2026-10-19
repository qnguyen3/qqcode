from __future__ import annotations

import base64
from functools import lru_cache
import importlib
import importlib.util
from io import BytesIO
from pathlib import Path
from typing import Any

from vibe.core.types import ImagePart, LLMMessage

IMAGE_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 1568


class ImageError(ValueError):
    """Raised when an image cannot be read, decoded or attached."""


def media_type_for(path: Path) -> str | None:
    """Return the media type for a supported image path, else ``None``."""
    return IMAGE_EXTENSIONS.get(path.suffix.lower())


def is_image_path(path: Path) -> bool:
    return media_type_for(path) is not None


def pillow_available() -> bool:
    """Whether Pillow is installed for resizing and dimension probing."""
    return importlib.util.find_spec("PIL") is not None


def read_image_bytes(path: Path) -> tuple[bytes, str]:
    """Read image bytes, resizing oversized images when Pillow is available.

    Raises:
        ImageError: If the file is too large, unreadable or not a supported
            image.
    """
    media_type = media_type_for(path)
    if media_type is None:
        raise ImageError(f"Unsupported image type: {path.suffix or '(none)'}")

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImageError(f"Could not read image {path}: {exc}") from exc

    if len(data) <= MAX_IMAGE_BYTES:
        return data, media_type

    resized = _resize_with_pillow(data)
    if resized is None:
        raise ImageError(
            f"Image {path} is larger than {MAX_IMAGE_BYTES} bytes; "
            "install Pillow to resize oversized images automatically"
        )
    return resized


def _pillow_image_module() -> Any | None:
    """Import ``PIL.Image`` lazily; Pillow stays an optional dependency."""
    try:
        return importlib.import_module("PIL.Image")
    except ImportError:
        return None


def _resize_with_pillow(data: bytes) -> tuple[bytes, str] | None:
    """Downscale an image to ``MAX_IMAGE_DIMENSION`` when Pillow is installed."""
    image_module = _pillow_image_module()
    if image_module is None:
        return None

    with image_module.open(BytesIO(data)) as image:
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.mode else "RGB")
        width, height = image.size
        scale = min(1.0, MAX_IMAGE_DIMENSION / max(width, height))
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        buffer = BytesIO()
        image.resize(new_size).save(buffer, format="PNG")
        return buffer.getvalue(), "image/png"


def describe_image(path: Path) -> ImagePart:
    """Build a reference-only image part for a file on disk.

    Raises:
        ImageError: If the file is not a supported image or cannot be read.
    """
    media_type = media_type_for(path)
    if media_type is None:
        raise ImageError(f"Unsupported image type: {path.suffix or '(none)'}")

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImageError(f"Could not stat image {path}: {exc}") from exc

    width, height = _pillow_dimensions(path) or (None, None)
    return ImagePart(
        media_type=media_type,
        source_path=str(path),
        size_bytes=size,
        width=width,
        height=height,
    )


def _pillow_dimensions(path: Path) -> tuple[int, int] | None:
    image_module = _pillow_image_module()
    if image_module is None:
        return None

    try:
        with image_module.open(path) as image:
            return image.size
    except Exception:
        return None


@lru_cache(maxsize=32)
def _load_image_data(
    path: str, mtime_ns: int, size: int
) -> tuple[str, str, int | None, int | None]:
    resolved = Path(path)
    data, media_type = read_image_bytes(resolved)
    width, height = _pillow_dimensions(resolved) or (None, None)
    return base64.b64encode(data).decode("ascii"), media_type, width, height


def load_image_part(part: ImagePart) -> ImagePart:
    """Return a copy of ``part`` with base64 data loaded from its file.

    Raises:
        ImageError: If the part has no data and its file cannot be read.
    """
    if part.is_loaded:
        return part
    if not part.source_path:
        raise ImageError("Image part has neither data nor a source path")

    path = Path(part.source_path)
    try:
        stat = path.stat()
    except OSError as exc:
        raise ImageError(f"Could not read image {path}: {exc}") from exc

    data, media_type, width, height = _load_image_data(
        str(path), stat.st_mtime_ns, stat.st_size
    )
    return part.model_copy(
        update={
            "data": data,
            "media_type": media_type,
            "size_bytes": stat.st_size,
            "width": width,
            "height": height,
        }
    )


def resolve_messages(messages: list[LLMMessage], *, vision: bool) -> list[LLMMessage]:
    """Resolve or downgrade images in ``messages`` for a request.

    With ``vision`` enabled, referenced files are loaded into base64 parts.
    Without vision, every image becomes a text placeholder so text-only models
    keep working. Missing or unreadable files degrade to a placeholder too.
    """
    if not any(message.images for message in messages):
        return messages

    resolved: list[LLMMessage] = []
    for message in messages:
        resolved.append(_resolve_message(message, vision=vision))
    return resolved


def _resolve_message(message: LLMMessage, *, vision: bool) -> LLMMessage:
    if not message.images:
        return message

    placeholders: list[str] = []
    images: list[ImagePart] = []
    for part in message.images:
        if not vision:
            placeholders.append(part.describe())
            continue
        try:
            images.append(load_image_part(part))
        except ImageError as exc:
            placeholders.append(f"[image unavailable: {exc}]")

    content = message.content
    if placeholders:
        note = "\n\n".join(placeholders)
        content = f"{content}\n\n{note}" if content else note
    return message.model_copy(update={"content": content, "images": images})
