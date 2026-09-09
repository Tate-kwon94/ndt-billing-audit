#!/usr/bin/env python3
"""가짜 HCX 서버 — 사내망 없이 hcx_client 의 실제 HTTP 왕복을 검증하기 위한 스탠드인.

HyperCLOVA X Chat Completions v3 계약을 충실히 흉내낸다 (app/hcx_client.py 기준):
  POST /v3/chat-completions/{modelName}
    - Authorization: Bearer <token>   (없거나 빈 값이면 401-계열 40100 반환)
    - body: CamelCase (messages/topP/topK/repetitionPenalty/maxTokens|maxCompletionTokens/thinking)
    - 200 + {"status":{"code":"20000"}, "result":{"message":{"content","thinkingContent"},
             "usage":{"totalTokens"}}}
  POST /v1/openai/chat/completions  (api_style=openai fallback)
    - {"choices":[{"message":{"content"}}], "usage":{"total_tokens"}}

주입 모드 (요청 헤더 X-Fake-Mode 또는 시스템 프롬프트 내 지시로):
  - "auth"     : 토큰 없으면 40100 (fail-fast 검증)
  - "rate2"    : 처음 2회 42900(429) 반환 후 3회째 성공 (tenacity 재시도 검증)
  - "context"  : 40003 (context length — fail-fast 검증)
  - "echo"(기본): 요청을 반영한 정상 응답 (본문 구조 검증)

사용:
  python scripts/fake_hcx_server.py --port 8443
  (별도 셸) NDT_HCX_TOKEN=testkey NDT_HCX_MOCK=0 \
    NDT_HCX_BASE_URL=http://127.0.0.1:8443 python -m app.main hcx-check
"""
from __future__ import annotations
import argparse, json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 재시도 주입용 카운터 (report_no 대신 body hash 로 세지 않고, 단순 전역 카운터)
_rate_hits: dict[str, int] = {}
_log: list[dict] = []

# 추론 계열 모델 — 이 목록은 app/hcx_client.py 의 _REASONING_MODELS 와 같아야 한다.
_REASONING_MODELS = {"HCX-007"}

# GET /v1/models 가 돌려주는 서빙 모델 id 목록 — studio 점검 테스트가 바꿔 끼운다.
_MODELS = ["bge-m3", "gemma4-31b"]



# ── 가짜 임베딩: 문자 3-gram 해싱 (결정적, 1024차원 = 실제 bge-m3 와 같은 폭, L2 정규화) ──
# 진짜 의미 임베딩은 아니지만 문자열이 겹치면 코사인이 오르므로
# 하이브리드 검색·RRF·후퇴 로직을 사외에서 검증하기에 충분하다.
import hashlib as _hl

