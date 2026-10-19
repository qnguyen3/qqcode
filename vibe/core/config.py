from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum, auto
import fnmatch
import logging
import os
from pathlib import Path
import re
import shlex
import tempfile
import tomllib
from typing import Annotated, Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_core import to_jsonable_python
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
import tomli_w

from vibe.core.model_catalog import SNAPSHOT_PATH, load_model_snapshot
from vibe.core.oauth.registry import get_flow
from vibe.core.oauth.token import OAuthToken
from vibe.core.prompts import SystemPrompt
from vibe.core.tools.base import BaseToolConfig, ToolExposure
from vibe.core.trust import (
    ProjectResources,
    TrustDecision,
    TrustPrompt,
    TrustResolution,
    TrustStore,
    read_default_project_trust,
    read_project_mcp_servers,
    resolve_trust,
)


def get_vibe_home() -> Path:
    if vibe_home := os.getenv("QQCODE_HOME"):
        return Path(vibe_home).expanduser().resolve()
    return Path.home() / ".qqcode"


GLOBAL_CONFIG_DIR = get_vibe_home()
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.toml"
GLOBAL_ENV_FILE = GLOBAL_CONFIG_DIR / ".env"

logger = logging.getLogger(__name__)

_config_file_override: ContextVar[Path | None] = ContextVar(
    "qqcode_config_file_override", default=None
)

_skip_api_key_check: ContextVar[bool] = ContextVar(
    "qqcode_skip_api_key_check", default=False
)


@contextmanager
def skip_api_key_check() -> Iterator[None]:
    """Temporarily load a config whose active model has no credential.

    Used by the login/logout commands: a missing API key is exactly the
    situation they exist to fix, so loading must not fail on it.
    """
    token = _skip_api_key_check.set(True)
    try:
        yield
    finally:
        _skip_api_key_check.reset(token)


@contextmanager
def config_file_override(path: Path | None) -> Iterator[None]:
    """Temporarily make ``TomlFileSettingsSource`` read ``path``.

    Used by trust-aware loading to switch between the global config and a
    trusted project's ``.qqcode/config.toml``.
    """
    token = _config_file_override.set(path)
    try:
        yield
    finally:
        _config_file_override.reset(token)


def resolve_config_file() -> Path:
    # If QQCODE_HOME is explicitly set, always use that config
    if os.getenv("QQCODE_HOME"):
        return GLOBAL_CONFIG_FILE

    for directory in (cwd := Path.cwd(), *cwd.parents):
        if (candidate := directory / ".qqcode" / "config.toml").is_file():
            return candidate
    return GLOBAL_CONFIG_FILE


def resolve_config_file_for_project(
    workdir: Path,
    *,
    trust: bool | None = None,
    trust_callback: TrustPrompt | None = None,
    trust_store: TrustStore | None = None,
    default_project_trust: bool = False,
    global_config_file: Path | None = None,
) -> tuple[Path, TrustResolution]:
    """Pick the config file to load for ``workdir`` under the trust policy.

    Returns the effective config path (global unless the project is trusted and
    has a project config) together with the trust resolution.
    """
    resolution = resolve_trust(
        workdir,
        explicit=trust,
        interactive_callback=trust_callback,
        store=trust_store,
        default_project_trust=default_project_trust,
    )
    project = resolution.project
    if resolution.trusted and project is not None and project.config_file is not None:
        return project.config_file, resolution
    return global_config_file or GLOBAL_CONFIG_FILE, resolution


def load_api_keys_from_env() -> None:
    if GLOBAL_ENV_FILE.is_file():
        env_vars = dotenv_values(GLOBAL_ENV_FILE)
        for key, value in env_vars.items():
            if value:
                os.environ.setdefault(key, value)


CONFIG_FILE = resolve_config_file()
CONFIG_DIR = CONFIG_FILE.parent
AGENT_DIR = CONFIG_DIR / "agents"
PROMPT_DIR = CONFIG_DIR / "prompts"
INSTRUCTIONS_FILE = CONFIG_DIR / "instructions.md"
HISTORY_FILE = CONFIG_DIR / "qqcodehistory"
PROJECT_DOC_FILENAMES = ["AGENTS.md", "QQCODE.md", ".qqcode.md"]


class MissingAPIKeyError(RuntimeError):
    def __init__(self, env_key: str, provider_name: str) -> None:
        super().__init__(
            f"Missing {env_key} environment variable for {provider_name} provider"
        )
        self.env_key = env_key
        self.provider_name = provider_name


class MissingAmbientCredentialsError(RuntimeError):
    def __init__(self, ambient: str, provider_name: str, setup: str) -> None:
        super().__init__(
            f"No ambient credentials found for {provider_name} provider "
            f"({ambient}). {setup}"
        )
        self.ambient = ambient
        self.provider_name = provider_name
        self.setup = setup


class MissingPromptFileError(RuntimeError):
    def __init__(self, system_prompt_id: str, prompt_dir: str) -> None:
        super().__init__(
            f"Invalid system_prompt_id value: '{system_prompt_id}'. "
            f"Must be one of the available prompts ({', '.join(f'{p.name.lower()}' for p in SystemPrompt)}), "
            f"or correspond to a .md file in {prompt_dir}"
        )
        self.system_prompt_id = system_prompt_id
        self.prompt_dir = prompt_dir


class WrongBackendError(RuntimeError):
    def __init__(self, backend: Backend, is_mistral_api: bool) -> None:
        super().__init__(
            f"Wrong backend '{backend}' for {'' if is_mistral_api else 'non-'}"
            f"mistral API. Use '{Backend.MISTRAL}' for mistral API and '{Backend.GENERIC}' for others."
        )
        self.backend = backend
        self.is_mistral_api = is_mistral_api


class TomlFileSettingsSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self.toml_data = self._load_toml()

    def _load_toml(self) -> dict[str, Any]:
        file = _config_file_override.get() or CONFIG_FILE
        try:
            with file.open("rb") as f:
                return tomllib.load(f)
        except FileNotFoundError:
            return {}
        except tomllib.TOMLDecodeError as e:
            raise RuntimeError(f"Invalid TOML in {file}: {e}") from e
        except OSError as e:
            raise RuntimeError(f"Cannot read {file}: {e}") from e

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        return self.toml_data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self.toml_data


class ProjectContextConfig(BaseSettings):
    max_chars: int = 40_000
    default_commit_count: int = 5
    max_doc_bytes: int = 32 * 1024
    truncation_buffer: int = 1_000
    max_depth: int = 3
    max_files: int = 1000
    max_dirs_per_level: int = 20
    timeout_seconds: float = 2.0


