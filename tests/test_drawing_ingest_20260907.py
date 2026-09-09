"""2026-09-07 사내 ② 재실행(패치 e) 로그에서 드러난 결함 회귀 테스트.

- 세트 2: thickness_mm=[3040, 20] (DC 의 '3040×20' 외경×두께) 가 Float 컬럼에 들어가 TypeError.
- 세트 3·4: 세트 2 실패 뒤 세션을 되돌리지 않아 PendingRollbackError 연쇄, 마지막 commit 에서 ② 전체 사망.
- 사용자 규칙(2026-09-07): 시공 관련 도면은 DC·DK 뿐. SD·BG 는 구매 도면 → 없어도 세트는 완전하다.
- 세트 1: 울타리는 벗겼는데도 parse 실패 → 잘림 의심. v3 stopReason 이 없을 때 result 키를 경고에 남긴다 (A-1).
"""
import logging

import pytest

from app.extractors.drawing import classifier as clsmod
from app.extractors.drawing import grouper
from app.extractors.drawing import requirements_extractor as rx
import app.hcx_client as hc


# ── 두께 정규화 ──────────────────────────────────────────────────────────────

DC = {"kks_lines": [
    {"kks_code": "40PAB21BR000SMR003", "dout_x_thickness_mm": "3040×20"},
    {"kks_code": "20PAB02BR005", "dout_x_thickness_mm": "88.9x7.1"},
]}


@pytest.mark.parametrize("value, joint, expected", [
    ([3040, 20], {}, 20.0),                       # LLM 이 [외경, 두께] 리스트로 준 경우
    ("3040×20", {}, 20.0),                        # 문자열 D×t
    ("88.9x7.1", {}, 7.1),                        # 소문자 x
    (12.5, {}, 12.5),                             # 이미 숫자
    (None, {}, None),                             # 없음
    ("두께 미상", {}, None),                       # 해석 불가 → None (죽지 않음)
    (3040, {"line_no": "40PAB21BR000SMR003"}, 20.0),   # LLM 이 외경을 두께로 넣음 → DC 표가 이긴다
    (None, {"joint_no": "20PAB02BR005"}, 7.1),         # LLM 이 비웠어도 DC 표에서 채움
])
def test_thickness_normalized_to_float_or_none(value, joint, expected):
    assert rx._thickness_mm(value, joint, DC) == expected


# ── DC 만 있는 세트는 완전하다 (SD/BG 는 구매 도면) ──────────────────────────

def _cls(dtype, no="NP.D.N000.1.0UMA&&PAB&&&.021", rev="C03"):
    return clsmod.Classification(drawing_type=dtype, drawing_no=no, revision=rev,
                                 confidence=0.99, source="filename")


def test_dc_only_set_is_complete():
    ds = grouper.DrawingSet(drawing_no="X", revision=None, files={"DC": _cls("DC")})
    assert ds.is_complete
    assert ds.missing == []


def test_set_without_dc_is_incomplete():
    ds = grouper.DrawingSet(drawing_no="X", revision=None, files={"SD": _cls("SD")})
    assert not ds.is_complete
    assert ds.missing == ["DC"]


def test_dk_recognized_in_filename_and_grouped():
    m = clsmod._FILENAME_PATTERN.match("[ABD]NP.D.N000.1.0UMA&&PAB&&&.021.DK.0001.E_C01.pdf")
    assert m and m.group("type") == "DK"
    res = grouper.group([_cls("DC"), _cls("DK", rev="C01")])
    assert len(res.sets) == 1
    assert set(res.sets[0].files) == {"DC", "DK"}
    assert "DK=C01" in res.sets[0].revision


def test_missing_joints_from_absent_optional_types_do_not_flag_review():
    ds = grouper.DrawingSet(drawing_no="X", revision=None, files={"DC": _cls("DC")})
    combined = {"missing_joints": [{"joint_no": "J1", "found_in": ["DC"], "missing_from": ["SD", "BG"]}],
                "extraction_confidence": 0.9}
    assert rx._aggregate_set_review_reasons(ds, combined) == []


def test_missing_joints_from_present_type_still_flag_review():
    ds = grouper.DrawingSet(drawing_no="X", revision=None, files={"DC": _cls("DC"), "SD": _cls("SD")})
    combined = {"missing_joints": [{"joint_no": "J1", "found_in": ["SD"], "missing_from": ["DC"]}],
                "extraction_confidence": 0.9}
    assert any("Joint" in r for r in rx._aggregate_set_review_reasons(ds, combined))


# ── 세트 하나의 실패가 나머지를 죽이지 않는다 ─────────────────────────────────

def test_failed_set_is_isolated_and_reported(fresh_db, tmp_path, monkeypatch):
    models, _ = fresh_db
    folder = tmp_path / "drawings"; folder.mkdir()
    a = folder / "[ABD]NP.D.N000.1.0UMA&&PAB&&&.021.DC.0001.E_C03.pdf"; a.write_bytes(b"%PDF a")
    b = folder / "[ABD]NP.D.N000.2.0UMA&&PAB&&&.021.DC.0001.E_C02.pdf"; b.write_bytes(b"%PDF b")

    def fake_classify(p):
        no = "NP.D.N000.1.0UMA&&PAB&&&.021" if p == a else "NP.D.N000.2.0UMA&&PAB&&&.021"
        return _cls("DC", no=no)
    monkeypatch.setattr(rx, "classify", fake_classify)
    monkeypatch.setattr(rx, "parse_dc", lambda fpath, **kw: DC)
    monkeypatch.setattr(rx, "combine", lambda **kw: {
        "drawing_no": kw["drawing_no"], "joints": [
            {"joint_no": "20PAB02BR005", "line_no": "20PAB02BR005", "thickness_mm": [88.9, 7.1]}],
        "extraction_confidence": 0.9})

    def boom(session, *, drawing_no, **kw):
        if drawing_no.startswith("NP.D.N000.1"):
            raise RuntimeError("세트 1 만 터짐")
    monkeypatch.setattr(rx, "supersede_old_revisions", boom)

    stats = rx.ingest_folder(folder)
    assert stats["failed_sets"] == ["NP.D.N000.1.0UMA&&PAB&&&.021"]
    assert stats["requirements_ingested"] == 1          # 세트 2 는 살아서 저장됨
    with models.get_session() as s:
        reqs = s.query(models.Requirement).all()
        assert [r.thickness_mm for r in reqs] == [7.1]


# ── A-1: parse 실패 + stopReason 없음 → v3 result 키를 경고에 남긴다 ─────────

def test_parse_failure_note_lists_v3_result_keys():
    raw = {"content": "```json\n{\"a\": [", "stop_reason": None,
           "_api": {"result": {"message": {}, "finishReason": "length", "usage": {}}}}
    note = hc._parse_failure_note(raw)
    assert "finishReason" in note and "stop_reason=None" in note
