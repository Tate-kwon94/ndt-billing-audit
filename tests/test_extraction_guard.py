"""추출 근거 검증 — LLM 이 원문에 없는 숫자를 지어내면 무효화한다 (2026-09-07 사내 사례).

사내 ② 에서 도면 검사표가 `vt/pt/rt/ut 전부 100` 으로 들어왔다. 실제 표는 **VT 100 · UT 100 · PT '-' · RT '-'** 다.
원인: tesseract 가 그 표에서 숫자를 0개 뽑았고(실측), 빈 입력을 받은 HCX-007 이 그럴듯한 값을 채웠다.
프롬프트에 "추정 금지" 가 이미 있으므로 프롬프트로는 못 막는다. **원문 대조**로 막는다.

원칙: 원문에 근거가 없는 값은 틀린 값보다 나쁘다 — 판정이 조용히 뒤집히기 때문이다.
"""
from app.analyzers import extraction_guard as g


def test_values_absent_from_source_are_invalidated():
    """사내 실사례 재현: 그 쪽 OCR 에는 'SN 527-80' 뿐이고 '100' 은 한 번도 없었다."""
    rows = [{"kks_code": "10PAB02BR005", "page_in_drawing": 13,
             "vt_pct": 100, "pt_or_mt_pct": 100, "rt_pct": 100, "ut_pct": 100}]
    pages = {13: "Methods and scope of welded joint inspection SN 527-80 KKS code"}
    out, warn = g.verify_matrix(rows, pages)
    assert all(out[0][k] is None for k in ("vt_pct", "pt_or_mt_pct", "rt_pct", "ut_pct"))
    assert out[0]["needs_review"] is True
    assert any("근거 없음" in w for w in warn)


def test_values_present_in_source_survive():
    rows = [{"kks_code": "10PAB02BR005", "page_in_drawing": 13,
             "vt_pct": 100, "pt_or_mt_pct": None, "ut_pct": 100}]
    pages = {13: "10PAB02BR005 88.9x7.1 SN 527-80 100 - - 100 100 -"}
    out, warn = g.verify_matrix(rows, pages)
    assert out[0]["vt_pct"] == 100 and out[0]["ut_pct"] == 100
    assert out[0].get("needs_review") is not True
    assert warn == []


def test_claimed_count_beyond_source_is_trimmed():
    """원문에 '100' 이 2번인데 4칸을 100 이라고 하면 2칸은 지어낸 것이다."""
    rows = [{"kks_code": "10PAB02BR005", "page_in_drawing": 13,
             "vt_pct": 100, "pt_or_mt_pct": 100, "rt_pct": 100, "ut_pct": 100}]
    pages = {13: "10PAB02BR005 100 - - 100"}
    out, warn = g.verify_matrix(rows, pages)
    kept = [k for k in ("vt_pct", "pt_or_mt_pct", "rt_pct", "ut_pct") if out[0][k] == 100]
    assert len(kept) == 2, out[0]
    assert out[0]["needs_review"] is True
    assert any("초과" in w for w in warn)


def test_unknown_page_is_treated_as_no_source():
    rows = [{"kks_code": "X", "page_in_drawing": 99, "vt_pct": 100}]
    out, warn = g.verify_matrix(rows, {13: "100 100 100"})
    assert out[0]["vt_pct"] is None and out[0]["needs_review"] is True


def test_kks_line_values_are_guarded_too():
    rows = [{"kks_code": "10PAB02BR005", "page_in_drawing": 12,
             "safety_class_np_001_15": "4", "design_pressure_mpag": 0.33}]
    out, warn = g.verify_matrix(rows, {12: "no numbers here"}, numeric_fields=("design_pressure_mpag",))
    assert out[0]["design_pressure_mpag"] is None


def test_empty_input_is_safe():
    assert g.verify_matrix([], {}) == ([], [])
    assert g.verify_matrix(None, None) == ([], [])


# ── 4개 도면 일관성 — 같은 표에서 다른 답이 나오면 읽은 게 아니라 찍은 것 ──────────

def test_consistency_across_identical_tables_is_reported():
    """사내 실사례: 어떤 도면은 vt/pt/rt/ut 전부 100, 어떤 도면은 값 없음. 표 4개는 동일한데."""
    sets = {
        "NP.D.N000.1": [{"kks_code": "10PAB02BR005", "vt_pct": 100, "pt_or_mt_pct": 100,
                         "rt_pct": 100, "ut_pct": 100}],
        "NP.D.N000.2": [{"kks_code": "20PAB02BR005", "vt_pct": None, "pt_or_mt_pct": None,
                         "rt_pct": None, "ut_pct": None}],
    }
    r = g.consistency_report(sets)
    assert r["consistent"] is False
    assert "pt_or_mt_pct" in r["differing_fields"] and "vt_pct" in r["differing_fields"]
    assert "도면마다 다르다" in r["message"]


