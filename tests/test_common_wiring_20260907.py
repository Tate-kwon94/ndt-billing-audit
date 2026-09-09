"""2026-09-07 "다른 곳에도 같은 사례 없나" 감사 → 사용자 승인 5건.

1. 성적서도 표 페이지 전사 — 대체가 아니라 OCR 변형본 하나를 더한다. 표 점수는 디스크 캐시.
2. SCWEP 도 표 페이지 전사 (규격처럼 전사 + OCR 꼬리로 대체).
3. vision 검증 기본 on + 파이프라인이 실제로 vision_fallback 을 넘긴다 (지금은 안 넘겨서 켜도 안 돈다).
4. 도면 추출 형식 검증기(validate_drawing_extract)를 combine 뒤에 호출해 재확인 사유로.
5. 코드 없는 vision stage 설정 삭제, 중복 validate_ocr_normalize 삭제.
"""
from pathlib import Path
from types import SimpleNamespace

from app.extractors.pdf_extractor import ExtractedPDF, PageText


def _extracted(path, n=2):
    return ExtractedPDF(path=Path(path), pages=[
        PageText(page_index=i, text=f"ocr text {i+1}", source="ocr", ocr_variants=[f"ocr text {i+1}", f"ocr alt {i+1}"])
        for i in range(n)], text_layer_present=False)


# ── 1a. 표 점수 디스크 캐시 ──────────────────────────────────────────────────

def test_table_score_is_cached_on_disk(monkeypatch, tmp_path):
    from PIL import Image
    from app.extractors import table_pages, table_transcriber as tt
    monkeypatch.setattr(table_pages, "DATA_DIR", tmp_path)
    monkeypatch.setattr(tt, "config", lambda: {**tt._DEFAULTS, "enabled": True, "min_table_score": 0.99})
    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF")
    calls = []
    monkeypatch.setattr(tt, "table_score", lambda img, lang=None: (calls.append(1), {"score": 0.1, "gappy_lines": 0, "multiword_lines": 9})[1])
    render = lambda p, i, dpi: Image.new("L", (10, 10), 255)
    table_pages.page_records(pdf, _extracted(pdf), render=render)
    table_pages.page_records(pdf, _extracted(pdf), render=render)
    assert len(calls) == 2, "두 번째 실행은 캐시에서 점수를 읽어야 한다 (성적서 1,167쪽 재계산 방지)"
    assert list((tmp_path / "table_score_cache").glob("*.json"))


# ── 1b. 성적서: 전사 결과가 변형본으로 추가되고 재확인 사유가 붙는다 ──────────

def test_report_ingest_adds_vlm_markdown_as_extra_variant(monkeypatch, fresh_db, tmp_path):
    from app.extractors import report_segmenter as rs
    pdf = tmp_path / "reports.pdf"; pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(rs, "extract", lambda p: _extracted(p))
    monkeypatch.setattr(rs, "page_records", lambda pdf_path, extracted, **kw: ([
        {"page": 1, "text": "| Joint | Record |\n|---|---|\n| 20PAB02BR005 | 12-056 |", "source": "vlm_table",
         "confidence": 0.85, "needs_review": True, "table_score": 0.7},
        {"page": 2, "text": "ocr text 2", "source": "ocr"},
    ], {"pages": 2, "table_like": 1, "transcribed": 1, "needs_review": 1, "vlm_failed": 0}))
    seen = {}

    def fake_normalize(*, report_id, billing_round_meta, pages_text, ocr_variants_per_page=None):
        seen["pages_text"] = pages_text; seen["variants"] = ocr_variants_per_page
        return {"report_no": "12-056", "extraction_confidence": 0.9}
    monkeypatch.setattr(rs, "normalize_report", fake_normalize)
    saved_kwargs = {}
    monkeypatch.setattr(rs, "add_inspection_report", lambda session, **kw: saved_kwargs.update(kw), raising=False)
    import app.database.repository as repo
    monkeypatch.setattr(repo, "add_inspection_report", lambda session, **kw: saved_kwargs.update(kw))

    seg = rs.ReportSegment(start_page=0, end_page=1, tentative_id="12-056", segmentation_confidence=0.9,
                           meta={"report_no": "12-056"})
    models, _ = fresh_db
    with models.get_session() as s:
        n = rs.normalize_and_ingest_segments(pdf, [seg], billing_round_meta={"id": 1}, session=s)
    assert n == 1
    assert seen["pages_text"][0] == "ocr text 1"                       # OCR 본문은 그대로
    assert any("20PAB02BR005" in v for v in seen["variants"][0])       # 전사가 변형본으로 추가
    assert "ocr text 1" in seen["variants"][0]
    reasons = (saved_kwargs.get("review_reasons_json") or {}).get("reasons") or []
    assert any("표 전사 재확인" in r and "p.1" in r for r in reasons)