def fake_embed(text: str, dim: int = 1024) -> list[float]:
    v = [0.0] * dim
    t = " " + (text or "").lower() + " "
    for i in range(len(t) - 2):
        g = t[i:i + 3]
        h = int(_hl.md5(g.encode("utf-8")).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 8) & 1 else -1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 조용히
        pass

    def _send(self, status: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # StudioXP 모델 목록 (OpenAI 호환). studio 점검이 모델 id 를 여기서 대조한다.
        if self.path.startswith("/v1/models"):
            self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in _MODELS]})
            return
        # hcx-check 의 방화벽 도달 테스트(GET base_url)에 응답 — 아무 응답이든 "도달 성공"
        self._send(200, {"ok": True, "service": "fake-hcx"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"status": {"code": "40000", "message": "bad json"}})
            return

        auth = self.headers.get("Authorization", "")
        mode = self.headers.get("X-Fake-Mode", "echo")
        # OpenAI 호환 경로 두 가지를 모두 받는다:
        #   /v1/openai/chat/completions  — HCX 게이트웨이의 오픈AI 호환 경로
        #   /v1/chat/completions         — StudioXP(vLLM 표준). Continue 도 이 경로로 보낸다.
        # OpenAI 호환 임베딩 — Studio XP 의 bge-m3 흉내
        if self.path.startswith("/v1/embeddings"):
            inp = body.get("input")
            texts = [inp] if isinstance(inp, str) else list(inp or [])
            # 가이드: "MSL 모델이 아니면 실패해도 HTTP 200". 정확한 본문은 사내 확인(A-2) 전이라
            # 가장 불리한 모양으로 흉내낸다 — error 키와 멀쩡해 보이는 data 가 같이 온다.
            if body.get("model") == "error-200":
                self._send(200, {"error": {"message": "model not found", "type": "invalid_request_error"},
                                 "data": [{"object": "embedding", "index": i,
                                           "embedding": fake_embed(t)} for i, t in enumerate(texts)]})
                return
            self._send(200, {"object": "list", "model": body.get("model", "bge-m3"),
                             "data": [{"object": "embedding", "index": i,
                                       "embedding": fake_embed(t)} for i, t in enumerate(texts)],
                             "usage": {"prompt_tokens": 0, "total_tokens": 0}})
            return

        is_openai = self.path.startswith("/v1/openai") or self.path.startswith("/v1/chat/completions")
        model = self.path.rsplit("/", 1)[-1] if not is_openai else body.get("model", "?")

        # 계약 검증 기록 (테스트가 확인)
        entry = {
            "path": self.path, "model": model, "auth_present": auth.startswith("Bearer "),
            "has_request_id": "X-Request-Id" in self.headers,
            "camelCase_ok": all(k in body for k in ("topP", "topK", "repetitionPenalty")) if not is_openai else None,
            # 어느 필드를 보냈는지 **정확히** 기록한다.
            # 2026-09-02 이전에는 OR 조건이라 어느 쪽을 보내도 True 였고,
            # 그래서 HCX-007+maxTokens 버그를 테스트가 전혀 못 잡았다.
            "token_field": ("maxCompletionTokens" if "maxCompletionTokens" in body
                            else "maxTokens" if "maxTokens" in body else None)
                           if not is_openai else None,
            "thinking": body.get("thinking"),
            # OpenAI 호환 본문에 어느 출력 길이 필드가 실렸는지 (StudioXP 는 max_completion_tokens 만 본다)
            "openai_token_fields": (sorted(k for k in ("max_tokens", "max_completion_tokens") if k in body)
                                    if is_openai else None),
            "mode": mode,
        }
        _log.append(entry)

        # ── 모델별 토큰 필드 계약 강제 (2026-09-02 사내 실측) ──
        # HCX-007 은 추론을 껐어도 maxTokens 를 거부하고 40001 을 준다.
        # 실측된 방향만 흉내낸다 — HCX-005 가 maxCompletionTokens 를 거부하는지는
        # 확인된 바 없으므로 강제하지 않는다.
        if not is_openai and model.upper() in _REASONING_MODELS and "maxTokens" in body:
            self._send(400, {"status": {"code": "40001",
                                        "message": "parameter: maxTokens"},
                             "result": None})
            return

        # ── 인증 검증 (auth 모드 또는 항상) ──
        if not auth.startswith("Bearer ") or auth == "Bearer ":
            if is_openai:
                self._send(401, {"error": {"code": "40100", "message": "invalid token"}})
            else:
                self._send(401, {"status": {"code": "40100", "message": "invalid token"}})
            return

        # ── rate2: 처음 2회 429 후 성공 (재시도 검증) ──
        if mode == "rate2":
            n = _rate_hits.get("k", 0) + 1
            _rate_hits["k"] = n
            if n <= 2:
                self._send(429, {"status": {"code": "42900", "message": "too many requests"}})
                return

        # ── context: 40003 fail-fast ──
        if mode == "context":
            self._send(200, {"status": {"code": "40003", "message": "context length exceeded"}})
            return

        # ── echo(정상): 요청을 반영한 응답 ──
        # 시스템 프롬프트가 JSON 을 요구하면 JSON 을, 아니면 평문을.
        sys_prompt = ""
        for msg in body.get("messages", []):
            if msg.get("role") == "system":
                c = msg.get("content")
                sys_prompt = c if isinstance(c, str) else json.dumps(c)
        # hcx_check 프롬프트는 {"pong": true} 를 기대
        if "pong" in sys_prompt.lower():
            content = json.dumps({"pong": True})
        else:
            content = json.dumps({"_fake": True, "echo_model": model,
                                  "note": "fake-hcx v3 정상 응답"}, ensure_ascii=False)

        # 종료 사유. truncate 모드 = 출력 상한에 걸려 잘린 응답 (JSON 도 중간에서 끊는다).
        # OpenAI/vLLM 표준값은 "length". v3 의 실제 값은 사내 확인(A-1) 전이라 같은 문자열을 쓰되
        # 정상 종료값 "stop" 은 자리표시자다 — 테스트는 값을 그대로 기록하는지만 본다.
        truncated = (mode == "truncate")
        if truncated:
            content = content[: max(1, len(content) // 2)]
        if is_openai:
            self._send(200, {
                "choices": [{"message": {"role": "assistant", "content": content},
                             "finish_reason": "length" if truncated else "stop"}],
                "usage": {"total_tokens": 123},
            })
        else:
            self._send(200, {
                "status": {"code": "20000", "message": "OK"},
                "result": {
                    "message": {"role": "assistant", "content": content,
                                "thinkingContent": "(fake 추론 과정)"},
                    # 실 v3 result 키 (2026-09-07 사내 확인): created·finishReason·message·seed·usage.
                    # 잘림 값은 아직 미확인 — 여기 "length" 는 가짜 서버의 가정일 뿐이다.
                    "finishReason": "length" if truncated else "stop",
                    "usage": {"promptTokens": 50, "completionTokens": 73, "totalTokens": 123},
                },
            })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dump-log", help="종료 시 요청 검증 로그를 이 파일에 JSON 으로 저장")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[fake-hcx] listening on http://{args.host}:{args.port}  (Ctrl-C 종료)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if args.dump_log:
            with open(args.dump_log, "w", encoding="utf-8") as f:
                json.dump(_log, f, ensure_ascii=False, indent=2)
            print(f"[fake-hcx] 로그 {len(_log)}건 저장: {args.dump_log}")


if __name__ == "__main__":
    main()
