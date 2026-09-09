"""구조화 파일 로깅 — 사내에서 발생한 모든 일을 mac 에서 재현 가능하게 기록.

- 매 실행마다 run_id (timestamp + 짧은 uuid) 생성
- data/logs/YYYY-MM-DD/{run_id}.log 에 파일 출력 + 콘솔에도 INFO 이상
- 환경 정보(OS, Python, 패키지 버전, 설정 파일 해시) 첫 줄에 기록
- 예외는 traceback 전체 기록

사용:
    from app.logging_setup import configure_logging
    run_id = configure_logging(stage="review")
    logger = logging.getLogger(__name__)
    logger.info("...")
"""
from __future__ import annotations

import getpass
import hashlib
import logging
import logging.handlers
import os
import platform
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import CONFIG_DIR, DATA_DIR

_LOGGER_NAME = "ndt"


def _today_dir() -> Path:
    d = DATA_DIR / "logs" / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_fingerprint() -> dict:
    fp: dict = {}
    for name in ("hcx.yaml", "templates.yaml", "matching_rules.yaml"):
        path = CONFIG_DIR / name
        if path.exists():
            h = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            fp[name] = h
    return fp


def configure_logging(*, stage: str = "default", verbose: bool = False) -> str:
    """루트 로거 설정 + 환경 헤더 기록. run_id 반환.

    여러 번 호출돼도 root handler 중복 등록되지 않게 idempotent.
    """
    run_id = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{stage}-{uuid.uuid4().hex[:6]}"
    log_path = _today_dir() / f"{run_id}.log"

    root = logging.getLogger()
    # 기존 NDT handler 제거 (재설정 시)
    for h in list(root.handlers):
        if getattr(h, "_ndt_handler", False):
            root.removeHandler(h)

    root.setLevel(logging.DEBUG)

    # 시끄러운 3rd-party 로거 quiet — PDF 파싱/HTTP 디버그가 로그를 점령하지 않게
    for noisy in ("pdfminer", "pdfminer.psparser", "pdfminer.pdfinterp",
                  "pdfminer.cmapdb", "pdfminer.pdfparser",
                  "httpcore", "httpx", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_h = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(fmt)
    file_h._ndt_handler = True  # type: ignore[attr-defined]
    root.addHandler(file_h)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    console._ndt_handler = True  # type: ignore[attr-defined]
    root.addHandler(console)

    # 헤더 — 사내 환경 정보를 기록해 mac 에서 재현 시 비교 가능
    logger = logging.getLogger(_LOGGER_NAME)
    logger.info("─" * 80)
    logger.info("NDT Assistant run started")
    logger.info("  run_id        : %s", run_id)
    logger.info("  stage         : %s", stage)
    logger.info("  log_path      : %s", log_path)
    logger.info("  host          : %s", socket.gethostname())
    logger.info("  user          : %s", _safe_user())
    logger.info("  os            : %s %s (%s)", platform.system(), platform.release(), platform.machine())
    logger.info("  python        : %s", sys.version.replace("\n", " "))
    logger.info("  cwd           : %s", os.getcwd())
    logger.info("  config_hashes : %s", _config_fingerprint())
    logger.info("  pkg_versions  : %s", _package_versions_brief())
    logger.info("  version       : %s", _app_version())
    logger.info("─" * 80)

    # 환경변수에 노출해 다른 모듈이 동일 run_id 사용 가능하게
    os.environ["NDT_RUN_ID"] = run_id
    return run_id


def get_run_id() -> str:
    return os.environ.get("NDT_RUN_ID", "")


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _package_versions_brief() -> dict:
    pkgs = ("httpx", "openpyxl", "pdfplumber", "pypdfium2", "pytesseract",
            "rapidfuzz", "sqlalchemy", "streamlit", "typer", "tenacity")
    out = {}
    for name in pkgs:
        try:
            mod = __import__(name)
            out[name] = getattr(mod, "__version__", "?")
        except Exception:
            out[name] = "missing"
    return out


def _app_version() -> str:
    """VERSION 파일이 있으면 첫 줄, 없으면 패키지 __version__."""
    _root = Path(__file__).resolve().parent.parent
    vfile = _root / "VERSION.txt"
    if not vfile.exists():
        vfile = _root / "VERSION"            # 구버전 설치본 호환
    if vfile.exists():
        try:
            return vfile.read_text(encoding="utf-8").splitlines()[0].strip()
        except Exception:
            pass
    try:
        from app import __version__
        return __version__
    except Exception:
        return "unknown"
