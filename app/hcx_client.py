"""HyperCLOVA X (HCX) 호출 래퍼 — Chat Completions v3 (사내 발급 실 스펙).

설계 메모:
- 사내 발급 endpoint = Chat Completions v3 (POST /v3/chat-completions/{modelName}).
  사내 AI추진부 발급 (2026-04-09), 가이드 "CLOVA Studio for the client".
- 인증: API Key (Bearer). 환경변수 NDT_HCX_TOKEN. 키는 파일에 안 적음.
- 발급 모델 = HCX-005(vision)/HCX-007(text·추론·structured). DASH-002 미발급.
- ⚠ 일일 호출 한도 10,000회 → call_budget 의 per_day 트래커로 보호 (data/hcx_daily_count.json).
- temperature=0 결정성. 동일 입력 재실행 가속 + 호출 절약을 위해 로컬 캐시.
- 사외 개발 시 NDT_HCX_MOCK=1 로 fixtures mock.
- api_style="openai" 로 두면 오픈AI 호환 endpoint 형식도 지원 (fallback).
"""
from __future__ import annotations

import collections
import datetime
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import DATA_DIR, PROJECT_ROOT, hcx_config, load_prompt

logger = logging.getLogger(__name__)


# ─────────────────────────── Call budget tracker ───────────────────────────


_call_count_total = 0
_call_count_by_stage: dict[str, int] = collections.Counter()
_call_count_by_model: dict[str, int] = collections.Counter()
_call_timestamps: list[float] = []   # epoch seconds, sliding 1-hour window
_token_total = 0                      # 누적 totalTokens (실 호출만)
_budget_warned_run = False
_budget_warned_hour = False
_budget_warned_day = False


def _daily_count_path() -> Path | None:
    cfg = hcx_config().get("call_budget", {}) or {}
    rel = cfg.get("daily_count_file")
    if not rel:
        return None
    return PROJECT_ROOT / rel


def _read_daily_file() -> dict:
    """data/hcx_daily_count.json → {"date","count","days":{iso:{calls,tokens}}}. 옛 모양({date,count})도 읽는다."""
    path = _daily_count_path()
    if not path or not path.exists():
        return {"days": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"days": {}}
    days = data.get("days")
    if not isinstance(days, dict):
        days = {}
        if data.get("date") and data.get("count") is not None:      # 2026-09-07 이전 파일: 하루치만
            days[str(data["date"])] = {"calls": int(data.get("count", 0)), "tokens": 0}
    data["days"] = days
    return data


def _load_daily_count(today: "datetime.date | None" = None) -> int:
    """오늘 날짜의 누적 호출 수 (프로세스 재실행 간 영속). 날짜 바뀌면 0."""
    d = (today or datetime.date.today()).isoformat()
    return int((_read_daily_file()["days"].get(d) or {}).get("calls", 0))


