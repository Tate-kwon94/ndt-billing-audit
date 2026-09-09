"""디버그 번들 — 사내 PC 에서 발생한 문제를 사외(mac) 에서 재현하기 위한 도구.

워크플로우:
  사내 PC :  python -m app.main debug-bundle export --out debug.zip
            (로그 + LLM 캐시 + 환경 정보 + SQLite 사본 + 설정 — 토큰 제외)
  사외 PC :  python -m app.main debug-bundle import debug.zip
            (캐시·로그·DB 사본을 로컬에 풀어서 동일 입력 재현 가능하게)

도면/성적서 원본 PDF 는 기본 포함하지 않음 (사내 정책상 도면 반출은 지양).
필요 시 --include-pdfs 로 명시 포함.
"""
from __future__ import annotations

import hashlib
import json
import logging
import platform
import shutil
import socket
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import CONFIG_DIR, DATA_DIR, PROJECT_ROOT

logger = logging.getLogger(__name__)

# 토큰·민감 ENV 차단 목록
_REDACT_ENV_KEYS = {"NDT_HCX_TOKEN", "TOKEN", "API_KEY", "PASSWORD", "SECRET"}

# 반출 제외 캐시 stage — 응답에 도면 기준(요구NDT·두께·조항 인용 등)이 포함되는 stage.
# 사내 정책 "도면 반출 지양": LLM 응답을 통한 간접 반출도 차단.
_SENSITIVE_CACHE_STAGES = {
    "drawing_dc", "drawing_sd", "drawing_bg", "drawing_combine",
    
    "compliance_explain", "scwep_extract", "code_lookup", "contract_extract",
}


# ─────────────────────────── EXPORT (사내 PC) ───────────────────────────


