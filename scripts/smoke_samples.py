# -*- coding: utf-8 -*-
"""실제 샘플 스모크 러너 — 운영 코드를 건드리지 않고 실행 방식만 바꾼다.

무엇을 하나
  1) 설정 주입: mode=local 이면 메모리 안의 hcx.yaml dict 에 Ollama provider 를 심는다 (A/B 하네스와 같은 방법)
  2) hcx_client.call / call_vision 을 감싼다:
       - stage 별로 mock ↔ 로컬 전환 (NDT_HCX_MOCK 은 호출마다 읽히므로 그 자리에서 바꾼다)
       - stage 별 로컬 호출 상한 (review 30행 같은 부분집합)
       - payload 를 케이스 뱅크(JSONL)로 캡처 → 사내 모델 비교의 입력
       - stage 별 호출 수·지연 집계
  3) 단계 실행: 규격 → 검색 5질의 → 도면 → SCWEP → 계약 → 성적서 발췌 분할 → review
  4) summary.md / summary.json

사용
  venv/bin/python scripts/smoke_samples.py --mode mock            # 1단계
  venv/bin/python scripts/smoke_samples.py --mode local --local-cap 30   # 2단계 (Ollama 떠 있어야)
  옵션: --skip 단계명,… / --only 단계명,… / --no-fresh (data/ 를 옆으로 안 치움) / --workers 7
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── 로컬 7B 가 감당하는 stage (프롬프트가 12k 토큰 안에 드는 것). 나머지는 mock → 사내 전용 표시. ──
LOCAL_STAGES = {"drawing_classify", "drawing_bg", "drawing_sd", "drawing_combine",
                "matching_judge", "code_lookup", "compliance_explain"}
LOCAL_ONE_SHOT = {"scwep_extract": 1, "drawing_dc": 1}     # 시험 삼아 1회만 로컬

SEARCH_QUERIES = [
    "배치 표본 검사 GOST 10922",
    "batch-wise visual inspection of welded joints",
    "визуальный контроль сварных соединений партия",
    "SP 70 welded reinforcement GOST 10922",
    "radiographic examination IQI film density",
]


def route(stage: str, mode: str, local_stages: set, counts: dict, cap: int) -> str:
    """이 호출을 mock 으로 보낼지 로컬 모델로 보낼지. counts 는 stage 별 로컬 호출 누계(여기서 갱신)."""
    if mode != "local":
        return "mock"
    limit = cap if stage in local_stages else LOCAL_ONE_SHOT.get(stage, 0)
    if counts.get(stage, 0) >= limit:
        return "mock"
    counts[stage] = counts.get(stage, 0) + 1
    return "local"


def inject_local(cfg: dict, *, base_url: str, model: str, embed_model: str, embed_dim: int) -> None:
    """메모리 안의 hcx.yaml dict 에 로컬 provider 를 심는다. 파일은 그대로."""
    cfg.setdefault("providers", {})["local"] = {
        "base_url": base_url, "api_style": "openai", "chat_path": "/v1/chat/completions",
        "model": model, "token_env": "NDT_LOCAL_TOKEN",
        "timeout_seconds": 900,
        "retry": {"max_attempts": 2, "initial_backoff_seconds": 2, "max_backoff_seconds": 10},
    }
    cfg["default_provider"] = "local"
    cfg.setdefault("embedding", {}).update({"base_url": base_url, "model": embed_model, "dim": embed_dim})


class CaseBank:
    """stage 별 payload 를 JSONL 로. 사내 모델 비교(Level 1)의 입력."""
    def __init__(self, root: Path):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self._n: dict[str, int] = defaultdict(int)

    def record(self, stage: str, payload: dict) -> None:
        line = json.dumps({"stage": stage, "payload": payload,
                           "payload_bytes": len(json.dumps(payload, ensure_ascii=False))}, ensure_ascii=False)
        with (self.root / f"{stage}.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._n[stage] += 1

    def sizes(self) -> dict:
        return dict(self._n)


def write_excerpt(src: Path, dst: Path, n_pages: int) -> int:
    import pypdfium2 as pdfium
    s = pdfium.PdfDocument(str(src))
    n = min(n_pages, len(s))
    d = pdfium.PdfDocument.new()
    d.import_pages(s, pages=list(range(n)))
    d.save(str(dst))
    return n


class Stats:
    def __init__(self):
        self.calls: dict[str, list[float]] = defaultdict(list)
        self.where: dict[str, dict[str, int]] = defaultdict(lambda: {"mock": 0, "local": 0})
        self.steps: dict[str, float] = {}

    def note(self, stage: str, where: str, seconds: float) -> None:
        self.calls[stage].append(seconds); self.where[stage][where] += 1


def install_wrappers(mode: str, bank: CaseBank, stats: Stats, cap: int) -> None:
    import app.hcx_client as hc
    counts: dict[str, int] = {}
    orig_call, orig_vision = hc.call, hc.call_vision

    def _wrapped(orig, stage, payload, *a, **k):
        where = route(stage, mode, LOCAL_STAGES, counts, cap)
        os.environ["NDT_HCX_MOCK"] = "0" if where == "local" else "1"
        bank.record(stage, payload)
        t = time.time()
        try:
            return orig(stage, payload, *a, **k)
        finally:
            stats.note(stage, where, time.time() - t)

    hc.call = lambda stage, payload, *a, **k: _wrapped(orig_call, stage, payload, *a, **k)
    hc.call_vision = lambda stage, payload, *a, **k: _wrapped(orig_vision, stage, payload, *a, **k)
    # 모듈에서 이미 이름을 가져간 소비자들도 바꿔 끼운다
    import importlib
    for name in ("app.analyzers.compliance", "app.analyzers.explainer", "app.matchers.llm_judge",
                 "app.extractors.scwep_parser", "app.extractors.ocr_normalizer", "app.extractors.ocr_corrector",
                 "app.extractors.drawing.classifier", "app.extractors.drawing.requirements_extractor",
                 "app.extractors.table_transcriber", "app.extractors.vision_verifier", "app.main"):
        try:
            m = importlib.import_module(name)
        except Exception:       # noqa: BLE001 - 선택적 소비자
            continue
        if hasattr(m, "call") and m.call is orig_call:
            m.call = hc.call
        if hasattr(m, "call_vision") and m.call_vision is orig_vision:
            m.call_vision = hc.call_vision


def _cli(args: list[str], log_path: Path) -> tuple[int, float]:
    from typer.testing import CliRunner
    from app.main import app
    t = time.time()
    r = CliRunner().invoke(app, args)
    log_path.write_text(r.output or "", encoding="utf-8")
    return r.exit_code, time.time() - t


def run(mode: str, *, out: Path, fresh: bool, workers: int, cap: int,
        only: Optional[set], skip: set, ollama: str, local_model: str, embed_model: str, embed_dim: int) -> dict:
    os.environ.setdefault("NDT_OCR_WORKERS", str(workers if mode == "mock" else min(workers, 3)))
    os.environ["NDT_HCX_TOKEN"] = os.environ.get("NDT_HCX_TOKEN") or "smoke"
    if mode == "local":
        os.environ["NDT_STUDIO_TOKEN"] = "local"          # Ollama 는 인증을 안 본다; available() 만 통과
        os.environ["OLLAMA_KEEP_ALIVE"] = "1h"
        from app.config import load_yaml
        inject_local(load_yaml("hcx.yaml"), base_url=ollama, model=local_model,
                     embed_model=embed_model, embed_dim=embed_dim)
    else:
        os.environ.pop("NDT_STUDIO_TOKEN", None)          # 임베딩 불가 → BM25 후퇴 경로 시험
    if fresh and (ROOT / "data").exists():
        stamp = time.strftime("%Y%m%d_%H%M%S")
        shutil.move(str(ROOT / "data"), str(ROOT / f"data_local_{stamp}"))
        print(f"data/ → data_local_{stamp}/ 로 옮김 (끝나면 되돌리세요)")

    out.mkdir(parents=True, exist_ok=True)
    bank, stats = CaseBank(out / "case_bank"), Stats()
    install_wrappers(mode, bank, stats, cap)

    def want(step: str) -> bool:
        return (only is None or step in only) and step not in skip

    results: dict = {"mode": mode, "steps": {}}

    def step(name: str, fn):
        if not want(name):
            return
        print(f"[{name}] 시작", flush=True); t = time.time()
        try:
            info = fn() or {}
        except Exception as e:      # noqa: BLE001 - 한 단계가 죽어도 요약은 나온다
            info = {"error": f"{type(e).__name__}: {e}"}
        stats.steps[name] = time.time() - t
        results["steps"][name] = {"seconds": round(stats.steps[name], 1), **info}
        print(f"[{name}] {stats.steps[name]:.0f}s {info}", flush=True)

    step("hcx-check", lambda: {"exit": _cli(["hcx-check"], out / "hcx-check.log")[0]})
    step("codes", lambda: {"exit": _cli(["ingest-standards", "samples/codes_standards", "--type", "code"], out / "codes.log")[0]})

    def _search():
        from app.extractors import code_indexer
        rows = []
        for q in SEARCH_QUERIES:
            hits = code_indexer.search(q, top_k=3)
            rows.append({"q": q, "top": [(h["doc"], h["page"], h["source"], (h["text"] or "")[:80]) for h in hits]})
        (out / "search.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"queries": len(rows), "with_hits": sum(1 for r in rows if r["top"])}
    step("search", _search)

    step("drawings", lambda: {"exit": _cli(["ingest-drawings", "samples/drawings", "--as-of", "2026-09-01"], out / "drawings.log")[0]})
    step("scwep", lambda: {"exit": _cli(["ingest-standards", "samples/scwep", "--type", "scwep"], out / "scwep.log")[0]})
    step("contracts", lambda: {"exit": _cli(["ingest-standards", "samples/contracts", "--type", "contract"], out / "contracts.log")[0]})

    report = next(iter((ROOT / "samples" / "NDT reports").glob("*.pdf")), None)
    excerpt = out / "report_head60.pdf"

    def _segment():
        from app.extractors.report_segmenter import segment
        n = write_excerpt(report, excerpt, 60)
        segs = segment(excerpt)
        (out / "segments_head60.json").write_text(json.dumps(
            [{"report_no": getattr(s, "report_no", None), "start": s.start_page, "end": s.end_page} for s in segs],
            ensure_ascii=False, indent=1), encoding="utf-8")
        t = time.time(); full = segment(report); full_s = time.time() - t
        return {"excerpt_pages": n, "segments_head60": len(segs), "segments_full": len(full), "full_segment_seconds": round(full_s, 1)}
    if report:
        step("segment", _segment)

    billing = next(iter((ROOT / "samples" / "billing" / "20260830").glob("첨부-2*.xlsx")), None)
    if billing and report:
        step("review", lambda: {"exit": _cli(["review", "--billing", str(billing), "--reports", str(excerpt if mode == "local" else report),
                                              "--round", "25", "--date", "2026-08-30", "--discipline", "CP-P1"], out / "review.log")[0]})

    from app.hcx_client import get_call_stats
    results["call_stats"] = get_call_stats()
    results["stages"] = {s: {"n": len(v), "avg_s": round(sum(v) / len(v), 2), **stats.where[s]} for s, v in stats.calls.items()}
    results["case_bank"] = bank.sizes()
    (out / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "summary.md").write_text(_markdown(results), encoding="utf-8")
    print((out / "summary.md").read_text(encoding="utf-8"))
    return results


def _markdown(r: dict) -> str:
    L = [f"# 스모크 요약 — mode={r['mode']}", "", "## 단계별 시간", "", "| 단계 | 초 | 결과 |", "|---|---|---|"]
    for k, v in r["steps"].items():
        L.append(f"| {k} | {v['seconds']} | {json.dumps({a: b for a, b in v.items() if a != 'seconds'}, ensure_ascii=False)} |")
    L += ["", "## stage 별 LLM 호출", "", "| stage | 호출 | 평균 초 | mock | 로컬 |", "|---|---|---|---|---|"]
    for s, v in sorted(r.get("stages", {}).items(), key=lambda x: -x[1]["n"]):
        L.append(f"| {s} | {v['n']} | {v['avg_s']} | {v['mock']} | {v['local']} |")
    L += ["", f"케이스 뱅크: {json.dumps(r.get('case_bank', {}), ensure_ascii=False)}",
          f"호출 통계: {json.dumps(r.get('call_stats', {}), ensure_ascii=False)}", "",
          "로컬로 안 돌린 stage 는 사내 전용이다. 이 표의 mock 열이 그 stage 의 사내 호출 수다."]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["mock", "local"], default="mock")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-fresh", action="store_true")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--local-cap", type=int, default=30, help="stage 별 로컬 호출 상한 (review 부분집합)")
    ap.add_argument("--only", default=None); ap.add_argument("--skip", default="")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--local-model", default="qwen2.5:7b-instruct")
    ap.add_argument("--embed-model", default="bge-m3"); ap.add_argument("--embed-dim", type=int, default=1024)
    a = ap.parse_args()
    out = Path(a.out) if a.out else ROOT / "data_smoke" / f"smoke_{a.mode}"
    run(a.mode, out=out, fresh=not a.no_fresh, workers=a.workers, cap=a.local_cap,
        only=set(a.only.split(",")) if a.only else None, skip=set(x for x in a.skip.split(",") if x),
        ollama=a.ollama, local_model=a.local_model, embed_model=a.embed_model, embed_dim=a.embed_dim)
    return 0


if __name__ == "__main__":
    sys.exit(main())
