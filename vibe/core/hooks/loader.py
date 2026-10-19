from __future__ import annotations

import importlib.util
from logging import getLogger
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING

from vibe.core.config import get_vibe_home
from vibe.core.hooks.api import HookBinding
from vibe.core.hooks.registry import HookRegistry
from vibe.core.packages import package_resource_dirs

if TYPE_CHECKING:
    from vibe.core.config import VibeConfig

logger = getLogger("vibe")


def compute_hook_dirs(config: VibeConfig) -> list[Path]:
    """Return hook directories by priority: explicit, project, package, global.

    Project hooks (``.qqcode/hooks``) execute repository code, so they only
    load when the project is trusted (``--trust``/trust store). Explicit
    ``hooks.paths``, package and global hooks always load.
    """
    candidates: list[Path] = [Path(raw).expanduser() for raw in config.hooks.paths]

    if config.project_trusted:
        cwd = config.effective_workdir
        for directory in (cwd, *cwd.parents):
            project_dir = directory / ".qqcode" / "hooks"
            if project_dir.is_dir():
                candidates.append(project_dir)
                break
    else:
        logger.debug(
            "Skipping project hooks in %s: project is not trusted",
            config.effective_workdir,
        )

    candidates.extend(package_resource_dirs(config.packages, "hooks"))

    global_dir = get_vibe_home() / "hooks"
    if global_dir.is_dir():
        candidates.append(global_dir)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if not path.is_dir():
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def load_hooks(
    config: VibeConfig, *, registry: HookRegistry | None = None
) -> HookRegistry:
    """Create a registry and load every enabled hook module for ``config``."""
    if registry is None:
        registry = HookRegistry(timeout=config.hooks.timeout)
    if not config.hooks.enabled:
        logger.debug("Hooks are disabled by configuration")
        return registry
    return load_hooks_from_dirs(compute_hook_dirs(config), registry)


def load_hooks_from_dirs(
    directories: list[Path], registry: HookRegistry
) -> HookRegistry:
    """Load ``*.py`` hook modules from ``directories`` into ``registry``."""
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            load_hook_file(path, registry)
    return registry


def load_hook_file(path: Path, registry: HookRegistry) -> bool:
    """Import one hook module and call its ``register(registry)``.

    Returns ``True`` when the module loaded. Import errors, missing/invalid
    ``register`` functions, and decorated bindings are handled without raising
    so a broken extension can never block startup.
    """
    if not path.is_file():
        logger.warning("Hook module not found: %s", path)
        return False

    module_name = _module_name(path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        logger.warning("Could not create import spec for hook module %s", path)
        return False

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        logger.warning("Failed to import hook module %s: %s", path, exc)
        return False

    register = getattr(module, "register", None)
    if callable(register):
        try:
            register(registry)
        except Exception as exc:
            logger.warning("Hook module %s register() failed: %s", path, exc)
            return False
    else:
        for binding in _iter_bindings(module):
            registry.register(binding.event_type, binding, priority=binding.priority)

    logger.info("Loaded hook module %s", path)
    return True


def _iter_bindings(module: object) -> list[HookBinding]:
    bindings: list[HookBinding] = []
    seen: set[int] = set()
    for _, value in sorted(vars(module).items()):
        if not isinstance(value, HookBinding) or id(value) in seen:
            continue
        seen.add(id(value))
        bindings.append(value)
    return bindings


def _module_name(path: Path) -> str:
    stem = re.sub(r"[^0-9A-Za-z_]", "_", path.stem) or "mod"
    return f"vibe_hooks_discovered_{stem}_{abs(hash(str(path.resolve())))}"
