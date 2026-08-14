from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any


CORE_WORKER_APK_PATTERN = re.compile(
    r"^CoreWorker-v[0-9A-Za-z][0-9A-Za-z_.-]*-debug\.apk$"
)
CORE_WORKER_IDSIG_PATTERN = re.compile(
    r"^CoreWorker-v[0-9A-Za-z][0-9A-Za-z_.-]*-debug\.apk\.idsig$"
)
DEFAULT_CORE_WORKER_RELEASE_KEEP = 3
DEFAULT_CORE_WORKER_RELEASE_MAX_BYTES = 256 * 1024 * 1024


def _is_regular_file(path: Path) -> bool:
    """Return True only for real regular files, never for symlinks."""
    try:
        return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def _validated_release_root(value: str | os.PathLike[str]) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute():
        root = root.resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("diretório de releases ausente ou inseguro")
    return root.resolve(strict=True)


def _validated_apk_basename(value: str) -> str:
    name = str(value or "").strip()
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("nome do APK atual não é um basename seguro")
    if not CORE_WORKER_APK_PATTERN.fullmatch(name):
        raise ValueError("nome do APK atual não corresponde ao Core Worker")
    return name


def prune_core_worker_releases(
    release_dir: str | os.PathLike[str],
    current_filename: str,
    *,
    keep: int = DEFAULT_CORE_WORKER_RELEASE_KEEP,
    max_total_bytes: int = DEFAULT_CORE_WORKER_RELEASE_MAX_BYTES,
) -> dict[str, Any]:
    """Prune old Core Worker APKs without touching the active release.

    Only exact ``CoreWorker-v*-debug.apk`` regular files directly under the
    release directory are considered. The active APK is mandatory and always
    wins over count/byte budgets. Matching ``.idsig`` files are retained only
    while their APK is retained. Unknown files, source archives and agent
    packages are deliberately ignored.
    """

    root = _validated_release_root(release_dir)
    current_name = _validated_apk_basename(current_filename)
    keep = max(1, min(int(keep), 10))
    max_total_bytes = max(1, int(max_total_bytes))

    current_path = root / current_name
    if not _is_regular_file(current_path):
        raise ValueError("APK atual não existe como arquivo regular")

    candidates: list[tuple[Path, int, int]] = []
    for path in root.iterdir():
        if not CORE_WORKER_APK_PATTERN.fullmatch(path.name) or not _is_regular_file(path):
            continue
        info = path.stat()
        candidates.append((path, int(info.st_size), int(info.st_mtime_ns)))

    current_entry = next((item for item in candidates if item[0].name == current_name), None)
    if current_entry is None:
        raise ValueError("APK atual não entrou no conjunto seguro de releases")

    older = sorted(
        (item for item in candidates if item[0].name != current_name),
        key=lambda item: (item[2], item[0].name),
        reverse=True,
    )
    retained = [current_entry, *older[: max(0, keep - 1)]]

    # Respect the byte budget by dropping the oldest non-current release. The
    # current APK is never removed even when it alone exceeds the configured cap.
    while len(retained) > 1 and sum(item[1] for item in retained) > max_total_bytes:
        retained.pop()

    retained_names = {item[0].name for item in retained}
    removed: list[str] = []
    removed_idsig: list[str] = []
    errors: list[str] = []
    reclaimed = 0

    for path, size, _mtime_ns in candidates:
        if path.name in retained_names:
            continue
        try:
            if _is_regular_file(path):
                path.unlink()
                removed.append(path.name)
                reclaimed += size
        except OSError as exc:
            errors.append(f"{path.name}: {type(exc).__name__}")

    for path in root.iterdir():
        if not CORE_WORKER_IDSIG_PATTERN.fullmatch(path.name) or not _is_regular_file(path):
            continue
        apk_name = path.name[: -len(".idsig")]
        if apk_name in retained_names:
            continue
        try:
            size = int(path.stat().st_size)
            path.unlink()
            removed_idsig.append(path.name)
            reclaimed += size
        except OSError as exc:
            errors.append(f"{path.name}: {type(exc).__name__}")

    retained_bytes = sum(
        int((root / name).stat().st_size)
        for name in retained_names
        if _is_regular_file(root / name)
    )
    return {
        "ok": not errors,
        "current": current_name,
        "retained": sorted(retained_names),
        "removed": sorted(removed),
        "removedIdsig": sorted(removed_idsig),
        "removedCount": len(removed),
        "removedIdsigCount": len(removed_idsig),
        "reclaimedBytes": reclaimed,
        "retainedBytes": retained_bytes,
        "keepLimit": keep,
        "byteLimit": max_total_bytes,
        "currentExceedsBudget": retained_bytes > max_total_bytes and len(retained_names) == 1,
        "errors": errors,
    }
