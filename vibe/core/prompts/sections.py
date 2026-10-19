from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import re

SECTION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

SECTION_ORDER: tuple[str, ...] = (
    "preamble",
    "commit_signature",
    "tool_prompts",
    "skills",
    "model_info",
    "docs_routing",
    "project_context",
    "project_files",
    "instructions",
    "mode",
    "os_env",
    "append",
)

_SECTION_RENDER_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class PromptSection:
    """A single named block of the system prompt.

    Attributes:
        name: Section identifier, matching ``^[a-z][a-z0-9_-]*$``.
        content: Raw text of the section.
        tagged: Wrap the content in ``<name>...</name>`` when rendering.
        cache_stable: Hint that the section belongs to the cacheable prefix.
    """

    name: str
    content: str
    tagged: bool = True
    cache_stable: bool = True

    def __post_init__(self) -> None:
        if not SECTION_NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                f"Invalid prompt section name {self.name!r}: "
                "expected ^[a-z][a-z0-9_-]*$"
            )

    def render(self) -> str:
        if not self.tagged:
            return self.content
        return f"<{self.name}>\n{self.content}\n</{self.name}>"


class PromptSections:
    """Ordered mapping of section names to their content.

    Sections are rendered in the canonical :data:`SECTION_ORDER`; unknown
    section names keep their insertion order and render after known ones.
    """

    def __init__(self, sections: Iterable[PromptSection] = ()) -> None:
        self._sections: dict[str, PromptSection] = {}
        for section in sections:
            self.set(section)

    def set(self, section: PromptSection) -> None:
        self._sections[section.name] = section

    def remove(self, name: str) -> None:
        self._sections.pop(name, None)

    def get(self, name: str) -> PromptSection | None:
        return self._sections.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._sections

    def __iter__(self) -> Iterator[PromptSection]:
        return iter(self._ordered())

    def __len__(self) -> int:
        return len(self._sections)

    def names(self) -> list[str]:
        return [section.name for section in self._ordered()]

    def render(self) -> str:
        return _SECTION_RENDER_SEPARATOR.join(
            rendered
            for section in self._ordered()
            if (rendered := section.render()).strip()
        )

    def diff(self, other: PromptSections) -> dict[str, str | None]:
        changes: dict[str, str | None] = {}
        for name in self._merged_names(other):
            current = self._sections.get(name)
            updated = other.get(name)
            current_content = current.content if current else None
            updated_content = updated.content if updated else None
            if current_content != updated_content:
                changes[name] = updated_content
        return changes

    def _ordered(self) -> list[PromptSection]:
        positions = {name: index for index, name in enumerate(SECTION_ORDER)}
        return sorted(
            self._sections.values(),
            key=lambda section: positions.get(section.name, len(SECTION_ORDER)),
        )

    def _merged_names(self, other: PromptSections) -> list[str]:
        names = self.names()
        names.extend(name for name in other.names() if name not in self._sections)
        return names
