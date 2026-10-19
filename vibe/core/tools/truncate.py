from __future__ import annotations

import atexit
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Literal

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_CHARS = 500
BASH_MAX_LINES = 2000
BASH_MAX_BYTES = 50 * 1024

_TEMP_FILES: list[Path] = []
_ATEXIT_REGISTERED = False


@dataclass(frozen=True)
class TruncationResult:
    content: str
    truncated: bool
    truncated_by: Literal["lines", "bytes", "none"]
    total_lines: int | None
    first_line: int | None
    last_line: int | None
    full_output_path: Path | None = None


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _empty_result() -> TruncationResult:
    return TruncationResult(
        content="",
        truncated=False,
        truncated_by="none",
        total_lines=0,
        first_line=None,
        last_line=None,
    )


def truncate_head(text: str, *, max_lines: int, max_bytes: int) -> TruncationResult:
    """Keep the first lines of ``text`` within the line and byte limits.

    Never returns a partially cut line, except when a single line alone exceeds
    ``max_bytes``: then the first ``max_bytes`` bytes of that line are returned.
    """
    if not text:
        return _empty_result()

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    line_limit = min(total_lines, max_lines)
    kept: list[str] = []
    used_bytes = 0
    truncated_by: Literal["lines", "bytes", "none"] = "none"

    for line in lines[:line_limit]:
        size = _byte_len(line)
        if used_bytes + size > max_bytes:
            truncated_by = "bytes"
            break
        kept.append(line)
        used_bytes += size
    else:
        if total_lines > line_limit:
            truncated_by = "lines"

    if truncated_by == "bytes" and not kept:
        first = lines[0].encode("utf-8")[:max_bytes]
        return TruncationResult(
            content=first.decode("utf-8", errors="ignore"),
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            first_line=1,
            last_line=1,
        )

    if truncated_by == "none":
        return TruncationResult(
            content=text,
            truncated=False,
            truncated_by="none",
            total_lines=total_lines,
            first_line=None,
            last_line=None,
        )

    return TruncationResult(
        content="".join(kept),
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        first_line=1,
        last_line=len(kept),
    )


def truncate_tail(text: str, *, max_lines: int, max_bytes: int) -> TruncationResult:
    """Keep the last lines of ``text`` within the line and byte limits.

    Never returns a partially cut line, except when a single line alone exceeds
    ``max_bytes``: then the last ``max_bytes`` bytes of that line are returned.
    """
    if not text:
        return _empty_result()

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    line_limit = min(total_lines, max_lines)
    kept_reversed: list[str] = []
    used_bytes = 0
    truncated_by: Literal["lines", "bytes", "none"] = "none"

    for line in reversed(lines[-line_limit:]):
        size = _byte_len(line)
        if used_bytes + size > max_bytes:
            truncated_by = "bytes"
            break
        kept_reversed.append(line)
        used_bytes += size
    else:
        if total_lines > line_limit:
            truncated_by = "lines"

    if truncated_by == "bytes" and not kept_reversed:
        last = lines[-1].encode("utf-8")[-max_bytes:]
        return TruncationResult(
            content=last.decode("utf-8", errors="ignore"),
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            first_line=total_lines,
            last_line=total_lines,
        )

    if truncated_by == "none":
        return TruncationResult(
            content=text,
            truncated=False,
            truncated_by="none",
            total_lines=total_lines,
            first_line=None,
            last_line=None,
        )

    kept_count = len(kept_reversed)
    return TruncationResult(
        content="".join(reversed(kept_reversed)),
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        first_line=total_lines - kept_count + 1,
        last_line=total_lines,
    )


def _cleanup_temp_files() -> None:
    while _TEMP_FILES:
        path = _TEMP_FILES.pop()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def spill_to_temp_file(text: str, prefix: str = "qqcode-tool") -> Path:
    """Write full output under ``$TMPDIR`` and remove it when the process exits."""
    global _ATEXIT_REGISTERED

    fd, raw_path = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".log")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(text)
    except OSError:
        path.unlink(missing_ok=True)
        raise

    _TEMP_FILES.append(path)
    if not _ATEXIT_REGISTERED:
        atexit.register(_cleanup_temp_files)
        _ATEXIT_REGISTERED = True

    return path


def format_notice(result: TruncationResult) -> str:
    """Build a model-facing notice. Never cuts in the middle of a line."""
    if not result.truncated:
        return ""

    if (
        result.truncated_by == "bytes"
        and result.first_line is not None
        and result.first_line == result.last_line
    ):
        shown_bytes = _byte_len(result.content)
        message = f"Showing {shown_bytes} bytes of line {result.first_line}"
    elif result.first_line is not None and result.last_line is not None:
        message = (
            f"Showing lines {result.first_line}-{result.last_line}"
            f" of {result.total_lines}"
        )
    else:
        message = "Output truncated"

    if result.full_output_path is not None:
        message += f". Full output: {result.full_output_path}"

    return f"[{message}]"
