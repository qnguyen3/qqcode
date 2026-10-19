from __future__ import annotations

from pathlib import Path

import pytest

from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.packages import (
    PackageError,
    install,
    list_packages,
    package_resource_dirs,
    remove,
    update,
)
from vibe.core.skills.manager import SkillManager
from vibe.core.tools.manager import ToolManager

TOOL_SOURCE = """
from __future__ import annotations

from pydantic import BaseModel

from vibe.core.tools.base import BaseTool, BaseToolConfig, BaseToolState


class PackPingArgs(BaseModel):
    pass


class PackPingResult(BaseModel):
    message: str = "pong from {origin}"


class PackPingState(BaseToolState):
    pass


class PackPing(BaseTool[PackPingArgs, PackPingResult, BaseToolConfig, PackPingState]):
    @classmethod
    def get_name(cls) -> str:
        return "pack_ping"

    async def run(self, args: PackPingArgs) -> PackPingResult:
        return PackPingResult()
"""

SKILL_SOURCE = """---
name: shared-skill
description: {description}
---

# Shared skill

{description}
"""


def make_package_source(root: Path, *, name: str = "demo-pack") -> Path:
    package = root / "src" / "demo-pack-src"
    (package / "tools").mkdir(parents=True)
    (package / "skills" / "shared").mkdir(parents=True)
    (package / "agents").mkdir(parents=True)
    (package / "qqcode.toml").write_text(
        "\n".join([
            "[package]",
            f'name = "{name}"',
            'version = "0.1.0"',
            "",
            "[resources]",
            'tools = ["tools/"]',
            'skills = ["skills/"]',
            'agents = ["agents/"]',
            "",
        ]),
        encoding="utf-8",
    )
    (package / "tools" / "pack_ping.py").write_text(
        TOOL_SOURCE.format(origin="package"), encoding="utf-8"
    )
    (package / "skills" / "shared" / "SKILL.md").write_text(
        SKILL_SOURCE.format(description="package skill"), encoding="utf-8"
    )
    (package / "agents" / "reviewer.toml").write_text(
        'active_model = "devstral-2"\n', encoding="utf-8"
    )
    return package


def make_config(tmp_path: Path, *, packages: list[str] | None = None) -> VibeConfig:
    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        workdir=tmp_path,
        packages=packages or [],
    )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("QQCODE_HOME", str(home))
    return home


def test_install_local_copies_package_and_exposes_resources(
    tmp_path: Path, isolated_home: Path
) -> None:
    source = make_package_source(tmp_path)

    info = install(str(source))

    assert info.name == "demo-pack"
    assert info.version == "0.1.0"
    assert info.git is False
    target = isolated_home / "packages" / "demo-pack"
    assert target.is_dir()
    assert (target / "qqcode.toml").is_file()

    assert package_resource_dirs(["demo-pack"], "tools") == [
        (target / "tools").resolve()
    ]
    assert package_resource_dirs(["demo-pack"], "skills") == [
        (target / "skills").resolve()
    ]

    config = make_config(tmp_path, packages=["demo-pack"])
    assert "pack_ping" in ToolManager(config).available_tools()
    skills = {skill.name for skill in SkillManager(config).get_available_skills()}
    assert "shared-skill" in skills


def test_install_with_name_override(tmp_path: Path, isolated_home: Path) -> None:
    source = make_package_source(tmp_path)

    info = install(str(source), name="renamed-pack")

    assert info.name == "renamed-pack"
    assert (isolated_home / "packages" / "renamed-pack").is_dir()
    assert package_resource_dirs(["renamed-pack"], "tools")


def test_install_rejects_missing_manifest(tmp_path: Path, isolated_home: Path) -> None:
    source = tmp_path / "src" / "no-manifest"
    source.mkdir(parents=True)

    with pytest.raises(PackageError, match="manifest not found"):
        install(str(source))


def test_install_rejects_duplicate_package(tmp_path: Path, isolated_home: Path) -> None:
    source = make_package_source(tmp_path)
    install(str(source))

    with pytest.raises(PackageError, match="already installed"):
        install(str(source))


def test_install_rejects_unknown_source(tmp_path: Path, isolated_home: Path) -> None:
    with pytest.raises(
        PackageError, match="neither an existing directory nor a git URL"
    ):
        install("not-a-real-source")


def test_remove_deletes_package_and_search_paths(
    tmp_path: Path, isolated_home: Path
) -> None:
    install(str(make_package_source(tmp_path)))
    assert package_resource_dirs(["demo-pack"], "tools")

    remove("demo-pack")

    assert not (isolated_home / "packages" / "demo-pack").exists()
    assert package_resource_dirs(["demo-pack"], "tools") == []

    with pytest.raises(PackageError, match="not installed"):
        remove("demo-pack")


def test_update_rejects_non_git_package(tmp_path: Path, isolated_home: Path) -> None:
    install(str(make_package_source(tmp_path)))

    with pytest.raises(PackageError, match="not installed from git"):
        update("demo-pack")


def test_list_packages_reports_installed_and_skips_missing(
    tmp_path: Path, isolated_home: Path
) -> None:
    install(str(make_package_source(tmp_path)))

    installed = list_packages(["demo-pack", "missing-pack"])

    assert [package.name for package in installed] == ["demo-pack"]
    assert installed[0].version == "0.1.0"


def test_agent_config_is_resolved_from_package(
    tmp_path: Path, isolated_home: Path
) -> None:
    install(str(make_package_source(tmp_path)))
    config = make_config(tmp_path, packages=["demo-pack"])

    agent_config = VibeConfig._get_agent_config("reviewer", config.packages)

    assert agent_config == {"active_model": "devstral-2"}


def test_project_skill_overrides_package_skill(
    tmp_path: Path, isolated_home: Path
) -> None:
    install(str(make_package_source(tmp_path)))
    project = tmp_path / "project"
    project.mkdir()
    project_skill = project / ".qqcode" / "skills" / "shared"
    project_skill.mkdir(parents=True)
    (project_skill / "SKILL.md").write_text(
        SKILL_SOURCE.format(description="project skill"), encoding="utf-8"
    )

    config = make_config(project, packages=["demo-pack"])
    skills = {
        skill.name: skill for skill in SkillManager(config).get_available_skills()
    }

    assert skills["shared-skill"].description == "project skill"
    assert skills["shared-skill"].path == (project_skill / "SKILL.md").resolve()