def _bump_daily(*, calls: int = 0, tokens: int = 0, day: "datetime.date | None" = None) -> None:
    """날짜별 호출·토큰을 쌓는다. 옛 필드(date/count)도 오늘 값으로 같이 유지한다."""
    path = _daily_count_path()
    if not path:
        return
    d = (day or datetime.date.today()).isoformat()
    try:
        data = _read_daily_file()
        cur = data["days"].setdefault(d, {"calls": 0, "tokens": 0})
        cur["calls"] = int(cur.get("calls", 0)) + int(calls)
        cur["tokens"] = int(cur.get("tokens", 0)) + int(tokens)
        data["date"], data["count"] = d, cur["calls"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def usage_summary(today: "datetime.date | None" = None) -> dict:
    """오늘 · 최근 7일 · 이번 달 · 전체 {calls, tokens}. 사내 호출량 통보용 (한도가 아니라 확인용)."""
    today = today or datetime.date.today()
    days = _read_daily_file()["days"]
    week_from = (today - datetime.timedelta(days=6)).isoformat()
    month_prefix = today.strftime("%Y-%m")
    out = {k: {"calls": 0, "tokens": 0} for k in ("today", "week", "month", "all")}
    for d, v in days.items():
        c, t = int(v.get("calls", 0)), int(v.get("tokens", 0))
        buckets = ["all"]
        if d == today.isoformat():
            buckets.append("today")
        if week_from <= d <= today.isoformat():
            buckets.append("week")
        if d.startswith(month_prefix):
            buckets.append("month")
        for b in buckets:
            out[b]["calls"] += c; out[b]["tokens"] += t
    out["days"] = len(days)
    return out


def usage_line_now() -> str:
    """지금까지의 사용량 한 줄 (런처가 파싱). 이번 실행 호출·오늘·토큰·이번주·이번달."""
    from app.progress_fmt import usage_line
    s = usage_summary()
    return usage_line(calls=_call_count_total, day=s["today"]["calls"], tokens=_token_total,
                      week=s["week"]["calls"], month=s["month"]["calls"])


def _save_daily_count(count: int) -> None:
    """(호환) 예전 호출부용 — 지금은 _bump_daily 가 쓴다."""
    path = _daily_count_path()
    if not path:
        return
    try:
        cur = _load_daily_count()
        _bump_daily(calls=max(0, int(count) - cur))
    except Exception as e:
        logger.debug("일일 카운트 저장 실패: %s", e)


def _budget_check_and_record(stage: str, model: str, *, is_live: bool) -> None:
    """호출 직전 예산 검사 + 카운터 갱신. 임계 WARN, hard cap RuntimeError.

    일일 한도(per_day)는 실 호출(is_live)만 영속 파일에 누적 — 사내 신청 한도 10,000/일 보호.
    mock 은 run/hour 카운트만 (사용자가 예산 영향 미리 체험).
    """
    global _call_count_total, _token_total
    global _budget_warned_run, _budget_warned_hour, _budget_warned_day

    cfg = hcx_config().get("call_budget", {}) or {}

    def _int_or_none(v):
        return None if v is None else int(v)

    run_warn = _int_or_none(cfg.get("per_run_warn", 5000))
    run_hard = _int_or_none(cfg.get("per_run_hard"))
    hour_warn = _int_or_none(cfg.get("per_hour_warn", 8000))
    hour_hard = _int_or_none(cfg.get("per_hour_hard"))
    day_warn = _int_or_none(cfg.get("per_day_warn", 8000))
    day_hard = _int_or_none(cfg.get("per_day_hard"))
    summary_every = int(cfg.get("print_summary_every", 200))
    # 본사 요청으로 한도 상향 가능 → 기본 hard cap 비활성 (경고만).
    enforce_hard = bool(cfg.get("enforce_hard_cap", False))

    bypass = os.environ.get("NDT_HCX_BUDGET_BYPASS") == "1"

    # 1시간 sliding window 갱신
    now = time.time()
    cutoff = now - 3600
    while _call_timestamps and _call_timestamps[0] < cutoff:
        _call_timestamps.pop(0)
    hour_count = len(_call_timestamps)

    # 일일 카운트 (실 호출만)
    day_count = _load_daily_count() if is_live else 0

    # hard cap 검사 — enforce_hard_cap=true 이고 해당 hard 값이 설정된 경우만 차단
    if enforce_hard and not bypass:
        if run_hard is not None and _call_count_total >= run_hard:
            raise RuntimeError(
                f"HCX 호출 예산 초과 (run 당 {run_hard}). "
                f"config/hcx.yaml call_budget 조정 또는 NDT_HCX_BUDGET_BYPASS=1."
            )
        if hour_hard is not None and hour_count >= hour_hard:
            raise RuntimeError(
                f"HCX 호출 예산 초과 (1시간 {hour_hard}). NDT_HCX_BUDGET_BYPASS=1 override."
            )
        if is_live and day_hard is not None and day_count >= day_hard:
            raise RuntimeError(
                f"HCX 일일 호출 한도 도달 ({day_count}/{day_hard}). "
                f"본사(AI추진부)에 한도 상향 요청 또는 NDT_HCX_BUDGET_BYPASS=1."
            )

    # 경고 (warn 값이 설정된 경우만) — hard cap 비활성이어도 항상 동작
    if run_warn is not None and _call_count_total >= run_warn and not _budget_warned_run:
        logger.warning(
            "⚠ HCX 호출 %d 회 도달 (run 임계 %d). 사용자 통보 권장.",
            _call_count_total, run_warn,
        )
        _budget_warned_run = True
    if hour_warn is not None and hour_count >= hour_warn and not _budget_warned_hour:
        logger.warning("⚠ HCX 호출 %d 회/시간 도달 (임계 %d).", hour_count, hour_warn)
        _budget_warned_hour = True
    if is_live and day_warn is not None and day_count >= day_warn and not _budget_warned_day:
        logger.warning(
            "⚠ HCX 일일 호출 %d 회 도달 (임계 %d). "
            "한도 상향 필요 시 본사(AI추진부) 통보 — data/hcx_daily_count.json 실적 첨부.",
            day_count, day_warn,
        )
        _budget_warned_day = True

    if summary_every > 0 and _call_count_total > 0 and _call_count_total % summary_every == 0:
        top_stages = ", ".join(f"{s}:{c}" for s, c in _call_count_by_stage.most_common(5))
        top_models = ", ".join(f"{m}:{c}" for m, c in _call_count_by_model.most_common())
        logger.info(
            "HCX 진행: total=%d, hour=%d, day=%d | tokens=%d | by_stage(top5)={%s} | by_model={%s}",
            _call_count_total, hour_count, day_count, _token_total, top_stages, top_models,
        )
        logger.info(usage_line_now())          # 런처가 읽는 한 줄 (사용자 2026-09-07: 호출·토큰 사용량 표시)

    # 카운터 갱신 (call 실행 직전)
    _call_count_total += 1
    _call_count_by_stage[stage] += 1
    _call_count_by_model[model] += 1
    _call_timestamps.append(now)
    if is_live:
        _bump_daily(calls=1)


def _record_tokens(total_tokens: int) -> None:
    global _token_total
    _token_total += int(total_tokens or 0)
    if total_tokens:
        _bump_daily(tokens=int(total_tokens))       # 날짜별 토큰 누적 (오늘·주·월 합계용)


def get_call_stats() -> dict:
    """현재 run 의 호출 통계 (디버그·진행 보고용)."""
    return {
        "total": _call_count_total,
        "by_stage": dict(_call_count_by_stage),
        "by_model": dict(_call_count_by_model),
        "hour_window": len(_call_timestamps),
        "day_count": _load_daily_count(),
        "token_total": _token_total,
    }


# ─────────────────────────── Data classes ───────────────────────────


@dataclass
class HCXResponse:
    content: str            # 모델의 raw 텍스트 응답
    parsed: Any | None      # JSON 파싱 결과 (실패 시 None)
    model: str
    cached: bool
    raw: dict               # 원본 응답 (디버깅용)
    stop_reason: str | None = None   # v3 result.stopReason / openai finish_reason 을 해석 없이 그대로
    truncated: bool = False          # stop_reason 이 models.truncation_values 에 있음 → 출력이 상한에서 잘림


# ─────────────────────────── Cache ───────────────────────────


def _cache_key(model: str, prompt: str, payload: dict) -> str:
    h = hashlib.sha256()
    h.update(b"mock\x00" if _mock_enabled() else b"live\x00")  # mock 답이 live 에서 돌아오지 않게 (감사 ⑦)
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(prompt.encode("utf-8"))
    h.update(b"\x00")
    h.update(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


def _cache_path(key: str) -> Path:
    # 환경변수 override 가 있으면 우선 (디버그 번들 import 시 사내 캐시 활성화 용도)
    override = os.environ.get("NDT_LLM_CACHE_DIR")
    if override:
        cache_dir = Path(override)
    else:
        cfg = hcx_config().get("cache", {})
        cache_dir = PROJECT_ROOT / cfg.get("directory", "data/llm_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{key}.json"


def _cache_get(key: str) -> dict | None:
    if not hcx_config().get("cache", {}).get("enabled", True):
        return None
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_put(key: str, data: dict) -> None:
    if not hcx_config().get("cache", {}).get("enabled", True):
        return
    _cache_path(key).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─────────────────────────── Mock ───────────────────────────


def _mock_enabled() -> bool:
    env = hcx_config().get("mock", {}).get("enabled_if_env", "NDT_HCX_MOCK")
    return os.environ.get(env) == "1"


def _mock_response(stage: str, payload: dict) -> dict:
    """fixtures 파일에서 stage 별 mock 응답을 찾는다. 없으면 generic placeholder.

    fixture 안에 '_mock_response_format' (string) 이 있으면 그것을 content 로 직접 사용
    (vision 등 자연어 응답 stage 용). 없으면 fixture dict 를 JSON 직렬화.
    """
    fixtures_rel = hcx_config().get("mock", {}).get("fixtures_path", "tests/fixtures/hcx_mock.json")
    fixtures_path = PROJECT_ROOT / fixtures_rel
    if fixtures_path.exists():
        try:
            fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
            if stage in fixtures:
                fx = fixtures[stage]
                if isinstance(fx, dict) and "_mock_response_format" in fx:
                    return {"content": fx["_mock_response_format"]}
                return {"content": json.dumps(fx, ensure_ascii=False)}
        except Exception as e:
            logger.warning("Mock fixtures parse failed: %s", e)
    # Placeholder
    return {
        "content": json.dumps(
            {
                "_mock": True,
                "stage": stage,
                "note": "Set NDT_HCX_MOCK=0 and configure config/hcx.yaml to call the real HCX endpoint.",
            },
            ensure_ascii=False,
        )
    }


# ─────────────────────────── HTTP call ───────────────────────────


class HCXError(Exception):
    """재시도 대상 (5xx / 408 / 424 / 429 / 네트워크)."""
    pass


class HCXClientError(Exception):
    """재시도 불가 (4xx 클라이언트 오류 — 인증·파라미터·context length 등)."""
    pass


# v3 status.code / HTTP status 중 재시도 가능한 것 (서버·일시 오류)
_RETRYABLE_HTTP = {408, 424, 429, 500, 501, 502, 503, 504}
_RETRYABLE_BODY_CODES = {
    "40800",  # Timeout
    "42400",  # Processing Failed
    "42900",  # Too many requests (rate limit)
    "42903",  # Too many requests - image queue
    "50000", "50100", "50400",  # 서버 공통
    "64000", "64424", "64500", "65002", "64429",  # 라우터/서버
}


def _resolve_model(stage: str) -> str:
    cfg = hcx_config()["models"]
    overrides = hcx_config().get("stage_overrides", {})
    return overrides.get(stage, cfg["default"])


_VALID_THINKING_EFFORTS = {"none", "low", "medium", "high"}


def _thinking_effort(stage: str) -> str | None:
    """stage 가 추론 사용 stage 면 유효 effort 문자열, 아니면 None.

    config 오탈자 등 무효값은 API 40009/40001 오류를 유발하므로
    경고 후 None(추론 미사용)으로 fallback.
    """
    effort = (hcx_config().get("thinking_stages", {}) or {}).get(stage)
    if effort is None:
        return None
    if not isinstance(effort, str) or effort not in _VALID_THINKING_EFFORTS:
        logger.warning(
            "hcx.yaml thinking_stages['%s']=%r 는 유효값(%s)이 아님 — 추론 미사용으로 fallback",
            stage, effort, "/".join(sorted(_VALID_THINKING_EFFORTS)),
        )
        return None
    return effort


def _resolve_provider(stage: str) -> dict:
    """stage 를 어느 모델 서버(provider)로 보낼지 결정.

    config 예시 (모두 선택 — 없으면 기존 top-level api 블록을 default provider 로 사용, 하위호환):
      providers:
        hcx:    {base_url, api_style: v3, token_env: NDT_HCX_TOKEN}
        gemma:  {base_url, api_style: openai, model: "gemma-4-31b", token_env: NDT_GEMMA_TOKEN}
        gptoss: {base_url, api_style: openai, model: "gpt-oss-120b"}
      default_provider: hcx
      stage_providers: {code_lookup: gemma, matching_judge: gemma, compliance_explain: hcx}

    provider 에 model 이 있으면 그 모델명을 URL/본문에 사용, 없으면 stage_overrides(_resolve_model).
    retry·timeout 은 top-level api 에서 상속(미지정 시).
    """
    cfg = hcx_config()
    api = cfg.get("api", {}) or {}
    providers = cfg.get("providers") or {}
    stage_providers = cfg.get("stage_providers") or {}
    name = stage_providers.get(stage) or cfg.get("default_provider")

    if name and name in providers:
        return resolve_provider_by_name(name)
    else:
        # 하위호환: 기존 단일 endpoint 를 default provider 로
        p = {
            "base_url": api.get("base_url", ""),
            "api_style": api.get("api_style", "v3"),
            "auth_type": api.get("auth_type", "bearer"),
            "token_env": api.get("token_env", "NDT_HCX_TOKEN"),
            "extra_headers": api.get("extra_headers") or {},
            "send_request_id": api.get("send_request_id", False),
        }
    # 공통 필드 상속·기본값
    p.setdefault("api_style", "v3")
    p.setdefault("chat_path", "/v1/openai/chat/completions")  # openai 경로. StudioXP 는 /v1/chat/completions
    p.setdefault("auth_type", "bearer")
    p.setdefault("token_env", "NDT_HCX_TOKEN")
    p.setdefault("extra_headers", {})
    p.setdefault("send_request_id", api.get("send_request_id", False))
    p.setdefault("timeout_seconds", api.get("timeout_seconds", 180))
    p.setdefault("retry", api.get("retry", {
        "max_attempts": 5, "initial_backoff_seconds": 2, "max_backoff_seconds": 60}))
    return p


def resolve_provider_by_name(name: str) -> dict:
    """hcx.yaml providers 의 항목 하나를 **기본값(retry·timeout·chat_path…)까지 채워** 돌려준다.

    2026-09-08 사내: vision-probe 가 providers[name] 을 날것으로 _post_chat 에 넘겨 재시도 데코레이터가
    `KeyError: 'retry'` 로 죽었다. 이름으로 provider 를 고르는 곳(프로브·벤치·하네스)은 전부 이 함수를 쓴다.
    모르는 이름은 조용히 기본 endpoint 로 후퇴하지 않고 KeyError 를 낸다.
    """
    cfg = hcx_config()
    api = cfg.get("api", {}) or {}
    providers = cfg.get("providers") or {}
    if name not in providers:
        raise KeyError(f"알 수 없는 provider: {name!r} (hcx.yaml providers: {', '.join(sorted(providers)) or '없음'})")
    p = dict(providers[name])
    p.setdefault("api_style", "v3")
    p.setdefault("chat_path", "/v1/openai/chat/completions")
    p.setdefault("auth_type", "bearer")
    p.setdefault("token_env", "NDT_HCX_TOKEN")
    p.setdefault("extra_headers", {})
    p.setdefault("send_request_id", api.get("send_request_id", False))
    p.setdefault("timeout_seconds", api.get("timeout_seconds", 180))
    p.setdefault("retry", api.get("retry", {
        "max_attempts": 5, "initial_backoff_seconds": 2, "max_backoff_seconds": 60}))
    return p


def _trust_env() -> bool:
    """httpx 가 프록시 환경변수를 따를지.

    2026-09-02 사내: curl 은 직접 나가서 401 을 받는데 앱만 ConnectTimeout 이
    났다. httpx 는 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY 를 자동으로 따르므로,
    PC 에 프록시 변수가 잡혀 있으면 curl 과 다른 경로로 나간다.
    config 의 api.ignore_proxy: true 또는 환경변수 NDT_HCX_IGNORE_PROXY=1 로
    프록시를 무시하고 직접 나가게 할 수 있다.
    """
    if os.environ.get("NDT_HCX_IGNORE_PROXY", "").strip() not in ("", "0"):
        return False
    api = hcx_config().get("api", {})
    return not bool(api.get("ignore_proxy", False))


def _build_headers(provider: dict | None = None) -> dict:
    if provider is None:
        provider = _resolve_provider("")
    headers = dict(provider.get("extra_headers") or {})
    headers["Content-Type"] = "application/json"
    if provider.get("auth_type", "bearer") == "bearer":
        token = os.environ.get(provider.get("token_env", "NDT_HCX_TOKEN"), "").strip()
        # 2026-09-02 사내 사고: 환경변수 값에 "Bearer " 를 같이 넣으면
        # "Bearer Bearer eyJ..." 가 되어 40102 로 거부된다. 비개발자가
        # 실제로 이렇게 등록했으므로 코드에서 막는다 (문서 안내만으로는 부족).
        if token.lower().startswith("bearer "):
            logger.warning("환경변수 %s 값에 'Bearer ' 가 포함돼 있어 자동으로 제거했습니다.",
                           provider.get("token_env", "NDT_HCX_TOKEN"))
            token = token[7:].strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    if provider.get("send_request_id", False):
        headers["X-Request-Id"] = uuid.uuid4().hex
    return headers


def _retry_decorator(provider: dict | None = None):
    retry_cfg = (provider or _resolve_provider(""))["retry"]
    return retry(
        retry=retry_if_exception_type((httpx.HTTPError, HCXError)),
        stop=stop_after_attempt(retry_cfg["max_attempts"]),
        wait=wait_exponential(
            multiplier=retry_cfg["initial_backoff_seconds"],
            max=retry_cfg["max_backoff_seconds"],
        ),
        reraise=True,
    )


def _post_chat(model: str, system_prompt: str, user_payload: str,
                images_b64: list[str] | None = None, *, stage: str = "",
                provider: dict | None = None, max_output_override: int | None = None) -> dict:
    """Chat Completions v3 호출. api_style=="openai" 면 오픈AI 호환 형식.

    images_b64 있으면 vision (HCX-005, dataUri.data). 추론 stage 면 maxCompletionTokens+thinking.
    provider 로 endpoint·api_style 을 stage별 라우팅 (미지정 시 stage 로 해석).
    """
    if provider is None:
        provider = _resolve_provider(stage)
    m = hcx_config()["models"]
    style = provider.get("api_style", "v3")

    if style == "openai":
        return _post_chat_openai(model, system_prompt, user_payload, images_b64, provider=provider)

    # ── Chat Completions v3 네이티브 ──
    url = provider["base_url"].rstrip("/") + f"/v3/chat-completions/{model}"

    # 메시지 빌드. v3 vision: content array + dataUri.data (data:image/...;base64,..)
    if images_b64:
        user_content: list[dict] = []
        for img_b64 in images_b64:
            user_content.append({
                "type": "image_url",
                "dataUri": {"data": f"data:image/png;base64,{img_b64}"},
            })
        user_content.append({"type": "text", "text": user_payload})
        user_msg: dict = {"role": "user", "content": user_content}
    else:
        user_msg = {"role": "user", "content": user_payload}

    body: dict = {
        "messages": [
            {"role": "system", "content": system_prompt},
            user_msg,
        ],
        "temperature": m.get("temperature", 0.0),
        "topP": m.get("top_p", 0.8),
        "topK": m.get("top_k", 0),
        "repetitionPenalty": m.get("repetition_penalty", 1.1),
    }

    # 추론 stage (HCX-007 thinking) → maxCompletionTokens + thinking.effort.
    # 이미지 입력(HCX-005)은 추론 미지원 → 추론 무시.
    # 추론 필드는 **추론 모델에만** 싣는다. 2026-09-07 A/B: HCX-005 를 code_lookup(medium) 에
    # 보냈더니 서버가 40001 "Invalid parameter: maxCompletionTokens" 로 6/12 거부.
    reasoning_model = _uses_completion_tokens(model, m)
    effort = _thinking_effort(stage) if (not images_b64 and reasoning_model) else None
    if not images_b64 and not reasoning_model and _thinking_effort(stage):
        logger.debug("HCX %s | model=%s 은 추론 모델이 아니라 thinking 생략 (maxTokens 사용)", stage, model)
    if effort:
        body["thinking"] = {"effort": effort}
        body["maxCompletionTokens"] = int(m.get("max_completion_tokens", 5120))
    elif _uses_completion_tokens(model, m):
        # 추론 계열 모델은 thinking 을 끄더라도 maxTokens 를 받지 않는다. 그리고 hcx-probe 실측(2026-09-07):
        # thinking 필드 없이도 completionTokens 16 = thinkingTokens 16 — 예산을 추론에 쓴다. 그래서 비추론
        # 경로도 max_completion_tokens 를 준다 (4096 은 숨은 추론에 깎여 답이 잘릴 수 있다).
        body["maxCompletionTokens"] = int(m.get("max_completion_tokens", 5120))
    else:
        body["maxTokens"] = int(m.get("max_tokens", 4096))
    if max_output_override is not None:
        # 잘림 프로브 전용 — 어느 키가 들어갔든 그 값을 덮는다.
        key = "maxCompletionTokens" if "maxCompletionTokens" in body else "maxTokens"
        body[key] = int(max_output_override)

    decorated = _retry_decorator(provider)(_do_post_v3)
    return decorated(url, body, provider)


def _post_chat_openai(model: str, system_prompt: str, user_payload: str,
                       images_b64: list[str] | None, *, provider: dict | None = None) -> dict:
    """오픈AI 호환 endpoint (fallback). image_url.url 형식, choices[] 응답.

    gpt-oss·Gemma 등 온프렘 vLLM/Ollama 서빙이 이 형식. provider 로 base_url·model 지정.
    """
    if provider is None:
        provider = _resolve_provider("")
    m = hcx_config()["models"]
    # chat_path 는 provider 별 설정. 기본은 HCX 오픈AI호환 경로(/v1/openai/…),
    # StudioXP(vLLM 표준)는 /v1/chat/completions 를 쓴다. (StudioXP 사용 가이드 p29)
    chat_path = provider.get("chat_path", "/v1/openai/chat/completions")
    url = provider["base_url"].rstrip("/") + chat_path
    if images_b64:
        user_content: list[dict] = [{"type": "text", "text": user_payload}]
        for img_b64 in images_b64:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            })
    else:
        user_content = user_payload
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": m.get("temperature", 0.0),
        # StudioXP 가이드: max_completion_tokens 만 지원, 미지원 필드는 오류 없이 무시.
        # 그래서 둘 다 보낸다 — vLLM·OpenAI 는 둘 다 받고, 한쪽만 보는 서버도 상한이 걸린다.
        # max_tokens 만 보내면 상한이 안 걸려 JSON 이 서버 기본값에서 잘리고, A/B 하네스는
        # 그것을 parse_ok=False 로 세어 모델 탓을 한다.
        "max_tokens": int(m.get("max_tokens", 4096)),
        "max_completion_tokens": int(m.get("max_tokens", 4096)),
    }
    decorated = _retry_decorator(provider)(_do_post_openai)
    return decorated(url, body, provider)


