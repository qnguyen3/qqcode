from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import tomllib
from typing import TYPE_CHECKING, Any

from textual.binding import Binding, BindingType
from textual.keys import Keys

from vibe.core.config import get_vibe_home

if TYPE_CHECKING:
    from vibe.core.config import VibeConfig

KEYBINDINGS_ENV_VAR = "QQCODE_KEYBINDINGS"
KEYBINDINGS_FILE_NAME = "keybindings.toml"
PROJECT_CONFIG_DIR = ".qqcode"

KEYBINDINGS_TEMPLATE = """\
# QQcode keybindings: change the keys below, then restart QQcode or use
# /hotkeys to review them. Run /hotkeys in the TUI for the action list.

[bindings]
# toggle_tool = "ctrl+o"
# cycle_model = ["ctrl+p", "f3"]
# cycle_thinking = "ctrl+y"
"""

_MODIFIERS = frozenset({"ctrl", "alt", "shift", "meta", "super", "hyper"})
_NAMED_KEYS = frozenset(
    key.value for key in Keys if "+" not in key.value and not key.value.startswith("<")
)
_KEY_DISPLAY_NAMES = {
    "escape": "Esc",
    "esc": "Esc",
    "enter": "Enter",
    "tab": "Tab",
    "space": "Space",
    "backspace": "Backspace",
    "delete": "Delete",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "pageup": "PageUp",
    "pagedown": "PageDown",
}


def format_key_display(key: str) -> str:
    """Render a key for humans, e.g. ``shift+ctrl+p`` -> ``Shift+Ctrl+P``."""

    def render(part: str) -> str:
        match part:
            case _ if part in _KEY_DISPLAY_NAMES:
                return _KEY_DISPLAY_NAMES[part]
            case _ if len(part) == 1:
                return part.upper()
            case _:
                return part.capitalize()

    return "+".join(render(part) for part in key.split("+"))


class KeybindingsFileError(ValueError):
    """Raised when a keybindings file cannot be parsed."""


@dataclass(frozen=True)
class KeyAction:
    """A named TUI action with its default keys and display metadata."""

    name: str
    description: str
    default: tuple[str, ...]
    priority: bool = False
    mandatory: bool = False


KEY_ACTIONS: tuple[KeyAction, ...] = (
    KeyAction(
        "interrupt",
        "Interrupt the agent or close dialogs",
        ("escape",),
        priority=True,
        mandatory=True,
    ),
    KeyAction("force_quit", "Quit QQcode", ("ctrl+c",), mandatory=True),
    KeyAction("toggle_tool", "Toggle tool output", ("ctrl+o",)),
    KeyAction("cycle_model", "Cycle to the next model in scope", ("ctrl+p",)),
    KeyAction(
        "cycle_model_previous",
        "Cycle to the previous model in scope",
        ("shift+ctrl+p",),
    ),
    KeyAction("cycle_thinking", "Cycle the thinking level", ("ctrl+y",)),
    KeyAction("cycle_mode", "Toggle auto-approve mode", ("shift+tab",), priority=True),
)

ACTION_NAMES = frozenset(action.name for action in KEY_ACTIONS)

DEFAULT_BINDINGS: dict[str, tuple[str, ...]] = {
    action.name: action.default for action in KEY_ACTIONS
}


def normalize_key(key: str) -> str | None:
    """Return the canonical ``modifier+key`` form, or ``None`` when invalid."""
    raw = "".join(key.split()).lower()
    if not raw:
        return None

    *modifiers, base = raw.split("+")
    if not base or len(set(modifiers)) != len(modifiers):
        return None
    if any(modifier not in _MODIFIERS for modifier in modifiers):
        return None
    if len(base) == 1 or base in _NAMED_KEYS:
        return "+".join([*modifiers, base])
    return None


def global_keybindings_path() -> Path:
    return get_vibe_home() / KEYBINDINGS_FILE_NAME


def project_keybindings_path(config: VibeConfig) -> Path:
    return config.effective_workdir / PROJECT_CONFIG_DIR / KEYBINDINGS_FILE_NAME


def keybindings_file_path(config: VibeConfig) -> Path:
    """File shown by ``/hotkeys edit`` and watched for hot reload."""
    if override := os.environ.get(KEYBINDINGS_ENV_VAR):
        return Path(override).expanduser()
    if config.keybindings.path is not None:
        return Path(config.keybindings.path).expanduser()
    return global_keybindings_path()


def keybinding_files(config: VibeConfig) -> list[Path]:
    """Files merged in increasing priority: global, then project."""
    if override := os.environ.get(KEYBINDINGS_ENV_VAR):
        return [Path(override).expanduser()]
    if config.keybindings.path is not None:
        return [Path(config.keybindings.path).expanduser()]

    files = [global_keybindings_path()]
    if config.project_trusted:
        files.append(project_keybindings_path(config))
    return files


