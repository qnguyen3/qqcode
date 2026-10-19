from __future__ import annotations

from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
import re

from vibe.core.prompts.sections import SECTION_NAME_PATTERN

logger = getLogger("vibe")

FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
KEY_VALUE_PATTERN = re.compile(r"^([\w-]+):\s*(.+)$", re.MULTILINE)

_DEFAULT_PATTERN = re.compile(r"\$\{([1-9]|@):-([^}]*)\}")
_SLICE_PATTERN = re.compile(r"\$\{@:([0-9]+)(?::([0-9]+))?\}")
_POSITIONAL_PATTERN = re.compile(r"\$([1-9])")
_ALL_ARGS_PATTERN = re.compile(r"\$@|\$ARGUMENTS")


@dataclass(frozen=True)
class PromptTemplate:
    """A prompt template loaded from a markdown file."""

    name: str
    description: str
    body: str
    source: Path
    argument_hint: str = ""


def parse_frontmatter(content: str) -> dict[str, str]:
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}

    result: dict[str, str] = {}
    for key_match in KEY_VALUE_PATTERN.finditer(match.group(1)):
        key = key_match.group(1).strip()
        value = key_match.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        result[key] = value
    return result


def split_args(raw_args: str) -> list[str]:
    """Tokenize raw arguments with shell-like single/double quote handling."""
    args: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    has_content = False

    for char in raw_args:
        if quote is not None:
            if char == quote:
                quote = None
            else:
                buffer.append(char)
                has_content = True
        elif char in {"'", '"'}:
            quote = char
            has_content = True
        elif char.isspace():
            if has_content:
                args.append("".join(buffer))
                buffer.clear()
                has_content = False
        else:
            buffer.append(char)
            has_content = True

    if has_content:
        args.append("".join(buffer))
    return args


def expand(body: str, args: list[str]) -> str:
    """Expand ``$1..$9``, ``$@``/``$ARGUMENTS`` and ``${...}`` placeholders."""

    def positional(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return args[index] if 0 <= index < len(args) else ""

    def all_args(_match: re.Match[str]) -> str:
        return " ".join(args)

    def default(match: re.Match[str]) -> str:
        target, fallback = match.group(1), match.group(2)
        if target == "@":
            return " ".join(args) if args else fallback
        index = int(target) - 1
        if 0 <= index < len(args) and args[index]:
            return args[index]
        return fallback

    def sliced(match: re.Match[str]) -> str:
        start = max(int(match.group(1)) - 1, 0)
        length = match.group(2)
        if length is None:
            return " ".join(args[start:])
        return " ".join(args[start : start + int(length)])

    body = _DEFAULT_PATTERN.sub(default, body)
    body = _SLICE_PATTERN.sub(sliced, body)
    body = _POSITIONAL_PATTERN.sub(positional, body)
    return _ALL_ARGS_PATTERN.sub(all_args, body)


def render_template(template: PromptTemplate, raw_args: str) -> str:
    return expand(template.body, split_args(raw_args))


def load_templates(dirs: list[Path]) -> dict[str, PromptTemplate]:
    """Load ``*.md`` templates from ``dirs``; earlier directories win."""
    templates: dict[str, PromptTemplate] = {}

    for directory in dirs:
        if not directory.is_dir():
            continue

        for path in sorted(directory.glob("*.md")):
            name = path.stem
            if not SECTION_NAME_PATTERN.fullmatch(name):
                continue
            if name in templates:
                logger.warning(
                    "Duplicate prompt template '%s' in %s ignored; keeping %s",
                    name,
                    directory,
                    templates[name].source,
                )
                continue

            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue

            frontmatter = parse_frontmatter(content)
            templates[name] = PromptTemplate(
                name=name,
                description=frontmatter.get("description", "").strip(),
                body=FRONTMATTER_PATTERN.sub("", content, count=1),
                source=path,
                argument_hint=frontmatter.get("argument-hint", "").strip(),
            )

    return templates