# 추론(reasoning) 계열 모델은 thinking 을 켰든 껐든 maxTokens 를 거부하고
# maxCompletionTokens 만 받는다.
#
# 2026-09-02 사내 PC 실측:
#   POST /v3/chat-completions/HCX-007 + {"maxTokens": ...}
#     -> HTTP 400 code=40001 "parameter: maxTokens ... ensure all parameter
#        values, types, and formats are correct"
#   같은 요청을 {"maxCompletionTokens": ...} 로 바꾸면 code=20000 정상 응답.
#
# 처음에는 config 주석대로 "추론을 쓸 때만" 필드를 바꿨는데, 실제로는
# **모델 기준** 규칙이었다. hcx-check 는 기본 모델(HCX-007)로 추론 없이
# 호출하므로 정확히 이 구멍에 빠졌다.
_REASONING_MODELS = {"HCX-007"}


def _uses_completion_tokens(model: str, cfg_models: dict) -> bool:
    """이 모델이 maxTokens 대신 maxCompletionTokens 를 요구하는가.

    config 의 models.reasoning_models 로 목록을 늘릴 수 있다 (코드 수정 불요).
    """
    raw = cfg_models.get("reasoning_models") or []
    # yaml 에 대괄호를 빠뜨리면 문자열이 와서 한 글자씩 순회된다(조용한 실패).
    # 숫자를 쓰면 아예 TypeError 로 죽는다. 비개발자가 편집하는 값이므로 방어한다.
    if isinstance(raw, str):
        raw = [raw]
    elif not isinstance(raw, (list, tuple, set)):
        logger.warning("hcx.yaml models.reasoning_models=%r 는 목록이 아님 — 무시", raw)
        raw = []
    extra = {str(x).strip().upper() for x in raw}
    return str(model).strip().upper() in (_REASONING_MODELS | extra)


