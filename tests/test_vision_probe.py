"""사내 모델 중 어느 것이 이미지를 받는가 — 확인용 프로브 (2026-09-07 사용자: "사내에 다양한 ai 모델 있지않나").

지금까지 비전은 HCX-005 뿐이라고 **가정**해 왔다 (Studio XP 가이드의 "텍스트 모델 관련 필드만 지원" 문장).
그러나 gemma4-31b·qwen36-27b 는 상위 계열에 비전판이 있어 실제로 받을 수도 있다. 추측 대신 서버에 묻는다.
표 전사 정확도가 판정 근거의 질을 좌우하므로, 비전 가능한 모델이 늘면 A/B 대상이 늘어난다.
"""
import app.hcx_client as hc
from app import vision_probe


def _fake_post(monkeypatch, behaviour):
    """provider 이름 → 응답(또는 예외) 을 주는 가짜 전송."""
    def fake(model, system_prompt, user_payload, images_b64=None, *, stage="", provider=None, **kw):
        name = (provider or {}).get("model", model)
        r = behaviour[name]
        if isinstance(r, Exception):
            raise r
        assert images_b64, "프로브는 이미지를 실어야 한다"
        return r
    monkeypatch.setattr(hc, "_post_chat", fake)


def test_probe_reports_accept_and_reject_per_model(monkeypatch):
    _fake_post(monkeypatch, {
        "HCX-005": {"status": {"code": "20000"}, "result": {"message": {"content": "빨간 사각형"}}},
        "gemma4-31b": {"choices": [{"message": {"content": "a red square"}, "finish_reason": "stop"}]},
        "gpt-oss-120b": hc.HCXClientError("HCX 요청 파라미터 오류 (code=40001)"),
        "qwen36-27b": hc.HCXError("일시 오류"),
    })
    out = vision_probe.run(["hcx005", "gemma", "gptoss", "qwen"])
    by = {r["provider"]: r for r in out}
    assert by["hcx005"]["vision"] is True and "빨간" in by["hcx005"]["answer"]
    assert by["gemma"]["vision"] is True
    assert by["gptoss"]["vision"] is False and "40001" in by["gptoss"]["error"]
    assert by["qwen"]["vision"] is None          # 일시 오류는 판정 보류 — 거부로 세지 않는다
    assert [r["provider"] for r in out] == ["hcx005", "gemma", "gptoss", "qwen"]


def test_probe_image_is_a_small_known_picture():
    b = vision_probe.probe_image_png()
    assert b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) < 20000


def test_unknown_provider_is_refused():
    out = vision_probe.run(["nope"])
    assert out[0]["vision"] is None and "알 수 없는" in out[0]["error"]
