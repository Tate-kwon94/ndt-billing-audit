"""2026-09-07 사내 A/B (ab_result.json) + ② 도면등록 로그에서 드러난 결함 회귀 테스트.

1. HCX-007 이 JSON 을 ```json 울타리로 감싸 보내 parsed=None → Combine returned None,
   requirements_ingested 0.  (② 로그: content head '```json\\n{ "drawing_no": …')
2. HCX-005 가 추론 stage(code_lookup 등) 에 라우팅되면 thinking + maxCompletionTokens 를
   받아 40001 'Invalid parameter: maxCompletionTokens' 로 거부.  (A/B hcx005 6건 오류)
3. 하네스가 parse 실패 응답의 앞부분을 결과 파일에 남기지 않아 사진으로 원인 추적 불가.
"""
import app.hcx_client as hc


# ── 1. 마크다운 울타리 ────────────────────────────────────────────────────────

def test_wrap_strips_markdown_json_fence():
    raw = {"content": '```json\n{ "drawing_no": "NP.D.N000.1", "rev": "C01" }\n```'}
    resp = hc._wrap(raw, "HCX-007", cached=False)
    assert resp.parsed == {"drawing_no": "NP.D.N000.1", "rev": "C01"}


def test_wrap_strips_bare_fence_and_surrounding_prose():
    raw = {"content": 'Here is the result:\n```\n{"a": 1}\n```\nLet me know.'}
    resp = hc._wrap(raw, "gemma4-31b", cached=False)
    assert resp.parsed == {"a": 1}


def test_wrap_plain_json_unchanged():
    resp = hc._wrap({"content": '{"a": 1}'}, "HCX-007", cached=False)
    assert resp.parsed == {"a": 1}


def test_wrap_non_json_still_none():
    resp = hc._wrap({"content": "죄송하지만 판단할 수 없습니다."}, "HCX-007", cached=False)
    assert resp.parsed is None


# ── 2. 추론 필드는 추론 모델에만 ─────────────────────────────────────────────

def _capture_post(monkeypatch):
    captured = {}

    def fake(url, body, provider=None):
        captured["body"] = body
        return {"status": {"code": "20000"}, "result": {"message": {"content": "{}"}}}

    monkeypatch.setattr(hc, "_do_post_v3", fake)
    return captured


def test_thinking_stage_on_non_reasoning_model_sends_max_tokens(monkeypatch):
    """A/B 에서 HCX-005 를 code_lookup(medium) 에 보내면 서버가 40001 로 거부했다."""
    captured = _capture_post(monkeypatch)
    hc._post_chat("HCX-005", "sys", "payload", stage="code_lookup")
    body = captured["body"]
    assert "thinking" not in body
    assert "maxCompletionTokens" not in body
    assert "maxTokens" in body


def test_thinking_stage_on_reasoning_model_keeps_thinking(monkeypatch):
    captured = _capture_post(monkeypatch)
    hc._post_chat("HCX-007", "sys", "payload", stage="code_lookup")
    body = captured["body"]
    assert body["thinking"]["effort"] == "medium"
    assert "maxCompletionTokens" in body


# ── 3. 하네스: parse 실패 시 content 앞부분 보존 ─────────────────────────────

def test_harness_keeps_content_head_when_parse_fails(monkeypatch, tmp_path):
    import scripts.ab_model_test as ab

    class R:
        parsed = None
        content = "```json\n{...}"
        model = "HCX-007"
        stop_reason = None
        truncated = False

    monkeypatch.setattr(hc, "call", lambda *a, **k: R())
    monkeypatch.setattr(hc, "get_call_stats", lambda: {})
    monkeypatch.setattr(hc, "_cache_put", lambda *a, **k: None)
    out = ab._run_stage("hcx007", "code_lookup", {"q": 1})
    assert out["parse_ok"] is False
    assert out["content_head"].startswith("```json")


def test_harness_omits_content_head_when_parse_ok(monkeypatch):
    import scripts.ab_model_test as ab

    class R:
        parsed = {"ok": 1}
        content = '{"ok": 1}'
        model = "HCX-007"
        stop_reason = "stop"
        truncated = False

    monkeypatch.setattr(hc, "call", lambda *a, **k: R())
    monkeypatch.setattr(hc, "get_call_stats", lambda: {})
    out = ab._run_stage("hcx007", "code_lookup", {"q": 1})
    assert "content_head" not in out


# ── 4. A-1: v3 stopReason 이 null 이면 result 키 목록을 남겨 다음 사진에서 실제 필드를 알 수 있게 ──

def test_harness_records_v3_result_keys_when_stop_reason_missing(monkeypatch):
    import scripts.ab_model_test as ab

    class R:
        parsed = {"ok": 1}
        content = '{"ok": 1}'
        model = "HCX-007"
        stop_reason = None
        truncated = False
        raw = {"_api": {"status": {"code": "20000"},
                        "result": {"message": {}, "finishReason": "stop", "usage": {}}}}

    monkeypatch.setattr(hc, "call", lambda *a, **k: R())
    monkeypatch.setattr(hc, "get_call_stats", lambda: {})
    out = ab._run_stage("hcx007", "code_lookup", {"q": 1})
    assert out["v3_result_keys"] == ["finishReason", "message", "usage"]