def _classify_http_error(status_code: int, body_code: str, text: str) -> Exception:
    """HTTP/body 코드 → 재시도(HCXError) vs fail-fast(HCXClientError) 분류."""
    if status_code in _RETRYABLE_HTTP or body_code in _RETRYABLE_BODY_CODES:
        return HCXError(f"HCX 일시 오류 http={status_code} code={body_code}: {text[:200]}")
    # 인증 오류는 명확한 메시지
    if body_code.startswith("4010") or status_code == 401:
        return HCXClientError(
            f"HCX 인증 실패 (code={body_code}). NDT_HCX_TOKEN 환경변수의 API Key 확인. {text[:120]}"
        )
    if body_code == "40001":
        return HCXClientError(
            f"HCX 요청 파라미터 오류 (code=40001). 모델이 받지 않는 항목이 있습니다. "
            f"추론 계열 모델(HCX-007 등)은 maxTokens 를 받지 않으므로 "
            f"config/hcx.yaml 의 models.reasoning_models 목록에 그 모델 이름이 "
            f"들어 있는지 확인하세요. 서버 메시지: {text[:150]}"
        )
    if body_code == "40003":
        return HCXClientError(
            f"HCX context length 초과 (code=40003). 입력을 줄이거나 청킹 필요. {text[:120]}"
        )
    return HCXClientError(f"HCX 클라이언트 오류 http={status_code} code={body_code}: {text[:200]}")


