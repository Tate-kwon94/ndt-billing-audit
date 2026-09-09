"""핫픽스 패치 — 사외(mac) 에서 수정한 코드를 사내 PC 로 안전 이전.

워크플로우:
  mac     :  python -m app.main patch export --out patch.zip
            (현재 파일과 VERSION 의 manifest 비교 → 변경된 .py/.yaml/.md 만 묶음)
  사내 PC :  python -m app.main patch apply patch.zip [--dry-run]
            (data/backups/<ts>/ 에 기존 파일 백업 후 덮어쓰기. 결재 첨부용 before/after 출력)

manifest: VERSION 파일과 함께 출하된 deployed_manifest.json
          (build_offline_bundle 시 생성, 모든 소스 파일의 sha256)
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from app.config import PROJECT_ROOT

logger = logging.getLogger(__name__)


# 패치 가능한 파일 확장자·디렉토리 (안전 화이트리스트)
_PATCH_EXTENSIONS = {".py", ".yaml", ".yml", ".md", ".bat", ".sh", ".ps1", ".txt"}
_PATCH_ROOTS = ("app", "config", "scripts", "tests")


@dataclass
class FileEntry:
    rel_path: str
    sha256: str
    size: int


# ─────────────────────────── EXPORT (사외 mac) ───────────────────────────


def export_patch(
    out_path: Path,
    *,
    baseline_manifest: Optional[Path] = None,
) -> dict:
    """현재 파일과 baseline 의 차이를 zip 으로 묶음.

    baseline_manifest 미지정 시 PROJECT_ROOT/deployed_manifest.json 사용.
    없으면 모든 화이트리스트 파일을 묶음.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    baseline = _load_manifest(baseline_manifest)
    baseline_by_path = {e.rel_path: e for e in baseline}
    current = list(_scan_files())

    changed: list[FileEntry] = []
    added: list[FileEntry] = []
    for e in current:
        b = baseline_by_path.get(e.rel_path)
        if b is None:
            added.append(e)
        elif b.sha256 != e.sha256:
            changed.append(e)

    removed: list[FileEntry] = []
    current_paths = {e.rel_path for e in current}
    for b in baseline:
        if b.rel_path not in current_paths:
            removed.append(b)

    patch_manifest = {
        "created_at": datetime.now().isoformat(),
        "baseline_version": _version_from_manifest(baseline_manifest),
        "current_version": _current_version(),
        "added": [_to_dict(e) for e in added],
        "changed": [_to_dict(e) for e in changed],
        "removed": [e.rel_path for e in removed],
    }

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("PATCH_MANIFEST.json", json.dumps(patch_manifest, ensure_ascii=False, indent=2))
        for e in added + changed:
            zf.write(PROJECT_ROOT / e.rel_path, arcname=f"files/{e.rel_path}")

    _gate_check_or_fail(out_path)

    return {
        "out_path": str(out_path),
        "out_size_bytes": out_path.stat().st_size,
        "added": len(added),
        "changed": len(changed),
        "removed": len(removed),
        "baseline_version": patch_manifest["baseline_version"],
        "current_version": patch_manifest["current_version"],
    }


