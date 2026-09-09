"""표 전사 정확도 벤치 — 정답표(도면1 p.13)로 모델을 재는 도구 (2026-09-07).

'멀티모달이 낫다' 는 사외에서 **제가** 읽은 결과일 뿐, 사내 HCX-005 가 같은지는 미확인이다.
정답이 있으니 재면 된다. 비전 가능한 모델이 여럿이면(vision-probe 로 확인) 전부 같은 표로 비교한다.
"""
from pathlib import Path

import pytest

import app.hcx_client as hc_mod

import app.hcx_client as hc_mod
from app import table_bench as tb

TRUTH = Path("data/정답표/표전사_정답_도면1_p13.json")


def test_truth_file_shape():
    t = tb.load_truth(TRUTH)
    assert len(t["rows"]) == 10
    assert t["rows"][0]["kks"] == "10AAA02BR005"
    assert all(r["ut_pct"] == "100" and r["pt_or_mt_pct"] == "-" for r in t["rows"])


def test_score_counts_kks_and_cells():
    t = tb.load_truth(TRUTH)
    perfect = "\n".join(
        f"| {r['kks']} | {r['dout_x_thickness_mm']} | {r['document']} | {r['vt_pct']} | "
        f"{r['pt_or_mt_pct']} | {r['rt_pct']} | {r['ut_pct']} | {r['aux_vt_pct']} | {r['aux_pt_or_mt_pct']} |"
        for r in t["rows"])
    s = tb.score(perfect, t)
    assert s["kks_found"] == 10 and s["rows_matched"] == 10
    assert s["accuracy"] == 1.0


def test_score_partial_and_empty():
    t = tb.load_truth(TRUTH)
    # 정답이 요구하는 두 규격을 다 적어야 그 칸이 맞는다 (하나만 적으면 완전행이 아니다)
    part = "| 10AAA02BR005 | 88.9×7.1 | SPEC 100-80 SPEC 200-84/VV | 100 | - | - | 100 | 100 | - |"
    s = tb.score(part, t)
    assert s["kks_found"] == 1 and s["rows_matched"] == 1 and 0 < s["accuracy"] < 0.2
    half = tb.score("| 10AAA02BR005 | 88.9×7.1 | SPEC 100-80 | 100 | - | - | 100 | 100 | - |", t)
    assert half["rows_matched"] == 0 and half["by_column"]["document"] == 0
    s0 = tb.score("", t)
    assert s0["kks_found"] == 0 and s0["accuracy"] == 0.0


def test_score_ignores_ocr_confusions_in_kks():
    """1↔I, 0↔O 혼동은 전사 실패가 아니라 글꼴 문제 — 정규화 후 비교한다."""
    t = tb.load_truth(TRUTH)
    s = tb.score("| IOAAAO2BROO5 | 88.9x7.1 | SPEC 100-80 | 100 | - | - | 100 | 100 | - |", t)
    assert s["kks_found"] == 1


def test_score_penalises_wrong_value():
    t = tb.load_truth(TRUTH)
    wrong = "| 10AAA02BR005 | 88.9×7.1 | SPEC 100-80 | 100 | 100 | - | 100 | 100 | - |"   # PT 를 100 으로 잘못 읽음
    s = tb.score(wrong, t)
    assert s["rows_matched"] == 0 and s["wrong_cells"] >= 1


# ── 쪽을 번호가 아니라 문서코드로 찾는다 (2026-09-08 사내: 개정이 달라 쪽이 밀렸다) ──

def test_locate_page_finds_the_marker_not_the_number(monkeypatch, tmp_path):
    from app.extractors.pdf_extractor import ExtractedPDF, PageText
    pdf = tmp_path / "d.pdf"; pdf.write_bytes(b"%PDF")
    pages = [PageText(page_index=i, text=t, source="ocr") for i, t in enumerate(
        ["표지", "일반지시", "NP.D.N000.1…DC.0001.E-MJH0001 배관특성", "NP.D.N000O.1.0UMA&PAB&021.DC.0001.E-MJZ0001 검사범위"])]
    monkeypatch.setattr(tb, "extract", lambda p: ExtractedPDF(path=pdf, pages=pages, text_layer_present=False))
    assert tb.locate_page(pdf, {"page_marker": "MJZ0001", "page": 13}) == 4      # 번호(13)가 아니라 표식으로
    assert tb.locate_page(pdf, {"page_marker": "MJH0001", "page": 13}) == 3


