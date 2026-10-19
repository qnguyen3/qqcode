from __future__ import annotations

from pathlib import Path
import stat
import sys

import pytest

from vibe.cli.entrypoint import parse_arguments
from vibe.core.config import (
    SessionLoggingConfig,
    VibeConfig,
    resolve_config_file_for_project,
)
from vibe.core.hooks.loader import compute_hook_dirs, load_hooks
from vibe.core.tools.manager import ToolManager
from vibe.core.trust import (
    ProjectResources,
    TrustChoice,
    TrustDecision,
    TrustResolution,
    TrustStore,
    pending_trust,
    read_default_project_trust,
    resolve_trust,
)

HOOK_MODULE = """
from vibe.core.hooks.api import deny


def register(registry):
    registry.register("tool_call", lambda event: deny("project hook"))
"""

PROJECT_CONFIG = 'active_model = "devstral-small"\n'

PROJECT_MCP = """
[[mcp_servers]]
name = "project-server"
transport = "http"
url = "http://127.0.0.1:9/mcp"
"""


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / ".qqcode").mkdir(parents=True)
    (project / ".qqcode" / "config.toml").write_text(PROJECT_CONFIG, encoding="utf-8")
    return project


def make_store(tmp_path: Path) -> TrustStore:
    return TrustStore.load(tmp_path / "trust.toml")


def make_config(project: Path, *, trusted: bool) -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        system_prompt_id="tests",
        include_project_context=False,
        include_context_files=False,
        workdir=project,
        project_trust=(TrustDecision.TRUSTED if trusted else TrustDecision.UNTRUSTED),
    )


def load_config(
    project: Path, *, trust: bool, store: TrustStore, global_file: Path
) -> tuple[VibeConfig, TrustResolution]:
    return VibeConfig.load_with_trust(
        workdir=project,
        trust=trust,
        trust_store=store,
        global_config_file=global_file,
        default_project_trust=False,
        session_logging=SessionLoggingConfig(enabled=False),
        system_prompt_id="tests",
        include_project_context=False,
        include_context_files=False,
    )


def test_discovers_only_gated_resources(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "AGENTS.md").write_text("context", encoding="utf-8")

    resources = ProjectResources.discover(project)

    assert resources is not None
    assert resources.project_dir == project
    assert resources.config_file == project / ".qqcode" / "config.toml"
    assert resources.resource_labels == ["config"]


def test_plain_directory_has_no_gated_resources(tmp_path: Path) -> None:
    project = tmp_path / "plain"
    (project / ".qqcode").mkdir(parents=True)
    (project / ".qqcode" / "notes.txt").write_text("hi", encoding="utf-8")

    assert ProjectResources.discover(project) is None

    resolution = resolve_trust(project, store=make_store(tmp_path))

    assert resolution.project is None
    assert resolution.trusted


