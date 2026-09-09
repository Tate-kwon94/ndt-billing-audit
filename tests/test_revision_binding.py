"""성적서가 인용한 **도면 개정**으로 요구사항을 고른다 (2026-09-08 사용자 지적).

1항차 성적서는 'Revision C02'(60건) · 'C01'(15건) 을 인용한다 — 시공사가 그 개정을 근거로 검사했다.
사내 도면 폴더에는 그보다 새로운 C03·C02 가 들어 있다. 지금 조회는 **가장 최신 개정**을 쓰므로
지난 작업을 나중 기준으로 판정하게 된다. 검사율이 개정에서 바뀌었다면 전 행이 조용히 틀린다.

원칙: 인용된 개정이 있으면 그것을 쓴다. 없으면 **다른 개정으로 대신하지 않고** 재확인으로 남긴다 —
'그럴듯한 최신본' 으로 판정하는 것이 근거 없는 값을 채우는 것과 같은 종류의 실패다.
"""
from datetime import date

import pytest

from app.analyzers import pipeline as pl


def _seed(models, repo, s, revisions=("C01", "C02", "C03")):
    br = repo.create_billing_round(s, round_no=1, discipline="CP-P1", billing_date=date(2024, 2, 27))
    ids = {}
    for rev in revisions:
        ds = repo.get_or_create_drawing_set(s, "NP.D.N000.1.0UMA&&PAB&&&.021", rev)
        s.flush()
        s.add(models.Requirement(drawing_set_id=ds.id, joint_no="FW12", line_no="L1",
                                 required_ndt_json={"items": [{"method": "VT", "rate_pct": 100},
                                                              {"method": "PT", "rate_pct": 100 if rev == "C03" else 0}]}))
        ids[rev] = ds.id
    repo.add_billing_items(s, br.id, [{"joint_no": "FW12", "ndt_method": "PT", "raw_json": {}}])
    s.commit()
    return br, ids


def test_uses_the_revision_the_report_cites(fresh_db):
    models, repo = fresh_db
    with models.get_session() as s:
        br, ids = _seed(models, repo, s)
        item = s.query(models.BillingItem).first()
        req = pl._find_joint_requirement(
            s, item, matched_report={"drawing_no": "NP.D.N000.1.0UMA&&PAB&&&.021.DC.0001.E",
                                     "drawing_revision": "C02"}, as_of=date(2024, 2, 27))
    assert req is not None
    assert req["drawing_set_id"] == ids["C02"], "인용된 C02 를 써야 한다 (최신 C03 이 아니라)"


def test_missing_cited_revision_is_flagged_not_substituted(fresh_db):
    models, repo = fresh_db
    with models.get_session() as s:
        br, ids = _seed(models, repo, s, revisions=("C03",))     # 인용된 C02 가 없다
        item = s.query(models.BillingItem).first()
        req = pl._find_joint_requirement(
            s, item, matched_report={"drawing_no": "NP.D.N000.1.0UMA&&PAB&&&.021.DC.0001.E",
                                     "drawing_revision": "C02"}, as_of=date(2024, 2, 27))
    assert req is not None
    assert req["needs_review"] is True
    assert any("C02" in r and "C03" in r for r in req["review_reasons"]), req["review_reasons"]


def test_without_a_cited_revision_behaviour_is_unchanged(fresh_db):
    models, repo = fresh_db
    with models.get_session() as s:
        br, ids = _seed(models, repo, s)
        item = s.query(models.BillingItem).first()
        req = pl._find_joint_requirement(
            s, item, matched_report={"drawing_no": "NP.D.N000.1.0UMA&&PAB&&&.021.DC.0001.E"},
            as_of=date(2024, 2, 27))
    assert req is not None and req["drawing_set_id"] in ids.values()


@pytest.mark.parametrize("raw, want", [
    ("C02", "C02"), ("Revision C02", "C02"), ("rev. c2", "C02"), ("СО2", "C02"), ("DC=C03", "C03"), ("", None), (None, None),
])
def test_revision_is_normalised(raw, want):
    assert pl._norm_revision(raw) == want