def export_bundle(
    out_path: Path,
    *,
    include_pdfs: bool = False,
    include_sqlite: bool = False,
    include_sensitive_cache: bool = False,
    days_of_logs: int = 7,
) -> dict:
    """디버그 번들 zip 생성. 통계 dict 반환.

    LLM 캐시는 (1) 도면 기준이 담기는 민감 stage 제외 (include_sensitive_cache=True 로 override),
    (2) 원본 API 응답(_api) 제거 후 content 만 반출. stage 미기록 구캐시는 보수적으로 제외.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    stats: dict = {
        "files_added": 0,
        "bytes_added": 0,
        "skipped_pdfs": 0,
        "log_files": 0,
        "cache_files": 0,
        "cache_skipped_sensitive": 0,
        "cache_skipped_unknown_stage": 0,
    }

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1) manifest + environment
        env = _collect_environment()
        _write_bytes(zf, "MANIFEST.json", json.dumps(env, ensure_ascii=False, indent=2).encode("utf-8"), stats)

        # 2) 설정 파일 (토큰 제외)
        for cfg in CONFIG_DIR.rglob("*"):
            if cfg.is_file():
                content = _redact_yaml_if_needed(cfg)
                arcname = f"config/{cfg.relative_to(CONFIG_DIR)}"
                _write_bytes(zf, arcname, content, stats)

        # 3) 로그 (최근 N일)
        log_root = DATA_DIR / "logs"
        if log_root.exists():
            for log_file in log_root.rglob("*.log*"):
                if _file_age_days(log_file) <= days_of_logs:
                    arcname = f"data/logs/{log_file.relative_to(log_root)}"
                    _add_file(zf, log_file, arcname, stats)
                    stats["log_files"] += 1

        # 4) LLM 캐시 — mac 에서 동일 호출 시 cache HIT 으로 사내 응답 재현.
        #    단, 도면 기준이 담기는 민감 stage 는 기본 제외 + _api(원본응답) 제거.
        cache_root = DATA_DIR / "llm_cache"
        if cache_root.exists():
            for cache_file in cache_root.rglob("*.json"):
                try:
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                except Exception:
                    stats["cache_skipped_unknown_stage"] += 1
                    continue
                stage = data.get("stage")
                if stage is None:
                    # stage 미기록 구캐시 — 내용 분류 불가 → 보수적으로 제외
                    stats["cache_skipped_unknown_stage"] += 1
                    continue
                if stage in _SENSITIVE_CACHE_STAGES and not include_sensitive_cache:
                    stats["cache_skipped_sensitive"] += 1
                    continue
                sanitized = {k: v for k, v in data.items() if k != "_api"}
                arcname = f"data/llm_cache/{cache_file.relative_to(cache_root)}"
                _write_bytes(
                    zf, arcname,
                    json.dumps(sanitized, ensure_ascii=False).encode("utf-8"),
                    stats,
                )
                stats["cache_files"] += 1

        # 5) SQLite 사본 (옵션)
        if include_sqlite:
            db = DATA_DIR / "ndt.sqlite"
            if db.exists():
                _add_file(zf, db, "data/ndt.sqlite", stats)

        # 6) 출력 산출물 (옵션 X — 일반적으로 검토 엑셀은 사내에 둠)
        # 7) 도면/성적서 PDF — 정책상 지양, 명시할 때만
        if include_pdfs:
            for samples in (DATA_DIR / "samples",):
                if samples.exists():
                    for pdf in samples.rglob("*.pdf"):
                        _add_file(zf, pdf, f"data/samples/{pdf.relative_to(samples)}", stats)
        else:
            # 통계만 산정
            samples = DATA_DIR / "samples"
            if samples.exists():
                stats["skipped_pdfs"] = sum(1 for _ in samples.rglob("*.pdf"))

    stats["out_path"] = str(out_path)
    stats["out_size_bytes"] = out_path.stat().st_size
    return stats


# ─────────────────────────── IMPORT (사외 mac) ───────────────────────────


def import_bundle(
    zip_path: Path,
    *,
    overwrite: bool = False,
    target_root: Optional[Path] = None,
) -> dict:
    """디버그 번들을 풀어서 mac 의 data/ 와 config/ 로 복원.

    overwrite=False (기본): data/logs/ 와 data/llm_cache/ 에 import-* 폴더로 격리.
    overwrite=True: 기존 파일을 덮어씀 (mac 로컬 작업 내용을 잃을 수 있음).
    """
    zip_path = Path(zip_path)
    target_root = Path(target_root or PROJECT_ROOT)
    stats: dict = {"extracted_files": 0, "cache_files": 0, "log_files": 0, "skipped": 0}

    suffix = "" if overwrite else datetime.now().strftime("_import-%Y%m%d-%H%M%S")

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.namelist()
        for member in members:
            if member.endswith("/"):
                continue

            # 격리 import 경로 매핑
            if not overwrite and member.startswith("data/llm_cache/"):
                rel = member.replace("data/llm_cache/", "")
                dest = target_root / "data" / f"llm_cache{suffix}" / rel
            elif not overwrite and member.startswith("data/logs/"):
                rel = member.replace("data/logs/", "")
                dest = target_root / "data" / f"logs{suffix}" / rel
            elif not overwrite and member == "data/ndt.sqlite":
                dest = target_root / "data" / f"ndt{suffix}.sqlite"
            elif not overwrite and member.startswith("config/"):
                rel = member.replace("config/", "")
                dest = target_root / f"config_imported{suffix}" / rel
            else:
                dest = target_root / member

            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)

            stats["extracted_files"] += 1
            if "llm_cache" in member:
                stats["cache_files"] += 1
            elif "logs" in member:
                stats["log_files"] += 1

    # 안내: cache 격리 import 시 사용법
    note = ""
    if not overwrite:
        cache_dir = target_root / "data" / f"llm_cache{suffix}"
        if cache_dir.exists():
            note = (
                f"\nLLM 캐시 격리 import: {cache_dir}\n"
                f"동일 입력 재현하려면 cache 디렉토리를 활성화하세요:\n"
                f"  export NDT_LLM_CACHE_DIR='{cache_dir}'\n"
                f"또는 config/hcx.yaml 의 cache.directory 를 수정."
            )
    stats["note"] = note
    return stats


# ─────────────────────────── Helpers ───────────────────────────


def _collect_environment() -> dict:
    try:
        from app.logging_setup import _config_fingerprint, _package_versions_brief
        cfg_fp = _config_fingerprint()
        pkg_ver = _package_versions_brief()
    except Exception:
        cfg_fp = {}
        pkg_ver = {}

    return {
        "bundle_created_at": datetime.now().isoformat(),
        "host": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": sys.version.replace("\n", " "),
        "config_hashes": cfg_fp,
        "package_versions": pkg_ver,
        "app_version": _app_version(),
        "purpose": "사내 → 사외 재현용. LLM 캐시 hit 로 동일 입력에 동일 응답 재생산.",
        "redacted": list(_REDACT_ENV_KEYS),
    }


def _app_version() -> str:
    vfile = PROJECT_ROOT / "VERSION.txt"
    if not vfile.exists():
        vfile = PROJECT_ROOT / "VERSION"      # 구버전 설치본 호환
    if vfile.exists():
        return vfile.read_text(encoding="utf-8").splitlines()[0].strip()
    try:
        from app import __version__
        return __version__
    except Exception:
        return "unknown"


def _file_age_days(p: Path) -> float:
    return (datetime.now().timestamp() - p.stat().st_mtime) / 86400.0


def _add_file(zf: zipfile.ZipFile, path: Path, arcname: str, stats: dict):
    zf.write(path, arcname=arcname)
    stats["files_added"] += 1
    stats["bytes_added"] += path.stat().st_size


def _write_bytes(zf: zipfile.ZipFile, arcname: str, content: bytes, stats: dict):
    zf.writestr(arcname, content)
    stats["files_added"] += 1
    stats["bytes_added"] += len(content)


def _redact_yaml_if_needed(path: Path) -> bytes:
    """YAML 설정에서 토큰 관련 키를 마스킹.

    실제 토큰은 환경변수로 주입되므로 파일에는 거의 없지만,
    혹시 실수로 하드코딩됐을 가능성을 차단.
    """
    raw = path.read_bytes()
    if path.suffix not in (".yaml", ".yml"):
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    # 단순 라인 기반 마스킹 (안전 우선)
    out_lines: list[str] = []
    for line in text.splitlines():
        lower = line.lower()
        if any(k in lower for k in ("token:", "api_key:", "password:", "secret:")):
            head = line.split(":", 1)[0]
            out_lines.append(f"{head}: <REDACTED_BY_DEBUG_BUNDLE>")
        else:
            out_lines.append(line)
    return ("\n".join(out_lines) + "\n").encode("utf-8")