def _do_post_v3(url: str, body: dict, provider: dict | None = None) -> dict:
    """v3 호출 — 응답 {status:{code}, result:{...}}. status.code 로 성공/오류 판정."""
    headers = _build_headers(provider)
    timeout = (provider or _resolve_provider(""))["timeout_seconds"]
    with httpx.Client(timeout=timeout, trust_env=_trust_env()) as client:
        resp = client.post(url, json=body, headers=headers)
        # 본문 파싱 시도 (status.code 추출)
        body_code = ""
        text = resp.text
        try:
            j = resp.json()
            body_code = str(((j or {}).get("status") or {}).get("code") or "")
        except Exception:
            j = None
        if resp.status_code != 200:
            raise _classify_http_error(resp.status_code, body_code, text)
        # HTTP 200 이라도 status.code 가 성공(20000/20400) 아니면 오류
        if body_code and body_code not in ("20000", "20400"):
            raise _classify_http_error(200, body_code, text)
        if j is None:
            raise HCXError(f"HCX 응답 JSON 파싱 실패: {text[:200]}")
        return j


def _do_post_openai(url: str, body: dict, provider: dict | None = None) -> dict:
    """오픈AI 호환 호출 — 응답 {choices:[...]} 또는 {error:{...}}."""
    headers = _build_headers(provider)
    timeout = (provider or _resolve_provider(""))["timeout_seconds"]
    with httpx.Client(timeout=timeout, trust_env=_trust_env()) as client:
        resp = client.post(url, json=body, headers=headers)
        text = resp.text
        if resp.status_code != 200:
            body_code = ""
            try:
                body_code = str(((resp.json() or {}).get("error") or {}).get("code") or "")
            except Exception:
                pass
            raise _classify_http_error(resp.status_code, body_code, text)
        try:
            return resp.json()
        except Exception:
            raise HCXError(f"HCX(openai) 응답 JSON 파싱 실패: {text[:200]}")


