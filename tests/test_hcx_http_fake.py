"""가짜 HCX 서버로 hcx_client 의 실제 HTTP 왕복 회귀 검증.

mock 이 못 덮는 층(요청 본문·헤더·Bearer·응답 파싱·에러 분류·재시도)을
로컬 HTTP 서버로 상시 검증한다. 사내 HCX 없이 첫 호출 리스크를 최소화.
"""
from __future__ import annotations
import os
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import tests.fake_hcx_server as fake


@pytest.fixture()
def fake_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fake.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    fake._log.clear()
    fake._rate_hits.clear()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _env(monkeypatch, base, token="testkey"):
    from app.config import load_yaml
    monkeypatch.setenv("NDT_HCX_BASE_URL", base)
    monkeypatch.setenv("NDT_HCX_MOCK", "0")
    monkeypatch.setenv("NDT_HCX_BUDGET_BYPASS", "1")
    if token is None:
        monkeypatch.delenv("NDT_HCX_TOKEN", raising=False)
    else:
        monkeypatch.setenv("NDT_HCX_TOKEN", token)
    load_yaml.cache_clear()


def _call(mode, monkeypatch):
    import app.hcx_client as hc
    orig = hc._build_headers
    monkeypatch.setattr(hc, "_build_headers",
                        lambda provider=None: {**orig(provider), "X-Fake-Mode": mode})
    return hc.call("hcx_check", {"ping": 1}, force_refresh=True)


def test_v3_roundtrip_contract(fake_server, monkeypatch):
    _env(monkeypatch, fake_server)
    resp = _call("echo", monkeypatch)
    assert resp.parsed == {"pong": True}
    log = fake._log[-1]
    assert log["model"] in ("HCX-007", "HCX-005")            # URL 에 모델명
    assert log["path"].startswith("/v3/chat-completions/")
    assert log["auth_present"]                                # Bearer
    assert log["has_request_id"]                              # X-Request-Id
    assert log["camelCase_ok"]                                # topP/topK/repetitionPenalty
    # 기본 모델은 HCX-007(추론 계열) → 반드시 maxCompletionTokens 여야 한다.
    assert log["token_field"] == "maxCompletionTokens"


def test_auth_failure_fastfail(fake_server, monkeypatch):
    from app.hcx_client import HCXClientError
    _env(monkeypatch, fake_server, token=None)
    t0 = time.time()
    with pytest.raises(HCXClientError) as ei:
        _call("echo", monkeypatch)
    assert "인증" in str(ei.value) or "NDT_HCX_TOKEN" in str(ei.value)
    assert time.time() - t0 < 3          # 재시도 없이 빠른 실패


def test_rate_limit_retry_then_success(fake_server, monkeypatch):
    _env(monkeypatch, fake_server)
    resp = _call("rate2", monkeypatch)
    assert resp.parsed == {"pong": True}
    assert fake._rate_hits.get("k") == 3   # 2 실패 + 1 성공


def test_context_length_fastfail(fake_server, monkeypatch):
    from app.hcx_client import HCXClientError
    _env(monkeypatch, fake_server)
    with pytest.raises(HCXClientError) as ei:
        _call("context", monkeypatch)
    assert "context" in str(ei.value).lower() or "40003" in str(ei.value)


def test_token_extraction(fake_server, monkeypatch):
    import app.hcx_client as hc
    _env(monkeypatch, fake_server)
    before = hc.get_call_stats()["token_total"]
    _call("echo", monkeypatch)
    assert hc.get_call_stats()["token_total"] >= before + 123


def test_fake_server_rejects_max_tokens_for_reasoning_model(fake_server):
    """가짜 서버가 사내 실측 규칙을 실제로 흉내내는지 — '테스트의 테스트'.

    2026-09-02 이전에는 fake 서버가 maxTokens/maxCompletionTokens 중
    아무거나 받아들였고, 그래서 HCX-007 + maxTokens 버그를 테스트가
    전혀 잡지 못했다 (134개 전부 통과한 채로 사내에서 40001 발생).
    이 테스트가 없으면 그 안전망이 사라져도 아무도 모른다.
    """
    import httpx
    r = httpx.post(
        f"{fake_server}/v3/chat-completions/HCX-007",
        json={"messages": [{"role": "user", "content": "ping"}], "maxTokens": 100},
        headers={"Authorization": "Bearer testkey", "Content-Type": "application/json"},
        timeout=10.0,
    )
    assert r.status_code == 400
    assert r.json()["status"]["code"] == "40001"
    assert "maxTokens" in r.json()["status"]["message"]


def test_fake_server_accepts_completion_tokens_for_reasoning_model(fake_server):
    """같은 요청을 maxCompletionTokens 로 바꾸면 통과해야 한다 (거짓 차단 방지)."""
    import httpx
    r = httpx.post(
        f"{fake_server}/v3/chat-completions/HCX-007",
        json={"messages": [{"role": "user", "content": "ping"}], "maxCompletionTokens": 100},
        headers={"Authorization": "Bearer testkey", "Content-Type": "application/json"},
        timeout=10.0,
    )
    assert r.status_code == 200
    assert r.json()["status"]["code"] == "20000"


def test_non_reasoning_model_may_use_max_tokens(fake_server):
    """HCX-005 는 maxTokens 를 그대로 쓴다 — 실측되지 않은 방향은 막지 않는다."""
    import httpx
    r = httpx.post(
        f"{fake_server}/v3/chat-completions/HCX-005",
        json={"messages": [{"role": "user", "content": "ping"}], "maxTokens": 100},
        headers={"Authorization": "Bearer testkey", "Content-Type": "application/json"},
        timeout=10.0,
    )
    assert r.status_code == 200


