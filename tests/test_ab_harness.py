"""A/B 모델 하네스(scripts/ab_model_test.py)의 집계 로직 회귀 가드.

하네스는 scripts/ 라 pytest 기본 수집 대상이 아니어서, 방치되면 조용히 썩는다.
서버 왕복(플레이키) 없이 순수 로직만 고정한다:
  - _agreement : provider 간 pairwise 일치/불일치 계산
  - run        : parse_ok 집계·에러 카운트·지연 통계·정확도(gold)·평균 일치율
_run_stage 는 monkeypatch 로 대체(라우팅/HTTP 는 test_provider_routing 이 담당).
"""
from __future__ import annotations
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ab_model_test",
    Path(__file__).resolve().parent.parent / "scripts" / "ab_model_test.py",
)
ab = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ab)


# ─────────────────────────── _agreement ───────────────────────────


def test_agreement_all_agree():
    results = {
        "a": {"ok": True, "parse_ok": True, "parsed": {"matched_report_no": "R1"}},
        "b": {"ok": True, "parse_ok": True, "parsed": {"matched_report_no": "R1"}},
        "c": {"ok": True, "parse_ok": True, "parsed": {"matched_report_no": "R1"}},
    }
    ag = ab._agreement(results, ab.KEY_FN["matching_judge"])
    assert ag["pairs"] == 3          # C(3,2)
    assert ag["agree"] == 3
    assert ag["rate"] == 1.0


def test_agreement_one_dissents():
    results = {
        "a": {"ok": True, "parse_ok": True, "parsed": {"matched_report_no": "R1"}},
        "b": {"ok": True, "parse_ok": True, "parsed": {"matched_report_no": "R1"}},
        "c": {"ok": True, "parse_ok": True, "parsed": {"matched_report_no": "R2"}},
    }
    ag = ab._agreement(results, ab.KEY_FN["matching_judge"])
    assert ag["pairs"] == 3
    assert ag["agree"] == 1          # a↔b 만 일치
    assert ag["detail"]["a↔b"] == "일치"
    assert ag["detail"]["a↔c"] == "불일치"


def test_agreement_ignores_failed_providers():
    """파싱 실패·에러 provider 는 일치율 계산에서 제외."""
    results = {
        "a": {"ok": True, "parse_ok": True, "parsed": {"matched_report_no": "R1"}},
        "b": {"ok": False, "error": "boom"},                 # 제외
        "c": {"ok": True, "parse_ok": False, "parsed": None},  # 제외
    }
    ag = ab._agreement(results, ab.KEY_FN["matching_judge"])
    assert ag["pairs"] == 0          # 비교 가능한 쌍 없음
    assert ag["rate"] is None


# ─────────────────────────── run (집계·정확도) ───────────────────────────


def _fake_run_stage(script):
    """provider→case별 응답 스크립트를 주입하는 _run_stage 대체.

    script[provider] = list of dict(부분): {parsed?, ok?, parse_ok?, ms?}
    """
    calls = {"i": {}}

    def _impl(provider_name, stage, payload):
        idx = calls["i"].get(provider_name, 0)
        calls["i"][provider_name] = idx + 1
        spec = script[provider_name][idx]
        base = {"ok": True, "parse_ok": True, "ms": 10, "model": provider_name}
        base.update(spec)
        return base

    return _impl