# ─────────────────────────── Public API ───────────────────────────


def call(stage: str, payload: dict, *, force_refresh: bool = False) -> HCXResponse:
    """
    stage:    프롬프트 이름 (config/prompts/{stage}.md). config/hcx.yaml 의 stage_overrides 로 모델 결정.
    payload:  유저 메시지로 전달할 dict (JSON 직렬화됨).
    """
    system_prompt = load_prompt(stage)
    provider = _resolve_provider(stage)
    model = provider.get("model") or _resolve_model(stage)  # provider 고정모델 우선
    user_payload = json.dumps(payload, ensure_ascii=False, indent=2)
    key = _cache_key(model, system_prompt, payload)
    payload_size = len(user_payload)

    if not force_refresh:
        cached = _cache_get(key)
        if cached:
            logger.debug("HCX %s | model=%s | cache=HIT key=%s", stage, model, key[:12])
            resp = _wrap(cached, model, cached=True)
            _warn_if_truncated(resp, stage, model)
            return resp

    live = not _mock_enabled()
    mode = "live" if live else "mock"
    logger.info("HCX %s | model=%s | mode=%s | payload=%dB | key=%s",
                stage, model, mode, payload_size, key[:12])

    # 호출 예산 검사 (mock 도 run/hour 카운트 — 예산 영향 미리 체험. 일일은 live 만)
    _budget_check_and_record(stage, model, is_live=live)

    try:
        if not live:
            raw = _mock_response(stage, payload)
        else:
            api_resp = _post_chat(model, system_prompt, user_payload, stage=stage, provider=provider)
            raw = {"content": _extract_content(api_resp), "_api": api_resp,
                   "stop_reason": _extract_stop_reason(api_resp)}
            _record_tokens(_extract_total_tokens(api_resp))
    except Exception:
        logger.exception("HCX %s | model=%s | call FAILED", stage, model)
        raise

    # 캐시에 stage 기록 — debug-bundle 반출 시 민감 stage(도면 기준 등) 필터링에 사용
    raw["stage"] = stage

    resp = _wrap(raw, model, cached=False)
    # 잘린 응답과 JSON 으로 안 읽히는 응답은 캐시하지 않는다. 캐시 키에 출력 상한·파서 버전이 없어서,
    # 고쳐도 같은 실패가 HIT 로 영원히 반복된다 (2026-09-06 검토 / 2026-09-07 사내 ② 세트 4).
    # 다음 실행이 다시 부른다 — 호출은 공짜(사용자 확정).
    if _is_truncated(raw.get("stop_reason")):
        logger.debug("HCX %s | model=%s | 잘린 응답은 캐시하지 않음", stage, model)
    elif resp.parsed is None:
        logger.debug("HCX %s | model=%s | JSON 실패 응답은 캐시하지 않음", stage, model)
    else:
        _cache_put(key, raw)
    _warn_if_truncated(resp, stage, model)
    if resp.parsed is None:
        dump = _dump_parse_failure(stage, key, raw)
        logger.warning("HCX %s | model=%s | response not JSON-parseable (%s) — 전문: %s",
                       stage, model, _parse_failure_note(raw), dump)
    else:
        logger.debug("HCX %s | model=%s | parsed OK", stage, model)
    return resp


