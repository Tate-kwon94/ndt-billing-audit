"""HCX Chat Completions v3 클라이언트 — 실 스펙(가이드 예시) 기반 단위 테스트.

스펙 출처: CLOVA Studio for the client API 가이드 (응답 예시 p43-45, 요청 예시 p38-41).
이 테스트는 사내 실 호출 전 포맷 정합성을 고정한다 (폐쇄망에서 디버그 어려움).
"""
from __future__ import annotations

import json

import pytest

import app.hcx_client as hc


# ─────────────────────────── 응답 파싱 (실 스펙 예시) ───────────────────────────


def test_extract_content_v3_text():
    """가이드 p43 성공(텍스트) 응답 — result.message.content 추출."""
    api_resp = {
        "status": {"code": "20000", "message": "OK"},
        "result": {
            "message": {"role": "assistant", "content": "문구: 내 삶의 기록"},
            "finishReason": "stop",
            "usage": {"promptTokens": 190, "completionTokens": 30, "totalTokens": 220},
        },
    }
    assert hc._extract_content(api_resp) == "문구: 내 삶의 기록"
    assert hc._extract_total_tokens(api_resp) == 220


def test_extract_content_v3_thinking():
    """가이드 p44 성공(추론) — content 는 최종답변만, thinkingContent 는 별도."""
    api_resp = {
        "status": {"code": "20000", "message": "OK"},
        "result": {
            "message": {
                "role": "assistant",
                "content": "부분집합의 수는 2^n 입니다.",
                "thinkingContent": "n개의 원소를 가진 집합...",
            },
            "usage": {
                "promptTokens": 58, "completionTokens": 489, "totalTokens": 547,
                "completionTokensDetails": {"thinkingTokens": 251},
            },
        },
    }
    assert hc._extract_content(api_resp) == "부분집합의 수는 2^n 입니다."
    assert hc.extract_thinking(api_resp) == "n개의 원소를 가진 집합..."
    assert hc._extract_total_tokens(api_resp) == 547


def test_extract_content_v3_structured_outputs():
    """가이드 p45 Structured Outputs — content 는 JSON 문자열."""
    api_resp = {
        "status": {"code": "20000", "message": "OK"},
        "result": {
            "message": {"role": "assistant",
                        "content": '{"temp_high_c": 32, "temp_low_c": 15, "precipitation_percent": 30}'},
            "usage": {"totalTokens": 76},
        },
    }
    content = hc._extract_content(api_resp)
    assert json.loads(content)["temp_high_c"] == 32