def test_locate_page_falls_back_to_the_number_when_marker_absent(monkeypatch, tmp_path):
    from app.extractors.pdf_extractor import ExtractedPDF, PageText
    pdf = tmp_path / "d.pdf"; pdf.write_bytes(b"%PDF")
    pages = [PageText(page_index=i, text="…", source="ocr") for i in range(20)]
    monkeypatch.setattr(tb, "extract", lambda p: ExtractedPDF(path=pdf, pages=pages, text_layer_present=False))
    assert tb.locate_page(pdf, {"page_marker": "NOPE", "page": 13}) == 13


def test_locate_page_without_marker_uses_the_number(tmp_path):
    assert tb.locate_page(tmp_path / "x.pdf", {"page": 7}) == 7


# ── 어느 열이 틀렸는지 · 전사본 원문 저장 (2026-09-08 사내 gemma 37.5%: 개정 차이인지 오독인지 가리려면 필요) ──

def test_score_reports_per_column_accuracy():
    t = tb.load_truth(TRUTH)
    # vt·ut 는 맞고 pt·rt 는 틀린 전사 (개정 차이의 전형적 모양)
    md = "\n".join(
        f"| {r['kks']} | {r['dout_x_thickness_mm']} | {r['document']} | 100 | 100 | 100 | 100 | 100 | 100 |"
        for r in t["rows"])
    s = tb.score(md, t)
    assert s["by_column"]["vt_pct"] == 10 and s["by_column"]["ut_pct"] == 10
    assert s["by_column"]["pt_or_mt_pct"] == 0 and s["by_column"]["rt_pct"] == 0
    assert s["worst_columns"][0] in ("pt_or_mt_pct", "rt_pct", "aux_pt_or_mt_pct")


def test_run_saves_each_transcript(monkeypatch, tmp_path):
    import pypdfium2 as pdfium
    monkeypatch.setenv("NDT_HCX_TOKEN", "t"); monkeypatch.setenv("NDT_STUDIO_TOKEN", "t")
    pdf = tmp_path / "[ABD]NP.D.N000.1.0UMA&&BBB&&&.021.DC.0001.E_C03.pdf"
    doc = pdfium.PdfDocument.new(); doc.new_page(200, 100); doc.save(str(pdf)); doc.close()
    truth = tmp_path / "t.json"
    truth.write_text('{"source": "%s", "page": 1, "rows": [{"kks": "10AAA02BR005", "dout_x_thickness_mm": "88.9×7.1",'
                     ' "document": "SPEC 100-80", "vt_pct": "100", "pt_or_mt_pct": "-", "rt_pct": "-", "ut_pct": "100",'
                     ' "aux_vt_pct": "100", "aux_pt_or_mt_pct": "-"}]}' % str(pdf).replace("\\", "/"), encoding="utf-8")
    md = "| 10AAA02BR005 | 88.9×7.1 | SPEC 100-80 | 100 | 100 | - | 100 | 100 | - |"
    def v3(url, body, provider=None):
        return {"status": {"code": "20000"}, "result": {"message": {"content": md}}}
    monkeypatch.setattr(hc_mod, "_do_post_v3", v3)
    out = tmp_path / "bench"
    rows = tb.run(truth, ["hcx005"], out_dir=out)
    saved = out / "hcx005.md"
    assert saved.exists() and "10AAA02BR005" in saved.read_text(encoding="utf-8")
    assert rows[0]["transcript"] == str(saved)
    assert rows[0]["by_column"]["pt_or_mt_pct"] == 0     # 100 이라고 읽었으나 정답은 '-'


# ── 사내 2차: 교체한 C02 파일에 13쪽이 없어 PdfiumError 로 죽었다 (2026-09-08) ──

def _stub_pages(monkeypatch, texts):
    from app.extractors.pdf_extractor import ExtractedPDF, PageText
    def fake(p):
        return ExtractedPDF(path=Path(str(p)),
                            pages=[PageText(page_index=i, text=t, source="ocr") for i, t in enumerate(texts)],
                            text_layer_present=False)
    monkeypatch.setattr(tb, "extract", fake)


def test_locate_page_refuses_a_number_beyond_the_document(monkeypatch, tmp_path):
    pdf = tmp_path / "d.pdf"; pdf.write_bytes(b"%PDF")
    _stub_pages(monkeypatch, ["표지", "일반지시", "배관특성"])          # 3쪽뿐
    with pytest.raises(ValueError) as e:
        tb.locate_page(pdf, {"page_marker": "MJZ0001", "page": 13})
    msg = str(e.value)
    assert "3쪽" in msg and "13" in msg and "--page" in msg


