"""Studio XP 점검 — hcx-check 가 HCX 만 보던 구멍을 막는다.

임베딩(bge-m3)은 운영 경로다. 그런데 점검은 HCX(cfg["api"])만 찔렀고, 실행 중에는
0 벡터가 정상으로 통과했다. 장애가 어느 쪽에서도 안 보이는 상태였다 (2026-09-06 검토).

run() 은 dict 만 돌려준다. 출력은 main.hcx_check 가 한다. 셋 다 일일 HCX 호출량을 안 쓴다.
  1) GET /v1/models      — 방화벽·토큰·서빙 모델 id 를 한 번에. hcx.yaml 의 Studio provider
                           모델과 embedding.model 이 목록에 있는지 이름별로.
  2) 임베딩 1건 실호출    — embeddings.embed_texts 를 그대로 태워 인코더 방어까지 통과하는지.
  3) 저장 벡터 스캔       — data/index/embeddings/*.npy 의 0 행·NaN 행·폭 불일치 (사내확인 A-4).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
import numpy as np

from app import embeddings
from app.config import hcx_config

logger = logging.getLogger(__name__)


def run(*, scan_stored: bool = True, timeout: float = 25.0) -> dict:
    cfg = hcx_config()
    emb = embeddings.config()
    base = str(emb.get("base_url") or "").rstrip("/")
    token = os.environ.get(emb.get("token_env", "NDT_STUDIO_TOKEN"), "").strip()
    res: dict = {
        "base_url": base,
        "enabled": bool(emb.get("enabled")),
        "token_present": bool(token),
        "models": {"ok": False, "served": [], "missing": [], "error": None},
        "embed": {"ok": False, "model": emb.get("model"), "dim": None, "served": None,
                  "expected_dim": emb.get("dim"), "reason": None},
        "stored": [],
        "stored_ok": True,
    }
    if not res["enabled"] or not base:
        res["embed"]["reason"] = "embedding.enabled 가 꺼져 있거나 base_url 이 없음"
        return res
    if not token:
        res["embed"]["reason"] = f"환경변수 {emb.get('token_env')} 비어 있음"
        return res

    # 1) 서빙 모델 목록
    served: list[str] = []
    try:
        r = httpx.get(base + "/v1/models", headers=embeddings._headers(emb),
                      timeout=timeout, trust_env=embeddings._trust_env())
        if r.status_code == 200:
            served = [str(d.get("id")) for d in (r.json() or {}).get("data", []) if d.get("id")]
            res["models"]["ok"] = True
            res["models"]["served"] = served
        else:
            res["models"]["error"] = f"http={r.status_code} {r.text[:120]}"
    except Exception as e:      # noqa: BLE001 - 점검은 원인 종류를 그대로 보여준다
        res["models"]["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    if res["models"]["ok"]:
        for name, pv in (cfg.get("providers") or {}).items():
            if pv.get("api_style") == "openai" and pv.get("model") and pv["model"] not in served:
                res["models"]["missing"].append((name, pv["model"]))
        res["embed"]["served"] = emb.get("model") in served

    # 2) 임베딩 실호출 — 인코더 방어(_usable)까지 그대로 태운다
    v = embeddings.embed_texts(["ping"])
    if v is not None and v.ndim == 2 and v.shape[0] == 1:
        res["embed"]["ok"] = True
        res["embed"]["dim"] = int(v.shape[1])
    else:
        res["embed"]["reason"] = "호출 실패 또는 퇴화 벡터(0/NaN/차원 불일치) — 직전 경고 로그 참조"

    # 3) 저장 벡터 스캔
    if scan_stored:
        res["stored"], note = _scan_stored(res["embed"]["dim"])
        if note:
            res["stored_note"] = note
        res["stored_ok"] = all(
            r_["zero_rows"] == 0 and r_["nan_rows"] == 0 and not r_["dim_drift"] and not r_["live_dim_drift"]
            for r_ in res["stored"])
    return res


def _scan_stored(live_dim: Optional[int]) -> tuple[list[dict], Optional[str]]:
    """(문서별 결과, 안내문). 첫 설치처럼 DB 테이블이 아직 없으면 빈 목록과 안내문 — 점검이 죽지 않는다."""
    from sqlalchemy import select
    from app.database.models import StandardDocument
    from app.extractors import code_indexer as ci
    out: list[dict] = []
    try:
        with ci.get_session() as s:
            docs = list(s.scalars(select(StandardDocument).where(StandardDocument.doc_type == "code")))
    except Exception as e:      # noqa: BLE001 - OperationalError(no such table) 등. 점검은 죽지 않는다
        return [], f"저장 벡터 스캔 생략 — DB 가 아직 없음 ({type(e).__name__}). ingest-standards 전이면 정상"
    with ci.get_session() as s:
        for doc in s.scalars(select(StandardDocument).where(StandardDocument.doc_type == "code")):
            meta = (doc.chunks_json or {}).get("embedding")
            if not meta:
                continue
            row = {"doc_id": int(doc.id), "doc": doc.document_no or doc.file_path,
                   "meta_dim": meta.get("dim"), "rows": 0, "width": None,
                   "zero_rows": 0, "nan_rows": 0, "dim_drift": False, "live_dim_drift": False,
                   "missing": False}
            p = ci._vectors_path(doc.id)
            if not p.exists():
                row["missing"] = True
                out.append(row)
                continue
            try:
                v = np.load(p)
            except Exception:       # noqa: BLE001
                row["missing"] = True
                out.append(row)
                continue
            if v.ndim == 2:
                row["rows"], row["width"] = int(v.shape[0]), int(v.shape[1])
                row["zero_rows"] = int((np.linalg.norm(v, axis=1) == 0).sum())
                row["nan_rows"] = int((~np.isfinite(v)).any(axis=1).sum())
                row["dim_drift"] = bool(meta.get("dim") and int(meta["dim"]) != int(v.shape[1]))
                row["live_dim_drift"] = bool(live_dim and int(live_dim) != int(v.shape[1]))
            else:
                row["dim_drift"] = True
            out.append(row)
    return out, None
