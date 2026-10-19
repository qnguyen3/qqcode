from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import logging
import os
from pathlib import Path
import tempfile
import tomllib
from typing import Any, Protocol

from pydantic import BaseModel, Field
import tomli_w

logger = logging.getLogger(__name__)

PROJECT_DIR_NAME = ".qqcode"
PROJECT_CONFIG_NAME = "config.toml"
PROJECT_MCP_NAMES = ("mcp.toml", "mcp.json")
TRUST_FILE_NAME = "trust.toml"


def get_qqcode_home() -> Path:
    """Return the qqcode home directory (``QQCODE_HOME`` wins over ``~/.qqcode``)."""
    if home := os.getenv("QQCODE_HOME"):
        return Path(home).expanduser().resolve()
    return Path.home() / ".qqcode"


def default_trust_file() -> Path:
    return get_qqcode_home() / TRUST_FILE_NAME


class TrustDecision(StrEnum):
    """Persisted decision for a project directory."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class TrustChoice(StrEnum):
    """Answer to an interactive trust prompt."""

    YES = "yes"
    NO = "no"
    ALWAYS = "always"


class TrustPrompt(Protocol):
    """Callable asking the user whether a project should be trusted."""

    def __call__(self, project: ProjectResources) -> TrustChoice: ...


@dataclass(frozen=True)
class ProjectResources:
    """Project-scoped resources that must not load without trust.

    Context files (``AGENTS.md`` and friends) are deliberately *not* part of
    this list: they are plain text read into the prompt and never execute code,
    so they keep loading regardless of the trust decision.
    """

    project_dir: Path
    config_file: Path | None = None
    tools_dir: Path | None = None
    hooks_dir: Path | None = None
    mcp_file: Path | None = None

    @property
    def resource_dir(self) -> Path:
        return self.project_dir / PROJECT_DIR_NAME

    @property
    def has_gated_resources(self) -> bool:
        return any((self.config_file, self.tools_dir, self.hooks_dir, self.mcp_file))

    @property
    def resource_labels(self) -> list[str]:
        labels: list[str] = []
        if self.config_file is not None:
            labels.append("config")
        if self.tools_dir is not None:
            labels.append("tools")
        if self.hooks_dir is not None:
            labels.append("hooks")
        if self.mcp_file is not None:
            labels.append("mcp")
        return labels

    @classmethod
    def discover(cls, workdir: Path) -> ProjectResources | None:
        """Find the nearest project directory with gated ``.qqcode`` resources.

        The global ``~/.qqcode`` directory is never treated as a project, and
        the search stops at the first (closest) directory containing at least
        one gated resource, mirroring the legacy config lookup.
        """
        cwd = Path(workdir).expanduser().resolve()
        global_dir = get_qqcode_home()
        for directory in (cwd, *cwd.parents):
            qqcode_dir = directory / PROJECT_DIR_NAME
            if qqcode_dir == global_dir:
                continue
            if (resources := cls._from_project_dir(directory)) is not None:
                return resources
        return None

    @classmethod
    def _from_project_dir(cls, project_dir: Path) -> ProjectResources | None:
        qqcode_dir = project_dir / PROJECT_DIR_NAME
        if not qqcode_dir.is_dir():
            return None

        config_file = qqcode_dir / PROJECT_CONFIG_NAME
        tools_dir = qqcode_dir / "tools"
        hooks_dir = qqcode_dir / "hooks"
        mcp_file = next(
            (
                qqcode_dir / name
                for name in PROJECT_MCP_NAMES
                if (qqcode_dir / name).is_file()
            ),
            None,
        )
        resources = cls(
            project_dir=project_dir,
            config_file=config_file if config_file.is_file() else None,
            tools_dir=tools_dir if tools_dir.is_dir() else None,
            hooks_dir=hooks_dir if hooks_dir.is_dir() else None,
            mcp_file=mcp_file,
        )
        return resources if resources.has_gated_resources else None


@dataclass(frozen=True)
class TrustResolution:
    """Outcome of a trust evaluation for the current project."""

    project: ProjectResources | None
    decision: TrustDecision
    prompted: bool = False
    persisted: bool = False

    @property
    def trusted(self) -> bool:
        return self.decision is TrustDecision.TRUSTED


class TrustStore(BaseModel):
    """``~/.qqcode/trust.toml`` project trust decisions."""

    path: Path = Field(default_factory=default_trust_file, exclude=True)
    projects: dict[str, TrustDecision] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> TrustStore:
        target = Path(path).expanduser() if path is not None else default_trust_file()
        if not target.is_file():
            return cls(path=target)

        try:
            data = tomllib.loads(target.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning("Could not read trust store %s: %s", target, exc)
            return cls(path=target)

        raw_projects = data.get("projects")
        projects: dict[str, TrustDecision] = {}
        if isinstance(raw_projects, dict):
            for key, value in raw_projects.items():
                try:
                    projects[str(Path(str(key)).expanduser())] = TrustDecision(
                        str(value).lower()
                    )
                except ValueError:
                    logger.warning("Ignoring invalid trust entry %r = %r", key, value)
        return cls(path=target, projects=projects)

    def get(self, project_dir: Path) -> TrustDecision | None:
        return self.projects.get(_project_key(project_dir))

    def set(self, project_dir: Path, decision: TrustDecision) -> None:
        """Persist ``decision`` for ``project_dir`` (atomic, owner-only file)."""
        self.projects[_project_key(project_dir)] = decision
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "projects": {
                key: value.value for key, value in sorted(self.projects.items())
            }
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                tomli_w.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.chmod(0o600)
            os.replace(tmp_path, self.path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


def _project_key(project_dir: Path) -> str:
    return str(Path(project_dir).expanduser().resolve())


def _predecided_trust(
    project: ProjectResources,
    *,
    explicit: bool | None,
    trust_store: TrustStore,
    default_project_trust: bool,
) -> TrustResolution | None:
    """Resolve trust without prompting, when a decision already exists."""
    if explicit is True:
        return TrustResolution(project=project, decision=TrustDecision.TRUSTED)
    if explicit is False:
        return TrustResolution(project=project, decision=TrustDecision.UNTRUSTED)
    if (stored := trust_store.get(project.project_dir)) is not None:
        return TrustResolution(project=project, decision=stored)
    if default_project_trust:
        return TrustResolution(project=project, decision=TrustDecision.TRUSTED)
    return None


def _interactive_trust(
    project: ProjectResources, callback: TrustPrompt, trust_store: TrustStore
) -> TrustResolution:
    match callback(project):
        case TrustChoice.ALWAYS:
            trust_store.set(project.project_dir, TrustDecision.TRUSTED)
            return TrustResolution(
                project=project,
                decision=TrustDecision.TRUSTED,
                prompted=True,
                persisted=True,
            )
        case TrustChoice.YES:
            return TrustResolution(
                project=project, decision=TrustDecision.TRUSTED, prompted=True
            )
        case _:
            return TrustResolution(
                project=project, decision=TrustDecision.UNTRUSTED, prompted=True
            )


def resolve_trust(
    workdir: Path,
    *,
    explicit: bool | None = None,
    interactive_callback: TrustPrompt | None = None,
    store: TrustStore | None = None,
    default_project_trust: bool = False,
) -> TrustResolution:
    """Decide whether ``workdir``'s project resources may load.

    Precedence: explicit CLI flag, persisted store, ``default_project_trust``,
    interactive callback, then a safe untrusted default with a warning.

    Args:
        workdir: Directory to inspect for gated project resources.
        explicit: ``True``/``False`` from ``--trust``/``--no-trust``.
        interactive_callback: Optional prompt used when no other decision
            exists; ``ALWAYS`` answers are persisted to the trust store.
        store: Trust store to use (defaults to ``~/.qqcode/trust.toml``).
        default_project_trust: Global config default for projects without a
            stored decision.

    Returns:
        The resolution including the discovered project resources, if any.
    """
    project = ProjectResources.discover(workdir)
    if project is None:
        return TrustResolution(project=None, decision=TrustDecision.TRUSTED)

    trust_store = store or TrustStore.load()
    if predecided := _predecided_trust(
        project,
        explicit=explicit,
        trust_store=trust_store,
        default_project_trust=default_project_trust,
    ):
        return predecided

    if interactive_callback is not None:
        return _interactive_trust(project, interactive_callback, trust_store)

    logger.warning(
        "Project resources found in %s (%s) but the project is not trusted; "
        "loading global configuration only. Re-run with --trust to allow them.",
        project.project_dir,
        ", ".join(project.resource_labels),
    )
    return TrustResolution(project=project, decision=TrustDecision.UNTRUSTED)


def pending_trust(
    workdir: Path,
    *,
    explicit: bool | None = None,
    store: TrustStore | None = None,
    default_project_trust: bool = False,
) -> ProjectResources | None:
    """Return project resources needing an interactive trust answer, if any."""
    if explicit is not None:
        return None
    project = ProjectResources.discover(workdir)
    if project is None:
        return None
    trust_store = store or TrustStore.load()
    if trust_store.get(project.project_dir) is not None:
        return None
    if default_project_trust:
        return None
    return project


def read_default_project_trust(config_file: Path | None = None) -> bool:
    """Read ``default_project_trust`` from the global config without validating it."""
    target = (
        config_file if config_file is not None else get_qqcode_home() / "config.toml"
    )
    if not target.is_file():
        return False
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return bool(data.get("default_project_trust", False))


def read_project_mcp_servers(project: ProjectResources) -> list[dict[str, Any]]:
    """Parse ``.qqcode/mcp.toml``/``.qqcode/mcp.json`` into raw server mappings.

    Both a ``[[mcp_servers]]`` array and a ``[mcp_servers.<name>]`` table are
    accepted. Invalid payloads are ignored with a warning so a broken project
    file can never block startup.
    """
    if project.mcp_file is None:
        return []

    try:
        raw_text = project.mcp_file.read_text(encoding="utf-8")
        data = (
            json.loads(raw_text)
            if project.mcp_file.suffix == ".json"
            else tomllib.loads(raw_text)
        )
    except (OSError, ValueError) as exc:
        logger.warning(
            "Could not read project MCP config %s: %s", project.mcp_file, exc
        )
        return []

    entries = data.get("mcp_servers") if isinstance(data, dict) else None
    match entries:
        case dict():
            normalized: list[dict[str, Any]] = []
            for name, entry in entries.items():
                if isinstance(entry, dict):
                    normalized.append({**entry, "name": entry.get("name", name)})
            return normalized
        case list():
            return [entry for entry in entries if isinstance(entry, dict)]
        case _:
            return []