def test_run_accuracy_and_summary(monkeypatch):
    """gold 라벨로 provider별 정확도·일치율·지연 요약이 맞는지."""
    script = {
        # case0: 둘 다 R1(정답 R1) → 둘 다 correct, 일치
        # case1: hcx=R2(정답)·gemma=R9(오답) → hcx correct, gemma wrong, 불일치
        "hcx":   [{"parsed": {"matched_report_no": "R1"}, "ms": 20},
                  {"parsed": {"matched_report_no": "R2"}, "ms": 40}],
        "gemma": [{"parsed": {"matched_report_no": "R1"}, "ms": 5},
                  {"parsed": {"matched_report_no": "R9"}, "ms": 7}],
    }
    monkeypatch.setattr(ab, "_run_stage", _fake_run_stage(script))
    cases = [{"stage": "matching_judge", "payload": {}},
             {"stage": "matching_judge", "payload": {}}]
    gold = {"0": "R1", "1": "R2"}
    out = ab.run(cases, ["hcx", "gemma"], gold=gold)

    assert out["summary"]["hcx"]["parse_ok"] == "2/2"
    assert out["summary"]["hcx"]["accuracy"] == "2/2"     # R1✓ R2✓
    assert out["summary"]["gemma"]["accuracy"] == "1/2"   # R1✓ R9✗
    assert out["summary"]["hcx"]["ms_med"] in (20, 40)
    assert out["summary"]["hcx"]["ms_max"] == 40
    # case0 일치(1.0), case1 불일치(0.0) → 평균 0.5
    assert out["summary"]["_pairwise_agreement_mean"] == 0.5


def test_run_counts_errors(monkeypatch):
    """호출 실패 provider 는 errors 로 집계되고 accuracy 계산에서 빠진다."""
    script = {
        "hcx":   [{"parsed": {"matched_report_no": "R1"}}],
        "gemma": [{"ok": False, "error": "HCXClientError: 401"}],
    }
    monkeypatch.setattr(ab, "_run_stage", _fake_run_stage(script))
    cases = [{"stage": "matching_judge", "payload": {}}]
    out = ab.run(cases, ["hcx", "gemma"], gold={"0": "R1"})
    assert out["summary"]["gemma"]["errors"] == 1
    assert out["summary"]["gemma"]["parse_ok"] == "0/1"
    assert out["summary"]["hcx"]["accuracy"] == "1/1"
    # 비교 가능한 provider 가 1개뿐(gemma 실패) → 일치 쌍 없음 → 평균 None
    assert out["summary"]["_pairwise_agreement_mean"] is None


# ─────────────────────────── 잘림 구분 집계 (2026-09-06 검토 C6) ───────────────────────────


def test_run_counts_truncated_separately(monkeypatch):
    """잘린 응답(parse_ok=False, truncated=True)은 '모델이 JSON 을 못 냈다' 와 구분돼 집계돼야
    한다. 안 그러면 상한 문제가 모델 성능 문제로 읽힌다."""
    script = {
        "hcx":   [{"parsed": {"matched_report_no": "R1"}},
                  {"parsed": None, "parse_ok": False, "truncated": True, "stop_reason": "length"}],
        "gemma": [{"parsed": {"matched_report_no": "R1"}},
                  {"parsed": None, "parse_ok": False, "truncated": False, "stop_reason": "stop"}],
    }
    monkeypatch.setattr(ab, "_run_stage", _fake_run_stage(script))
    cases = [{"stage": "matching_judge", "payload": {}}, {"stage": "matching_judge", "payload": {}}]
    out = ab.run(cases, ["hcx", "gemma"])
    assert out["summary"]["hcx"]["truncated"] == 1
    assert out["summary"]["gemma"]["truncated"] == 0
    assert out["cases"][1]["results"]["hcx"]["stop_reason"] == "length"


def test_run_stage_records_truncation(monkeypatch):
    """_run_stage 가 HCXResponse.truncated / stop_reason 을 기록에 남기는지 (집계의 입력)."""
    import app.hcx_client as hc
    from app.hcx_client import HCXResponse
    from app.config import load_yaml
    monkeypatch.setattr(hc, "call", lambda stage, payload, force_refresh=False: HCXResponse(
        content="{", parsed=None, model="m", cached=False, raw={}, stop_reason="length", truncated=True))
    monkeypatch.setattr(hc, "get_call_stats", lambda: {"token_total": 0})
    try:
        r = ab._run_stage("hcx", "matching_judge", {})
    finally:
        load_yaml.cache_clear()          # _run_stage 가 캐시된 yaml 에 stage_providers 를 심는다
    assert r["parse_ok"] is False and r["truncated"] is True and r["stop_reason"] == "length"