def test_consistency_holds_when_only_unit_prefix_differs():
    sets = {
        "NP.D.N000.1": [{"kks_code": "10PAB02BR005", "vt_pct": 100, "pt_or_mt_pct": None, "ut_pct": 100}],
        "NP.D.N000.3": [{"kks_code": "30PAB02BR005", "vt_pct": 100, "pt_or_mt_pct": None, "ut_pct": 100}],
    }
    r = g.consistency_report(sets)
    assert r["consistent"] is True and r["differing_fields"] == []


def test_consistency_needs_two_sets():
    assert g.consistency_report({"a": []})["consistent"] is None


# ── 인용문 검증 — 지어낸 문장으로 근거·면책이 성립하면 안 된다 ────────────────

def test_quote_present_in_source_passes():
    src = "6.7 Welded reinforcement assemblies shall be inspected in accordance with GOST 10922."
    ok, ratio = g.verify_quote("Welded reinforcement assemblies shall be inspected in accordance with GOST 10922", src)
    assert ok and ratio > 0.95


def test_quote_survives_ocr_noise():
    """OCR 이 1↔I, 0↔O 로 깨뜨려도 정직한 인용은 통과해야 한다 (아니면 경고가 소음이 된다)."""
    src = "6.7 Welded reinforcernent assernblies shall be inspected in accordance with GOST 1O922."
    ok, ratio = g.verify_quote("Welded reinforcement assemblies shall be inspected in accordance with GOST 10922", src)
    assert ok, ratio


def test_fabricated_quote_fails():
    src = "6.7 Welded reinforcement assemblies shall be inspected in accordance with GOST 10922."
    ok, ratio = g.verify_quote("Penetrant testing of 100% of welds is mandatory for all pipelines", src)
    assert not ok and ratio < 0.7


def test_empty_quote_or_source_fails_closed():
    assert g.verify_quote("", "text")[0] is False
    assert g.verify_quote("something", "")[0] is False
    assert g.verify_quote(None, None)[0] is False


def test_verify_citations_marks_unverified_and_keeps_reason():
    snippets = [{"doc": "SP 70", "page": 88, "text": "VT of welded joints shall be 100 % for all types."}]
    cites = [
        {"doc": "SP 70", "page": 88, "quote": "VT of welded joints shall be 100 % for all types"},
        {"doc": "SP 70", "page": 88, "quote": "PT is required for every joint without exception"},
    ]
    out = g.verify_citations(cites, snippets)
    assert out[0]["quote_verified"] is True and out[0].get("needs_review") is not True
    assert out[1]["quote_verified"] is False and out[1]["needs_review"] is True
    assert "인용문" in " ".join(out[1]["review_reasons"])


def test_verify_citations_untraceable_page_is_not_verified():
    out = g.verify_citations([{"doc": "X", "page": 9, "quote": "any"}],
                             [{"doc": "SP 70", "page": 88, "text": "any"}])
    assert out[0]["quote_verified"] is False and out[0]["needs_review"] is True


# ── 쪽을 안 준 것과 원문에 없는 것은 다르다 (2026-09-08 사내 ②: '쪽 None 원문을 못 찾아 근거 없음') ──

def test_missing_page_is_unverifiable_not_unsupported():
    """모델이 page_in_drawing 을 안 채우면 대조할 원문이 없다. 그것은 '값이 틀렸다' 가 아니다 —
    값을 비우면 정상 추출까지 지워진다. 표시만 남기고 값은 살린다."""
    rows = [{"kks_code": "40PAB02BR005", "page_in_drawing": None,
             "vt_pct": 100, "pt_or_mt_pct": None, "rt_pct": None, "ut_pct": 100}]
    out, warn = g.verify_matrix(rows, {13: "…원문…"})
    assert out[0]["vt_pct"] == 100 and out[0]["ut_pct"] == 100
    assert out[0]["needs_review"] is True
    assert any("쪽을 대지 않아" in w for w in warn)
    assert not any("근거 없음" in w for w in warn)


def test_page_given_but_absent_from_document_still_clears_the_values():
    """쪽을 댔는데 그 쪽이 문서에 없으면 검증 실패다 — 값을 비운다."""
    rows = [{"kks_code": "X", "page_in_drawing": 99, "vt_pct": 100}]
    out, warn = g.verify_matrix(rows, {13: "…원문…"})
    assert out[0]["vt_pct"] is None and out[0]["needs_review"] is True
    assert any("근거 없음" in w or "못 찾" in w for w in warn)


def test_unverifiable_row_is_marked_so_the_gate_can_see_it():
    rows = [{"kks_code": "X", "page_in_drawing": None, "vt_pct": 100}]
    out, _ = g.verify_matrix(rows, {})
    assert out[0]["verification"] == "unverifiable"


def test_verified_row_is_marked_too():
    rows = [{"kks_code": "X", "page_in_drawing": 1, "vt_pct": 100}]
    out, _ = g.verify_matrix(rows, {1: "VT 100 for all"})
    assert out[0]["verification"] == "verified" and out[0]["vt_pct"] == 100
