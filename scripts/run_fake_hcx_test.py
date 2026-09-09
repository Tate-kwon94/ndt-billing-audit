#!/usr/bin/env python3
"""가짜 HCX 서버로 hcx_client 의 실제 HTTP 왕복을 전 시나리오 검증.

mock 이 못 덮는 층: 요청 본문 구성(CamelCase·Bearer·URL 모델명·request-id),
응답 파싱(status.code·result.message.content·usage.totalTokens),
에러 분류(401→fail-fast, 42900→재시도, 40003→fail-fast).

실행: source venv/bin/activate && python scripts/run_fake_hcx_test.py
"""
from __future__ import annotations
import json, os, socket, sys, threading, time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.fake_hcx_server as fake

PORT = 8459
BASE = f"http://127.0.0.1:{PORT}"


def free_port(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); return True
    except OSError:
        return False
    finally:
        s.close()


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), fake.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _reset_client_env(mode="echo", token="testkey"):
    # config 캐시 무효화 + 환경 설정
    os.environ["NDT_HCX_BASE_URL"] = BASE
    os.environ["NDT_HCX_MOCK"] = "0"
    os.environ["NDT_HCX_BUDGET_BYPASS"] = "1"
    if token is None:
        os.environ.pop("NDT_HCX_TOKEN", None)
    else:
        os.environ["NDT_HCX_TOKEN"] = token
    from app.config import load_yaml
    load_yaml.cache_clear()


def call_with_mode(mode: str):
    """X-Fake-Mode 헤더를 주입하기 위해 hcx_client 의 헤더 빌더를 몽키패치."""
    import app.hcx_client as hc
    orig = hc._build_headers
    def patched(provider=None):
        h = orig(provider); h["X-Fake-Mode"] = mode; return h
    hc._build_headers = patched
    try:
        return hc.call("hcx_check", {"ping": 1}, force_refresh=True)
    finally:
        hc._build_headers = orig


results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def main():
    if not free_port(PORT):
        print(f"포트 {PORT} 사용 중"); sys.exit(1)
    srv = start_server()
    time.sleep(0.3)
    print(f"[fake-hcx] {BASE} 기동\n")

    import app.hcx_client as hc

    # ── 1) 정상 왕복 (echo) ──
    print("[1] 정상 v3 왕복")
    _reset_client_env(token="testkey")
    fake._log.clear()
    resp = call_with_mode("echo")
    check("응답 수신", resp is not None)
    check("content 파싱", resp.parsed == {"pong": True}, str(resp.parsed))
    check("model 반영", resp.model in ("HCX-007", "HCX-005"), resp.model)
    log = fake._log[-1]
    check("URL 에 모델명", log["model"] in ("HCX-007", "HCX-005"), log["path"])
    check("Bearer 인증 헤더 전달", log["auth_present"])
    check("X-Request-Id 헤더 전달", log["has_request_id"])
    check("CamelCase 본문(topP/topK/repetitionPenalty)", log["camelCase_ok"])
    check("maxTokens|maxCompletionTokens 존재", log["has_maxTokens"])
    stats = hc.get_call_stats()
    check("totalTokens 집계(123)", stats["token_total"] >= 123, str(stats.get("token_total")))

    # ── 2) 인증 실패 (토큰 없음 → 40100 fail-fast, 재시도 안 함) ──
    print("\n[2] 인증 실패 fail-fast")
    _reset_client_env(token=None)
    t0 = time.time()
    try:
        call_with_mode("echo")
        check("HCXClientError 발생", False, "예외 안 남")
    except Exception as e:
        check("HCXClientError(인증) 발생", type(e).__name__ == "HCXClientError", type(e).__name__)
        check("인증 메시지 한글 안내", "인증" in str(e) or "NDT_HCX_TOKEN" in str(e), str(e)[:60])
        check("재시도 없이 빠른 실패(<3s)", time.time() - t0 < 3, f"{time.time()-t0:.1f}s")

    # ── 3) 429 두 번 후 성공 (tenacity 재시도) ──
    print("\n[3] 429 재시도 후 성공")
    _reset_client_env(token="testkey")
    fake._rate_hits.clear()
    t0 = time.time()
    try:
        resp = call_with_mode("rate2")
        check("최종 성공", resp.parsed == {"pong": True}, str(resp.parsed))
        check("재시도 발생(backoff 로 >=2s)", time.time() - t0 >= 2, f"{time.time()-t0:.1f}s")
        check("서버가 3회 받음(2 실패+1 성공)", fake._rate_hits.get("k") == 3, str(fake._rate_hits.get("k")))
    except Exception as e:
        check("최종 성공", False, f"{type(e).__name__}: {e}")

    # ── 4) context 초과 (40003 fail-fast) ──
    print("\n[4] context 초과 fail-fast")
    _reset_client_env(token="testkey")
    try:
        call_with_mode("context")
        check("HCXClientError 발생", False, "예외 안 남")
    except Exception as e:
        check("HCXClientError(40003) 발생", type(e).__name__ == "HCXClientError", type(e).__name__)
        check("context 안내 메시지", "context" in str(e).lower() or "40003" in str(e), str(e)[:60])

    # ── 5) openai fallback 스타일 ──
    print("\n[5] api_style=openai fallback")
    _reset_client_env(token="testkey")
    from app.config import load_yaml
    # api_style 을 openai 로 강제 (yaml 캐시 후 파이썬에서 교체)
    cfg = load_yaml("hcx.yaml")
    orig_style = cfg["api"].get("api_style")
    cfg["api"]["api_style"] = "openai"
    try:
        resp = call_with_mode("echo")
        check("openai 응답 파싱", resp.parsed == {"pong": True}, str(resp.parsed))
        check("openai 경로 URL", fake._log[-1]["path"].startswith("/v1/openai"), fake._log[-1]["path"])
    except Exception as e:
        check("openai 왕복", False, f"{type(e).__name__}: {e}")
    finally:
        cfg["api"]["api_style"] = orig_style

    srv.shutdown()
    n_ok = sum(1 for _, ok, _ in results if ok)
    n = len(results)
    print(f"\n{'='*50}\n결과: {n_ok}/{n} 통과")
    if n_ok != n:
        print("실패 항목:")
        for name, ok, d in results:
            if not ok: print(f"  ✗ {name} — {d}")
        sys.exit(1)
    print("전 시나리오 통과 — hcx_client HTTP 계층 검증 완료")


if __name__ == "__main__":
    main()