def ensure_keybindings_file(config: VibeConfig) -> Path:
    """Create the keybindings file from a template when it does not exist."""
    path = keybindings_file_path(config)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(KEYBINDINGS_TEMPLATE, encoding="utf-8")
    return path


def _coerce_keys(value: Any) -> list[str]:
    match value:
        case str() as single:
            return [single]
        case [*items] if all(isinstance(item, str) for item in items):
            return [str(item) for item in items]
        case _:
            return []


def _read_bindings_table(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise KeybindingsFileError(f"could not read {path}: {exc}") from exc

    table = data.get("bindings", data)
    if not isinstance(table, dict):
        raise KeybindingsFileError(f"{path}: expected a [bindings] table")
    return table


def load_overrides(config: VibeConfig) -> tuple[dict[str, list[str]], list[str]]:
    """Merge keybinding overrides and return them with human-readable warnings.

    Later sources win: defaults (code) < global file < project file < the
    ``[keybindings]`` table in ``config.toml``.
    """
    overrides: dict[str, list[str]] = {}
    warnings: list[str] = []
    if not config.keybindings.enabled:
        return overrides, warnings

    for path in keybinding_files(config):
        try:
            table = _read_bindings_table(path)
        except KeybindingsFileError as exc:
            warnings.append(str(exc))
            continue
        for action, value in table.items():
            if action not in ACTION_NAMES:
                warnings.append(f"{path.name}: unknown action '{action}'")
                continue
            overrides[action] = _coerce_keys(value)

    for action, value in config.keybindings.bindings.items():
        if action not in ACTION_NAMES:
            warnings.append(f"config.toml: unknown action '{action}'")
            continue
        overrides[action] = _coerce_keys(value)
    return overrides, warnings


def _validated_keys(
    action: KeyAction, override: list[str]
) -> tuple[tuple[str, ...], list[str]]:
    invalid = [raw for raw in override if normalize_key(raw) is None]
    if invalid:
        return action.default, invalid
    return tuple(normalize_key(raw) or raw for raw in override), []


def describe_actions(effective: dict[str, list[str]]) -> list[str]:
    """Human-readable key/description lines for the help screen."""
    return [
        f"- `{format_key_display(key)}` {action.description}"
        for action in KEY_ACTIONS
        for key in effective.get(action.name, list(action.default))
    ]


DEFAULT_HELP_LINES = describe_actions({})


def _resolve_action_keys(
    overrides: dict[str, list[str]],
) -> tuple[list[tuple[KeyAction, tuple[str, ...], bool]], list[str]]:
    """Resolve the keys each action uses and whether they are overridden."""
    effective: list[tuple[KeyAction, tuple[str, ...], bool]] = []
    warnings: list[str] = []

    for action in KEY_ACTIONS:
        override = overrides.get(action.name)
        if override is None:
            effective.append((action, action.default, False))
            continue

        if action.mandatory:
            warnings.append(
                f"'{action.name}' keeps its default key ({', '.join(action.default)})"
            )
            effective.append((action, action.default, False))
            continue

        if not override:
            warnings.append(f"'{action.name}' has no keys configured; using default")
            effective.append((action, action.default, False))
            continue

        keys, invalid = _validated_keys(action, override)
        if invalid:
            warnings.append(
                f"Ignored invalid key(s) {', '.join(invalid)} for "
                f"'{action.name}'; using default"
            )
            effective.append((action, action.default, False))
            continue

        effective.append((action, keys, True))

    return effective, warnings


def build_bindings(
    overrides: dict[str, list[str]],
) -> tuple[Sequence[BindingType], list[str]]:
    """Build app bindings from the action registry plus validated overrides.

    A key claimed by several actions belongs to the explicit override when
    there is one, otherwise to the later action in the registry. Losers are
    dropped with a warning. Invalid keys or overrides of mandatory actions
    keep the default.
    """
    effective, warnings = _resolve_action_keys(overrides)

    claimed: dict[str, tuple[str, bool]] = {}
    for action, keys, is_override in effective:
        for key in keys:
            owner, owner_override = claimed.get(key, (None, False))
            if owner is None or is_override or not owner_override:
                claimed[key] = (action.name, is_override)

    bindings: list[Binding] = []
    for action, keys, _ in effective:
        seen: set[str] = set()
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            owner, _ = claimed[key]
            if owner != action.name:
                warnings.append(
                    f"'{key}' now belongs to '{owner}', not '{action.name}'"
                )
                continue
            bindings.append(
                Binding(
                    key,
                    action.name,
                    action.description,
                    show=False,
                    priority=action.priority,
                )
            )

    return bindings, warnings
