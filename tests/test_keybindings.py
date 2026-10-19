from __future__ import annotations

from pathlib import Path

import pytest

from vibe.cli.keybindings import (
    KEY_ACTIONS,
    build_bindings,
    ensure_keybindings_file,
    format_key_display,
    load_overrides,
    normalize_key,
)
from vibe.cli.textual_ui.app import VibeApp
from vibe.core.config import KeybindingsConfig, SessionLoggingConfig, VibeConfig
from vibe.core.trust import TrustDecision

DEFAULTS = {action.name: list(action.default) for action in KEY_ACTIONS}


@pytest.fixture(autouse=True)
def isolated_keybindings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QQCODE_KEYBINDINGS", raising=False)
    monkeypatch.setattr("vibe.cli.keybindings.get_vibe_home", lambda: tmp_path / "home")


def make_config(tmp_path: Path, **keybindings: object) -> VibeConfig:
    return VibeConfig(
        workdir=tmp_path,
        session_logging=SessionLoggingConfig(enabled=False),
        enable_update_checks=False,
        keybindings=KeybindingsConfig.model_validate(keybindings),
    )


def write_bindings(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def binding_keys(bindings) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    for binding in bindings:
        keys.setdefault(binding.action, []).append(binding.key)
    return keys


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ctrl+o", "ctrl+o"),
        ("Ctrl+O", "ctrl+o"),
        ("ctrl + p", "ctrl+p"),
        ("shift+ctrl+p", "shift+ctrl+p"),
        ("f3", "f3"),
        ("escape", "escape"),
        ("", None),
        ("ctrl+", None),
        ("ctrlo", None),
        ("ctrl+ctrl+o", None),
        ("ctrl+not a key", None),
    ],
)
def test_normalize_key(raw: str, expected: str | None) -> None:
    assert normalize_key(raw) == expected


def test_format_key_display() -> None:
    assert format_key_display("shift+ctrl+p") == "Shift+Ctrl+P"
    assert format_key_display("escape") == "Esc"
    assert format_key_display("f3") == "F3"


def test_config_without_keybindings_keeps_defaults(tmp_path: Path) -> None:
    config = VibeConfig(
        workdir=tmp_path, session_logging=SessionLoggingConfig(enabled=False)
    )

    overrides, warnings = load_overrides(config)
    bindings, binding_warnings = build_bindings(overrides)

    assert overrides == {}
    assert warnings == []
    assert binding_warnings == []
    assert binding_keys(bindings) == DEFAULTS


def test_files_merge_with_increasing_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "vibe.cli.keybindings.get_vibe_home", lambda: tmp_path / "global"
    )
    write_bindings(
        tmp_path / "global" / "keybindings.toml",
        '[bindings]\ntoggle_tool = "f4"\ncycle_model = "f5"\n',
    )
    project = tmp_path / "project"
    write_bindings(
        project / ".qqcode" / "keybindings.toml", '[bindings]\ncycle_model = "f6"\n'
    )

    config = make_config(project, bindings={"cycle_thinking": "f7"})
    overrides, warnings = load_overrides(config)

    assert warnings == []
    assert overrides["toggle_tool"] == ["f4"]
    assert overrides["cycle_model"] == ["f6"]
    assert overrides["cycle_thinking"] == ["f7"]


def test_project_file_needs_trust(tmp_path: Path) -> None:
    write_bindings(
        tmp_path / ".qqcode" / "keybindings.toml", '[bindings]\ntoggle_tool = "f4"\n'
    )
    config = make_config(tmp_path)
    config.project_trust = TrustDecision.UNTRUSTED

    assert "toggle_tool" not in load_overrides(config)[0]


def test_invalid_key_keeps_default_and_warns(tmp_path: Path) -> None:
    config = make_config(tmp_path, bindings={"toggle_tool": "ctrl+"})

    overrides, _ = load_overrides(config)
    bindings, warnings = build_bindings(overrides)

    assert binding_keys(bindings)["toggle_tool"] == ["ctrl+o"]
    assert any("invalid key" in warning.lower() for warning in warnings)


def test_conflicting_key_belongs_to_the_later_action(tmp_path: Path) -> None:
    config = make_config(tmp_path, bindings={"toggle_tool": "ctrl+p"})

    overrides, _ = load_overrides(config)
    bindings, warnings = build_bindings(overrides)
    keys = binding_keys(bindings)

    assert keys["toggle_tool"] == ["ctrl+p"]
    assert "cycle_model" not in keys
    assert any("belongs to" in warning for warning in warnings)


def test_mandatory_actions_keep_their_default(tmp_path: Path) -> None:
    config = make_config(tmp_path, bindings={"force_quit": "f12"})

    overrides, _ = load_overrides(config)
    bindings, warnings = build_bindings(overrides)

    assert binding_keys(bindings)["force_quit"] == ["ctrl+c"]
    assert any("keeps its default" in warning for warning in warnings)


def test_unknown_action_and_broken_file_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "keybindings.toml"
    broken.write_text("not = [valid", encoding="utf-8")
    monkeypatch.setenv("QQCODE_KEYBINDINGS", str(broken))
    config = make_config(tmp_path, bindings={"not_an_action": "f1"})

    overrides, warnings = load_overrides(config)

    assert overrides == {}
    assert any("unknown action 'not_an_action'" in warning for warning in warnings)
    assert any("could not read" in warning for warning in warnings)


def test_disabled_keybindings_ignore_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "keybindings.toml"
    write_bindings(path, '[bindings]\ntoggle_tool = "f4"\n')
    monkeypatch.setenv("QQCODE_KEYBINDINGS", str(path))
    config = make_config(tmp_path, enabled=False, bindings={"cycle_model": "f5"})

    overrides, warnings = load_overrides(config)

    assert overrides == {}
    assert warnings == []


def test_ensure_keybindings_file_creates_template(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    config = make_config(tmp_path, path=path)

    created = ensure_keybindings_file(config)

    assert created == path
    assert "[bindings]" in path.read_text(encoding="utf-8")
    assert ensure_keybindings_file(config) == path


def test_app_applies_overrides_and_reports_them(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    write_bindings(path, '[bindings]\ntoggle_tool = "f4"\n')
    config = make_config(tmp_path, path=path)

    app = VibeApp(config=config)
    bindings = app._effective_keybinding_keys()

    assert bindings["toggle_tool"] == ["f4"]
    assert "f4" in app._bindings.key_to_bindings
    assert "(custom)" in app._hotkeys_text()


def test_hotkeys_command_is_registered(tmp_path: Path) -> None:
    app = VibeApp(config=make_config(tmp_path))

    command = app.commands.find_command("/hotkeys")

    assert command is not None
    assert command.handler == "_show_hotkeys"
    assert app.commands.find_command("/keys") is command


@pytest.mark.asyncio
async def test_reload_applies_edited_file(tmp_path: Path) -> None:
    path = tmp_path / "keybindings.toml"
    write_bindings(path, '[bindings]\ntoggle_tool = "f4"\n')
    app = VibeApp(config=make_config(tmp_path, path=path))

    async with app.run_test():
        assert app._effective_keybinding_keys()["toggle_tool"] == ["f4"]

        write_bindings(path, '[bindings]\ntoggle_tool = "f5"\n')
        await app._reload_keybindings()

        assert app._effective_keybinding_keys()["toggle_tool"] == ["f5"]
