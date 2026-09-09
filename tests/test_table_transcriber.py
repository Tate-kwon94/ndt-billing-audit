"""표 전사 파이프라인 회귀 테스트 — 분류·숫자 대조·전사 파싱·후퇴·적재 연동.

2026-09-03 실측(PNAEG 7-010-89 p.46: OCR 이 9열 중 8열을 조용히 누락)에서 출발했다.
VLM 은 몽키패치로 대체한다 — 사외엔 HCX-005 가 없고, 검증 대상은 배관과 방어 논리다.
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.extractors import table_transcriber as tt


# 앱과 **같은 해석기**를 쓴다. 테스트와 앱이 서로 다른 방법으로 tesseract 를 찾으면
# 사내에서 또 조용히 건너뛴다 (2026-09-03 실측: 6건이 그렇게 사라졌다).
from app.extractors.ocr_engine import tesseract_available

HAS_TESSERACT = tesseract_available()


# ─────────────────────────── 숫자 대조 (순수 함수) ───────────────────────────

def test_numeric_tokens_normalizes_thousands_and_decimals():
    c = tt.numeric_tokens("1,250 pcs; 0.30 mm; 1.5; 78.0; 2024; 6.7. end")
    assert c["1250"] == 1 and c["0.30"] == 1 and c["1.5"] == 1 and c["78.0"] == 1
    assert c["6.7"] == 1          # 문장 끝 '.' 제거
    assert "6.7." not in c


def test_numeric_tokens_russian_decimal_comma():
    """감사: '0,125' 가 천단위로 오인되어 '0125'. 러시아 규격은 소수점이 쉼표다."""
    assert tt.numeric_tokens("0,125 MPa")["0.125"] == 1
    assert tt.numeric_tokens("1,250 pcs")["1250"] == 1          # 진짜 천단위는 유지


def test_numeric_tokens_with_unit_suffix_and_prefix():
    c = tt.numeric_tokens("25mm DN100 Φ28 t=6mm")
    assert c["25"] == 1 and c["100"] == 1 and c["28"] == 1 and c["6"] == 1


def test_numeric_tokens_skips_digits_embedded_in_identifiers():
    """도면·성적서 번호 속 숫자(UGB95GML)는 측정값이 아니다. 단위·접두 한쪽만 붙은 것은 남긴다."""
    c = tt.numeric_tokens("NP.D.P000.1.0UGB95GML90&.052.DC.0001.E DN100 25mm")
    assert "95" not in c and c["100"] == 1 and c["25"] == 1


def test_digit_agreement_no_numbers_is_unverified():
    ag = tt.digit_agreement("| a | b |", ["x"])
    assert ag["ratio"] == 1.0 and ag["unverified"] is True


def test_digit_agreement_full_match():
    md = "| Over 70.0 to 85.0 | 1.25 | 5.0 | 7.0 | 25 |"
    refs = ["Over 70.0 to 85.0 inclusive 1.25 5.0 7.0 25 78.0 14.0 5.0 4", "Over 70.0 to 85.0"]
    ag = tt.digit_agreement(md, refs)
    assert ag["ratio"] == 1.0 and not ag["invented"]


def test_digit_agreement_flags_invented_numbers():
    md = "| Over 70.0 to 85.0 | 1.25 | 9.99 |"      # 9.99 는 OCR 어디에도 없다
    refs = ["Over 70.0 to 85.0 inclusive 1.25 5.0 7.0 25"]
    ag = tt.digit_agreement(md, refs)
    assert ag["ratio"] < 1.0
    assert "9.99" in ag["invented"]


def test_digit_agreement_union_over_variants():
    """psm3 가 놓친 숫자를 psm6 가 살렸으면 인정한다 (변형별 최댓값 합집합)."""
    md = "| 1.25 | 78.0 |"
    ag = tt.digit_agreement(md, ["Over 70.0 to 85.0", "1.25 5.0 7.0 25 78.0"])
    assert ag["ratio"] == 1.0


def test_digit_agreement_no_numbers_is_confident():
    assert tt.digit_agreement("| a | b |", ["x"])["ratio"] == 1.0


# ─────────────────────────── 응답 파싱 ───────────────────────────

def test_extract_markdown_table_from_fence():
    content = "표 여부: 예\n표 개수: 1\n```markdown\n| 1 | 2 |\n|---|---|\n| a | 1.25 |\n```\n비고: 없음"
    md = tt.extract_markdown_table(content)
    assert md and md.startswith("| 1 | 2 |") and "1.25" in md


def test_extract_markdown_table_not_a_table():
    assert tt.extract_markdown_table("표 여부: 아니오\n이유: 등각도 도면") is None


def test_extract_markdown_table_bare_pipes():
    assert tt.extract_markdown_table("| a | b |\n|---|---|\n| 1 | 2 |") is not None


# ─────────────────────────── 분류 (tesseract 필요) ───────────────────────────

# PIL 기본 비트맵 폰트는 tesseract 가 못 읽는다 (실측: 숫자가 전부 엉뚱하게 나온다).
# 그래서 합성 이미지도 실제 폰트로 그린다. 사외 macOS·사내 Windows 양쪽에 있는 것을 찾고,
# 없으면 이미지 기반 테스트는 건너뛴다. tessdata/pdf.ttf 는 글리프 없는 폰트라 쓸 수 없다.
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font(size: int = 30):
    for c in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(c, size)
        except Exception:       # noqa: BLE001 - 다음 후보로
            continue
    return None


HAS_FONT = _font() is not None
NEEDS_IMAGE = pytest.mark.skipif(not (HAS_TESSERACT and HAS_FONT),
                                 reason="tesseract 또는 사용 가능한 트루타입 폰트 없음")


def _prose_image() -> Image.Image:
    img = Image.new("L", (1400, 900), 255); d = ImageDraw.Draw(img)
    f = _font(26)
    line = "The visual inspection of welded joints shall be performed batch-wise by the laboratory."
    for i in range(14):
        d.text((60, 40 + i * 58), line, fill=0, font=f)
    return img


def _table_image() -> Image.Image:
    img = Image.new("L", (1400, 900), 255); d = ImageDraw.Draw(img)
    f = _font(30)
    cols = [60, 380, 640, 900, 1160]
    for r in range(9):
        y = 40 + r * 80
        vals = [f"Over {70+r*15}.0", f"{1.25+r*0.25:.2f}", f"{5.0+r:.1f}", f"{25+r}", f"{78.0+r*14:.1f}"]
        for x, v in zip(cols, vals):
            d.text((x, y), v, fill=0, font=f)
    return img


@NEEDS_IMAGE
def test_table_score_separates_prose_from_table():
    prose = tt.table_score(_prose_image())["score"]
    table = tt.table_score(_table_image())["score"]
    assert prose < 0.25, prose
    assert table >= 0.5, table


# ─────────────────────────── process_page 방어 논리 (VLM 몽키패치) ───────────────────────────

def _fake_call_vision(markdown: str | None):
    class R:
        def __init__(self, content, model):
            self.content, self.model, self.parsed, self.cached, self.raw = content, model, None, False, {}
    def _f(stage, payload, images, **kw):
        assert stage == "table_transcribe" and images
        if markdown is None:
            return R("표 여부: 아니오\n이유: 도면", "HCX-005")
        return R(f"표 여부: 예\n표 개수: 1\n```markdown\n{markdown}\n```\n비고: 없음", "HCX-005")
    return _f


@NEEDS_IMAGE
def test_process_page_prose_is_not_transcribed(monkeypatch):
    calls = []
    monkeypatch.setattr("app.hcx_client.call_vision", lambda *a, **k: calls.append(1))
    res = tt.process_page(_prose_image(), b"png", ["text"], doc_hint="d", page_no=1)
    assert res["is_table_like"] is False and res["markdown"] is None
    assert not calls                      # 본문에는 VLM 을 부르지 않는다


@NEEDS_IMAGE
def test_process_page_table_high_agreement(monkeypatch):
    md = "| 1 | 2 | 3 |\n|---|---|---|\n| Over 70.0 | 1.25 | 78.0 |"
    monkeypatch.setattr("app.hcx_client.call_vision", _fake_call_vision(md))
    res = tt.process_page(_table_image(), b"png", ["Over 70.0 1.25 5.0 25 78.0"], doc_hint="d", page_no=46)
    assert res["is_table_like"] and res["markdown"] == md
    assert res["confidence"] == 1.0 and res["needs_review"] is False


@NEEDS_IMAGE
def test_process_page_invented_numbers_need_review(monkeypatch):
    md = "| 1 | 2 |\n|---|---|\n| Over 70.0 | 9.99 |"          # 9.99 지어냄
    monkeypatch.setattr("app.hcx_client.call_vision", _fake_call_vision(md))
    res = tt.process_page(_table_image(), b"png", ["Over 70.0 1.25 5.0 25 78.0"], doc_hint="d", page_no=46)
    assert res["needs_review"] is True and "9.99" in res["invented"]


@NEEDS_IMAGE
def test_process_page_vlm_says_not_table_keeps_ocr(monkeypatch):
    monkeypatch.setattr("app.hcx_client.call_vision", _fake_call_vision(None))
    res = tt.process_page(_table_image(), b"png", ["x"], doc_hint="d", page_no=1)
    assert res["is_table_like"] and res["markdown"] is None and "표 아님" in res["note"]


@NEEDS_IMAGE
def test_process_page_vlm_failure_does_not_raise(monkeypatch):
    def boom(*a, **k): raise RuntimeError("HCX down")
    monkeypatch.setattr("app.hcx_client.call_vision", boom)
    res = tt.process_page(_table_image(), b"png", ["x"], doc_hint="d", page_no=1)
    assert res["markdown"] is None and "VLM 실패" in res["note"]


# ─────────────────────────── 적재 연동: 청크 신뢰도가 검색까지 전파 ───────────────────────────

@pytest.fixture()
def indexer(fresh_db):
    from app.extractors import code_indexer as ci
    return ci


def test_chunk_confidence_propagates_to_search(indexer, monkeypatch):
    monkeypatch.setattr("app.embeddings.available", lambda: False)
    pages = [
        {"page": 46, "text": "| 1 | 2 |\n|---|---|\n| Over 70.0 | 1.25 |", "source": "vlm_table",
         "confidence": 0.6, "needs_review": True, "table_score": 0.7},
        (47, "GOST 10922 batch-wise inspection prose page"),
    ]
    indexer.index_document("pnaeg.pdf", "PNAEG 7-010-89", pages)
    hits = indexer.search("Over 70.0 1.25", top_k=2)
    assert hits and hits[0]["page"] == 46
    assert hits[0]["chunk_source"] == "vlm_table"
    assert hits[0]["confidence"] == 0.6 and hits[0]["needs_review"] is True
    prose = [h for h in hits if h["page"] == 47]
    assert not prose or prose[0]["needs_review"] is False


def test_ingest_uses_table_pipeline_and_falls_back(indexer, monkeypatch, tmp_path):
    """extract·렌더·VLM 을 전부 가짜로 — 표 페이지는 전사되고 본문은 그대로."""
    from types import SimpleNamespace as NS
    fake_pdf = tmp_path / "doc.pdf"; fake_pdf.write_bytes(b"%PDF-fake")
    extracted = NS(pages=[NS(page_index=0, text="prose page text", source="ocr", ocr_variants=["prose page text"], confidence=0.9),
                          NS(page_index=1, text="Over 70.0 1.25 78.0", source="ocr", ocr_variants=["Over 70.0 1.25 78.0"], confidence=0.8)],
                   text_layer_present=False)
    monkeypatch.setattr(indexer, "extract", lambda p: extracted)
    monkeypatch.setattr(indexer, "_render_for_score", lambda pdf, i, dpi: Image.new("L", (10, 10), 255))
    scores = {0: {"score": 0.0, "multiword_lines": 20}, 1: {"score": 0.8, "multiword_lines": 20}}
    calls = {"n": 0}
    def fake_score(img, lang=None):
        i = calls["n"]; calls["n"] += 1; return scores[i]
    monkeypatch.setattr("app.extractors.table_transcriber.table_score", fake_score)
    monkeypatch.setattr("app.extractors.vision_verifier.render_page_png", lambda p, i: b"png")
    md = "| 1 | 2 |\n|---|---|\n| Over 70.0 | 1.25 |"
    monkeypatch.setattr("app.hcx_client.call_vision", _fake_call_vision(md))

    res = indexer.ingest(fake_pdf, embed=False, tables=True)
    assert res["table_stats"]["table_like"] == 1 and res["table_stats"]["transcribed"] == 1
    hits = indexer.search("Over 70.0 1.25", top_k=1)
    assert hits[0]["chunk_source"] == "vlm_table" and hits[0]["confidence"] == 1.0
    assert hits[0]["text"].startswith("| 1 | 2 |")            # Markdown 이 앞, OCR 평문이 뒤
    assert "[OCR]" in hits[0]["text"]

# ─── 대조 기준이 눈감고 있던 버그 (2026-09-03 사내 첫 실행) ───

@NEEDS_IMAGE
def test_reference_ocr_rescues_layer_blind_pages(monkeypatch):
    """텍스트레이어가 표의 숫자를 떨어뜨린 페이지에서 VLM 을 환각으로 몰지 않는다.

    실측(GOST 23118-2019 p35): 레이어 숫자 20개 vs OCR 147개 → conf 0.16 오판정.
    이 테스트는 레이어가 비어 있는 극단을 만들어 그 상황을 고정한다.
    """
    img = _table_image()
    md = "| 1 | 2 |\n|---|---|\n| Over 70.0 | 1.25 |"
    monkeypatch.setattr("app.hcx_client.call_vision", _fake_call_vision(md))

    # 레이어(ocr_text)·변형 모두 비어 있다 = 기준이 눈을 감은 상태
    on = tt.process_page(img, b"png", [], doc_hint="d", page_no=35, ocr_text="")
    assert on["confidence"] == 1.0, on          # 이미지에서 다시 읽어 구제
    assert on["needs_review"] is False

    # 2차 읽기를 끄면 예전 동작(오판정)으로 되돌아간다 — 무엇이 구제하는지 못 박는다
    monkeypatch.setitem(tt.hcx_config().setdefault("table_pipeline", {}), "reference_ocr", False)
    off = tt.process_page(img, b"png", [], doc_hint="d", page_no=35, ocr_text="")
    assert off["confidence"] == 0.0 and off["needs_review"] is True


@NEEDS_IMAGE
def test_reference_ocr_unions_with_text_layer(monkeypatch):
    """레이어가 더 잘 읽은 숫자도 함께 인정한다 (합집합) — 같은 문서 p27 사례."""
    img = _table_image()
    md = "| 1 |\n|---|\n| 99999.5 |"          # 이미지엔 없고 레이어에만 있는 값
    monkeypatch.setattr("app.hcx_client.call_vision", _fake_call_vision(md))
    res = tt.process_page(img, b"png", [], doc_hint="d", page_no=27,
                          ocr_text="table continues 99999.5 mm")
    assert res["confidence"] == 1.0, res