class SessionLoggingConfig(BaseSettings):
    save_dir: str = ""
    session_prefix: str = "session"
    enabled: bool = True
    format: Literal["jsonl", "json"] = "jsonl"
    fsync: bool = True
    sessions_dir: str = ""

    @field_validator("save_dir", mode="before")
    @classmethod
    def set_default_save_dir(cls, v: str) -> str:
        if not v:
            return str(get_vibe_home() / "logs" / "session")
        return v

    @field_validator("save_dir", mode="after")
    @classmethod
    def expand_save_dir(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())

    @field_validator("sessions_dir", mode="before")
    @classmethod
    def set_default_sessions_dir(cls, v: str) -> str:
        if not v:
            return str(get_vibe_home() / "sessions")
        return v

    @field_validator("sessions_dir", mode="after")
    @classmethod
    def expand_sessions_dir(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


class CompactionConfig(BaseSettings):
    enabled: bool = True
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000
    threshold_ratio: float = 0.90
    prompt: Literal["summary", "legacy"] = "summary"


class RetryConfig(BaseSettings):
    honor_retry_after: bool = True


class HooksConfig(BaseSettings):
    enabled: bool = True
    timeout: float = 5.0
    paths: list[str] = Field(
        default_factory=list,
        description="Additional directories to search for hook modules.",
    )


class AuditConfig(BaseSettings):
    """Tool permission audit log (``~/.qqcode/logs/audit.jsonl``)."""

    enabled: bool = False
    log_args: bool = Field(
        default=False,
        description="Store full tool arguments; sensitive tools are always reduced.",
    )
    log_path: str = ""
    max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    keep_files: int = Field(default=3, ge=1)

    @field_validator("log_path", mode="before")
    @classmethod
    def set_default_log_path(cls, v: str) -> str:
        if not v:
            return str(get_vibe_home() / "logs" / "audit.jsonl")
        return v

    @field_validator("log_path", mode="after")
    @classmethod
    def expand_log_path(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


class DebugConfig(BaseSettings):
    """Structured request-metadata debug log (no message contents)."""

    enabled: bool = False
    log_path: str = ""

    @field_validator("log_path", mode="before")
    @classmethod
    def set_default_log_path(cls, v: str) -> str:
        if not v:
            return str(get_vibe_home() / "logs" / "debug.jsonl")
        return v

    @field_validator("log_path", mode="after")
    @classmethod
    def expand_log_path(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


class TelemetryConfig(BaseSettings):
    """Local-only counters; ``off`` disables collection entirely."""

    mode: Literal["off", "local"] = "off"
    metrics_path: str = ""
    flush_interval: float = Field(default=5.0, ge=0.0)

    @field_validator("metrics_path", mode="before")
    @classmethod
    def set_default_metrics_path(cls, v: str) -> str:
        if not v:
            return str(get_vibe_home() / "logs" / "metrics.json")
        return v

    @field_validator("metrics_path", mode="after")
    @classmethod
    def expand_metrics_path(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


class SubagentConfig(BaseSettings):
    """Limits and isolation rules for delegated ``task`` runs."""

    enabled: bool = Field(
        default=True,
        description="Expose the ``task`` tool; when false it is hidden entirely.",
    )
    timeout: float = Field(
        default=600.0,
        gt=0.0,
        description="Hard wall-clock timeout in seconds for one subagent run.",
    )
    max_concurrent: int = Field(
        default=2, ge=1, description="Maximum simultaneous subagent runs."
    )
    max_price: float | None = Field(
        default=None, gt=0.0, description="Optional per-subagent cost cap in dollars."
    )
    stream_tools: bool = Field(
        default=False,
        description="Forward subagent tool call/result events to the parent UI.",
    )
    default_agent: str = Field(
        default="general",
        description="Agent config used when the model does not name one.",
    )


class Backend(StrEnum):
    MISTRAL = auto()
    GENERIC = auto()
    ANTHROPIC = auto()


class ProviderConfig(BaseModel):
    name: str
    api_base: str
    api_key_env_var: str = ""
    api_style: str = "openai"
    oauth_flow: str = ""
    backend: Backend = Backend.GENERIC
    oauth_token: OAuthToken | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    cache_retention: Literal["default", "none", "short", "long"] = "default"
    timeout: float | None = None
    models_path: str | None = None
    ambient: str = Field(
        default="",
        description=(
            "Ambient credential kind resolved from local configuration "
            "(e.g. 'google-vertex', 'amazon-bedrock', 'cloudflare-workers-ai')."
        ),
    )


class MCPOAuthConfig(BaseModel):
    client_id: str | None = Field(
        default=None, description="Pre-registered OAuth client id (skips DCR)."
    )
    client_secret: str | None = Field(
        default=None, description="OAuth client secret, if the client is confidential."
    )
    callback_port: int | None = Field(
        default=None,
        description="Fixed loopback callback port for the OAuth redirect (random if unset).",
    )
    auth_server_url: str | None = Field(
        default=None, description="Override OAuth authorization server discovery."
    )
    scopes: list[str] = Field(
        default_factory=list, description="OAuth scopes to request."
    )


class _MCPBase(BaseModel):
    name: str = Field(description="Short alias used to prefix tool names")
    prompt: str | None = Field(
        default=None, description="Optional usage hint appended to tool descriptions"
    )
    enabled: bool = Field(
        default=True, description="Whether this server is connected at startup."
    )
    timeout: float = Field(
        default=60.0, description="Per-operation timeout in seconds for this server."
    )
    tool_exposure: Literal["direct", "deferred", "hidden"] = Field(
        default="direct",
        description=(
            "How the server's tools are exposed: 'direct' adds them to the tool "
            "list, 'deferred' hides them behind the mcp_search tool, 'hidden' "
            "keeps them callable only by name."
        ),
    )

    @field_validator("name", mode="after")
    @classmethod
    def normalize_name(cls, v: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", v)
        normalized = normalized.strip("_-")
        return normalized[:256]


class _MCPHttpFields(BaseModel):
    url: str = Field(description="Base URL of the MCP HTTP server")
    oauth: MCPOAuthConfig | None = Field(
        default=None,
        description="OAuth 2.1 client configuration for protected HTTP servers.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Additional HTTP headers when using 'http' transport (e.g., Authorization or X-API-Key)."
        ),
    )
    api_key_env: str = Field(
        default="",
        description=(
            "Environment variable name containing an API token to send for HTTP transport."
        ),
    )
    api_key_header: str = Field(
        default="Authorization",
        description=(
            "HTTP header name to carry the token when 'api_key_env' is set (e.g., 'Authorization' or 'X-API-Key')."
        ),
    )
    api_key_format: str = Field(
        default="Bearer {token}",
        description=(
            "Format string for the header value when 'api_key_env' is set. Use '{token}' placeholder."
        ),
    )

    def http_headers(self) -> dict[str, str]:
        hdrs = dict(self.headers or {})
        env_var = (self.api_key_env or "").strip()
        if env_var and (token := os.getenv(env_var)):
            target = (self.api_key_header or "").strip() or "Authorization"
            if not any(h.lower() == target.lower() for h in hdrs):
                try:
                    value = (self.api_key_format or "{token}").format(token=token)
                except Exception:
                    value = token
                hdrs[target] = value
        return hdrs


class MCPHttp(_MCPBase, _MCPHttpFields):
    transport: Literal["http"]


class MCPStreamableHttp(_MCPBase, _MCPHttpFields):
    transport: Literal["streamable-http"]


class MCPStdio(_MCPBase):
    transport: Literal["stdio"]
    command: str | list[str]
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(
        default_factory=dict, description="Extra environment variables for the process."
    )

    def argv(self) -> list[str]:
        base = (
            shlex.split(self.command)
            if isinstance(self.command, str)
            else list(self.command or [])
        )
        return [*base, *self.args] if self.args else base

    def stdio_env(self) -> dict[str, str]:
        return {**os.environ, **self.env} if self.env else dict(os.environ)


MCPServer = Annotated[
    MCPHttp | MCPStreamableHttp | MCPStdio, Field(discriminator="transport")
]


class ThinkingLevel(StrEnum):
    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


# Every thinking level, from the least effort to the most.
THINKING_LEVEL_ORDER: tuple[ThinkingLevel, ...] = (
    ThinkingLevel.OFF,
    ThinkingLevel.MINIMAL,
    ThinkingLevel.LOW,
    ThinkingLevel.MEDIUM,
    ThinkingLevel.HIGH,
    ThinkingLevel.XHIGH,
    ThinkingLevel.MAX,
)
# Levels a model accepts when its catalog entry declares none.
LEGACY_THINKING_LEVELS: tuple[ThinkingLevel, ...] = THINKING_LEVEL_ORDER[:5]


DEFAULT_BUDGETS: dict[ThinkingLevel, int] = {
    ThinkingLevel.MINIMAL: 1024,
    ThinkingLevel.LOW: 2048,
    ThinkingLevel.MEDIUM: 8192,
    ThinkingLevel.HIGH: 16384,
    ThinkingLevel.XHIGH: 24576,
    ThinkingLevel.MAX: 32768,
}

REASONING_EXTRA_BODY_KEYS = frozenset({"reasoning", "reasoning_effort", "thinking"})


class ModelCapabilities(BaseModel):
    tools: bool = True
    tool_choice: bool = True
    reasoning: bool = False
    vision: bool = False
    prompt_cache: Literal["none", "short", "long"] = "none"
    parallel_tool_calls: bool = True
    accepts_temperature: bool = True


class ModelPricing(BaseModel):
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cache_read_per_million: float = 0.0
    cache_write_per_million: float = 0.0


class ModelConfig(BaseModel):
    name: str
    provider: str
    alias: str
    api_style: str | None = Field(
        default=None,
        description=(
            "Per-model api_style overriding the provider default; lets one "
            "provider serve several request/response styles."
        ),
    )
    temperature: float = 0.2
    context_window: int = 200_000
    max_output_tokens: int | None = None
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    pricing: ModelPricing = Field(default_factory=ModelPricing)
    reasoning_level: ThinkingLevel | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    thinking_levels: list[ThinkingLevel] = Field(
        default_factory=list,
        description=(
            "Thinking levels the model accepts. Empty means the legacy set "
            "(off through high)."
        ),
    )
    thinking_efforts: dict[ThinkingLevel, str] = Field(
        default_factory=dict,
        description=(
            "Provider effort value for a level when it differs from the level "
            "name. Off sends no effort parameter unless it is mapped here."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        data = dict(data)
        if not data.get("alias"):
            data["alias"] = data.get("name")

        if "context_window" not in data:
            context_limit = data.get("context_limit")
            if context_limit is not None:
                data["context_window"] = context_limit

        if "input_price" not in data and "output_price" not in data:
            return data

        existing = data.get("pricing")
        match existing:
            case ModelPricing():
                pricing = existing.model_dump(exclude_unset=True)
            case dict():
                pricing = dict(existing)
            case _:
                pricing = {}

        if (input_price := data.get("input_price")) is not None:
            pricing.setdefault("input_per_million", input_price)
        if (output_price := data.get("output_price")) is not None:
            pricing.setdefault("output_per_million", output_price)

        data["pricing"] = pricing
        return data

    @model_validator(mode="after")
    def _check_thinking_levels(self) -> ModelConfig:
        if self.thinking_levels and not self.capabilities.reasoning:
            raise ValueError(
                f"Model '{self.alias}' declares thinking levels but reasoning is off"
            )
        if len(set(self.thinking_levels)) != len(self.thinking_levels):
            raise ValueError(f"Model '{self.alias}' lists a thinking level twice")
        supported = set(supported_thinking_levels(self))
        unsupported = [
            level.value for level in self.thinking_efforts if level not in supported
        ]
        if unsupported:
            raise ValueError(
                f"Model '{self.alias}' maps levels it does not support: "
                f"{', '.join(unsupported)}"
            )
        if any(not effort.strip() for effort in self.thinking_efforts.values()):
            raise ValueError(f"Model '{self.alias}' has an empty effort value")
        return self

    @property
    def context_limit(self) -> int:
        return self.context_window

    @property
    def input_price(self) -> float:
        return self.pricing.input_per_million

    @property
    def output_price(self) -> float:
        return self.pricing.output_per_million


def extra_body_has_reasoning(extra_body: dict[str, Any] | None) -> bool:
    """Whether the caller's extra_body already configures reasoning settings."""
    return bool(extra_body) and any(
        key in extra_body for key in REASONING_EXTRA_BODY_KEYS
    )


def supported_thinking_levels(model: ModelConfig) -> tuple[ThinkingLevel, ...]:
    """Return the thinking levels ``model`` accepts, from off to max."""
    if not model.thinking_levels:
        return LEGACY_THINKING_LEVELS
    declared = set(model.thinking_levels)
    return tuple(level for level in THINKING_LEVEL_ORDER if level in declared)


def clamp_thinking_level(model: ModelConfig, level: ThinkingLevel) -> ThinkingLevel:
    """Return ``level`` if ``model`` accepts it, else the nearest level it accepts.

    A higher level is preferred, so an OFF the model cannot honor becomes its
    lowest effort level.
    """
    supported = supported_thinking_levels(model)
    if level in supported:
        return level
    index = THINKING_LEVEL_ORDER.index(level)
    if above := [
        candidate
        for candidate in THINKING_LEVEL_ORDER[index:]
        if candidate in supported
    ]:
        return above[0]
    below = [
        candidate
        for candidate in THINKING_LEVEL_ORDER[:index]
        if candidate in supported
    ]
    return below[-1]


def thinking_effort(model: ModelConfig, level: ThinkingLevel | None) -> str | None:
    """Return the provider effort value for ``level``, or None to send none."""
    if level is None:
        return None
    if level in model.thinking_efforts:
        return model.thinking_efforts[level]
    if level is ThinkingLevel.OFF:
        return None
    return level.value


def resolve_thinking_level(
    model: ModelConfig, override: ThinkingLevel | None
) -> ThinkingLevel | None:
    """Resolve the effective thinking level for a request.

    The session override wins over the model's configured level. The result is
    clamped to the levels the model accepts. Levels other than OFF are ignored
    (with a debug log) when the model does not declare reasoning support.
    """
    level = override if override is not None else model.reasoning_level
    if level is None:
        return None
    if not model.capabilities.reasoning:
        if level is ThinkingLevel.OFF:
            return level
        logger.debug(
            "Ignoring thinking level '%s' for model '%s': reasoning not supported",
            level,
            model.name,
        )
        return None
    return clamp_thinking_level(model, level)


DEFAULT_PROVIDERS = [
    ProviderConfig(
        name="mistral",
        api_base="https://api.mistral.ai/v1",
        api_key_env_var="MISTRAL_API_KEY",
        backend=Backend.MISTRAL,
    ),
    ProviderConfig(
        name="llamacpp",
        api_base="http://127.0.0.1:8080/v1",
        api_key_env_var="",  # NOTE: if you wish to use --api-key in llama-server, change this value
    ),
    ProviderConfig(
        name="openrouter",
        api_base="https://openrouter.ai/api/v1",
        api_key_env_var="OPENROUTER_API_KEY",
    ),
    ProviderConfig(
        name="anthropic",
        api_base="https://api.anthropic.com",
        api_key_env_var="ANTHROPIC_API_KEY",
        api_style="anthropic",
        backend=Backend.ANTHROPIC,
    ),
    ProviderConfig(
        name="zai",
        api_base="https://api.z.ai/api/coding/paas/v4",
        api_key_env_var="ZAI_API_KEY",
    ),
    ProviderConfig(
        name="qwen",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env_var="DASHSCOPE_API_KEY",
        api_style="openai",
        backend=Backend.GENERIC,
    ),
    ProviderConfig(
        name="xiaomimimo",
        api_base="https://api.xiaomimimo.com/v1",
        api_key_env_var="MIMO_API_KEY",
        api_style="openai",
        backend=Backend.GENERIC,
    ),
    ProviderConfig(
        name="openai",
        api_base="https://api.openai.com/v1",
        api_key_env_var="OPENAI_API_KEY",
        api_style="openai-responses",
    ),
    ProviderConfig(
        name="openai-codex",
        api_base="https://chatgpt.com/backend-api",
        api_key_env_var="",
        api_style="openai-codex-responses",
        oauth_flow="openai-codex",
    ),
    ProviderConfig(
        name="deepseek",
        api_base="https://api.deepseek.com",
        api_key_env_var="DEEPSEEK_API_KEY",
    ),
    ProviderConfig(
        name="nvidia",
        api_base="https://integrate.api.nvidia.com/v1",
        api_key_env_var="NVIDIA_API_KEY",
        extra_headers={"NVCF-POLL-SECONDS": "3600"},
    ),
    ProviderConfig(
        name="google",
        api_base="https://generativelanguage.googleapis.com/v1beta",
        api_key_env_var="GEMINI_API_KEY",
        api_style="google-generative-ai",
    ),
    ProviderConfig(
        name="github-copilot",
        api_base="https://api.individual.githubcopilot.com",
        api_key_env_var="COPILOT_GITHUB_TOKEN",
        extra_headers={
            "User-Agent": "GitHubCopilotChat/0.35.0",
            "Editor-Version": "vscode/1.107.0",
            "Editor-Plugin-Version": "copilot-chat/0.35.0",
            "Copilot-Integration-Id": "vscode-chat",
        },
    ),
    ProviderConfig(
        name="groq",
        api_base="https://api.groq.com/openai/v1",
        api_key_env_var="GROQ_API_KEY",
    ),
    ProviderConfig(
        name="cerebras",
        api_base="https://api.cerebras.ai/v1",
        api_key_env_var="CEREBRAS_API_KEY",
    ),
    ProviderConfig(
        name="xai",
        api_base="https://api.x.ai/v1",
        api_key_env_var="XAI_API_KEY",
        api_style="openai-responses",
    ),
    ProviderConfig(
        name="vercel-ai-gateway",
        api_base="https://ai-gateway.vercel.sh/v1",
        api_key_env_var="AI_GATEWAY_API_KEY",
        api_style="anthropic",
    ),
    ProviderConfig(
        name="zai-coding-cn",
        api_base="https://open.bigmodel.cn/api/coding/paas/v4",
        api_key_env_var="ZAI_CODING_CN_API_KEY",
    ),
    ProviderConfig(
        name="radius",
        api_base="https://radius.pi.dev/v1",
        api_key_env_var="RADIUS_API_KEY",
        api_style="pi-messages",
    ),
    ProviderConfig(
        name="typesafe",
        api_base="https://api.typesafe.ai/v1",
        api_key_env_var="TYPESAFE_API_KEY",
    ),
    ProviderConfig(
        name="huggingface",
        api_base="https://router.huggingface.co/v1",
        api_key_env_var="HF_TOKEN",
    ),
    ProviderConfig(
        name="fireworks",
        api_base="https://api.fireworks.ai/inference/v1",
        api_key_env_var="FIREWORKS_API_KEY",
    ),
    ProviderConfig(
        name="together",
        api_base="https://api.together.ai/v1",
        api_key_env_var="TOGETHER_API_KEY",
    ),
    ProviderConfig(
        name="baseten",
        api_base="https://inference.baseten.co/v1",
        api_key_env_var="BASETEN_API_KEY",
    ),
    ProviderConfig(
        name="kimi-coding",
        api_base="https://api.kimi.com/coding/v1",
        api_key_env_var="KIMI_API_KEY",
        api_style="anthropic",
    ),
    ProviderConfig(
        name="meta",
        api_base="https://api.meta.ai/v1",
        api_key_env_var="META_API_KEY",
        api_style="openai-responses",
    ),
    ProviderConfig(
        name="minimax",
        api_base="https://api.minimax.io/anthropic/v1",
        api_key_env_var="MINIMAX_API_KEY",
        api_style="anthropic",
    ),
    ProviderConfig(
        name="minimax-cn",
        api_base="https://api.minimaxi.com/anthropic/v1",
        api_key_env_var="MINIMAX_CN_API_KEY",
        api_style="anthropic",
    ),
    ProviderConfig(
        name="moonshotai",
        api_base="https://api.moonshot.ai/v1",
        api_key_env_var="MOONSHOT_API_KEY",
    ),
    ProviderConfig(
        name="moonshotai-cn",
        api_base="https://api.moonshot.cn/v1",
        api_key_env_var="MOONSHOT_API_KEY",
    ),
    ProviderConfig(
        name="qwen-token-plan",
        api_base="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        api_key_env_var="QWEN_TOKEN_PLAN_API_KEY",
    ),
    ProviderConfig(
        name="qwen-token-plan-cn",
        api_base="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        api_key_env_var="QWEN_TOKEN_PLAN_CN_API_KEY",
    ),
    ProviderConfig(
        name="ant-ling",
        api_base="https://api.ant-ling.com/v1",
        api_key_env_var="ANT_LING_API_KEY",
    ),
    ProviderConfig(
        name="opencode",
        api_base="https://opencode.ai/zen/v1",
        api_key_env_var="OPENCODE_API_KEY",
    ),
    ProviderConfig(
        name="opencode-go",
        api_base="https://opencode.ai/zen/go/v1",
        api_key_env_var="OPENCODE_API_KEY",
    ),
    ProviderConfig(
        name="google-vertex",
        api_base="https://aiplatform.googleapis.com/v1",
        api_style="google-vertex",
        ambient="google-vertex",
    ),
    ProviderConfig(
        name="amazon-bedrock",
        api_base="https://bedrock-runtime.us-east-1.amazonaws.com",
        api_style="bedrock-converse",
        ambient="amazon-bedrock",
    ),
    ProviderConfig(
        name="cloudflare-workers-ai",
        api_base=(
            "https://api.cloudflare.com/client/v4/accounts"
            "/{CLOUDFLARE_ACCOUNT_ID}/ai/v1"
        ),
        api_style="openai",
        ambient="cloudflare-workers-ai",
    ),
    ProviderConfig(
        name="cloudflare-ai-gateway",
        api_base=(
            "https://gateway.ai.cloudflare.com/v1"
            "/{CLOUDFLARE_ACCOUNT_ID}/{CLOUDFLARE_GATEWAY_ID}/compat"
        ),
        api_style="openai",
        ambient="cloudflare-ai-gateway",
    ),
]


def _load_builtin_models() -> list[ModelConfig]:
    """Return the built-in models from the committed snapshot."""
    payload = load_model_snapshot(SNAPSHOT_PATH)
    return [ModelConfig.model_validate(entry) for entry in payload["models"]]


DEFAULT_MODELS = _load_builtin_models()


class ScriptToolConfig(BaseModel):
    """Sandboxed tool scripting configuration (``[tools.script]``)."""

    enabled: bool = False
    timeout: float = Field(default=30.0, gt=0)
    max_calls: int = Field(default=20, ge=1)
    max_output_bytes: int = Field(default=16384, ge=256)
    memory_mb: int = Field(default=256, ge=32)


class ToolRuntimeConfig(BaseModel):
    """Runtime behavior shared by all tools."""

    parallel: bool = True
    max_parallel_tools: int = Field(default=4, ge=1)
    sequential: list[str] = Field(
        default_factory=list,
        description="Tool names that must run sequentially, outside parallel batches.",
    )
    exposure: dict[str, ToolExposure] = Field(
        default_factory=dict,
        description=(
            "Per-tool exposure overrides by name or glob pattern "
            "(direct, model_only, deferred or hidden)."
        ),
    )
    defer_when_more_than: int = Field(
        default=30,
        ge=0,
        description=(
            "Defer MCP tools automatically when a server exposes more tools "
            "than this. Set a large value to keep every tool direct."
        ),
    )
    script: ScriptToolConfig = Field(default_factory=ScriptToolConfig)


class KeybindingsConfig(BaseModel):
    """Overrides for the interactive keybindings."""

    enabled: bool = True
    path: Path | None = Field(
        default=None,
        description="Keybindings file to use instead of the default locations.",
    )
    bindings: dict[str, str | list[str]] = Field(
        default_factory=dict,
        description="Action name to key (or list of keys) overrides.",
    )

    @field_validator("bindings")
    @classmethod
    def _validate_bindings(
        cls, value: dict[str, str | list[str]]
    ) -> dict[str, str | list[str]]:
        for action, keys in value.items():
            if not action.strip():
                raise ValueError("Keybinding actions must not be empty")
            match keys:
                case str() as single if single.strip():
                    continue
                case [*parts] if parts and all(
                    isinstance(part, str) and part.strip() for part in parts
                ):
                    continue
                case _:
                    raise ValueError(
                        f"Keybinding '{action}' must map to a key or a "
                        "non-empty list of keys"
                    )
        return value


class VibeConfig(BaseSettings):
    active_model: str = "devstral-2"
    enabled_models: list[str] = Field(
        default_factory=list,
        description=(
            "Glob patterns restricting which model aliases are selectable "
            "(e.g. ['claude-*', 'zai/*']). Empty means every model is in scope."
        ),
    )
    system_prompt_override: str | None = Field(
        default=None,
        exclude=True,
        description="Replace the preamble system prompt (CLI --system-prompt).",
    )
    append_system_prompt: list[str] = Field(
        default_factory=list,
        exclude=True,
        description="Extra text appended to the system prompt (CLI --append-system-prompt).",
    )
    vim_keybindings: bool = False
    keybindings: KeybindingsConfig = Field(default_factory=KeybindingsConfig)
    disable_welcome_banner_animation: bool = False
    displayed_workdir: str = ""
    auto_compact_threshold: int = 200_000
    context_warnings: bool = True
    textual_theme: str = "textual-dark"
    instructions: str = ""
    workdir: Path | None = Field(default=None, exclude=True)
    system_prompt_id: str = "cli"
    include_commit_signature: bool = True
    include_model_info: bool = True
    include_project_context: bool = True
    include_context_files: bool = True
    include_prompt_detail: bool = True
    enable_update_checks: bool = True
    api_timeout: float = 720.0
    providers: list[ProviderConfig] = Field(
        default_factory=lambda: list(DEFAULT_PROVIDERS)
    )
    models: list[ModelConfig] = Field(default_factory=lambda: list(DEFAULT_MODELS))

    project_context: ProjectContextConfig = Field(default_factory=ProjectContextConfig)
    session_logging: SessionLoggingConfig = Field(default_factory=SessionLoggingConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    subagent: SubagentConfig = Field(default_factory=SubagentConfig)
    default_project_trust: bool = Field(
        default=False,
        description="Trust projects without a stored decision (secure default: false).",
    )
    project_trust: TrustDecision | None = Field(
        default=None,
        exclude=True,
        description=(
            "Trust decision applied by VibeConfig.load; None means resources "
            "were constructed directly (legacy behaviour)."
        ),
    )
    packages: list[str] = Field(
        default_factory=list,
        description="Names of installed resource packages under ~/.qqcode/packages.",
    )
    tool_runtime: ToolRuntimeConfig = Field(default_factory=ToolRuntimeConfig)
    tools: dict[str, BaseToolConfig] = Field(default_factory=dict)
    tool_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Additional directories to search for custom tools. "
            "Each path may be absolute or relative to the current working directory."
        ),
    )

    mcp_servers: list[MCPServer] = Field(
        default_factory=list, description="Preferred MCP server configuration entries."
    )

    enabled_tools: list[str] = Field(
        default_factory=list,
        description=(
            "An explicit list of tool names/patterns to enable. If set, only these"
            " tools will be active. Supports exact names, glob patterns (e.g.,"
            " 'serena_*'), and regex with 're:' prefix or regex-like patterns (e.g.,"
            " 're:^serena_.*' or 'serena.*')."
        ),
    )
    disabled_tools: list[str] = Field(
        default_factory=list,
        description=(
            "A list of tool names/patterns to disable. Ignored if 'enabled_tools'"
            " is set. Supports exact names, glob patterns (e.g., 'bash*'), and"
            " regex with 're:' prefix or regex-like patterns."
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="QQCODE_", case_sensitive=False, extra="forbid"
    )

    @property
    def effective_workdir(self) -> Path:
        return self.workdir if self.workdir is not None else Path.cwd()

    @property
    def project_trusted(self) -> bool:
        """Whether project-scoped resources may load.

        ``None`` keeps the legacy behaviour for directly-constructed configs
        (tests, embedded use); production loading always sets a decision.
        """
        return self.project_trust is not TrustDecision.UNTRUSTED

    @property
    def system_prompt(self) -> str:
        try:
            return SystemPrompt[self.system_prompt_id.upper()].read()
        except KeyError:
            pass

        custom_sp_path = (PROMPT_DIR / self.system_prompt_id).with_suffix(".md")
        if not custom_sp_path.is_file():
            raise MissingPromptFileError(self.system_prompt_id, str(PROMPT_DIR))
        return custom_sp_path.read_text()

    def get_active_model(self) -> ModelConfig:
        for model in self.models:
            if model.alias == self.active_model:
                return model
        raise ValueError(
            f"Active model '{self.active_model}' not found in configuration."
        )

    def get_model_by_alias(self, alias: str) -> ModelConfig:
        """Return the model with ``alias``.

        Raises:
            ValueError: If no configured model matches ``alias``.
        """
        for model in self.models:
            if model.alias == alias:
                return model
        raise ValueError(f"Model '{alias}' not found in configuration.")

    def is_model_in_scope(self, alias: str) -> bool:
        """Whether ``alias`` is selectable under ``enabled_models``."""
        if not self.enabled_models:
            return True
        return any(fnmatch.fnmatch(alias, pattern) for pattern in self.enabled_models)

    def models_in_scope(self) -> list[ModelConfig]:
        """Configured models selectable under ``enabled_models``."""
        return [model for model in self.models if self.is_model_in_scope(model.alias)]

    def get_context_limit_for_active_model(self) -> int:
        """Return the context limit for the active model.

        Falls back to auto_compact_threshold if the active model is not found.
        """
        try:
            return self.get_active_model().context_window
        except ValueError:
            return self.auto_compact_threshold

    def get_compaction_threshold(self) -> int:
        """Token threshold that triggers auto-compaction.

        Reserves room for the next response and system prompt on top of the
        model context window, with a ratio fallback when the window is unknown.
        The reserve grows to the model's max output tokens when those are larger.
        """
        try:
            active_model = self.get_active_model()
        except ValueError:
            return max(
                1, int(self.auto_compact_threshold * self.compaction.threshold_ratio)
            )

        reserve = self.compaction.reserve_tokens
        if active_model.max_output_tokens is not None:
            reserve = max(reserve, active_model.max_output_tokens)
        return max(1, active_model.context_window - reserve)

    def get_provider_for_model(self, model: ModelConfig) -> ProviderConfig:
        for provider in self.providers:
            if provider.name == model.provider:
                return provider
        raise ValueError(
            f"Provider '{model.provider}' for model '{model.name}' not found in configuration."
        )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Define the priority of settings sources.

        Note: dotenv_settings is intentionally excluded. API keys and other
        non-config environment variables are stored in .env but loaded manually
        into os.environ for use by providers. Only VIBE_* prefixed environment
        variables (via env_settings) and TOML config are used for Pydantic settings.
        """
        return (
            init_settings,
            env_settings,
            TomlFileSettingsSource(settings_cls),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _merge_default_providers(self) -> VibeConfig:
        """Ensure DEFAULT_PROVIDERS are always available, merged with user providers."""
        # Get names from user-configured providers
        user_provider_names = {p.name for p in self.providers}

        # Add any DEFAULT_PROVIDERS that aren't already configured
        for default_provider in DEFAULT_PROVIDERS:
            if default_provider.name not in user_provider_names:
                self.providers.append(default_provider)

        return self

    @model_validator(mode="after")
    def _merge_default_models(self) -> VibeConfig:
        """Ensure DEFAULT_MODELS are always available, merged with user models."""
        # Build a lookup of default models by alias
        default_by_alias = {m.alias: m for m in DEFAULT_MODELS}

        # Update existing user models with context_window from defaults if not explicitly set
        for model in self.models:
            if default_model := default_by_alias.get(model.alias):
                # If user model has default context_window, use the one from DEFAULT_MODELS
                if model.context_window == 200_000:  # noqa: PLR2004
                    model.context_window = default_model.context_window

        # Get aliases from user-configured models
        user_aliases = {m.alias for m in self.models}

        # Add any DEFAULT_MODELS that aren't already configured
        for default_model in DEFAULT_MODELS:
            if default_model.alias not in user_aliases:
                self.models.append(default_model)

        return self

    @model_validator(mode="after")
    def _check_api_key(self) -> VibeConfig:
        if _skip_api_key_check.get():
            return self
        try:
            active_model = self.get_active_model()
            provider = self.get_provider_for_model(active_model)

            if provider.oauth_token and provider.oauth_token.is_expired(skew=60):
                try:
                    import asyncio

                    flow = get_flow(provider.oauth_flow or provider.name)
                    new_token = asyncio.run(flow.refresh(provider.oauth_token))
                    provider.oauth_token = new_token

                    save_oauth_token(provider.name, new_token)

                    return self
                except Exception:
                    pass

            if provider.oauth_token and not provider.oauth_token.is_expired(skew=60):
                return self
            if provider.ambient:
                from vibe.core.ambient import ambient_setup, resolve_ambient

                if resolve_ambient(provider.ambient) is None:
                    raise MissingAmbientCredentialsError(
                        provider.ambient, provider.name, ambient_setup(provider.ambient)
                    )
                return self
            api_key_env = provider.api_key_env_var
            if api_key_env and not os.getenv(api_key_env):
                raise MissingAPIKeyError(api_key_env, provider.name)
        except ValueError:
            pass
        return self

    @model_validator(mode="after")
    def _check_api_backend_compatibility(self) -> VibeConfig:
        try:
            active_model = self.get_active_model()
            provider = self.get_provider_for_model(active_model)
            MISTRAL_API_BASES = [
                "https://codestral.mistral.ai",
                "https://api.mistral.ai",
            ]
            ANTHROPIC_API_BASES = ["https://api.anthropic.com"]
            is_mistral_api = any(
                provider.api_base.startswith(api_base) for api_base in MISTRAL_API_BASES
            )
            is_anthropic_api = any(
                provider.api_base.startswith(api_base)
                for api_base in ANTHROPIC_API_BASES
            )

            # Check backend compatibility
            if is_mistral_api and provider.backend != Backend.MISTRAL:
                raise WrongBackendError(provider.backend, is_mistral_api)
            elif is_anthropic_api and provider.backend not in {
                Backend.ANTHROPIC,
                Backend.GENERIC,
            }:
                raise WrongBackendError(provider.backend, is_mistral_api)
            elif (
                not is_mistral_api
                and not is_anthropic_api
                and provider.backend != Backend.GENERIC
            ):
                raise WrongBackendError(provider.backend, is_mistral_api)

        except ValueError:
            pass
        return self

    @field_validator("workdir", mode="before")
    @classmethod
    def _expand_workdir(cls, v: Any) -> Path | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None

        if isinstance(v, str):
            v = Path(v).expanduser().resolve()
        elif isinstance(v, Path):
            v = v.expanduser().resolve()
        if not v.is_dir():
            raise ValueError(
                f"Tried to set {v} as working directory, path doesn't exist"
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def _hoist_tool_runtime_settings(cls, data: Any) -> Any:
        """Support ``[tools] parallel = ...`` next to per-tool ``[tools.<name>]``."""
        if not isinstance(data, dict):
            return data

        raw_tools = data.get("tools")
        if not isinstance(raw_tools, dict):
            return data

        runtime_keys = (
            "parallel",
            "max_parallel_tools",
            "sequential",
            "exposure",
            "defer_when_more_than",
            "script",
        )
        runtime = {key: raw_tools[key] for key in runtime_keys if key in raw_tools}
        if not runtime:
            return data

        data = dict(data)
        data["tools"] = {
            name: value for name, value in raw_tools.items() if name not in runtime_keys
        }
        existing = data.get("tool_runtime")
        match existing:
            case ToolRuntimeConfig():
                data["tool_runtime"] = {
                    **runtime,
                    **existing.model_dump(exclude_unset=True),
                }
            case dict():
                data["tool_runtime"] = {**runtime, **existing}
            case _:
                data["tool_runtime"] = runtime
        return data

    @field_validator("tools", mode="before")
    @classmethod
    def _normalize_tool_configs(cls, v: Any) -> dict[str, BaseToolConfig]:
        if not isinstance(v, dict):
            return {}

        normalized: dict[str, BaseToolConfig] = {}
        for tool_name, tool_config in v.items():
            if isinstance(tool_config, BaseToolConfig):
                normalized[tool_name] = tool_config
            elif isinstance(tool_config, dict):
                normalized[tool_name] = BaseToolConfig.model_validate(tool_config)
            else:
                normalized[tool_name] = BaseToolConfig()

        return normalized

    @model_validator(mode="after")
    def _validate_model_uniqueness(self) -> VibeConfig:
        seen_aliases: set[str] = set()
        for model in self.models:
            if model.alias in seen_aliases:
                raise ValueError(
                    f"Duplicate model alias found: '{model.alias}'. Aliases must be unique."
                )
            seen_aliases.add(model.alias)
        return self

    @model_validator(mode="after")
    def _check_system_prompt(self) -> VibeConfig:
        if self.system_prompt_override is None:
            _ = self.system_prompt
        return self

    @classmethod
    def save_updates(cls, updates: dict[str, Any]) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        current_config = TomlFileSettingsSource(cls).toml_data

        def deep_merge(target: dict, source: dict) -> None:
            for key, value in source.items():
                if (
                    key in target
                    and isinstance(target.get(key), dict)
                    and isinstance(value, dict)
                ):
                    deep_merge(target[key], value)
                elif (
                    key in target
                    and isinstance(target.get(key), list)
                    and isinstance(value, list)
                ):
                    if key in {"providers", "models", "enabled_models", "mcp_servers"}:
                        target[key] = value
                    else:
                        target[key] = list(set(value + target[key]))
                else:
                    target[key] = value

        deep_merge(current_config, updates)
        cls.dump_config(
            to_jsonable_python(current_config, exclude_none=True, fallback=str)
        )

    @classmethod
    def dump_config(cls, config: dict[str, Any]) -> None:
        with CONFIG_FILE.open("wb") as f:
            tomli_w.dump(config, f)

    @classmethod
    def set_packages(cls, names: list[str]) -> None:
        """Persist the installed resource package list to the config file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        current_config = TomlFileSettingsSource(cls).toml_data
        current_config["packages"] = list(names)
        cls.dump_config(
            to_jsonable_python(current_config, exclude_none=True, fallback=str)
        )

    @classmethod
    def _get_agent_config(
        cls, agent: str | None, packages: list[str] | None = None
    ) -> dict[str, Any] | None:
        if agent is None:
            return None

        search_dirs: list[Path] = []
        cwd = Path.cwd()
        for directory in (cwd, *cwd.parents):
            project_dir = directory / ".qqcode" / "agents"
            if project_dir.is_dir():
                search_dirs.append(project_dir)
                break

        from vibe.core.packages import package_resource_dirs

        search_dirs.extend(package_resource_dirs(packages or [], "agents"))
        search_dirs.append(AGENT_DIR)

        for directory in search_dirs:
            agent_config_path = (directory / agent).with_suffix(".toml")
            if agent_config_path.is_file():
                return tomllib.load(agent_config_path.open("rb"))

        raise ValueError(f"Config '{agent}.toml' for agent not found in {AGENT_DIR}")

    @classmethod
    def _migrate(cls) -> None:
        if not CONFIG_FILE.exists():
            return

        try:
            with CONFIG_FILE.open("rb") as f:
                config = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return

        needs_save = False

        if (
            "auto_compact_threshold" not in config
            or config["auto_compact_threshold"] == 100_000  # noqa: PLR2004
        ):
            config["auto_compact_threshold"] = 200_000
            needs_save = True

        if needs_save:
            cls.dump_config(config)

    @classmethod
    def load(
        cls,
        agent: str | None = None,
        *,
        workdir: Path | None = None,
        trust: bool | None = None,
        trust_callback: TrustPrompt | None = None,
        trust_store: TrustStore | None = None,
        **overrides: Any,
    ) -> VibeConfig:
        """Load configuration with project resources gated by trust.

        ``workdir`` (defaulting to the current directory) is inspected for
        ``.qqcode`` resources; they only load when the project is trusted (see
        :func:`vibe.core.trust.resolve_trust`). Context files such as
        ``AGENTS.md`` are never gated because they are inert text.
        """
        config, _ = cls.load_with_trust(
            agent,
            workdir=workdir,
            trust=trust,
            trust_callback=trust_callback,
            trust_store=trust_store,
            **overrides,
        )
        return config

    @classmethod
    def load_with_trust(
        cls,
        agent: str | None = None,
        *,
        workdir: Path | None = None,
        trust: bool | None = None,
        trust_callback: TrustPrompt | None = None,
        trust_store: TrustStore | None = None,
        global_config_file: Path | None = None,
        default_project_trust: bool | None = None,
        **overrides: Any,
    ) -> tuple[VibeConfig, TrustResolution]:
        """Trust-aware :meth:`load` returning the resolution alongside the config.

        Kept separate from ``load`` so tests can bypass the suite-wide
        ``VibeConfig.load`` patch and exercise the gating logic directly.
        """
        cls._migrate()
        cwd = Path(workdir).expanduser() if workdir is not None else Path.cwd()
        effective_default = (
            default_project_trust
            if default_project_trust is not None
            else read_default_project_trust(global_config_file)
        )
        config_file, resolution = resolve_config_file_for_project(
            cwd,
            trust=trust,
            trust_callback=trust_callback,
            trust_store=trust_store,
            default_project_trust=effective_default,
            global_config_file=global_config_file,
        )
        if workdir is not None:
            overrides = {**overrides, "workdir": workdir}

        with config_file_override(config_file):
            base = cls(**overrides)
            config = base
            if agent is not None:
                agent_config = cls._get_agent_config(agent, base.packages)
                config = cls(**{**(agent_config or {}), **overrides})

            from vibe.core.models_overlay import (
                default_overlay_paths,
                load_model_overlay,
                merge_model_overlays,
            )

            overlay_paths = default_overlay_paths()
            for provider in config.providers:
                if not provider.models_path:
                    continue
                if provider.models_path.startswith(("http://", "https://")):
                    logger.warning(
                        "Ignoring models_path for provider %s: remote model "
                        "overlays are not supported",
                        provider.name,
                    )
                    continue
                overlay_paths.append(Path(provider.models_path).expanduser())
            if overlay_models := load_model_overlay(overlay_paths):
                config.models = merge_model_overlays(config.models, overlay_models)

        config.project_trust = resolution.decision
        if resolution.trusted and resolution.project is not None:
            config.mcp_servers.extend(
                cls._validated_project_mcp_servers(resolution.project)
            )

        return config, resolution

    @staticmethod
    def _validated_project_mcp_servers(project: ProjectResources) -> list[MCPServer]:
        from pydantic import TypeAdapter, ValidationError

        adapter: TypeAdapter[MCPServer] = TypeAdapter(MCPServer)
        servers: list[MCPServer] = []
        for raw in read_project_mcp_servers(project):
            try:
                servers.append(adapter.validate_python(raw))
            except ValidationError as exc:
                logger.warning(
                    "Ignoring invalid project MCP server in %s: %s",
                    project.mcp_file,
                    exc,
                )
        return servers

    @classmethod
    def create_default(cls) -> dict[str, Any]:
        try:
            config = cls()
        except MissingAPIKeyError:
            config = cls.model_construct()

        config_dict = config.model_dump(mode="json", exclude_none=True)

        from vibe.core.tools.manager import ToolManager

        tool_defaults = ToolManager.discover_tool_defaults()
        if tool_defaults:
            config_dict["tools"] = tool_defaults

        return config_dict


def _atomic_write_toml(path: Path, config: dict[str, Any]) -> None:
    """Write ``config`` to ``path`` atomically with owner-only permissions.

    The TOML payload is written to a temporary file in the same directory,
    fsynced, chmodded to 600 and moved over the target with ``os.replace`` so
    an interrupted write can never corrupt the existing config.

    Raises:
        OSError: If the file cannot be written or replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            tomli_w.dump(config, handle)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def save_oauth_token(provider_name: str, token: OAuthToken) -> None:
    """Save an OAuth token to the config file for a specific provider.

    Args:
        provider_name: The name of the provider (e.g., "anthropic", "qwen").
        token: The OAuth token to save.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current_config = TomlFileSettingsSource(VibeConfig).toml_data

    # Find the provider in the providers list and update its oauth_token
    providers = current_config.get("providers", [])
    provider_found = False
    for provider in providers:
        if provider.get("name") == provider_name:
            provider["oauth_token"] = token.model_dump()
            provider_found = True
            break

    # If provider not found, add it
    if not provider_found:
        # Find the default provider config
        default_provider = None
        for p in DEFAULT_PROVIDERS:
            if p.name == provider_name:
                default_provider = p
                break

        if default_provider:
            provider_dict = default_provider.model_dump(exclude_none=True)
            provider_dict["oauth_token"] = token.model_dump()
            providers.append(provider_dict)
            current_config["providers"] = providers

    _atomic_write_toml(CONFIG_FILE, current_config)


def clear_oauth_token(provider_name: str) -> bool:
    """Remove a stored OAuth token from the config file.

    Returns:
        ``True`` when a token was removed.
    """
    file = _config_file_override.get() or CONFIG_FILE
    current_config = TomlFileSettingsSource(VibeConfig).toml_data

    providers = current_config.get("providers", [])
    for provider in providers:
        if provider.get("name") == provider_name and "oauth_token" in provider:
            provider.pop("oauth_token", None)
            _atomic_write_toml(file, current_config)
            return True
    return False
