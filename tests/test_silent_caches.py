"""감사 ⑥⑦⑧: 고쳐도 영구 재사용되는 OCR 실패 캐시, mock 답이 live 로 돌아오는 LLM 캐시,
헤더 못 읽는 양식이 매 페이지 새 성적서(0.95) 가 되는 분할."""
from __future__ import annotations

from pathlib import Path

from app import hcx_client
from app.extractors import pdf_extractor, report_segmenter


def _pdf(tmp_path) -> Path:
    p = tmp_path / "a.pdf"; p.write_bytes(b"%PDF-1.4 fake"); return p


def test_ocr_cache_key_changes_with_backend(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path)
    p = _pdf(tmp_path)
    monkeypatch.setenv("NDT_OCR_BACKEND", "tesseract"); k1 = pdf_extractor._ocr_cache_path(p)
    monkeypatch.setenv("NDT_OCR_BACKEND", "paddle");    k2 = pdf_extractor._ocr_cache_path(p)
    assert k1 != k2


def test_failed_ocr_pages_are_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path)
    p = _pdf(tmp_path)
    bad = pdf_extractor.ExtractedPDF(path=p, pages=[pdf_extractor._empty_ocr_page(0, "no tesseract")],
                                     text_layer_present=False)
    pdf_extractor._save_ocr_cache(p, bad)
    assert pdf_extractor._load_ocr_cache(p) is None


def test_llm_cache_key_separates_mock_and_live(monkeypatch):
    monkeypatch.setenv("NDT_HCX_MOCK", "1"); k_mock = hcx_client._cache_key("m", "p", {"a": 1})
    monkeypatch.setenv("NDT_HCX_MOCK", "0"); k_live = hcx_client._cache_key("m", "p", {"a": 1})
    assert k_mock != k_live


def test_empty_prev_meta_is_below_review_threshold(monkeypatch):
    monkeypatch.setattr(report_segmenter, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM 호출 금지")))
    is_new, conf = report_segmenter._decide_boundary(prev_header="", curr_header="", prev_meta={}, curr_meta={}, page_index=3)
    assert is_new is True and conf < 0.7
