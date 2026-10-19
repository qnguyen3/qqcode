from __future__ import annotations

from logging import getLogger
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

from pydantic import BaseModel, Field, ValidationError

from vibe.core.config import get_vibe_home

logger = getLogger("vibe")

MANIFEST_FILENAME = "qqcode.toml"
RESOURCE_KINDS = ("tools", "skills", "prompts", "agents", "hooks")
GIT_URL_PREFIXES = ("http://", "https://", "git://", "ssh://", "git@")


class PackageError(RuntimeError):
    """Raised when a resource package cannot be installed, loaded or removed."""


class PackageInfo(BaseModel):
    name: str
    version: str


class PackageResources(BaseModel):
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    prompts: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    hooks: list[str] = Field(default_factory=list)


class PackageManifest(BaseModel):
    package: PackageInfo
    resources: PackageResources = Field(default_factory=PackageResources)


class InstalledPackage(BaseModel):
    name: str
    version: str
    path: Path
    source: str = ""
    git: bool = False


def packages_root() -> Path:
    """Return the directory holding installed packages."""
    return get_vibe_home() / "packages"


def load_manifest(path: Path) -> PackageManifest:
    """Load and validate a ``qqcode.toml`` manifest.

    Raises:
        PackageError: If the manifest is missing, invalid TOML, or does not
            contain the required ``[package] name``/``version`` fields.
    """
    if not path.is_file():
        raise PackageError(f"Package manifest not found: {path}")

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PackageError(f"Invalid manifest at {path}: {exc}") from exc

    try:
        return PackageManifest.model_validate(data)
    except ValidationError as exc:
        missing = [
            str(error["loc"][-1])
            for error in exc.errors()
            if error.get("type") == "missing"
        ]
        detail = (
            f"missing required field(s): {', '.join(missing)}" if missing else str(exc)
        )
        raise PackageError(f"Invalid manifest at {path}: {detail}") from exc


def package_resource_dirs(
    names: list[str], resource: str, *, root: Path | None = None
) -> list[Path]:
    """Return existing resource directories declared by installed packages.

    ``resource`` is one of ``tools``, ``skills``, ``prompts``, ``agents`` or
    ``hooks``. Unknown resources raise ``ValueError``.
    """
    if resource not in RESOURCE_KINDS:
        raise ValueError(
            f"Unknown resource kind '{resource}'; expected one of {RESOURCE_KINDS}"
        )
    if not names:
        return []

    packages = (root or packages_root()).expanduser()
    directories: list[Path] = []
    for name in names:
        manifest_path = packages / name / MANIFEST_FILENAME
        if not manifest_path.is_file():
            logger.warning("Package '%s' is configured but not installed", name)
            continue

        try:
            manifest = load_manifest(manifest_path)
        except PackageError as exc:
            logger.warning("Skipping package '%s': %s", name, exc)
            continue

        for relative in getattr(manifest.resources, resource):
            candidate = (manifest_path.parent / relative).expanduser()
            if candidate.is_dir():
                directories.append(candidate.resolve())
            else:
                logger.warning(
                    "Package '%s' declares missing %s path '%s'",
                    name,
                    resource,
                    relative,
                )
    return directories


def list_packages(
    names: list[str], *, root: Path | None = None
) -> list[InstalledPackage]:
    """Describe the installed packages listed in ``names``, skipping missing ones."""
    packages = (root or packages_root()).expanduser()
    installed: list[InstalledPackage] = []
    for name in names:
        path = packages / name
        manifest_path = path / MANIFEST_FILENAME
        if not manifest_path.is_file():
            logger.warning("Package '%s' is configured but not installed", name)
            continue
        try:
            manifest = load_manifest(manifest_path)
        except PackageError as exc:
            logger.warning("Skipping package '%s': %s", name, exc)
            continue
        installed.append(
            InstalledPackage(
                name=name,
                version=manifest.package.version,
                path=path,
                source=_git_remote(path),
                git=(path / ".git").is_dir(),
            )
        )
    return installed


