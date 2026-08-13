"""Deterministic workspace and module-artifact helpers."""

from __future__ import annotations

import json
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath


def materialize_workspace(source: str | Path, target: str | Path) -> Path:
    """Copy a source workspace into a new, independent target directory."""

    source_path = Path(source)
    if not source_path.is_dir():
        raise NotADirectoryError(f"source workspace is not a directory: {source_path}")

    source_root = source_path.resolve()
    target_path = Path(target)
    target_root = target_path.resolve()
    if target_root == source_root or source_root in target_root.parents:
        raise ValueError("target workspace must be outside the source workspace")
    if target_root.exists() or target_root.is_symlink():
        raise FileExistsError(f"target workspace already exists: {target_path}")

    shutil.copytree(source_root, target_root)
    return target_path


def _relative_paths(values: tuple[str, ...] | list[str], field: str) -> set[str]:
    """Normalize a small set of source-relative paths without accepting escapes."""

    result: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must contain non-empty relative paths")
        portable = value.replace("\\", "/")
        windows = PureWindowsPath(value)
        parts = portable.split("/")
        if windows.drive or portable.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"{field} must contain safe relative paths")
        result.add("/".join(parts))
    return result


def materialize_worker_workspace(
    source: str | Path,
    target: str | Path,
    *,
    raw_source_paths: tuple[str, ...] | list[str],
    owned_write_paths: tuple[str, ...] | list[str],
    python_executable: str | Path | None = None,
) -> Path:
    """Create a worker view with only authorized source bodies as text.

    The benchmark needs normal Python imports at test time, but a worker must
    not be able to recover a peer-owned source body by casually inspecting its
    worktree.  Authorized direct reads remain ``.py`` files.  All other fixture
    modules are compiled into sourceless ``.pyc`` files: they can satisfy an
    import during the focused test but are not model-readable source.  The
    ContextMesh broker remains the sole raw-source route for overlaps.
    """

    source_path = Path(source)
    if not source_path.is_dir():
        raise NotADirectoryError(f"source workspace is not a directory: {source_path}")
    source_root = source_path.resolve()
    target_path = Path(target)
    target_root = target_path.resolve()
    if target_root == source_root or source_root in target_root.parents:
        raise ValueError("target workspace must be outside the source workspace")
    if target_root.exists() or target_root.is_symlink():
        raise FileExistsError(f"target workspace already exists: {target_path}")

    raw_paths = _relative_paths(raw_source_paths, "raw_source_paths")
    write_paths = _relative_paths(owned_write_paths, "owned_write_paths")
    runtime_python = str(python_executable or sys.executable)
    source_files = {
        path.relative_to(source_root).as_posix(): path
        for path in source_root.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    missing = raw_paths - set(source_files)
    if missing:
        raise ValueError(f"raw_source_paths do not exist in source workspace: {sorted(missing)}")

    target_root.mkdir(parents=True)
    bytecode_paths: list[str] = []
    external_compile_pairs: list[tuple[Path, Path]] = []
    for relative, source_file in source_files.items():
        destination = target_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative in raw_paths:
            shutil.copy2(source_file, destination)
        elif relative not in write_paths:
            compiled = destination.with_suffix(destination.suffix + "c")
            if runtime_python == sys.executable:
                py_compile.compile(str(source_file), cfile=str(compiled), doraise=True)
            else:
                external_compile_pairs.append((source_file, compiled))
            bytecode_paths.append(compiled.relative_to(target_root).as_posix())
    if external_compile_pairs:
        pairs = json.dumps([(str(source_file), str(compiled)) for source_file, compiled in external_compile_pairs])
        subprocess.run(
            [
                runtime_python,
                "-c",
                "import json,py_compile,sys; [py_compile.compile(source, cfile=target, doraise=True) for source,target in json.loads(sys.argv[1])]",
                pairs,
            ],
            check=True,
        )
    for relative in write_paths:
        (target_root / relative).parent.mkdir(parents=True, exist_ok=True)

    (target_root / ".harness-source-view.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "raw_source_paths": sorted(raw_paths),
                "owned_write_paths": sorted(write_paths),
                "bytecode_runtime_paths": sorted(bytecode_paths),
                "runtime_python": runtime_python,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target_path


def create_module_artifact_dir(run_root: str | Path, module: str) -> Path:
    """Create and return the durable artifact directory for one module."""

    if not isinstance(module, str) or not module:
        raise ValueError("module must be a non-empty relative path")
    portable = module.replace("\\", "/")
    windows = PureWindowsPath(module)
    parts = portable.split("/")
    if windows.drive or portable.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("module must be a non-empty relative path")

    root = Path(run_root)
    artifact = root / Path(*parts)
    root_resolved = root.resolve()
    artifact_resolved = artifact.resolve()
    if artifact_resolved != root_resolved and root_resolved not in artifact_resolved.parents:
        raise ValueError("module artifact directory must remain under the run root")

    root.mkdir(parents=True, exist_ok=True)
    artifact.mkdir(parents=True, exist_ok=True)
    return artifact


__all__ = ["create_module_artifact_dir", "materialize_worker_workspace", "materialize_workspace"]
