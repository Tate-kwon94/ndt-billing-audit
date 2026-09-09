"""pdf_extractor 의 tesseract 가용성 판정은 ocr_engine 과 같은 답을 내야 한다.

2026-09-04 감사: ocr-check(ocr_engine.resolve_tesseract = env→번들→PATH) 는 ✓ 인데
pdf_extractor._tesseract_available(env→PATH 만) 은 ✗ → 실제 추출이 [OCR_UNAVAILABLE] 1장.
"""
from __future__ import annotations

from app.extractors import ocr_engine, pdf_extractor


def test_bundled_tesseract_counts_as_available(monkeypatch):
    monkeypatch.delenv("NDT_TESSERACT_CMD", raising=False)
    monkeypatch.setattr(pdf_extractor, "_tesseract_check_cache", None, raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)          # PATH 에는 없다
    monkeypatch.setattr(ocr_engine, "resolve_tesseract",
                        lambda: ("/bundle/installer/tesseract/tesseract", "/bundle/tessdata"))
    assert pdf_extractor._tesseract_available() is True


def test_unavailable_everywhere(monkeypatch):
    monkeypatch.delenv("NDT_TESSERACT_CMD", raising=False)
    monkeypatch.setattr(pdf_extractor, "_tesseract_check_cache", None, raising=False)
    monkeypatch.setattr(ocr_engine, "resolve_tesseract", lambda: (None, None))
    assert pdf_extractor._tesseract_available() is False
