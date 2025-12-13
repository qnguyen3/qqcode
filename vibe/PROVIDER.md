# Adding a new model provider

Vibe separates **models** (what you run) from **providers** (where requests go).

In most cases, adding a provider requires **only a config change** (no code).

## 1) Add it via config (recommended)

Vibe loads configuration from:

- `./.vibe/config.toml` (if present in the current project or any parent directory)
- otherwise `~/.vibe/config.toml`

### Provider fields

A provider is defined by `ProviderConfig` (see `core/config.py`):

- `name`: short identifier (used by models via `provider = "..."`)
- `api_base`: base URL, typically ending in `/v1`
- `api_key_env_var` (optional): environment variable name containing the API key
- `api_style` (optional, default: `"openai"`): request/response adapter name for the generic backend
- `backend` (optional, default: `GENERIC`): which backend implementation to use

### Model fields

A model is defined by `ModelConfig` (see `core/config.py`):

- `name`: provider’s model identifier
- `provider`: must match a `providers[].name`
- `alias`: friendly name you select via `active_model`
- `temperature` (optional)
- `extra_body` (optional): extra JSON merged into the request payload (provider/model specific)

### Example `config.toml`

```toml
# ~/.vibe/config.toml or ./.vibe/config.toml

active_model = "myprovider/my-model"

[[providers]]
name = "myprovider"
api_base = "https://api.myprovider.com/v1"
api_key_env_var = "MYPROVIDER_API_KEY"
backend = "GENERIC"      # use HTTPX + OpenAI-style chat completions
api_style = "openai"     # default; can be omitted

[[models]]
name = "my-model-id"
provider = "myprovider"
alias = "myprovider/my-model"
# extra_body = { some_provider_flag = true }
```

## 2) Set the API key

Vibe checks that the configured provider’s `api_key_env_var` is set.

You can set it in your shell environment, or put it in `~/.vibe/.env` (Vibe loads
that file and exports values into `os.environ` at startup).

Example:

```sh
export MYPROVIDER_API_KEY="..."
```

## 3) When you need code changes

### A) Provider is OpenAI-compatible (most common)

Use `backend = "GENERIC"` and keep `api_style = "openai"`.

The generic backend posts to:

- `POST {api_base}/chat/completions`

and expects OpenAI-like response/streaming shapes.

### B) Provider is *not* OpenAI-compatible, but is “close”

Add a new **adapter** for the generic backend:

- File: `core/llm/backend/generic.py`
- Mechanism: `@register_adapter(BACKEND_ADAPTERS, "<api_style_name>")`

An adapter controls:

- endpoint path (e.g. `/chat/completions`)
- request payload/headers
- response parsing

Then set in config:

```toml
api_style = "<api_style_name>"
backend = "GENERIC"
```

### C) Provider needs a completely different client

Add a new backend implementation:

- Add a new backend class under `core/llm/backend/`
- Extend the `Backend` enum in `core/config.py`
- Register it in `core/llm/backend/factory.py`

Backends are chosen via `provider.backend` and created by the agent (see `core/agent.py`).

## Notes / gotchas

- Vibe enforces a compatibility check for Mistral API bases:
  - Mistral API (`https://api.mistral.ai` / `https://codestral.mistral.ai`) must use `backend = "MISTRAL"`.
  - Other API bases should use `backend = "GENERIC"`.
- If a provider’s streaming format differs from OpenAI’s SSE (`data: {...}` lines), you will likely need a custom adapter/backend.