def test_untrusted_project_config_is_skipped(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    global_file = tmp_path / "global.toml"

    chosen, resolution = resolve_config_file_for_project(
        project, trust_store=make_store(tmp_path), global_config_file=global_file
    )

    assert resolution.decision is TrustDecision.UNTRUSTED
    assert resolution.project is not None
    assert chosen == global_file


def test_explicit_trust_selects_project_config(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    global_file = tmp_path / "global.toml"

    chosen, resolution = resolve_config_file_for_project(
        project,
        trust=True,
        trust_store=make_store(tmp_path),
        global_config_file=global_file,
    )

    assert resolution.trusted
    assert chosen == project / ".qqcode" / "config.toml"


def test_load_with_trust_marks_untrusted_and_skips_project_mcp(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / ".qqcode" / "mcp.toml").write_text(PROJECT_MCP, encoding="utf-8")

    config, resolution = load_config(
        project,
        trust=False,
        store=make_store(tmp_path),
        global_file=tmp_path / "global.toml",
    )

    assert resolution.decision is TrustDecision.UNTRUSTED
    assert config.project_trust is TrustDecision.UNTRUSTED
    assert config.project_trusted is False
    assert config.mcp_servers == []


def test_load_with_trust_applies_project_mcp_when_trusted(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / ".qqcode" / "mcp.toml").write_text(PROJECT_MCP, encoding="utf-8")

    config, resolution = load_config(
        project,
        trust=True,
        store=make_store(tmp_path),
        global_file=tmp_path / "global.toml",
    )

    assert resolution.trusted
    assert config.project_trust is TrustDecision.TRUSTED
    assert config.project_trusted is True
    assert [server.name for server in config.mcp_servers] == ["project-server"]


def test_trust_store_persists_owner_only(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    store = make_store(tmp_path)

    store.set(project, TrustDecision.TRUSTED)

    assert store.path.is_file()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    reloaded = TrustStore.load(store.path)
    assert reloaded.get(project) is TrustDecision.TRUSTED


def test_stored_decision_skips_interactive_prompt(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    store = make_store(tmp_path)
    store.set(project, TrustDecision.TRUSTED)
    prompts: list[ProjectResources] = []

    def prompt(project: ProjectResources) -> TrustChoice:
        prompts.append(project)
        return TrustChoice.NO

    resolution = resolve_trust(project, interactive_callback=prompt, store=store)

    assert resolution.trusted
    assert resolution.prompted is False
    assert prompts == []


def test_interactive_always_persists_decision(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    store = make_store(tmp_path)

    resolution = resolve_trust(
        project, interactive_callback=lambda project: TrustChoice.ALWAYS, store=store
    )

    assert resolution.trusted
    assert resolution.prompted is True
    assert resolution.persisted is True
    assert TrustStore.load(store.path).get(project) is TrustDecision.TRUSTED


def test_no_trust_headless_never_prompts(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    store = make_store(tmp_path)
    prompts: list[ProjectResources] = []

    def prompt(project: ProjectResources) -> TrustChoice:
        prompts.append(project)
        return TrustChoice.YES

    resolution = resolve_trust(
        project, explicit=False, interactive_callback=prompt, store=store
    )

    assert resolution.decision is TrustDecision.UNTRUSTED
    assert prompts == []
    assert pending_trust(project, explicit=False, store=store) is None


def test_pending_trust_only_without_explicit_decision(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    store = make_store(tmp_path)

    assert pending_trust(project, store=store) is not None
    assert pending_trust(project, explicit=True, store=store) is None
    store.set(project, TrustDecision.TRUSTED)
    assert pending_trust(project, store=store) is None


def test_default_project_trust_applies_without_prompt(tmp_path: Path) -> None:
    project = make_project(tmp_path)

    resolution = resolve_trust(
        project, store=make_store(tmp_path), default_project_trust=True
    )

    assert resolution.trusted
    assert resolution.prompted is False


def test_read_default_project_trust(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("default_project_trust = true\n", encoding="utf-8")

    assert read_default_project_trust(config_file) is True
    assert read_default_project_trust(tmp_path / "missing.toml") is False


def test_cli_trust_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["qqcode", "--trust"])
    assert parse_arguments().trust is True

    monkeypatch.setattr(sys, "argv", ["qqcode", "--no-trust"])
    assert parse_arguments().trust is False

    monkeypatch.setattr(sys, "argv", ["qqcode"])
    assert parse_arguments().trust is None


def test_hooks_loader_skips_untrusted_project_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QQCODE_HOME", str(tmp_path / "home"))
    project = make_project(tmp_path)
    hooks_dir = project / ".qqcode" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "guard.py").write_text(HOOK_MODULE, encoding="utf-8")

    untrusted = make_config(project, trusted=False)
    trusted = make_config(project, trusted=True)

    assert hooks_dir.resolve() not in compute_hook_dirs(untrusted)
    assert hooks_dir.resolve() in compute_hook_dirs(trusted)
    assert load_hooks(untrusted).registered_types() == []
    assert load_hooks(trusted).has_handlers("tool_call")


def test_tool_manager_skips_untrusted_project_tools(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    tools_dir = project / ".qqcode" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "custom.py").write_text("VALUE = 1\n", encoding="utf-8")

    untrusted_paths = ToolManager._compute_search_paths(
        make_config(project, trusted=False)
    )
    trusted_paths = ToolManager._compute_search_paths(
        make_config(project, trusted=True)
    )

    assert tools_dir.resolve() not in untrusted_paths
    assert tools_dir.resolve() in trusted_paths
