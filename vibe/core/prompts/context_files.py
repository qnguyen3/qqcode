from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

CONTEXT_FILENAMES: tuple[str, ...] = (
    "AGENTS.override.md",
    "AGENTS.md",
    "AGENTS.MD",
    "QQCODE.md",
    ".qqcode.md",
    "CLAUDE.md",
    "CLAUDE.MD",
)

TRUNCATION_NOTE = "\n... (truncated)"


@dataclass(frozen=True)
class ContextFile:
    """A context instruction file loaded for the system prompt."""

    path: Path
    content: str
    truncated: bool = False

    def render(self) -> str:
        return (
            f'<project_instructions path="{self.path}">\n'
            f"{self.content}\n"
            "</project_instructions>"
        )


def find_context_file(directory: Path) -> Path | None:
    for name in CONTEXT_FILENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _read_capped(path: Path, max_bytes: int) -> tuple[str, bool]:
    try:
        data = path.read_bytes()
    except OSError:
        return "", False
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="ignore"), False
    return data[:max_bytes].decode("utf-8", errors="ignore"), True


def _candidate_files(workdir: Path, agent_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    if (global_file := find_context_file(agent_dir.resolve())) is not None:
        candidates.append(global_file.resolve())

    for directory in (workdir.resolve(), *workdir.resolve().parents):
        if (path := find_context_file(directory)) is not None:
            candidates.append(path.resolve())

    return candidates


def _fit_total_budget(
    files: list[ContextFile], max_total_bytes: int
) -> list[ContextFile]:
    total = sum(len(file.content.encode("utf-8")) for file in files)
    if total <= max_total_bytes:
        return files

    result = list(files)
    index = len(result) - 1
    while index >= 0 and total > max_total_bytes:
        file = result[index]
        file_bytes = len(file.content.encode("utf-8"))
        remaining = max_total_bytes - (total - file_bytes)
        if remaining <= len(TRUNCATION_NOTE):
            total -= file_bytes
            result.pop(index)
        else:
            content = file.content[: remaining - len(TRUNCATION_NOTE)]
            result[index] = replace(
                file, content=content + TRUNCATION_NOTE, truncated=True
            )
            total = sum(len(item.content.encode("utf-8")) for item in result)
        index -= 1
    return result


def load_context_files(
    workdir: Path, agent_dir: Path, *, max_bytes_per_file: int, max_total_bytes: int
) -> list[ContextFile]:
    """Load context instruction files from the global dir and ancestors.

    Global ``agent_dir`` files come first, then each directory from ``workdir``
    up to the filesystem root contributes its first matching file. Files are
    deduplicated by content, capped per file, and finally capped by total size
    by trimming the farthest files first.
    """
    files: list[ContextFile] = []
    seen: set[str] = set()

    for path in _candidate_files(workdir, agent_dir):
        content, truncated = _read_capped(path, max_bytes_per_file)
        if truncated:
            content += TRUNCATION_NOTE
        if content in seen:
            continue
        seen.add(content)
        files.append(ContextFile(path=path, content=content, truncated=truncated))

    return _fit_total_budget(files, max_total_bytes)


def render_context_files(files: list[ContextFile]) -> str:
    return "\n\n".join(file.render() for file in files)
