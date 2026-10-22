"""Disk housekeeping for generated job folders under `outputs/`."""

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = frozenset({"queued", "running"})


def dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def list_job_dirs(output_dir: Path) -> List[Tuple[Path, float]]:
    """Job folders (direct, non-symlink subdirectories) as (path, mtime), newest first."""
    if not output_dir.is_dir():
        return []
    items = []
    for d in output_dir.iterdir():
        if d.is_dir() and not d.is_symlink():
            items.append((d, d.stat().st_mtime))
    return sorted(items, key=lambda x: x[1], reverse=True)


def storage_summary(output_dir: Path) -> Dict[str, Any]:
    dirs = list_job_dirs(output_dir)
    total = sum(dir_size(d) for d, _ in dirs)
    return {"count": len(dirs), "total_mb": round(total / (1024 ** 2), 1)}


def prune_outputs(
    output_dir: Path,
    keep: int = 10,
    protect: Iterable[str] = (),
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Delete all but the `keep` newest job folders. Folders named in `protect` are never removed."""
    keep = max(0, int(keep))
    protected = set(protect)
    removed: List[str] = []
    freed = 0
    for d, _ in list_job_dirs(output_dir)[keep:]:
        if d.name in protected:
            continue
        size = dir_size(d)
        if not dry_run:
            try:
                shutil.rmtree(d)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", d, exc)
                continue
        removed.append(d.name)
        freed += size
    return {"removed": removed, "freed_mb": round(freed / (1024 ** 2), 1), "dry_run": dry_run}
