# Keybindings

QQcode ships with a default key for every interactive action, and you can
change any of them from a file instead of editing code.

## Where keybindings load from

Overrides merge in this order, with later sources winning:

1. built-in defaults (code),
2. `~/.qqcode/keybindings.toml` (global),
3. `.qqcode/keybindings.toml` in the project (only when the project is trusted),
4. the `[keybindings.bindings]` table in `config.toml`.

Set `QQCODE_KEYBINDINGS` or `keybindings.path` in `config.toml` to use a single
file instead of the default locations.

```toml
[keybindings]
enabled = true
# path = "/home/me/work.keys.toml"

[keybindings.bindings]
toggle_tool = "f4"
cycle_model = ["ctrl+p", "f3"]
```

The file itself uses a flat `[bindings]` table:

```toml
[bindings]
toggle_tool = "f4"
cycle_model = ["ctrl+p", "f3"]
```

## Actions

Run `/hotkeys` in the TUI to see every action, its current keys, its
description, and whether it is customized. `/hotkeys edit` creates the file
from a template when missing and prints its path.

| Action | Default | Description |
| --- | --- | --- |
| `interrupt` | `Esc` | Interrupt the agent or close dialogs |
| `force_quit` | `Ctrl+C` | Quit QQcode |
| `toggle_tool` | `Ctrl+O` | Toggle tool output |
| `cycle_model` | `Ctrl+P` | Cycle to the next model in scope |
| `cycle_model_previous` | `Shift+Ctrl+P` | Cycle to the previous model in scope |
| `cycle_thinking` | `Ctrl+Y` | Cycle the thinking level |
| `cycle_mode` | `Shift+Tab` | Toggle auto-approve mode |

`interrupt` and `force_quit` are mandatory: QQcode keeps their default keys so
dialogs and the app can always be closed.

## Conflicts and invalid keys

A key can only trigger one action. When two actions claim the same key, an
explicit override wins over a default, and the later action in the table above
wins between two overrides. QQcode keeps running, drops the losing binding,
and lists every warning in `/hotkeys` and on startup.

Keys are validated with Textual's key names (`f1`–`f24`, `escape`, `tab`,
`space`, `pageup`, ...) plus single characters with optional `ctrl`, `alt`,
`shift`, `meta`, `super`, and `hyper` modifiers. An invalid key keeps the
default for that action and produces a warning.

## Hot reload

While QQcode is running, the keybindings file is watched when it exists.
Saving a change rebuilds the bindings and posts a "Keybindings reloaded."
message in the conversation; the session and focus are untouched. Set
`keybindings.enabled = false` to ignore keybinding files entirely.
