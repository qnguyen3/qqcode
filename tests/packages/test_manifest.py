from __future__ import annotations

from pathlib import Path

import pytest

from vibe.core.packages import (
    MANIFEST_FILENAME,
    PackageError,
    load_manifest,
    package_resource_dirs,
)


def write_manifest(tmp_path: Path, content: str) -> Path:
    path = tmp_path / MANIFEST_FILENAME
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_manifest_is_parsed(tmp_path: Path) -> None:
    path = write_manifest(
        tmp_path,
        """
[package]
name = "my-pack"
version = "1.2.3"

[resources]
tools = ["tools/"]
skills = ["skills/"]
prompts = ["prompts/commands"]
agents = ["agents/"]
hooks = ["hooks/"]
""",
    )

    manifest = load_manifest(path)

    assert manifest.package.name == "my-pack"
    assert manifest.package.version == "1.2.3"
    assert manifest.resources.tools == ["tools/"]
    assert manifest.resources.hooks == ["hooks/"]


def test_resources_section_is_optional(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, '[package]\nname = "bare"\nversion = "0.1.0"\n')

    manifest = load_manifest(path)

    assert manifest.resources.tools == []
    assert manifest.resources.skills == []
    assert manifest.resources.prompts == []
    assert manifest.resources.agents == []
    assert manifest.resources.hooks == []


def test_missing_manifest_reports_path(tmp_path: Path) -> None:
    path = tmp_path / MANIFEST_FILENAME

    with pytest.raises(PackageError, match="not found"):
        load_manifest(path)


def test_missing_package_section_reports_clear_error(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, '[resources]\ntools = ["tools/"]\n')

    with pytest.raises(PackageError) as excinfo:
        load_manifest(path)

    assert "missing required field(s): package" in str(excinfo.value)


def test_missing_name_reports_clear_error(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, '[package]\nversion = "1.0.0"\n')

    with pytest.raises(PackageError) as excinfo:
        load_manifest(path)

    assert "missing required field(s): name" in str(excinfo.value)


def test_missing_version_reports_clear_error(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, '[package]\nname = "no-version"\n')

    with pytest.raises(PackageError) as excinfo:
        load_manifest(path)

    assert "missing required field(s): version" in str(excinfo.value)


def test_invalid_toml_reports_clear_error(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, "[package\nname = 'broken'\n")

    with pytest.raises(PackageError, match="Invalid manifest"):
        load_manifest(path)


def test_unknown_resource_kind_raises() -> None:
    with pytest.raises(ValueError, match="Unknown resource kind"):
        package_resource_dirs(["pack"], "not-a-resource")