# ── 2. SCWEP: 표 페이지는 전사 + OCR 꼬리로 대체 ────────────────────────────

def test_scwep_window_uses_vlm_markdown_for_table_pages(monkeypatch, tmp_path):
    from app.extractors import scwep_parser as sp
    pdf = tmp_path / "NP.KE.0001.E.pdf"; pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(sp, "extract", lambda p: _extracted(p))
    monkeypatch.setattr(sp, "page_records", lambda pdf_path, extracted, **kw: ([
        {"page": 1, "text": "| Weld | VT | PT |\n|---|---|---|\n| all | 100% | 100% |\n\n[OCR]\nocr text 1",
         "source": "vlm_table", "confidence": 0.9, "needs_review": False},
        {"page": 2, "text": "ocr text 2", "source": "ocr"},
    ], {"pages": 2, "table_like": 1, "transcribed": 1, "needs_review": 0, "vlm_failed": 0}))
    monkeypatch.setattr(sp, "select_pages", lambda pages, **kw: [0, 1])
    seen = []
    monkeypatch.setattr(sp, "call", lambda stage, payload, **kw: (seen.append(payload), SimpleNamespace(parsed=None))[1])
    sp.ingest(pdf)
    assert seen and "| Weld | VT | PT |" in seen[0]["text_full"]


# ── 3. vision 검증 기본 on + 파이프라인이 fallback 을 넘긴다 ────────────────

def test_vision_verify_enabled_by_default(monkeypatch):
    from app.extractors import vision_verifier as vv
    monkeypatch.delenv("NDT_VISION_VERIFY", raising=False)
    assert vv.is_enabled() is True
    monkeypatch.setenv("NDT_VISION_VERIFY", "0")
    assert vv.is_enabled() is False


def test_pipeline_builds_vision_fallback_from_report_row():
    from app.analyzers import pipeline
    rep = SimpleNamespace(source_pdf="C:/x/reports.pdf", start_page=41, report_no="12-056")
    fb = pipeline._vision_fallback_for(rep)
    assert fb["pdf_path"] == Path("C:/x/reports.pdf") and fb["page_index"] == 41
    assert "12-056" in fb["context_hint"]
    assert pipeline._vision_fallback_for(None) is None


# ── 4. 도면 형식 검증기 → 재확인 사유 ───────────────────────────────────────

def test_drawing_format_violations_become_review_reasons():
    from app.extractors.drawing import requirements_extractor as rx
    from app.extractors.drawing import grouper, classifier as clsmod
    ds = grouper.DrawingSet(drawing_no="X", revision=None, files={"DC": clsmod.Classification(
        drawing_type="DC", drawing_no="X", revision="C01", confidence=0.99, source="filename")})
    combined = {"drawing_no": "NOT-A-DRAWING-NO", "extraction_confidence": 0.9, "joints": []}
    dc = {"kks_lines": [{"kks_code": "20PAB02BR005", "safety_class_np_001_15": "9"}]}   # 안전등급 9 = 없는 값
    reasons = rx._aggregate_set_review_reasons(ds, combined, dc_result=dc)
    assert any("형식 검증" in r for r in reasons), reasons
    assert any("safety_class" in r or "안전등급" in r for r in reasons)


# ── 5. 죽은 설정·중복 함수 제거 ─────────────────────────────────────────────

def test_dead_vision_stages_removed_from_config():
    import yaml
    cfg = yaml.safe_load(Path("config/hcx.yaml").read_text(encoding="utf-8"))
    for dead in ("ocr_normalize_vision", "drawing_dc_vision", "isometric_vision", "scan_table_vision"):
        assert dead not in (cfg.get("stage_overrides") or {}), dead


def test_duplicate_validator_removed():
    from app.analyzers import rule_engine
    assert not hasattr(rule_engine, "validate_ocr_normalize")
