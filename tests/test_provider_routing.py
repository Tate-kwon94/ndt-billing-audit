"""stage별 provider 라우팅 검증 — 서로 다른 endpoint(HCX v3 / OpenAI 호환)로
stage 를 나눠 보낼 수 있는지, 가짜 서버 2대로 확인.
"""
from __future__ import annotations
import threading
from http.server import ThreadingHTTPServer

import pytest

import tests.fake_hcx_server as fake


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fake.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.fixture()
def two_servers():
    fake._log.clear()
    a, pa = _serve()   # HCX v3 흉내
    b, pb = _serve()   # OpenAI 호환(Gemma/gpt-oss) 흉내
    yield pa, pb
    a.shutdown(); b.shutdown()


def _configure(monkeypatch, hcx_port, open_port, stage_providers):
    """providers/stage_providers 를 yaml 캐시에 주입."""
    from app.config import load_yaml
    load_yaml.cache_clear()
    cfg = load_yaml("hcx.yaml")
    cfg["providers"] = {
        "hcx":  {"base_url": f"http://127.0.0.1:{hcx_port}", "api_style": "v3",
                 "token_env": "NDT_HCX_TOKEN"},
        "gemma": {"base_url": f"http://127.0.0.1:{open_port}", "api_style": "openai",
                  "model": "gemma-4-31b", "token_env": "NDT_GEMMA_TOKEN"},
    }
    cfg["default_provider"] = "hcx"
    cfg["stage_providers"] = stage_providers
    monkeypatch.setenv("NDT_HCX_MOCK", "0")
    monkeypatch.setenv("NDT_HCX_BUDGET_BYPASS", "1")
    monkeypatch.setenv("NDT_HCX_TOKEN", "hcxkey")
    monkeypatch.setenv("NDT_GEMMA_TOKEN", "gemmakey")


def test_stage_routes_to_correct_provider(monkeypatch, two_servers):
    """code_lookup → gemma(openai 경로·gemma 모델), 그 외 → hcx(v3 경로)."""
    hcx_port, open_port = two_servers
    _configure(monkeypatch, hcx_port, open_port,
               {"code_lookup": "gemma", "compliance_explain": "hcx"})
    import app.hcx_client as hc

    # code_lookup → gemma (OpenAI 호환)
    hc.call("code_lookup", {"q": 1}, force_refresh=True)
    gemma_hit = fake._log[-1]
    assert gemma_hit["path"].startswith("/v1/openai"), gemma_hit["path"]
    assert gemma_hit["model"] == "gemma-4-31b"

    # compliance_explain → hcx (v3)
    hc.call("compliance_explain", {"q": 2}, force_refresh=True)
    hcx_hit = fake._log[-1]
    assert hcx_hit["path"].startswith("/v3/chat-completions/"), hcx_hit["path"]
    assert hcx_hit["model"] in ("HCX-007", "HCX-005")


def test_default_provider_when_unmapped(monkeypatch, two_servers):
    """stage_providers 에 없는 stage 는 default_provider(hcx)로."""
    hcx_port, open_port = two_servers
    _configure(monkeypatch, hcx_port, open_port, {"code_lookup": "gemma"})
    import app.hcx_client as hc
    hc.call("matching_judge", {"q": 3}, force_refresh=True)   # 미매핑 → hcx
    assert fake._log[-1]["path"].startswith("/v3/chat-completions/")


def test_per_provider_token(monkeypatch, two_servers):
    """provider 마다 다른 token_env 를 쓴다 (gemma 는 NDT_GEMMA_TOKEN)."""
    hcx_port, open_port = two_servers
    _configure(monkeypatch, hcx_port, open_port, {"code_lookup": "gemma"})
    # gemma 토큰만 지우면 gemma 라우팅 stage 는 인증 실패, hcx stage 는 정상
    monkeypatch.delenv("NDT_GEMMA_TOKEN", raising=False)
    import app.hcx_client as hc
    from app.hcx_client import HCXClientError
    with pytest.raises(HCXClientError):
        hc.call("code_lookup", {"q": 4}, force_refresh=True)   # gemma 토큰 없음 → 401
    # hcx 는 여전히 정상
    r = hc.call("compliance_explain", {"q": 5}, force_refresh=True)
    assert r is not None


def test_backward_compat_no_providers(monkeypatch, two_servers):
    """providers 미설정 시 기존 단일 endpoint(api 블록)로 동작 (하위호환)."""
    from app.config import load_yaml
    hcx_port, _ = two_servers
    load_yaml.cache_clear()
    cfg = load_yaml("hcx.yaml")
    cfg.pop("providers", None); cfg.pop("stage_providers", None); cfg.pop("default_provider", None)
    cfg["api"]["base_url"] = f"http://127.0.0.1:{hcx_port}"
    monkeypatch.setenv("NDT_HCX_MOCK", "0")
    monkeypatch.setenv("NDT_HCX_BUDGET_BYPASS", "1")
    monkeypatch.setenv("NDT_HCX_TOKEN", "k")
    import app.hcx_client as hc
    hc.call("code_lookup", {"q": 6}, force_refresh=True)
    assert fake._log[-1]["path"].startswith("/v3/chat-completions/")