def call_vision(stage: str, payload: dict, image_bytes_list: list[bytes],
                *, force_refresh: bool = False) -> HCXResponse:
    """Vision 전용 호출 (HCX-005). image_bytes_list 각 항목은 PNG/JPEG raw bytes.

    structured outputs 동시 불가 → 응답은 자연어. 호출부가 별도 정규화 (HCX-007) chain.
    """
    import base64

    system_prompt = load_prompt(stage)
    provider = _resolve_provider(stage)
    model = provider.get("model") or _resolve_model(stage)
    user_payload = json.dumps(payload, ensure_ascii=False, indent=2)
    images_b64 = [base64.b64encode(b).decode("ascii") for b in image_bytes_list]
    # 캐시 키에 이미지 hash 포함
    img_hash = hashlib.sha256(b"".join(image_bytes_list)).hexdigest()[:16]
    key = _cache_key(model, system_prompt, {**payload, "_img_hash": img_hash})

    if not force_refresh:
        cached = _cache_get(key)
        if cached:
            logger.debug("HCX %s | vision cache HIT key=%s", stage, key[:12])
            resp = _wrap(cached, model, cached=True)
            _warn_if_truncated(resp, stage, model)
            return resp

    payload_size = len(user_payload) + sum(len(b) for b in image_bytes_list)
    live = not _mock_enabled()
    mode = "live" if live else "mock"
    logger.info("HCX %s | model=%s | mode=%s | vision payload=%dB (%d imgs) | key=%s",
                stage, model, mode, payload_size, len(image_bytes_list), key[:12])

    _budget_check_and_record(stage, model, is_live=live)

    try:
        if not live:
            raw = _mock_response(stage, payload)
        else:
            api_resp = _post_chat(model, system_prompt, user_payload,
                                   images_b64=images_b64, stage=stage, provider=provider)
            raw = {"content": _extract_content(api_resp), "_api": api_resp,
                   "stop_reason": _extract_stop_reason(api_resp)}
            _record_tokens(_extract_total_tokens(api_resp))
    except Exception:
        logger.exception("HCX %s | vision call FAILED", stage)
        raise

    raw["stage"] = stage

    # 잘린 응답은 캐시하지 않는다. 캐시 키에 출력 상한이 없어서, 상한을 올려도 잘린 내용이
    # 그대로 재사용되기 때문이다 (2026-09-06 검토). 다음 실행이 다시 부른다 — 호출은 공짜(사용자 확정).
    if _is_truncated(raw.get("stop_reason")):
        logger.debug("HCX %s | model=%s | 잘린 응답은 캐시하지 않음", stage, model)
    else:
        _cache_put(key, raw)
    resp = _wrap(raw, model, cached=False)
    _warn_if_truncated(resp, stage, model)
    return resp