def test_locate_page_finds_the_table_by_title_when_the_code_is_garbled(monkeypatch, tmp_path):
    pdf = tmp_path / "d.pdf"; pdf.write_bytes(b"%PDF")
    _stub_pages(monkeypatch, ["표지", "Methods and scope of welded joint inspection  KKS code", "끝"])
    truth = {"page_marker": "MJZ0001", "page": 99,
             "title": "Methods and scope of welded joint inspection (MJZ0001)"}
    assert tb.locate_page(pdf, truth) == 2


def test_locate_page_reports_what_it_saw(monkeypatch, tmp_path, caplog):
    pdf = tmp_path / "d.pdf"; pdf.write_bytes(b"%PDF")
    _stub_pages(monkeypatch, ["표지", "NP.D…-MJH0001 배관특성", "끝"])
    with caplog.at_level("WARNING", logger="app.table_bench"):
        with pytest.raises(ValueError):
            tb.locate_page(pdf, {"page_marker": "MJZ0001", "page": 13})
    assert any("MJH0001" in r.getMessage() for r in caplog.records), "본 문서코드를 알려줘야 어느 파일인지 안다"


# ── 실제 전사본 모양 그대로의 채점 (값은 합성) (2026-09-08): gemma 는 표를 정확히 읽었는데 87.5% 가 나왔다 ──

GEMMA_REAL = """| KKS code | Outside diameter and thickness of welded parts, mm | Document/ Category of welded joints or pipeline Category & Group | Scope of inspection, % | | | | Note |
|---|---|---|---|---|---|---|---|---|
| | | | Visual inspection and measurements | Liquid penetrant or magnetic particle inspection | Radiographic inspection | Ultrasonic inspection | For welding of auxiliary parts: Visual inspection and measurements | For welding of auxiliary parts: Liquid penetrant or magnetic particle inspection | |
| 10AAA02BR005 | 88.9x7.1 | SPEC 100-80 SPEC 200-84/ VV | 100 | - | - | 100 | 100 | - | - |
| 10AAA02BR006 | 88.9x7.1 | SPEC 100-80 SPEC 200-84/ VV | 100 | - | - | 100 | 100 | - | - |
| 10AAA11BR005 | 2000x18 | SPEC 100-80 SPEC 200-84/ VV | 100 | - | - | 100 | 100 | - | - |
| 10AAA12BR005 | 2000x18 | SPEC 100-80 SPEC 200-84/ VV | 100 | - | - | 100 | 100 | - | - |
| 10AAA13BR005 | 2000x18 | SPEC 100-80 SPEC 200-84/ VV | 100 | - | - | 100 | 100 | - | - |
| 10AAA14BR005 | 2000x18 | SPEC 100-80 SPEC 200-84/ VV | 100 | - | - | 100 | 100 | - | - |
| 10AAA15BR005 | 2000x18 | SPEC 100-80 SPEC 200-84/ VV | 100 | - | - | 100 | 100 | - | - |
| 10AAA16BR005 | 2000x18 | SPEC 100-80 SPEC 200-84/ VV | 100 | - | - | 100 | 100 | - | - |
| 10AAA17BR005 | 2000x18 | SPEC 100-80 SPEC 200-84/ VV | 100 | - | - | 100 | 100 | - | - |
| 10AAA18BR005 | 2000x18 | SPEC 100-80 SPEC 200-84/ VV | 100 | - | - | 100 | 100 | - | - |"""


def test_document_cell_is_compared_by_the_codes_it_names():
    """정답은 두 줄짜리 칸을 'SPEC 100-80 / SPEC 200-84/VV' 로 적었고 모델은 슬래시 없이 이었다.
    규격 이름이 같으면 같은 것이다 — 구분자 차이로 오답을 만들면 채점기가 모델을 억울하게 깎는다."""
    t = tb.load_truth(TRUTH)
    s = tb.score(GEMMA_REAL, t)
    assert s["by_column"]["document"] == 10, s["by_column"]
    assert s["kks_found"] == 10 and s["rows_matched"] == 10
    assert s["accuracy"] == 1.0


def test_document_cell_still_fails_when_a_code_is_missing():
    t = tb.load_truth(TRUTH)
    bad = GEMMA_REAL.replace("SPEC 100-80 SPEC 200-84/ VV", "SPEC 100-80")   # SNiP 누락
    assert tb.score(bad, t)["by_column"]["document"] == 0


def test_worst_columns_lists_only_columns_that_lost_rows():
    t = tb.load_truth(TRUTH)
    s = tb.score(GEMMA_REAL, t)
    assert s["worst_columns"] == [], "다 맞았는데 '틀린 열' 을 보여주면 안 된다"
