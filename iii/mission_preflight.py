"""Fail-closed simulation preflight for installed mission-catalog freshness."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Mapping


class MissionPreflightError(RuntimeError):
    pass


def _workspace_root(environment: Mapping[str, str]) -> Path | None:
    configured = environment.get("WORKSPACE_DIR")
    candidates = [Path(configured)] if configured else []
    current = Path.cwd().resolve()
    candidates.extend((current, *current.parents))
    for candidate in candidates:
        if (candidate / "src/III-Drone-Mission/iii_drone_mission/mission_catalog.py").is_file():
            return candidate.resolve()
    return None


def _package_share(package_name: str, workspace: Path | None, environment: Mapping[str, str]) -> Path:
    explicit_name = "III_MISSION_CATALOG_DIR" if package_name == "iii_drone_mission" else "III_INTERFACES_SHARE_DIR"
    explicit = environment.get(explicit_name)
    if explicit:
        path = Path(explicit)
        return path.parent if package_name == "iii_drone_mission" and path.name == "mission_catalog" else path
    try:
        from ament_index_python.packages import get_package_share_directory

        return Path(get_package_share_directory(package_name))
    except Exception:
        if workspace is not None:
            candidate = workspace / "install" / package_name / "share" / package_name
            if candidate.is_dir():
                return candidate
        raise MissionPreflightError(f"installed ROS package share is unavailable: {package_name}")


def _load_source_catalog_module(source_root: Path) -> ModuleType:
    path = source_root / "iii_drone_mission/mission_catalog.py"
    spec = importlib.util.spec_from_file_location("_iii_mission_catalog_preflight", path)
    if spec is None or spec.loader is None:
        raise MissionPreflightError(f"cannot load mission catalog verifier: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise MissionPreflightError(f"mission catalog verifier cannot be loaded: {exc}") from exc
    return module


def _drift_reasons(module: ModuleType, *, source_root: Path, catalog: Path, interfaces: Path) -> list[str]:
    try:
        return list(
            module.source_drift(
                source_root=source_root,
                catalog_directory=catalog,
                interface_directory=interfaces,
            )
        )
    except Exception as exc:
        return [f"installed catalog verification failed: {exc}"]


def ensure_sim_mission_catalog(
    *,
    environment: Mapping[str, str] | None = None,
    runner=subprocess.run,
) -> dict[str, object]:
    """Verify source/install identity, rebuilding only Mission when drift exists."""

    env = dict(os.environ if environment is None else environment)
    workspace = _workspace_root(env)
    if workspace is None:
        raise MissionPreflightError("simulation mission preflight requires an III source workspace")
    source_root = workspace / "src/III-Drone-Mission"
    mission_share = _package_share("iii_drone_mission", workspace, env)
    interfaces_share = _package_share("iii_drone_interfaces", workspace, env)
    catalog = Path(env.get("III_MISSION_CATALOG_DIR", str(mission_share / "mission_catalog")))
    module = _load_source_catalog_module(source_root)
    reasons = _drift_reasons(
        module,
        source_root=source_root,
        catalog=catalog,
        interfaces=interfaces_share,
    )
    if not reasons:
        verified = module.verify_catalog(catalog, expected_scope="local")
        return {
            "rebuilt": False,
            "catalog_hash": verified["catalog_hash"],
            "reason": "installed mission catalog matches source state",
        }

    command = [
        "colcon",
        "build",
        "--base-paths",
        "src",
        "--packages-select",
        "iii_drone_mission",
        "--symlink-install",
    ]
    build_env = dict(env)
    build_env.setdefault("COLCON_HOME", str(workspace))
    completed = runner(command, cwd=workspace, env=build_env, text=True)
    if completed.returncode != 0:
        raise MissionPreflightError(
            "targeted iii_drone_mission rebuild failed after source/install drift: "
            + "; ".join(reasons)
        )

    mission_share = _package_share("iii_drone_mission", workspace, env)
    interfaces_share = _package_share("iii_drone_interfaces", workspace, env)
    catalog = Path(env.get("III_MISSION_CATALOG_DIR", str(mission_share / "mission_catalog")))
    module = _load_source_catalog_module(source_root)
    remaining = _drift_reasons(
        module,
        source_root=source_root,
        catalog=catalog,
        interfaces=interfaces_share,
    )
    if remaining:
        raise MissionPreflightError(
            "mission catalog remains stale or invalid after targeted rebuild: " + "; ".join(remaining)
        )
    verified = module.verify_catalog(catalog, expected_scope="local")
    return {
        "rebuilt": True,
        "catalog_hash": verified["catalog_hash"],
        "reason": "; ".join(reasons),
    }