# ─────────────────────────── 출력 상한·잘림 (2026-09-06) ───────────────────────────

def test_openai_body_carries_both_output_length_fields(fake_server, monkeypatch):
    """StudioXP 는 max_completion_tokens 만 보고 미지원 필드는 조용히 무시한다.
    max_tokens 만 보내면 상한이 안 걸려 JSON 이 서버 기본값에서 잘리고, A/B 하네스는
    그걸 parse_ok=False 로 세어 모델 탓을 한다. 둘 다 보낸다 (vLLM·OpenAI 모두 둘 다 받음)."""
    _env(monkeypatch, fake_server)
    import app.hcx_client as hc
    p = {**hc._resolve_provider(""), "api_style": "openai",
         "chat_path": "/v1/chat/completions", "model": "gemma4-31b"}
    hc._post_chat_openai("gemma4-31b", "sys", "{}", None, provider=p)
    assert fake._log[-1]["path"] == "/v1/chat/completions"
    assert fake._log[-1]["openai_token_fields"] == ["max_completion_tokens", "max_tokens"]


def test_v3_stop_reason_is_recorded_verbatim(fake_server, monkeypatch):
    """result.stopReason 값을 해석하지 않고 그대로 싣는다. 실제 값은 사내 확인(A-1) 대상."""
    _env(monkeypatch, fake_server)
    resp = _call("echo", monkeypatch)
    assert resp.stop_reason == "stop"
    assert resp.truncated is False


def test_truncated_response_is_flagged_and_warned(fake_server, monkeypatch, caplog):
    """상한에 걸린 응답: JSON 이 중간에서 끊겨 parsed=None 이 되는데, 지금은 그게 '모델이
    JSON 을 못 냈다' 와 구분되지 않는다. stopReason=length 면 truncated=True + 경고."""
    _env(monkeypatch, fake_server)
    with caplog.at_level("WARNING", logger="app.hcx_client"):
        resp = _call("truncate", monkeypatch)
    assert resp.truncated is True
    assert resp.stop_reason == "length"
    assert resp.parsed is None                      # 실제로 잘렸다 (가짜 서버가 절반에서 끊음)
    assert any("잘림" in r.getMessage() or "length" in r.getMessage() for r in caplog.records)


def test_openai_finish_reason_length_is_flagged(fake_server, monkeypatch):
    _env(monkeypatch, fake_server)
    import app.hcx_client as hc
    orig = hc._build_headers
    monkeypatch.setattr(hc, "_build_headers",
                        lambda provider=None: {**orig(provider), "X-Fake-Mode": "truncate"})
    p = {**hc._resolve_provider(""), "api_style": "openai",
         "chat_path": "/v1/chat/completions", "model": "gemma4-31b"}
    api = hc._post_chat_openai("gemma4-31b", "sys", "{}", None, provider=p)
    assert hc._extract_stop_reason(api) == "length"


# ─────────────────────────── 잘림 × 캐시 (2026-09-06 검토 C1/C5, C3) ───────────────────────────

def test_truncated_response_is_not_cached(fake_server, monkeypatch, tmp_path):
    """잘린 응답을 캐시하면 상한을 올려도 캐시 키(모델·프롬프트·payload)가 같아 잘린 내용이
    경고 없이 재사용된다. 경고가 권한 조치가 효과가 없어진다. 잘린 응답은 캐시에 쓰지 않는다 —
    다음 실행이 다시 호출한다 (호출은 공짜, 사용자 확정)."""
    _env(monkeypatch, fake_server)
    monkeypatch.setenv("NDT_LLM_CACHE_DIR", str(tmp_path / "cache"))
    import app.hcx_client as hc
    orig = hc._build_headers
    monkeypatch.setattr(hc, "_build_headers",
                        lambda provider=None: {**orig(provider), "X-Fake-Mode": "truncate"})
    fake._log.clear()
    r1 = hc.call("hcx_check", {"ping": 1})          # force_refresh 없이 — 운영 경로 그대로
    r2 = hc.call("hcx_check", {"ping": 1})
    assert r1.truncated and r2.truncated
    assert r2.cached is False
    assert sum(1 for e in fake._log if e["path"].startswith("/v3/")) == 2


def test_cache_hit_of_truncated_response_still_warns(fake_server, monkeypatch, tmp_path, caplog):
    """패치 이전에 캐시된 잘린 응답이 HIT 되면 그때도 경고가 나야 한다. 지금은 HIT 경로에 경고가 없다."""
    _env(monkeypatch, fake_server)
    monkeypatch.setenv("NDT_LLM_CACHE_DIR", str(tmp_path / "cache"))
    import app.hcx_client as hc
    model = hc._resolve_model("hcx_check")
    key = hc._cache_key(model, hc.load_prompt("hcx_check"), {"ping": 1})
    hc._cache_put(key, {"content": '{"pong": tr', "_api": {}, "stop_reason": "length", "stage": "hcx_check"})
    with caplog.at_level("WARNING", logger="app.hcx_client"):
        resp = hc.call("hcx_check", {"ping": 1})
    assert resp.cached is True and resp.truncated is True
    assert any("잘림" in r.getMessage() for r in caplog.records)


def test_truncation_values_scalar_string_is_accepted(monkeypatch):
    """yaml 에 리스트 대신 문자열 하나를 적으면 글자 단위로 쪼개져 감지가 영원히 안 울린다."""
    import app.hcx_client as hc
    monkeypatch.setattr(hc, "hcx_config", lambda: {"models": {"truncation_values": "length"}})
    assert hc._is_truncated("length") is True
    assert hc._is_truncated("stop") is False
