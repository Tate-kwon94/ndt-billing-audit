"""병렬 OCR 워커가 tesseract 경로를 모른다 (2026-09-07 사내 ② 실패: 'Invalid tesseract version: ""').

Windows 의 ProcessPoolExecutor 는 spawn 이라 워커가 새 인터프리터로 뜬다. 부모가 resolve_tesseract() 로
잡아 둔 pytesseract.tesseract_cmd(번들 경로)는 워커에 없고, 워커는 PATH 의 'tesseract' 를 찾다가 죽는다.
사외(macOS)는 PATH 에 tesseract 가 있어 이 경로가 우연히 통과했다.

재현: 부모는 NDT_TESSERACT_CMD 로 실제 경로를 알고, PATH 에서는 tesseract 를 뺀다 (사내와 같은 조건 —
번들 경로는 부모만 안다). macOS 도 3.8+ 는 spawn 이라 워커는 새로 뜬다.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app.extractors import pdf_extractor


def _blank_pdf(path: Path, n: int) -> Path:
    import pypdfium2 as pdfium
    d = pdfium.PdfDocument.new()
    for _ in range(n):
        d.new_page(300, 200)
    d.save(str(path))
    return path


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract 없음")
def test_parallel_workers_find_tesseract_without_path(tmp_path, monkeypatch):
    real = shutil.which("tesseract")
    tess_dir = str(Path(real).parent)
    stripped = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p != tess_dir)
    monkeypatch.setenv("PATH", stripped)                    # 워커가 PATH 로는 못 찾게
    monkeypatch.setenv("NDT_TESSERACT_CMD", real)           # 부모(와 워커)는 이 경로로만 알 수 있다
    monkeypatch.setenv("NDT_OCR_WORKERS", "2")
    monkeypatch.setattr(pdf_extractor, "_tesseract_check_cache", None, raising=False)
    assert shutil.which("tesseract") is None                # 재현 조건 확인
    pdf = _blank_pdf(tmp_path / "blank2.pdf", 2)
    pages = pdf_extractor._extract_via_ocr(pdf)             # 워커가 죽으면 여기서 예외
    assert [p.page_index for p in pages] == [0, 1]
    assert all(p.source != "ocr_unavailable" and "Invalid tesseract" not in (p.text or "") for p in pages)


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract 없음")
def test_worker_crash_falls_back_to_serial_and_logs_reason(tmp_path, monkeypatch, caplog):
    """사내: 워커 안의 pytesseract 가 SystemExit('Invalid tesseract version: ""') 로 죽어 ② 전체가 실패했다.
    워커가 어떤 이유로든 죽으면 그 문서는 직렬 OCR 로 마저 하고, 이유를 경고로 남긴다 (조용히 죽지 않는다)."""
    import logging
    monkeypatch.setenv("NDT_OCR_WORKERS", "2")
    monkeypatch.setenv("NDT_OCR_TEST_FAIL_WORKER", "1")     # 워커 프로세스에서만 SystemExit 를 흉내낸다
    monkeypatch.setattr(pdf_extractor, "_tesseract_check_cache", None, raising=False)
    pdf = _blank_pdf(tmp_path / "blank2.pdf", 2)
    with caplog.at_level(logging.WARNING, logger="app.extractors.pdf_extractor"):
        pages = pdf_extractor._extract_via_ocr(pdf)
    assert [p.page_index for p in pages] == [0, 1]
    assert all(p.source != "ocr_unavailable" for p in pages)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "직렬" in msgs and "Invalid tesseract version" in msgs


def test_worker_diagnostics_report_tesseract_environment():
    """워커가 죽으면 부모가 이 진단을 로그에 남긴다 — 사내 원인(경로·PATH·--version 원문)을 다음 실행에서 본다."""
    d = pdf_extractor._worker_diagnostics()
    assert set(d) >= {"tesseract_cmd", "tessdata_prefix", "path_has_cmd_dir", "version_rc", "version_raw"}
    assert d["tesseract_cmd"]
    assert isinstance(d["version_raw"], str)
