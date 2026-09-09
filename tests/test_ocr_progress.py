"""병렬 OCR 진행률 — 1,167쪽을 세 시간 돌리는 동안 로그 한 줄도 없으면 비개발자는 멈춘 줄 안다 (2026-09-07 스모크).

기존 코드는 `list(ex.map(...))` 뒤에 한꺼번에 기록했다. 이제 페이지가 끝날 때마다 세고,
10% 단위(최소 1쪽)로 progress_fmt 형식의 "진행 [OCR] k/n쪽" 을 INFO 로 남긴다. 결과 순서는 그대로 page_index 로 정렬한다.
"""
from __future__ import annotations

import logging
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


@pytest.mark.skipif(not pdf_extractor._tesseract_available(), reason="tesseract 없음")
def test_parallel_ocr_logs_progress_and_keeps_page_order(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("NDT_OCR_WORKERS", "2")
    pdf = _blank_pdf(tmp_path / "blank3.pdf", 3)
    with caplog.at_level(logging.INFO, logger="app.extractors.pdf_extractor"):
        pages = pdf_extractor._extract_via_ocr(pdf)
    assert [p.page_index for p in pages] == [0, 1, 2]
    msgs = [r.getMessage() for r in caplog.records]
    from app.gui import progress_parse as pp
    got = [pp.parse_progress(m) for m in msgs]
    assert any(p and p.stage == "OCR" and p.k == p.n == 3 for p in got), msgs