# 모델이 JSON 을 마크다운 코드 울타리로 감싸 보내는 경우 (```json … ```).
# 2026-09-07 사내 ② 로그: HCX-007 drawing_dc/drawing_combine 전부 '```json\n{ "drawing_no": …'
# → json.loads 실패 → parsed=None → Combine returned None → requirements_ingested 0.
# A/B 에서도 HCX-007 4/12, gemma 0/12, qwen 2/12 parse_ok 가 같은 원인으로 의심된다.
_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n(.*?)\r?\n?[ \t]*```", re.DOTALL)


def _repair_json(text: str) -> str:
    """LLM 이 흔히 내는 JSON 문법 오류 둘만 결정론으로 고친다: 주석(// …, /* … */)과 끝에 남은 쉼표(,} ,]).

    문자열 안은 건드리지 않는다 (URL 의 '//', 인용문 속 ',}'). 값은 바꾸지 않는다 — 포장만 정리한다.
    (2026-09-07 사내 ② 세트 4: finishReason=stop 인데 울타리 안 JSON 이 안 읽힘 → 문법 오류 의심)
    """
    out: list[str] = []
    i, n, in_str, esc = 0, len(text), False, False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True; out.append(ch); i += 1; continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1          # 끝 쉼표 제거
                continue
        out.append(ch); i += 1
    return "".join(out)


def _parse_json_lenient(content: str) -> Any | None:
    """그대로 → 코드 울타리 안 → 첫 '{'/'[' 부터 마지막 '}'/']' 까지 순으로, 각각 원문과 _repair_json 결과를 시도.

    파싱 결과가 달라지는 게 아니라, 같은 JSON 을 감싼 포장·문법 찌꺼기만 벗긴다. 판정은 바꾸지 않는다.
    """
    if not content:
        return None
    text = content.strip()
    candidates = [text]
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            candidates.append(text[start:end + 1])
    for cand in candidates:
        for attempt in (cand, _repair_json(cand)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    return None


def _wrap(raw: dict, model: str, cached: bool) -> HCXResponse:
    content = raw.get("content", "")
    parsed = _parse_json_lenient(content)
    stop = raw.get("stop_reason")
    return HCXResponse(content=content, parsed=parsed, model=model, cached=cached, raw=raw,
                       stop_reason=stop, truncated=_is_truncated(stop))


def probe_truncation(max_completion_tokens: int = 16, stage: str = "hcx_check") -> dict:
    """일부러 출력 상한을 아주 작게 주고 v3 응답의 finishReason **실제 값**을 읽는다 (확인 목록 A-1).

    잘림을 뜻하는 문자열을 추측해 config 에 넣으면 감지가 조용히 한 번도 안 울린다. 그래서 값은 서버에게 묻는다.
    추론(thinking) 없는 stage 로 부른다 — 추론 토큰이 예산을 먹으면 프로브가 아니다. 캐시를 거치지 않는다.
    """
    provider = _resolve_provider(stage)
    model = provider.get("model") or _resolve_model(stage)
    system_prompt = load_prompt(stage)
    api = _post_chat(model, system_prompt, json.dumps({"ping": 1, "probe": "truncation"}),
                     stage=stage, provider=provider, max_output_override=max_completion_tokens)
    result = api.get("result") if isinstance(api, dict) else None
    content = _extract_content(api)
    return {
        "model": model,
        "max_output": int(max_completion_tokens),
        "finish_reason": _extract_stop_reason(api),
        "result_keys": sorted(result.keys()) if isinstance(result, dict) else [],
        "content_len": len(content or ""),
        "content_head": (content or "")[:80],
        "usage": (result or {}).get("usage") if isinstance(result, dict) else None,
    }


def _dump_parse_failure(stage: str, key: str, raw: dict) -> str:
    """JSON 으로 안 읽힌 응답 전문을 data/logs/<날짜>/parse_fail_<stage>_<key>.txt 에 남긴다.

    로그의 120자 머리만으로는 원인(문법 오류·사과문·잘림)을 못 본다. 파일 경로를 경고에 싣는다.
    """
    try:
        d = DATA_DIR / "logs" / time.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"parse_fail_{stage}_{key[:12]}.txt"
        path.write_text(
            f"stage={stage}\nstop_reason={raw.get('stop_reason')!r}\n"
            f"---- content ----\n{raw.get('content') or ''}\n",
            encoding="utf-8")
        return str(path)
    except Exception as e:      # 진단 파일 실패가 본 작업을 막으면 안 된다
        return f"(저장 실패: {e})"


def _parse_failure_note(raw: dict) -> str:
    """parse 실패 경고 한 줄: content 앞부분 + stop_reason + (v3 에 stopReason 이 없으면) result 의 실제 키 목록.

    2026-09-07 사내 ② 세트 1: 울타리를 벗기고도 실패 = 잘림 의심인데 v3 잘림 필드를 못 잡아(A-1) 경고가 없었다.
    키 목록이 로그에 남으면 다음 사진에서 그 필드 이름을 확정할 수 있다.
    """
    content = raw.get("content") or ""
    stop = raw.get("stop_reason")
    note = f"content head: {content[:120]!r}, stop_reason={stop!r}"
    if stop is None:
        api = raw.get("_api")
        result = api.get("result") if isinstance(api, dict) else None
        if isinstance(result, dict):
            note += f", v3 result keys={sorted(result.keys())}"
    return note


def _warn_if_truncated(resp: "HCXResponse", stage: str, model: str) -> None:
    """캐시 HIT 든 실호출이든 잘린 응답이면 같은 경고. HIT 는 예전 캐시(패치 전) 가 남아 있는 경우다."""
    if not resp.truncated:
        return
    logger.warning("HCX %s | model=%s | 출력 잘림 (stop=%s)%s — maxTokens/maxCompletionTokens 상한을 올리거나 "
                   "입력을 나누세요. parsed=%s", stage, model, resp.stop_reason,
                   " [캐시된 응답 — 다음 실행부터는 다시 호출됨]" if resp.cached else "",
                   "OK" if resp.parsed is not None else "None(JSON 끊김)")


def _truncation_values() -> set[str]:
    """출력이 상한에서 잘렸음을 뜻하는 stop 값. OpenAI/vLLM 표준은 "length".
    HCX v3 의 실제 값은 사내 확인(docs/사내확인_목록 A-1) 뒤 hcx.yaml models.truncation_values 에 추가."""
    try:
        vals = hcx_config().get("models", {}).get("truncation_values")
    except Exception:       # noqa: BLE001
        vals = None
    if isinstance(vals, str):          # yaml 에 문자열 하나만 적으면 글자 단위로 쪼개져 감지가 영원히 안 울린다
        vals = [vals]
    return {str(v).strip().lower() for v in (vals or ["length"])}


def _is_truncated(stop: str | None) -> bool:
    return bool(stop) and str(stop).strip().lower() in _truncation_values()


def _extract_stop_reason(api_resp: dict) -> str | None:
    """v3: result.finishReason / openai: choices[0].finish_reason. 해석하지 않고 문자열 그대로.

    2026-09-07 사내 A/B 2차 (v3_result_keys): HCX v3 result 의 키는 created·finishReason·message·seed·usage.
    처음 짐작한 stopReason 은 없다 — 그래서 HCX-007 은 전부 null 이었다. 값 문자열(정상/잘림)은 probe_truncation 으로 확인.
    """
    if not isinstance(api_resp, dict):
        return None
    result = api_resp.get("result")
    if isinstance(result, dict):
        for key in ("finishReason", "stopReason"):
            if result.get(key) is not None:
                return str(result[key])
    try:
        fr = api_resp["choices"][0].get("finish_reason")
        return None if fr is None else str(fr)
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def _extract_content(api_resp: dict) -> str:
    """v3 / 오픈AI 호환 응답에서 모델 최종 답변(content) 추출.

    v3:     result.message.content   (추론 내용 thinkingContent 는 제외 — 최종 답변만)
    openai: choices[0].message.content
    """
    if not isinstance(api_resp, dict):
        return ""
    # v3
    result = api_resp.get("result")
    if isinstance(result, dict):
        msg = result.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
    # openai 호환
    try:
        return api_resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _extract_total_tokens(api_resp: dict) -> int:
    """응답에서 totalTokens 추출 (예산·통계용). 없으면 0."""
    if not isinstance(api_resp, dict):
        return 0
    # v3: result.usage.totalTokens
    result = api_resp.get("result")
    if isinstance(result, dict):
        usage = result.get("usage") or {}
        if usage.get("totalTokens") is not None:
            return int(usage["totalTokens"])
    # openai: usage.total_tokens
    usage = api_resp.get("usage") or {}
    if usage.get("total_tokens") is not None:
        return int(usage["total_tokens"])
    return 0


def extract_thinking(api_resp: dict) -> str:
    """추론 모델의 사고 과정(thinkingContent) 추출 — 개발/디버그용 (사용자 노출 지양)."""
    result = (api_resp or {}).get("result") or {}
    msg = result.get("message") or {}
    return msg.get("thinkingContent") or ""
