"""SCWEP 인용문 검증 — 지어낸 문장으로 면책이 성립하면 안 된다 (2026-09-07 파생 검토).

`scwep_basis` 는 면책(과다청구 아님) 조건으로 '원문 인용이 있다' 를 요구한다. 그런데 그 인용문이
SCWEP 원문에 **실재하는지** 는 확인한 적이 없다. 도면 검사표가 빈 입력에서 채워진 것과 같은 결함이고,
이쪽은 방향이 반대라 더 위험하다 — 요구를 만드는 게 아니라 **면책을 만든다**.
1항차 PT 399행이 정확히 이 경로로 정당화되므로 여기서 막는다.

설계: 적재 때(원문이 있을 때) 한 번 검증해 규칙에 quote_verified 를 저장하고,
판정 때는 그 값을 요구한다. 값이 없으면(옛 적재) 면책으로 인정하지 않고 재적재를 안내한다.
"""
from app.analyzers import scwep_basis as sb
from app.extractors import scwep_parser as sp


# ── 적재 시 검증 ──────────────────────────────────────────────────────────────

def test_ingest_marks_each_rule_verified_against_its_page():
    parsed = {"conditional_ndt_requirements": [
        {"rule_id": "R1", "ndt_method": "PT", "trigger": "러그 제거 후", "page": 25,
         "quote": "After removal of temporary lugs the area shall be examined by penetrant testing"},
        {"rule_id": "R2", "ndt_method": "PT", "trigger": "지어낸 조건", "page": 25,
         "quote": "All welds require radiographic testing at 100 percent"},
        {"rule_id": "R3", "ndt_method": "VT", "trigger": "쪽 없음", "page": None, "quote": "무엇이든"},
    ]}
    pages = {25: "7.3 After removal of temporary lugs the area shall be examined by penetrant testing (PT)."}
    out = sp.verify_quotes(parsed, pages)
    r = {x["rule_id"]: x for x in out["conditional_ndt_requirements"]}
    assert r["R1"]["quote_verified"] is True
    assert r["R2"]["quote_verified"] is False
    assert r["R3"]["quote_verified"] is None         # 쪽을 안 대면 '모른다' (거짓이 아니다)
    assert out["needs_review"] is True
    assert any("인용문" in x for x in out["review_reasons"])


def test_ingest_verification_tolerates_ocr_noise():
    parsed = {"conditional_ndt_requirements": [
        {"rule_id": "R1", "ndt_method": "PT", "trigger": "t", "page": 3,
         "quote": "the area shall be examined by penetrant testing"}]}
    pages = {3: "the area shaII be exarnined by penetrant testlng"}    # OCR 깨짐
    out = sp.verify_quotes(parsed, pages)
    assert out["conditional_ndt_requirements"][0]["quote_verified"] is True


def test_ingest_without_pages_leaves_unknown_not_false():
    parsed = {"conditional_ndt_requirements": [{"rule_id": "R1", "ndt_method": "PT", "page": 3, "quote": "q"}]}
    out = sp.verify_quotes(parsed, {})
    assert out["conditional_ndt_requirements"][0]["quote_verified"] is None


# ── 판정 시 요구 ──────────────────────────────────────────────────────────────

def _rule(**kw):
    base = {"ndt_method": "PT", "trigger": "러그 제거 후", "quote": "…원문…", "confidence": 0.9}
    base.update(kw)
    return base


def test_verified_rule_is_strong():
    strong, weak = sb._conditional_hits({"conditional_ndt_requirements": [_rule(quote_verified=True)]}, "PT")
    assert len(strong) == 1 and weak == []


def test_unverified_rule_is_not_strong():
    strong, weak = sb._conditional_hits({"conditional_ndt_requirements": [_rule(quote_verified=False)]}, "PT")
    assert strong == [] and len(weak) == 1


def test_rule_from_before_verification_stays_strong_but_flagged():
    """옛 적재본(quote_verified 없음)은 '모른다' 다. 판정을 뒤집지 않고 표시만 남긴다 —
    '모른다' 를 '틀렸다' 로 바꾸면 정당한 면책이 한꺼번에 과다청구로 몰린다."""
    strong, weak = sb._conditional_hits({"conditional_ndt_requirements": [_rule()]}, "PT")
    assert len(strong) == 1 and weak == []
    ref = sb._ref("DOC-1", strong[0], "conditional")
    assert ref["quote_verified"] is None and ref["needs_review"] is True


def test_verified_rule_reference_is_not_flagged():
    ref = sb._ref("DOC-1", _rule(quote_verified=True), "conditional")
    assert ref["quote_verified"] is True and ref["needs_review"] is False
