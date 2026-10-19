# Resource packages

Resource packages distribute tools, skills, prompts, agents and hooks as a
single installable unit. Packages are installed from a local directory or a git
URL into `~/.qqcode/packages/<name>` and recorded in the config `packages`
list.

## Manifest

Every package root must contain a `qqcode.toml` manifest:

```toml
[package]
name = "my-qqcode-pack"
version = "0.1.0"

[resources]
tools = ["tools/"]
skills = ["skills/"]
prompts = ["prompts/commands"]
agents = ["agents/"]
hooks = ["hooks/"]
```

- `[package].name` and `[package].version` are required.
- `[resources]` is optional; each entry is a list of paths relative to the
  package root. Missing paths are skipped with a warning.
- `skills/` should contain one subdirectory per skill with a `SKILL.md`.
- `prompts/` entries point at a directory of `*.md` prompt templates (for
  example a `prompts/commands` directory).
- `agents/` entries point at directories of `<name>.toml` agent configs.
- `hooks/` entries point at directories of hook `*.py` modules.

## Commands

```bash
# Local directory
qqcode install ./path/to/pack
qqcode install ./path/to/pack --name custom-name

# Git URL (cloned with --depth 1)
qqcode install https://github.com/example/qqcode-pack.git

qqcode list            # installed packages with version and source
qqcode update <name>   # git pull --ff-only (git installs only)
qqcode remove <name>   # delete the package and drop it from the config
```

`install` validates the manifest, copies or clones the package into
`~/.qqcode/packages/<name>`, and persists the name in `packages` in the config
file. `remove` deletes the directory and removes the name from the config list.

`qqcode packages <install|list|remove|update>` is accepted as an explicit
alias for the same commands.

## Resource discovery and priority

When an agent starts, loaders search for each resource kind in this order
(higher priority wins on duplicate names):

1. **Project** – `.qqcode/...` directories in the work directory or its parents
2. **Package** – resource directories declared by installed packages
3. **Global** – `~/.qqcode/...` (and `~/.agents/skills` for skills)

Duplicates are logged with a warning and the higher-priority definition is
kept. Built-in tools are the lowest priority.

| Resource | Project | Package | Global |
|---|---|---|---|
| tools | `.qqcode/tools/` | manifest `tools` | `~/.qqcode/tools/` |
| skills | `.qqcode/skills/`, `.agents/skills/` | manifest `skills` | `~/.qqcode/skills/`, `~/.agents/skills/` |
| prompts | `.qqcode/prompts/commands/` | manifest `prompts` | `~/.qqcode/prompts/commands/` |
| agents | `.qqcode/agents/` | manifest `agents` | `~/.qqcode/agents/` |
| hooks | `.qqcode/hooks/` | manifest `hooks` | `~/.qqcode/hooks/` |

## Example package layout

```
my-qqcode-pack/
├── qqcode.toml
├── tools/
│   └── deploy.py
├── skills/
│   └── release-checklist/
│       └── SKILL.md
├── prompts/commands/
│   └── release.md
├── agents/
│   └── reviewer.toml
└── hooks/
    └── audit.py
```

After `qqcode install ./my-qqcode-pack`, the tools/skills/prompts/agents/hooks
are discovered automatically on the next run. Removing the package removes its
search paths; there is no central registry in this phase.