def _gate_check_or_fail(out_path: Path) -> None:
    """산출물이 사내 반입 게이트에 걸리면 지우고 실패시킨다.

    2026-09-02 교차검증 지적: "반려될 zip 은 애초에 만들어지지 않는다" 는
    make_import_zip 에만 해당했고, 여기(export)는 무검사였다.
    검사기를 못 불러오는 환경(사내 등)에서는 조용히 건너뛴다 — export 는
    사외에서만 돌리므로 사외에서 반드시 걸린다.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from check_import_gate import scan as _gate_scan   # noqa: PLC0415
    except Exception:
        return
    hits = _gate_scan(out_path)
    if not hits:
        return
    out_path.unlink(missing_ok=True)
    lines = [f"  - {rel} ({reason}: {detail})" for rel, reason, detail in hits[:10]]
    raise ValueError(
        "사내 반입 게이트에 걸릴 파일이 있어 산출물을 폐기했습니다 "
        f"({len(hits)}건):\n" + "\n".join(lines)
    )


# ─────────────────────────── APPLY (사내 PC) ───────────────────────────


def apply_patch(zip_path: Path, *, dry_run: bool = False) -> dict:
    """패치 zip 을 적용. data/backups/<ts>/ 에 기존 파일 백업."""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = PROJECT_ROOT / "data" / "backups" / f"patch-{ts}"

    stats: dict = {
        "dry_run": dry_run,
        "patched_files": [],
        "backed_up_files": [],
        "removed_files": [],
        "skipped_outside_whitelist": [],
        "backup_root": str(backup_root),
    }

    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            manifest = json.loads(zf.read("PATCH_MANIFEST.json").decode("utf-8"))
        except KeyError as e:
            raise ValueError("PATCH_MANIFEST.json 없음 — 유효한 패치 zip 아님") from e
        stats["patch_manifest"] = manifest

        members = [n for n in zf.namelist() if n.startswith("files/")]
        for member in members:
            rel = member[len("files/"):]
            if not _is_safe_rel_path(rel):
                stats["skipped_outside_whitelist"].append(rel)
                continue

            dest = PROJECT_ROOT / rel
            # 백업
            if dest.exists() and not dry_run:
                backup_path = backup_root / rel
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, backup_path)
                stats["backed_up_files"].append(rel)
            # 적용
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            stats["patched_files"].append(rel)

        # removed 파일 처리: 백업 후 삭제 (자동 삭제는 보수적으로 — dry-run 만)
        for rel in manifest.get("removed", []):
            if not _is_safe_rel_path(rel):
                continue
            dest = PROJECT_ROOT / rel
            if dest.exists():
                if not dry_run:
                    # 삭제는 결재 단위가 모호하므로 백업만 + 안내
                    backup_path = backup_root / rel
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, backup_path)
                    # 실제 삭제는 사용자 직접 (안전)
                stats["removed_files"].append(rel)

    return stats


# ─────────────────────────── Helpers ───────────────────────────


def _scan_files() -> Iterable[FileEntry]:
    for root in _PATCH_ROOTS:
        root_dir = PROJECT_ROOT / root
        if not root_dir.exists():
            continue
        for p in root_dir.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in _PATCH_EXTENSIONS:
                continue
            if "__pycache__" in p.parts:
                continue
            data = p.read_bytes()
            yield FileEntry(
                rel_path=str(p.relative_to(PROJECT_ROOT)),
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )
    # 루트 레벨 배치파일·README·requirements
    for name in ("install.bat", "run_ingest_drawings.bat", "run_ingest_standards.bat",
                 "run_review.bat", "run_dashboard.bat", "requirements.txt", "README.md"):
        p = PROJECT_ROOT / name
        if p.is_file():
            data = p.read_bytes()
            yield FileEntry(rel_path=name, sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def _load_manifest(path: Optional[Path]) -> list[FileEntry]:
    path = Path(path) if path else PROJECT_ROOT / "deployed_manifest.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [FileEntry(**e) for e in data.get("files", [])]


def _version_from_manifest(path: Optional[Path]) -> str:
    path = Path(path) if path else PROJECT_ROOT / "deployed_manifest.json"
    if not path.exists():
        return "unknown"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("version", "unknown")


def _current_version() -> str:
    v = PROJECT_ROOT / "VERSION.txt"
    if not v.exists():
        v = PROJECT_ROOT / "VERSION"      # 구버전 설치본 호환
    if v.exists():
        return v.read_text(encoding="utf-8").splitlines()[0].strip()
    try:
        from app import __version__
        return __version__
    except Exception:
        return "unknown"


def _is_safe_rel_path(rel: str) -> bool:
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return False
    # 허용 루트 또는 루트 레벨 화이트리스트
    if p.parts and p.parts[0] in _PATCH_ROOTS:
        return p.suffix in _PATCH_EXTENSIONS
    if len(p.parts) == 1 and p.name in {
        "install.bat", "run_ingest_drawings.bat", "run_ingest_standards.bat",
        "run_review.bat", "run_dashboard.bat", "requirements.txt", "README.md",
    }:
        return True
    return False


def _to_dict(e: FileEntry) -> dict:
    return {"rel_path": e.rel_path, "sha256": e.sha256, "size": e.size}


# ─────────────────────────── Manifest write (build time) ───────────────────────────


def write_deployed_manifest(version: str, out_path: Optional[Path] = None) -> Path:
    """build_offline_bundle 가 호출. 현재 소스 트리의 manifest 를 deployed_manifest.json 으로."""
    out_path = out_path or (PROJECT_ROOT / "deployed_manifest.json")
    entries = [_to_dict(e) for e in _scan_files()]
    out_path.write_text(
        json.dumps(
            {"version": version, "created_at": datetime.now().isoformat(), "files": entries},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out_path
