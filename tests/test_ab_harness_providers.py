"""A/B 하네스 — 모르는 provider 이름은 조용히 HCX 로 흘러가면 안 된다.

2026-09-07 사내 실측: Studio XP 에 hcx-seed-32b 가 없어 hcx.yaml 에서 `hcxseed` 를 뺐다. 그런데 사내에
결재돼 들어간 run_model_ab.bat 의 "all" 목록에는 아직 hcxseed 가 있다. `_resolve_provider` 는 providers 에
없는 이름을 받으면 기본(HCX v3)으로 후퇴하므로, 그대로 두면 "hcxseed" 열이 사실은 HCX-007 결과가 된다 —
모델 비교가 조용히 오염된다. 이름을 먼저 검증해 멈춘다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ab_model_test", Path(__file__).resolve().parent.parent / "scripts" / "ab_model_test.py")
ab = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ab)


def test_unknown_provider_names_are_reported():
    cfg = {"providers": {"hcx007": {}, "gemma": {}, "gptoss": {}}}
    assert ab.unknown_providers(["hcx007", "hcxseed", "gemma", "typo"], cfg) == ["hcxseed", "typo"]
    assert ab.unknown_providers(["gemma", "gptoss"], cfg) == []


def test_cli_refuses_unknown_provider(monkeypatch, tmp_path):
    """사내 run_model_ab.bat 이 부르는 것은 CLI(main) 이다. 실제 hcx.yaml 기준 (hcxseed 는 2026-09-07 제거됨).
    mock 을 켜 두어 검증이 빠지더라도 실서버로 나가지 않는다."""
    import json
    monkeypatch.setenv("NDT_HCX_MOCK", "1")
    monkeypatch.setenv("NDT_HCX_BUDGET_BYPASS", "1")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"stage": "code_lookup", "payload": {"question": "q", "context_snippets": []}}]), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        ab.main(["--providers", "gemma,hcxseed", "--cases", str(cases), "--out", str(tmp_path / "o.json"),
                 "--report", str(tmp_path / "r.md")])
    assert "hcxseed" in str(e.value) and "gemma" in str(e.value)


def test_split_providers_accepts_commas_and_spaces():
    """PowerShell 은 공백 없는 문자열의 따옴표를 떼고, cmd 배치는 쉼표를 인자 구분자로 본다 (2026-09-07 사내:
    'hcx007,hcx005,…' 를 넘겼는데 hcx007 만 돌았다). 하네스는 쉼표·공백 어느 쪽으로 와도 같은 목록으로 읽는다."""
    assert ab.split_providers("hcx007,hcx005, gemma gptoss") == ["hcx007", "hcx005", "gemma", "gptoss"]
    assert ab.split_providers(" all ") == ["all"]
    assert ab.split_providers("") == []
