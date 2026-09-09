"""도면 DC 파서도 표 페이지를 멀티모달(HCX-005)로 전사해야 한다 (2026-09-07 사용자: 스캔 DC 1장에서 적용 코드가 3~4개만 나옴).

지금까지 표 전사는 규격(③) 에만 붙어 있었고 도면은 tesseract 글자만 HCX-007 에 넘겼다. 스캔 도면의 작은 표는
tesseract 가 가장 못 읽는 종류라 코드 목록이 빠진다. 규격과 같은 파이프라인(table_pages.page_records)을 공유한다.
그리고 검토자가 결과를 눈으로 대조할 화면(show-drawing)이 없어서 E-9(두께)·코드 개수 확인을 못 했다.
"""
from pathlib import Path

from app.extractors.pdf_extractor import ExtractedPDF, PageText
from app.extractors.drawing import dc_parser


class _Resp:
    def __init__(self, parsed):
        self.parsed = parsed


def _extracted(path):
    return ExtractedPDF(path=Path(path), pages=[
        PageText(page_index=0, text="ocr garbage 1", source="ocr", ocr_variants=["ocr garbage 1"]),
        PageText(page_index=1, text="plain page 2", source="ocr", ocr_variants=["plain page 2"]),
    ], text_layer_present=False)


def test_parse_dc_feeds_vlm_table_markdown_and_flags_review(monkeypatch, tmp_path):
    pdf = tmp_path / "x.DC.0001.E.pdf"; pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(dc_parser, "extract", lambda p: _extracted(p))
    monkeypatch.setattr(dc_parser, "_extract_priority_tables", lambda p: {
        "inspection_scope_tables": [], "safety_class_tables": [], "other_tables": []})
    monkeypatch.setattr(dc_parser, "page_records", lambda pdf_path, extracted, **kw: ([
        {"page": 1, "text": "| Code | Title |\n|---|---|\n| NP-001-15 | Safety |\n| PNAE G-7-010-89 | Welding |",
         "source": "vlm_table", "confidence": 0.85, "needs_review": True, "table_score": 0.8, "table_model": "HCX-005"},
        {"page": 2, "text": "plain page 2", "source": "ocr", "confidence": 0.7},
    ], {"pages": 2, "table_like": 1, "transcribed": 1, "needs_review": 1, "vlm_failed": 0}))
    seen = {}

    def fake_call(stage, payload, **kw):
        seen["stage"] = stage; seen["payload"] = payload
        return _Resp({"applicable_codes": [{"code": "NP-001-15", "page_in_drawing": 1}],
                      "needs_review": False, "review_reasons": []})
    monkeypatch.setattr(dc_parser, "call", fake_call)

    out = dc_parser.parse_dc(pdf, drawing_no="NP.D.N000.1", revision="C03")
    assert seen["stage"] == "drawing_dc"
    assert "PNAE G-7-010-89" in seen["payload"]["text_full"]          # 전사된 표가 LLM 입력에 들어간다
    assert "---PAGE 1---" in seen["payload"]["text_full"]
    assert out["_pages"][0]["source"] == "vlm_table" and out["_pages"][1]["source"] == "ocr"
    assert out["_table_stats"]["transcribed"] == 1
    assert out["needs_review"] is True
    assert any("표 전사 재확인" in r and "p.1" in r for r in out["review_reasons"])


def test_code_indexer_still_uses_shared_page_records():
    from app.extractors import code_indexer, table_pages
    assert code_indexer.page_records is table_pages.page_records


def test_show_drawing_lists_codes_pages_and_thickness(fresh_db):
    from typer.testing import CliRunner
    from app.main import app
    models, _ = fresh_db
    with models.get_session() as s:
        ds = models.DrawingSet(drawing_no="NP.D.N000.2.0UMA&&PAB&&&.021", set_revision="DC=C02", has_dc=True)
        s.add(ds); s.flush()
        s.add(models.DrawingFile(
            file_path="C:/x/[ABD]NP.D.N000.2.DC.0001.E_C02.pdf", file_hash="h", drawing_no=ds.drawing_no,
            drawing_type="DC", revision="C02", set_id=ds.id,
            extracted_json={"applicable_codes": [{"code": "NP-001-15", "page_in_drawing": 8},
                                                 {"code": "SN 527-80", "page_in_drawing": 8}],
                            "_pages": [{"page": 8, "source": "vlm_table", "table_score": 0.8, "needs_review": True},
                                       {"page": 9, "source": "ocr"}],
                            "_table_stats": {"pages": 2, "table_like": 1, "transcribed": 1, "needs_review": 1, "vlm_failed": 0}}))
        s.add(models.Requirement(drawing_set_id=ds.id, joint_no="20PAB02BR005", line_no="20PAB02BR005",
                                 thickness_mm=7.1, safety_class="4"))
        s.commit()
    r = CliRunner().invoke(app, ["show-drawing"])
    assert r.exit_code == 0, r.output
    for needle in ("NP.D.N000.2.0UMA&&PAB&&&.021", "NP-001-15", "SN 527-80", "p.8", "20PAB02BR005", "7.1", "vlm_table", "적용 코드 2"):
        assert needle in r.output, needle


def test_parse_dc_invalidates_unsupported_matrix_values(monkeypatch, tmp_path):
    """사내 사례: 검사표가 전부 100 으로 들어왔다. 원문에 100 이 없으면 비우고 재확인으로."""
    from app.extractors.drawing import dc_parser
    pdf = tmp_path / "y.DC.0001.E.pdf"; pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(dc_parser, "extract", lambda p: _extracted(p))
    monkeypatch.setattr(dc_parser, "_extract_priority_tables", lambda p: {
        "inspection_scope_tables": [], "safety_class_tables": [], "other_tables": []})
    monkeypatch.setattr(dc_parser, "page_records", lambda pdf_path, extracted, **kw: ([
        {"page": 1, "text": "Methods and scope of welded joint inspection SN 527-80", "source": "ocr"},
        {"page": 2, "text": "plain", "source": "ocr"},
    ], {"pages": 2, "table_like": 1, "transcribed": 0, "needs_review": 0, "vlm_failed": 1}))
    monkeypatch.setattr(dc_parser, "call", lambda stage, payload, **kw: _Resp({
        "inspection_matrix": [{"kks_code": "10PAB02BR005", "page_in_drawing": 1,
                               "vt_pct": 100, "pt_or_mt_pct": 100, "rt_pct": 100, "ut_pct": 100}],
        "needs_review": False, "review_reasons": []}))
    out = dc_parser.parse_dc(pdf, drawing_no="NP.D.N000.1", revision="C02")
    m = out["inspection_matrix"][0]
    assert all(m[k] is None for k in ("vt_pct", "pt_or_mt_pct", "rt_pct", "ut_pct"))
    assert out["needs_review"] is True
    assert any("근거 없음" in r for r in out["review_reasons"])