def install(
    source: str, name: str | None = None, *, root: Path | None = None
) -> InstalledPackage:
    """Install a package from a local directory or a git URL.

    The package is copied/cloned into ``<root>/<name>`` and its manifest is
    validated. ``name`` defaults to the manifest package name (local sources)
    or the repository basename (git sources).

    Raises:
        PackageError: If the source is invalid, the target already exists, or
            the manifest is missing/invalid.
    """
    packages = (root or packages_root()).expanduser()
    source_path = Path(source).expanduser()
    is_local = source_path.exists()

    if is_local:
        if not source_path.is_dir():
            raise PackageError(f"Package source is not a directory: {source_path}")
        manifest = load_manifest(source_path / MANIFEST_FILENAME)
        package_name = name or manifest.package.name
        target = packages / package_name
        _ensure_target_free(target, package_name)
        packages.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source_path,
            target,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
    else:
        if not _looks_like_git_url(source):
            raise PackageError(
                f"Package source is neither an existing directory nor a git URL: {source}"
            )
        package_name = name or _name_from_git_url(source)
        target = packages / package_name
        _ensure_target_free(target, package_name)
        packages.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", "--depth", "1", source, str(target)], source)
        if not (target / MANIFEST_FILENAME).is_file():
            shutil.rmtree(target, ignore_errors=True)
            raise PackageError(
                f"Repository {source} does not contain a {MANIFEST_FILENAME} manifest"
            )

    manifest = load_manifest(target / MANIFEST_FILENAME)
    logger.info("Installed package '%s' (%s) at %s", package_name, source, target)
    return InstalledPackage(
        name=package_name,
        version=manifest.package.version,
        path=target,
        source=source,
        git=not is_local,
    )


def remove(name: str, *, root: Path | None = None) -> Path:
    """Remove an installed package directory.

    Raises:
        PackageError: If the package is not installed.
    """
    target = (root or packages_root()).expanduser() / name
    if not target.is_dir():
        raise PackageError(f"Package '{name}' is not installed at {target}")
    shutil.rmtree(target)
    logger.info("Removed package '%s' from %s", name, target)
    return target


def update(name: str, *, root: Path | None = None) -> str:
    """Run ``git pull --ff-only`` inside a git-installed package.

    Returns the combined command output.

    Raises:
        PackageError: If the package is missing, not a git checkout, or the
            pull fails.
    """
    target = (root or packages_root()).expanduser() / name
    if not target.is_dir():
        raise PackageError(f"Package '{name}' is not installed at {target}")
    if not (target / ".git").is_dir():
        raise PackageError(
            f"Package '{name}' was not installed from git and cannot be updated"
        )
    return _run_git(["pull", "--ff-only"], str(target), cwd=target)


def _ensure_target_free(target: Path, name: str) -> None:
    if target.exists():
        raise PackageError(f"Package '{name}' is already installed at {target}")


def _looks_like_git_url(source: str) -> bool:
    return source.startswith(GIT_URL_PREFIXES) or source.endswith(".git")


def _name_from_git_url(source: str) -> str:
    cleaned = source.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    name = Path(cleaned.split(":")[-1]).name
    safe = re.sub(r"[^0-9A-Za-z._-]", "-", name).strip(".-")
    if not safe:
        raise PackageError(f"Could not derive a package name from git URL: {source}")
    return safe


def _run_git(args: list[str], source: str, *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise PackageError(f"Failed to run git for {source}: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PackageError(f"git {' '.join(args)} failed for {source}: {detail}")
    return (result.stdout or "").strip()


def _git_remote(path: Path) -> str:
    if not (path / ".git").is_dir():
        return ""
    try:
        return _run_git(["config", "--get", "remote.origin.url"], str(path), cwd=path)
    except PackageError:
        return ""
