"""CRST 성적서 헤더 번호 추출 — 1항차 실물에서 본 깨짐 8종 (2026-09-08 재검증).

① 결과 재검증에서 "성적서 570개 결번" 이라고 했던 것의 상당수가 결번이 아니라 **헤더 정규식 실패**였다.
879 세그먼트 중 262건(30%)이 번호를 못 뽑았고, 그중 215건은 표지 문구가 있는 진짜 성적서였다.
실패 유형은 전부 글자 깨짐이다. 그리고 OCR 이 깨진 쪽은 텍스트 레이어가 멀쩡하고 그 반대도 있어서,
**여러 출처를 후보로 놓고 청구서의 성적서번호 집합으로 고르면** 거의 다 회수된다.
"""
import pytest

from app.extractors import report_segmenter as rs


@pytest.mark.parametrize("text, no, method", [
    ("New Cairo No. 77-005PT dated 19.02.2024 10UMA",           "77005PT",  "PT"),   # 정상
    ('New Cairo No. 77_005 U"l‘ dated 20. 02 2024 (.',            None,       None),   # 밑줄+방법 깨짐 → 이 출처는 포기
    ("New Cairo No. 77-132VMC(C dated 17.05.2024 IOUMA",          "77132VMC", "VT"),   # 방법 뒤 찌꺼기
    ("New Cairo No. 77-259УМС dated — 30.07.2024 10UGB",          "77259VMC", "VT"),   # 키릴 УМС
    ("New Cairo ) No. 77-104РТ dated 10.08.2024 | 90 UGG",        "77104PT",  "PT"),   # 키릴 РТ
    ("New Cairo No. 77-527У МС dated 03.09.2024 20UGB",           "77527VMC", "VT"),   # 키릴 + 방법 안 공백
    ("Welded Joints (Surfacing) 77-138PT dated 28.10.2024 | 30UMA", "77138PT", "PT"),  # 'No.' 없음
    ("New Cairo o. 77-lSSPT dated 23.11.2024 JOUSG",              "77155PT",  "PT"),   # l→1, S→5
    ("New Cairo No. 77-528/1VMC dated 03.09.2024 20UGB",          "77528/1VMC", "VT"), # 분수형 번호 (청구서 실재)
    ("No. 77-013CT dated 27.02.2024 10UMA",                      "77013CT",  "PT"),   # CT = 침투(러시아식)
])
def test_tolerant_header(text, no, method):
    m = rs._extract_crst_header(text)
    if no is None:
        assert m is None
    else:
        assert m["report_no_normalized"] == no and m["ndt_method"] == method, m


def test_accreditation_certificate_number_is_not_a_report_number():
    """'No. AAC.T.00646, valid until 10.10.2027' 은 인증서 번호다 — 성적서 번호로 잡히면 안 된다."""
    assert rs._extract_crst_header("Certificate No. AAC.T.00646, valid until 10.10.2027 Form No.") is None


def test_multi_source_picks_the_reading_the_billing_sheet_knows():
    """OCR 은 77-5321VMC(자릿수 삽입), 레이어는 77-521VMC. 청구서에 521 이 있으면 그것이다."""
    ocr = "New Cairo No. 77-5321VMC dated 03.09.2024 20UGB"
    layer = "New Cairo No. 77-521VMC dated 03.09.2024 20UGB"
    m = rs.header_from_sources([ocr, layer], known={"77521VMC", "77005PT"})
    assert m["report_no_normalized"] == "77521VMC" and m["source"] == "regex+billing"


def test_multi_source_rescues_when_one_source_is_broken():
    ocr = 'New Cairo No. 77_005 U"l‘ dated 20. 02 2024'
    layer = "New Cairo No. 77-005UT dated 20.02.2024 !OUM/\\"
    m = rs.header_from_sources([ocr, layer], known=None)
    assert m and m["report_no_normalized"] == "77005UT"


def test_multi_source_majority_without_billing_set():
    m = rs.header_from_sources(["No. 77-521VMC dated 1.1.2024", "No. 77-5321VMC dated 1.1.2024",
                                "No. 77-521VMC dated 1.1.2024"], known=None)
    assert m["report_no_normalized"] == "77521VMC"


def test_multi_source_all_fail_is_none():
    assert rs.header_from_sources(["garbage", ""], known={"77005PT"}) is None


# ── 분할기 배선: 페이지마다 OCR 본문·변형본·레이어를 후보로, 청구서 집합으로 고른다 ──

def test_segment_crst_uses_variants_layer_and_billing_set(monkeypatch):
    from app.extractors.pdf_extractor import ExtractedPDF, PageText
    from pathlib import Path
    pages = [
        PageText(page_index=0, text='Conclusion on PT No. 77_005 U"l‘ dated 20. 02 2024', source="ocr",
                 ocr_variants=['garbage', 'No. 77-005UT dated 20.02.2024']),               # 변형본이 구함
        PageText(page_index=1, text="Conclusion on VMC No. 77-5321VMC dated 03.09.2024", source="ocr",
                 ocr_variants=[]),                                                              # 레이어+청구서가 구함
        PageText(page_index=2, text="Conclusion on VMC totally broken header", source="ocr", ocr_variants=[]),
    ]
    ex = ExtractedPDF(path=Path("x.pdf"), pages=pages, text_layer_present=False)
    layer = {1: "No. 77-521VMC dated 03.09.2024"}
    called = []
    monkeypatch.setattr(rs, "_llm_crst_header", lambda text, i: (called.append(i), None)[1])
    segs = rs.segment_crst(ex, layer_texts=layer, known_report_nos={"77005UT", "77521VMC"})
    nos = {s.meta.get("report_no_normalized") for s in segs}
    assert "77005UT" in nos and "77521VMC" in nos and "775321VMC" not in nos
    assert called == [2]                                        # 정규식이 다 실패한 쪽만 LLM 으로
    assert any(s.meta.get("source") == "regex+billing" for s in segs)
