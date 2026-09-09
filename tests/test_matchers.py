"""deterministic.find_match / fuzzy.find_candidates — 2026-09-04 감사 전까지 테스트 0건.

최우선 결함 ①: `all(_normalize(a) == _normalize(b))` 가 양쪽 빈칸이면 참.
report_no 가 둘 다 없으면 첫 성적서에 100점 매칭 → 엉뚱한 성적서로 판정.
"""
from __future__ import annotations

import pytest

from app.matchers import deterministic, fuzzy


def _rep(**kw):
    d = {"report_no": None, "ndt_method": "PT", "joints": [], "_db_id": 1}
    d.update(kw)
    return d


def test_blank_report_no_does_not_match_blank_report_no():
    billing = {"report_no": None, "joint_no": "FW99", "ndt_method": "PT"}
    cands = [_rep(report_no=None, _db_id=1), _rep(report_no="77-001PT", _db_id=2)]
    assert deterministic.find_match(billing, cands) is None


def test_report_no_exact_match_still_works():
    billing = {"report_no": "No. 77-001PT", "ndt_method": "PT"}
    cands = [_rep(report_no="77-001PT", _db_id=7)]
    m = deterministic.find_match(billing, cands)
    assert m is not None and m["_db_id"] == 7 and m["_match_rule"] == "report_no"


def test_joint_method_rule_matches_report_joint_list():
    """감사 ②: _to_report_dict 는 joint_no 를 최상위로 안 올린다. joints 안을 봐야 2순위가 산다."""
    billing = {"report_no": None, "joint_no": "FW12", "ndt_method": "PT"}
    cands = [_rep(_db_id=1, joints=[{"joint_no": "FW07"}]),
             _rep(_db_id=2, joints=[{"joint_no": "FW11"}, {"joint_no": "fw-12"}])]
    m = deterministic.find_match(billing, cands)
    assert m is not None and m["_db_id"] == 2
    assert m["_match_rule"] == "joint+method"
    assert m["joint_no"] == "fw-12"          # 성적서측 표기 그대로 → matched_joint_no


def test_method_mismatch_blocks_joint_rule():
    billing = {"report_no": None, "joint_no": "FW12", "ndt_method": "RT"}
    cands = [_rep(_db_id=2, ndt_method="PT", joints=[{"joint_no": "FW12"}])]
    assert deterministic.find_match(billing, cands) is None


def test_fuzzy_reads_joint_list():
    pytest.importorskip("rapidfuzz")
    billing = {"joint_no": "FW-12A", "ndt_method": "PT", "welder_id": "DW001"}
    cands = [_rep(_db_id=3, joints=[{"joint_no": "FW12A", "welder_id": "DW-001"}]),
             _rep(_db_id=4, joints=[{"joint_no": "BW90", "welder_id": "ZZ999"}])]
    out = fuzzy.find_candidates(billing, cands)
    assert [c["_db_id"] for c in out] == [3]
    assert out[0]["_field_scores"]["ndt_method"] == 100


def test_ambiguous_joint_method_defers_to_welder_rule():
    """리뷰 재현: 같은 Joint·방법, 용접사만 다른 성적서 둘 → 2순위는 모호(2개) → 3순위가 용접사로 확정."""
    billing = {"report_no": None, "joint_no": "FW12", "ndt_method": "PT", "welder_id": "WELDER_B"}
    cands = [_rep(_db_id=1, joints=[{"joint_no": "FW12", "welder_id": "WELDER_A"}]),
             _rep(_db_id=2, joints=[{"joint_no": "FW12", "welder_id": "WELDER_B"}])]
    m = deterministic.find_match(billing, cands)
    assert m is not None and m["_db_id"] == 2 and m["_match_rule"] == "joint+method+welder"


def test_ambiguous_without_welder_returns_none():
    billing = {"report_no": None, "joint_no": "FW12", "ndt_method": "PT", "welder_id": None}
    cands = [_rep(_db_id=1, joints=[{"joint_no": "FW12"}]), _rep(_db_id=2, joints=[{"joint_no": "FW12"}])]
    assert deterministic.find_match(billing, cands) is None


def test_joint_fields_must_come_from_same_joint_row():
    """리뷰 재현(결함②): 후보 안에서 joint_no 는 A행, welder_id 는 B행인 교차 일치를 인정하지 않는다.

    브리프 원안은 후보를 1건만 두었는데, 그러면 joint+method 가 (welder 를 안 보므로) 그 1건을
    유일 확정해버려 joint+method+welder 자체가 시도되지 않는다 — "같은 행이어야 한다" 는
    본 결함을 전혀 검증하지 못한다(용접사 불일치는 별도로 risk_score.welder_mismatch=5 가 다룸).
    후보를 2건으로 늘려 joint+method 를 진짜로 모호하게 만들어 joint+method+welder 로 넘어가게
    하고, 그 단계에서 "같은 행" 매칭이라 두 후보 다 걸리지 않아 None 이 되는지를 검증한다.
    """
    billing = {"report_no": None, "joint_no": "FW12", "ndt_method": "PT", "welder_id": "DW002"}
    cands = [_rep(_db_id=1, joints=[{"joint_no": "FW12", "welder_id": "DW001"}]),
             _rep(_db_id=2, joints=[{"joint_no": "FW12", "welder_id": "DW999"},
                                    {"joint_no": "FW13", "welder_id": "DW002"}])]
    assert deterministic.find_match(billing, cands) is None


def test_fuzzy_sets_matched_joint():
    pytest.importorskip("rapidfuzz")
    billing = {"joint_no": "FW-12A", "ndt_method": "PT", "welder_id": "DW001"}
    cands = [_rep(_db_id=3, joints=[{"joint_no": "BW90", "welder_id": "DW001"}, {"joint_no": "FW12A", "welder_id": "DW-001"}])]
    out = fuzzy.find_candidates(billing, cands)
    assert out and out[0]["joint_no"] == "FW12A"