# ── 5. A-1 답 (2026-09-07 사내 A/B 2차, v3_result_keys): result 아래 키는
#       created · finishReason · message · seed · usage — 잘림 필드는 result.finishReason 이다. ──

def test_v3_finish_reason_is_read_from_result():
    api = {"status": {"code": "20000"},
           "result": {"created": 1, "finishReason": "stop", "message": {"content": "{}"}, "seed": 1, "usage": {}}}
    assert hc._extract_stop_reason(api) == "stop"


def test_v3_legacy_stop_reason_still_read():
    assert hc._extract_stop_reason({"result": {"stopReason": "x"}}) == "x"


def test_probe_truncation_forces_tiny_budget_and_reports_finish_reason(monkeypatch):
    """finishReason 의 '잘림' 값은 아직 모른다 (추측 금지). 일부러 상한을 아주 작게 주고 실제 값을 읽는 프로브."""
    captured = {}

    def fake(url, body, provider=None):
        captured["body"] = body
        return {"status": {"code": "20000"},
                "result": {"created": 1, "finishReason": "SOME_VALUE", "message": {"content": "{\"pong"},
                           "seed": 1, "usage": {"completionTokens": 16}}}

    monkeypatch.setattr(hc, "_do_post_v3", fake)
    out = hc.probe_truncation(max_completion_tokens=16)
    assert captured["body"]["maxCompletionTokens"] == 16
    assert "thinking" not in captured["body"]           # 추론이 예산을 먹으면 프로브가 아니다
    assert out["finish_reason"] == "SOME_VALUE"
    assert out["result_keys"] == ["created", "finishReason", "message", "seed", "usage"]
    assert out["content_head"].startswith("{\"pong")


# ── 6. 2026-09-07 g 적용 후 ② 세트 4: stop_reason='stop' 인데 parse 실패 → JSON 문법 오류, 캐시돼 매번 반복 ──

def test_lenient_parser_tolerates_trailing_commas_and_comments():
    assert hc._parse_json_lenient('```json\n{"a": 1, "b": [1, 2,],}\n```') == {"a": 1, "b": [1, 2]}
    assert hc._parse_json_lenient('{"a": 1, // 주석\n "url": "http://x/y", /* 블록 */ "c": 2}') == \
        {"a": 1, "url": "http://x/y", "c": 2}
    # 문자열 안의 '//' 와 ',}' 는 건드리지 않는다
    assert hc._parse_json_lenient('{"s": "a,}b //c"}') == {"s": "a,}b //c"}


def _live(monkeypatch, tmp_path):
    monkeypatch.setenv("NDT_HCX_MOCK", "0")
    monkeypatch.setenv("NDT_HCX_BUDGET_BYPASS", "1")
    monkeypatch.setenv("NDT_HCX_TOKEN", "t")
    monkeypatch.setenv("NDT_LLM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(hc, "DATA_DIR", tmp_path)


def test_unparseable_response_is_not_cached_and_is_dumped(monkeypatch, tmp_path, caplog):
    _live(monkeypatch, tmp_path)
    calls = []

    def fake_post(model, system_prompt, user_payload, images_b64=None, *, stage="", provider=None, **kw):
        calls.append(stage)
        return {"status": {"code": "20000"},
                "result": {"finishReason": "stop", "message": {"content": "```json\n{\"a\": <bad>}\n```"}}}

    monkeypatch.setattr(hc, "_post_chat", fake_post)
    with caplog.at_level("WARNING", logger="app.hcx_client"):
        r1 = hc.call("hcx_check", {"ping": 7})
        r2 = hc.call("hcx_check", {"ping": 7})
    assert r1.parsed is None and r2.parsed is None
    assert r2.cached is False and len(calls) == 2          # 두 번째도 실호출 — 캐시 안 됨
    dumps = list((tmp_path / "logs").rglob("parse_fail_hcx_check_*.txt"))
    assert dumps, "실패 응답 전문이 파일로 남아야 사진 없이 원인을 본다"
    assert "<bad>" in dumps[0].read_text(encoding="utf-8")
    assert any(str(dumps[0].name) in rec.getMessage() for rec in caplog.records)


def test_reasoning_model_without_thinking_gets_full_completion_budget(monkeypatch):
    """hcx-probe 실측 (2026-09-07): thinking 없이 불러도 completionTokens 16 = thinkingTokens 16.
    HCX-007 은 추론 필드가 없어도 예산을 추론에 쓰므로 비추론 경로도 max_completion_tokens 를 준다."""
    captured = _capture_post(monkeypatch)
    hc._post_chat("HCX-007", "sys", "payload", stage="scwep_extract")
    body = captured["body"]
    assert body["maxCompletionTokens"] == int(hc.hcx_config()["models"]["max_completion_tokens"])
    assert "thinking" not in body
