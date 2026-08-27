from __future__ import annotations

from pathlib import Path


def resolve_repo_regular_file(path: Path, *, repo_root: Path, field_name: str) -> Path:
    """Resolve a file path against repo_root with strict anti-symlink checks."""
    root = Path(repo_root).resolve(strict=True)
    raw = Path(path)
    candidate = raw if raw.is_absolute() else root / raw

    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be inside repo_root") from exc
    if any(part in {".", ".."} for part in relative.parts):
        raise ValueError(f"{field_name} must be inside repo_root")

    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        if cursor.exists() and cursor.is_symlink():
            raise ValueError(f"{field_name} must not contain symlink components")

    if not candidate.exists():
        raise FileNotFoundError(f"{field_name} file not found: {candidate}")
    if candidate.is_symlink():
        raise ValueError(f"{field_name} must not be a symlink")
    if not candidate.is_file():
        raise ValueError(f"{field_name} must be a regular file")

    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be inside repo_root") from exc
    return resolved


__all__ = ["resolve_repo_regular_file"]
