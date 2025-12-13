# Adding a new model provider

QQcode separates **models** (what you run) from **providers** (where requests go).

In most cases, adding a provider requires **only a config change** (no code).

## 1) Add it via config (recommended)

QQcode loads configuration from:

- `./.qqcode/config.toml` (if present in the current project or any parent directory)
- otherwise `~/.qqcode/config.toml`

If multiple `./.qqcode/config.toml` files exist in parent directories, the **closest one to your current directory wins**.

### Provider fields

A provider is defined by `ProviderConfig` (see `core/config.py`):

- `name`: short identifier (used by models via `provider = "..."`)
- `api_base`: base URL, typically ending in `/v1`
- `api_key_env_var` (optional): environment variable name containing the API key
- `api_style` (optional, default: `"openai"`): request/response adapter name for the generic backend
- `backend` (optional, default: `GENERIC`): which backend implementation to use

### Model fields

A model is defined by `ModelConfig` (see `core/config.py`):

- `name`: provider's model identifier
- `provider`: must match a `providers[].name`
- `alias`: friendly name you select via `active_model`
- `temperature` (optional)
- `input_price` / `output_price` (optional): price per million tokens (used for session cost estimates)
- `extra_body` (optional): extra JSON **merged into the request payload** (provider/model specific)

### Examples

#### A) OpenAI-compatible provider (generic)

```toml
# ~/.qqcode/config.toml or ./.qqcode/config.toml

active_model = "myprovider/my-model"

[[providers]]
name = "myprovider"
api_base = "https://api.myprovider.com/v1"
api_key_env_var = "MYPROVIDER_API_KEY"
backend = "GENERIC"
# api_style = "openai"  # default

[[models]]
name = "my-model-id"
provider = "myprovider"
alias = "myprovider/my-model"
```

#### B) OpenRouter

```toml
active_model = "openrouter/gpt-5.2:medium"

[[providers]]
name = "openrouter"
api_base = "https://openrouter.ai/api/v1"
api_key_env_var = "OPENROUTER_API_KEY"
backend = "GENERIC"

[[models]]
name = "openai/gpt-5.2"
provider = "openrouter"
alias = "openrouter/gpt-5.2:medium"
extra_body = { reasoning = { effort = "medium" } }
```

#### C) Local llama.cpp server

```toml
active_model = "local"

[[providers]]
name = "llamacpp"
api_base = "http://127.0.0.1:8080/v1"
api_key_env_var = ""  # no key required by default
backend = "GENERIC"

[[models]]
name = "devstral"
provider = "llamacpp"
alias = "local"
```

## 2) Set the API key

QQcode checks that the configured provider's `api_key_env_var` is set.

You can set it in your shell environment, or put it in `~/.qqcode/.env` (QQcode loads
that file and exports values into `os.environ` at startup).

Example:

```sh
export MYPROVIDER_API_KEY="..."
```

## Troubleshooting

- **Missing API key**: if you configured `api_key_env_var`, make sure it is set in your shell env, or add it to `~/.qqcode/.env`.
- **Wrong backend**: `https://api.mistral.ai` and `https://codestral.mistral.ai` must use `backend = "MISTRAL"`. Everything else should use `backend = "GENERIC"`.
- **Provider isn't OpenAI-compatible**: start with `backend = "GENERIC"`, but you may need a custom `api_style` adapter or a new backend (see next section).

## 3) When you need code changes

### A) Provider is OpenAI-compatible (most common)

Use `backend = "GENERIC"` and keep `api_style = "openai"`.

The generic backend posts to:

- `POST {api_base}/chat/completions`

and expects OpenAI-like response/streaming shapes.

### B) Provider is *not* OpenAI-compatible, but is "close"

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

- QQcode enforces a compatibility check for Mistral API bases:
  - Mistral API (`https://api.mistral.ai` / `https://codestral.mistral.ai`) must use `backend = "MISTRAL"`.
  - Other API bases should use `backend = "GENERIC"`.
- If a provider's streaming format differs from OpenAI's SSE (`data: {...}` lines), you will likely need a custom adapter/backend.