def test_extract_content_openai_fallback():
    """오픈AI 호환 응답도 파싱 (api_style=openai)."""
    api_resp = {"choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "usage": {"total_tokens": 42}}
    assert hc._extract_content(api_resp) == "hello"
    assert hc._extract_total_tokens(api_resp) == 42


# ─────────────────────────── 요청 빌드 (URL·바디 포맷) ───────────────────────────


def _capture_post(monkeypatch):
    """_do_post_v3 를 가로채 (url, body) 를 캡처하고 더미 성공응답 반환."""
    captured = {}

    def fake(url, body, provider=None):
        captured["url"] = url
        captured["body"] = body
        captured["provider"] = provider
        return {"status": {"code": "20000"}, "result": {"message": {"content": "{}"}}}

    monkeypatch.setattr(hc, "_do_post_v3", fake)
    # retry decorator 가 _do_post_v3 를 감싸므로, _post_chat 안에서 다시 가져가도록
    return captured


def test_post_chat_v3_url_and_body(monkeypatch):
    """v3 URL = /v3/chat-completions/{model}, 바디는 CamelCase."""
    captured = _capture_post(monkeypatch)
    hc._post_chat("HCX-007", "system prompt", "user payload", stage="matching_judge")
    assert captured["url"].endswith("/v3/chat-completions/HCX-007")
    body = captured["body"]
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"] == "user payload"
    # CamelCase 파라미터
    assert "topP" in body and "topK" in body and "repetitionPenalty" in body
    assert body["temperature"] == 0.0
    # HCX-007 은 추론 stage 가 아니어도 maxCompletionTokens (서버가 maxTokens 거부)
    assert "maxCompletionTokens" in body
    assert "maxTokens" not in body


def test_post_chat_v3_thinking_stage(monkeypatch):
    """추론 stage (compliance_explain) → maxCompletionTokens + thinking.effort."""
    captured = _capture_post(monkeypatch)
    hc._post_chat("HCX-007", "sys", "payload", stage="compliance_explain")
    body = captured["body"]
    assert "thinking" in body
    assert body["thinking"]["effort"] in ("none", "low", "medium", "high")
    assert "maxCompletionTokens" in body
    assert "maxTokens" not in body  # 추론 모델은 maxTokens 사용 불가


def test_post_chat_v3_reasoning_model_rejects_max_tokens(monkeypatch):
    """추론 모델(HCX-007)은 thinking 을 안 써도 maxCompletionTokens 를 보내야 한다.

    2026-09-02 사내 실측 회귀: HCX-007 + maxTokens 는 서버가 400/40001 로 거부한다.
    hcx-check 가 기본 모델(HCX-007)을 추론 없이 부르다 정확히 이 구멍에 빠졌다.
    """
    captured = _capture_post(monkeypatch)
    hc._post_chat("HCX-007", "sys", "payload", stage="scwep_extract")   # 비추론 stage
    body = captured["body"]
    assert "maxCompletionTokens" in body
    assert "maxTokens" not in body


def test_post_chat_v3_non_reasoning_model_uses_max_tokens(monkeypatch):
    """비추론 모델(HCX-005)은 그대로 maxTokens 를 쓴다."""
    captured = _capture_post(monkeypatch)
    hc._post_chat("HCX-005", "sys", "payload", stage="scwep_extract")
    body = captured["body"]
    assert "maxTokens" in body
    assert "maxCompletionTokens" not in body


def test_post_chat_v3_vision_datauri(monkeypatch):
    """vision → content array + dataUri.data (v3 형식, openai 의 image_url.url 아님)."""
    captured = _capture_post(monkeypatch)
    hc._post_chat("HCX-005", "sys", "describe", images_b64=["BASE64DATA"],
                  stage="vision_table_verify")
    body = captured["body"]
    user_content = body["messages"][1]["content"]
    assert isinstance(user_content, list)
    img_block = [b for b in user_content if b.get("type") == "image_url"][0]
    assert "dataUri" in img_block
    assert img_block["dataUri"]["data"].startswith("data:image/png;base64,")
    # vision 은 추론 무시 → maxTokens
    assert "maxTokens" in body


# ─────────────────────────── 에러 분류 ───────────────────────────


def test_error_classification_retryable():
    """42900 (rate limit), 5xxxx (서버) → 재시도 대상 HCXError."""
    assert isinstance(hc._classify_http_error(429, "42900", "too many"), hc.HCXError)
    assert isinstance(hc._classify_http_error(500, "50000", "server"), hc.HCXError)
    assert isinstance(hc._classify_http_error(408, "40800", "timeout"), hc.HCXError)


def test_error_classification_fail_fast():
    """40100 (인증), 40003 (context length), 40009 (미지원) → fail-fast HCXClientError."""
    assert isinstance(hc._classify_http_error(401, "40100", "unauth"), hc.HCXClientError)
    assert isinstance(hc._classify_http_error(400, "40003", "too long"), hc.HCXClientError)
    assert isinstance(hc._classify_http_error(400, "40009", "unsupported"), hc.HCXClientError)


def test_auth_error_has_helpful_message():
    """인증 오류는 NDT_HCX_TOKEN 안내 포함."""
    err = hc._classify_http_error(401, "40100", "Unauthorized")
    assert "NDT_HCX_TOKEN" in str(err)


# ─────────────────────────── 예산 (한도 무제한 / null hard cap) ───────────────────────────


def test_budget_no_hardcap_does_not_raise(monkeypatch):
    """enforce_hard_cap=false + per_*_hard=null → 호출 차단 안 함 (int(None) 크래시 없음)."""
    monkeypatch.setattr(hc, "hcx_config", lambda: {
        "call_budget": {
            "enforce_hard_cap": False,
            "per_day_warn": 8000, "per_day_hard": None,
            "per_run_warn": 5000, "per_run_hard": None,
            "per_hour_warn": 8000, "per_hour_hard": None,
            "print_summary_every": 200,
        }
    })
    # 여러 번 호출해도 예외 없어야 함
    for _ in range(50):
        hc._budget_check_and_record("test", "HCX-007", is_live=False)


def test_thinking_effort_invalid_falls_back_to_none(monkeypatch):
    """config 오탈자 (예: 'logw') → 무효값 API 전송 대신 None fallback + 경고."""
    monkeypatch.setattr(hc, "hcx_config", lambda: {
        "thinking_stages": {"compliance_explain": "logw", "code_lookup": "medium"}
    })
    assert hc._thinking_effort("compliance_explain") is None   # 오탈자 → fallback
    assert hc._thinking_effort("code_lookup") == "medium"      # 유효값 통과
    assert hc._thinking_effort("unknown_stage") is None        # 미지정 stage


def test_budget_hardcap_enforced_when_enabled(monkeypatch):
    """enforce_hard_cap=true + per_run_hard 설정 → 초과 시 RuntimeError."""
    cap = hc._call_count_total + 3   # 고정값 (lambda 안에서 재계산 방지)
    cfg = {
        "call_budget": {
            "enforce_hard_cap": True,
            "per_run_hard": cap,
            "per_hour_hard": None, "per_day_hard": None,
            "per_run_warn": None, "per_hour_warn": None, "per_day_warn": None,
            "print_summary_every": 1000,
        }
    }
    monkeypatch.setattr(hc, "hcx_config", lambda: cfg)
    monkeypatch.delenv("NDT_HCX_BUDGET_BYPASS", raising=False)
    with pytest.raises(RuntimeError):
        for _ in range(10):
            hc._budget_check_and_record("test", "HCX-007", is_live=False)
