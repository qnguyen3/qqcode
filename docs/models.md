# Built-in models

qqcode ships a list of models. The list is generated from upstream catalogs
and reviewed in pull requests, so qqcode does not fetch models at runtime.

## Files

| File | Role |
| --- | --- |
| `vibe/core/data/models.curated.toml` | Hand-edited source: the models qqcode ships, their aliases, and any overrides. |
| `vibe/core/data/models.generated.json` | Generated snapshot that qqcode loads at startup. Do not edit it by hand. |
| `vibe/core/config.py` (`DEFAULT_PROVIDERS`) | Provider base URLs, API key variables and API styles. Hardcoded. |
| `~/.qqcode/models.toml`, `.qqcode/models.toml` | Local overlay. Entries are merged by alias and override the built-in list. |

## Updating the catalog

```bash
uv run python scripts/generate_models.py --dry-run   # report only
uv run python scripts/generate_models.py             # rewrite the snapshot
uv run qqcode models validate                        # check the snapshot
```

The generator needs network access and no API keys. It reads three public
catalogs: models.dev, OpenRouter and the Vercel AI Gateway. It stops on the
first failed download.

A weekly workflow (`.github/workflows/update-models.yml`) runs the generator
and opens a pull request when the snapshot changes.

## Curated entries

Each `[[models]]` entry names a provider, the provider's model id (`name`) and
the user-facing `alias`. Aliases must be unique, and existing aliases must not
change because `active_model` in user configs refers to them.

- Providers with an upstream catalog take context window, pricing and
  reasoning metadata from it. Fields written in the entry override it.
- An entry with `manual = true` has no upstream source. It must set
  `context_window` and `pricing` itself. Missing values are never defaulted.
- Upstream models that are not listed are reported as candidates. Add one to
  the curated file to ship it.

## Catalog rules

A `[[catalog]]` entry builds every active, tool-capable model that models.dev
lists for its provider. `exclude` drops model names that must not be generated
for it:

```toml
[[catalog]]
provider = "opencode"
exclude = ["gpt-5.3-codex-spark"]
```

- Each generated model gets the alias `provider/name`.
- Its `api_style` comes from the AI SDK package models.dev names:
  `@ai-sdk/anthropic` gives `anthropic`, `@ai-sdk/openai` gives
  `openai-responses`, `@ai-sdk/google` gives `google-generative-ai`. Any other
  package uses the provider's default.
- Deprecated models are skipped. A rule that finds no active model fails the
  run, so a provider cannot empty out silently.
- A `[[models]]` entry with the same provider and name replaces the generated
  entry, so curated overrides still apply.
- Providers with a rule are not reported as candidates.

## Thinking levels

A model accepts the levels in `thinking_levels`. A request for an unsupported
level is clamped to the nearest level the model accepts. OFF is clamped up when
the model cannot disable thinking. `thinking_efforts` maps a level to a provider
value when the value differs from the level name. Off sends no effort parameter
unless it is mapped; the generator maps it to `none` when the upstream accepts
it.

Effort tables are generated only for API styles that send `reasoning_effort`
(`openai`, `openai-responses`, `openai-codex-responses`, `pi-messages`).
Anthropic, Google and Bedrock use token budgets, so they get no table.

## Validation

`qqcode models validate` checks the aliases and providers in the loaded
config, and the snapshot:

- the schema version is known,
- `models_sha256` matches the model list, which catches hand edits,
- `generated_at` is present and timezone-aware.

A snapshot older than 30 days is reported as a warning, not an error. Pass
`--strict` (`uv run qqcode models validate --strict`) to make that an error.
