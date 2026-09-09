#!/usr/bin/env python3
"""A/B 모델 정확도 하네스 — 같은 실데이터로 provider(HCX / Gemma 4 / gpt-oss) 판정 비교.

벤치마크가 아니라 우리 업무(NDT 기성 검증)의 실제 stage 로 비교한다:
  - matching_judge : 청구행 ↔ 성적서 후보 매칭 판정
  - code_lookup    : GOST/SP 조항 대조·권위계층 추론
비교 지표: 판정 일치율(모델 간 pairwise), 파싱 성공률, 토큰, 지연(ms).
정답 라벨이 있으면 정확도(accuracy)도 산출.

사용 (사내, endpoint 꽂은 뒤):
  # hcx.yaml 에 providers: {hcx, gemma, gptoss} 정의 후
  python scripts/ab_model_test.py --providers hcx,gemma,gptoss \
      --cases scripts/ab_cases.sample.json --out data/outputs/ab_result.json

사외 검증 (가짜 서버로 하네스 로직 확인):
  python scripts/ab_model_test.py --self-test
"""
from __future__ import annotations
import argparse, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run_stage(provider_name: str, stage: str, payload: dict) -> dict:
    """지정 provider 로 stage 1건 호출. (provider 는 hcx.yaml stage_providers 임시 오버라이드)"""
    import app.hcx_client as hc
    from app.config import load_yaml
    cfg = load_yaml("hcx.yaml")   # 캐시된 객체 — 클라이언트가 읽는 것과 동일 (clear 금지: 주입 config 유지)
    cfg.setdefault("stage_providers", {})[stage] = provider_name  # 이 호출만 라우팅
    t0 = time.time()
    try:
        resp = hc.call(stage, payload, force_refresh=True)
        dt = (time.time() - t0) * 1000
        tok = hc.get_call_stats().get("token_total", 0)
        out = {"ok": True, "parsed": resp.parsed, "content": (resp.content or "")[:400],
               "model": resp.model, "ms": round(dt), "parse_ok": resp.parsed is not None,
               # 잘림은 모델 성능이 아니라 출력 상한 문제 — parse_ok 와 따로 센다 (2026-09-06)
               "stop_reason": getattr(resp, "stop_reason", None),
               "truncated": bool(getattr(resp, "truncated", False))}
        if resp.parsed is None:
            # parse 실패 원인은 응답 앞부분에 있다 (울타리·사과문·잘림). 결과 파일에 남겨야
            # 사내 사진만으로 원인을 볼 수 있다 (2026-09-07: content 는 요약에서 제거되므로 별도 키).
            out["content_head"] = (resp.content or "")[:160]
        if out["stop_reason"] is None:
            # A-1: v3 응답의 잘림 필드가 result.stopReason 이 아니면 실제 키 이름을 알아야 한다.
            api = (getattr(resp, "raw", None) or {}).get("_api") or {}
            result = api.get("result") if isinstance(api, dict) else None
            if isinstance(result, dict):
                out["v3_result_keys"] = sorted(result.keys())
        return out
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "ms": round((time.time()-t0)*1000)}


def _agreement(results: dict, key_fn) -> dict:
    """provider 간 판정 pairwise 일치율."""
    names = [n for n, r in results.items() if r.get("ok") and r.get("parse_ok")]
    pairs, agree = 0, 0
    detail = {}
    for i in range(len(names)):
        for j in range(i+1, len(names)):
            a, b = names[i], names[j]
            pairs += 1
            same = key_fn(results[a]["parsed"]) == key_fn(results[b]["parsed"])
            agree += 1 if same else 0
            detail[f"{a}↔{b}"] = "일치" if same else "불일치"
    return {"pairs": pairs, "agree": agree, "rate": (agree/pairs if pairs else None), "detail": detail}


# stage별 "판정 핵심값" 추출기 (일치율·정확도 비교용).
# 각 프롬프트의 Output 스키마에 실제로 존재하는 필드만 쓴다.
#  ⚠ code_lookup 은 예전에 `verdict` 를 봤는데 그 필드는 스키마에 없다 —
#    항상 None 이 되어 일치율이 무의미했다. found_in_context 로 정정 (2026-09-03).
KEY_FN = {
    "matching_judge":     lambda p: (p or {}).get("matched_report_no"),
    "code_lookup":        lambda p: (p or {}).get("found_in_context"),
    "compliance_explain": lambda p: (p or {}).get("verdict"),
    "ocr_normalize":      lambda p: (p or {}).get("report_no"),
    "scwep_extract":      lambda p: (p or {}).get("document_no"),
}


def split_providers(raw: str) -> list[str]:
    """'a,b' 도 'a b' 도 같은 목록. cmd 배치가 쉼표를 인자 구분자로 쪼개 넘겨도 살아남게 (2026-09-07 사내)."""
    import re as _re
    return [p for p in _re.split(r"[,\s]+", (raw or "").strip()) if p]


def unknown_providers(names: list[str], cfg: dict | None = None) -> list[str]:
    """hcx.yaml providers 에 없는 이름. _resolve_provider 는 모르는 이름을 기본(HCX v3)으로 조용히 후퇴시키므로
    하네스는 여기서 먼저 멈춘다. 2026-09-07 사내 실측: hcx-seed-32b 가 서빙 목록에 없어 provider 를 뺐는데,
    결재돼 들어간 run_model_ab.bat 의 all 목록에는 남아 있다 — 그대로 돌리면 'hcxseed' 열이 사실은 HCX-007 결과가 된다."""
    if cfg is None:
        from app.config import load_yaml
        cfg = load_yaml("hcx.yaml")
    known = set((cfg.get("providers") or {}).keys())
    return [n for n in names if n not in known]


def run(cases: list[dict], providers: list[str], gold: dict | None = None) -> dict:
    out = {"providers": providers, "n_cases": len(cases), "cases": [], "summary": {}}
    agg = {p: {"parse_ok": 0, "ms": [], "err": 0, "truncated": 0} for p in providers}
    agree_rates = []
    for ci, case in enumerate(cases):
        stage = case["stage"]; payload = case["payload"]
        results = {p: _run_stage(p, stage, payload) for p in providers}
        for p in providers:
            r = results[p]
            if r.get("ok"):
                agg[p]["ms"].append(r["ms"])
                if r.get("parse_ok"): agg[p]["parse_ok"] += 1
                if r.get("truncated"): agg[p]["truncated"] += 1
            else:
                agg[p]["err"] += 1
        ag = _agreement(results, KEY_FN.get(stage, lambda p: json.dumps(p, sort_keys=True)))
        if ag["rate"] is not None: agree_rates.append(ag["rate"])
        rec = {"i": ci, "stage": stage,
               "name": case.get("name", ""), "category": case.get("category", "기타"),
               "agreement": ag,
               "results": {p: {k: v for k, v in results[p].items() if k != "content"} for p in providers}}
        # 정답 라벨 있으면 provider별 정확도
        if gold and str(ci) in gold:
            g = gold[str(ci)]
            rec["gold"] = g
            for p in providers:
                r = results[p]
                cat = case.get("category", "기타")
                agg[p].setdefault("correct", 0)
                agg[p].setdefault("by_cat", {})
                agg[p]["by_cat"].setdefault(cat, [0, 0])
                agg[p]["by_cat"][cat][1] += 1
                if r.get("ok") and r.get("parse_ok"):
                    hit = KEY_FN.get(stage, lambda x: x)(r["parsed"]) == g
                    agg[p]["correct"] += 1 if hit else 0
                    agg[p]["by_cat"][cat][0] += 1 if hit else 0
        out["cases"].append(rec)

    for p in providers:
        a = agg[p]
        ms = a["ms"]
        out["summary"][p] = {
            "parse_ok": f"{a['parse_ok']}/{len(cases)}",
            "truncated": a["truncated"],
            "errors": a["err"],
            "ms_med": (sorted(ms)[len(ms)//2] if ms else None),
            "ms_max": (max(ms) if ms else None),
            **({"accuracy": f"{a['correct']}/{len(cases)}"} if gold and "correct" in a else {}),
            **({"by_category": {k: f"{v[0]}/{v[1]}" for k, v in a["by_cat"].items()}}
               if a.get("by_cat") else {}),
        }
    out["summary"]["_pairwise_agreement_mean"] = (
        round(sum(agree_rates)/len(agree_rates), 3) if agree_rates else None)
    return out



# ─────────────────────── 사람이 읽는 보고서 ───────────────────────

# 이 프로젝트의 적합성 판단 기준.
#  잘못된 판정이 청구 금액 오류로 이어지므로, "모르면 모른다고 하는가"(환각저항)를
#  전체 정확도보다 무겁게 본다. 비개발자가 읽고 모델을 고를 수 있게 표로 낸다.
_WEIGHTS = {"환각저항": 3.0, "근거인용": 2.0, "매칭판정": 2.0,
            "적합성판정": 1.5, "추출정확도": 1.0}


def _score(summary: dict) -> tuple[float, str]:
    """가중 점수(0~100)와 한 줄 판정."""
    by = summary.get("by_category") or {}
    num = den = 0.0
    for cat, frac in by.items():
        ok, tot = (int(x) for x in frac.split("/"))
        w = _WEIGHTS.get(cat, 1.0)
        num += w * ok
        den += w * tot
    base = (num / den * 100) if den else 0.0
    halluc = by.get("환각저항")
    if halluc and int(halluc.split("/")[0]) < int(halluc.split("/")[1]):
        verdict = "부적합 — 근거 없는 답을 지어냄"
    elif base >= 85:
        verdict = "권장"
    elif base >= 65:
        verdict = "조건부 — 사람 확인 병행"
    else:
        verdict = "부적합"
    return round(base, 1), verdict


def write_report(out: dict, path: Path) -> Path:
    L = ["# 사내 AI 모델 벤치마크 — NDT 기성검증 적합성", ""]
    L += [f"- 케이스 {out['n_cases']}건 / 모델 {len(out['providers'])}종",
          f"- 모델 간 판정 일치율 평균: **{out['summary'].get('_pairwise_agreement_mean')}**", ""]
    L += ["## 종합", "",
          "| 모델 | 적합성 | 점수 | 정확도 | JSON 준수 | 잘림 | 오류 | 응답(중앙) |",
          "|---|---|---|---|---|---|---|---|"]
    rows = []
    for p in out["providers"]:
        s = out["summary"][p]
        sc, vd = _score(s)
        rows.append((sc, p, vd, s))
    for sc, p, vd, s in sorted(rows, key=lambda r: -r[0]):
        L.append(f"| `{p}` | **{vd}** | {sc} | {s.get('accuracy','-')} | "
                 f"{s['parse_ok']} | {s.get('truncated', 0)} | {s['errors']} | {s['ms_med']} ms |")
    L += ["", "## 항목별 정확도", "",
          "| 모델 | " + " | ".join(_WEIGHTS) + " |",
          "|---" * (len(_WEIGHTS) + 1) + "|"]
    for sc, p, vd, s in sorted(rows, key=lambda r: -r[0]):
        by = s.get("by_category") or {}
        L.append(f"| `{p}` | " + " | ".join(by.get(c, "-") for c in _WEIGHTS) + " |")
    L += ["", "### 항목이 뜻하는 것", "",
          "| 항목 | 가중 | 무엇을 보는가 |", "|---|---|---|",
          "| 환각저항 | 3.0 | context 에 근거가 없을 때 '근거 없음'이라 답하는가. "
          "**지어내면 청구 금액 오류로 직결되므로 가장 무겁게 본다** |",
          "| 근거인용 | 2.0 | 근거가 있을 때 실제로 찾아 인용하는가 |",
          "| 매칭판정 | 2.0 | 청구행↔성적서 매칭. 무리한 매칭을 하지 않는가 |",
          "| 적합성판정 | 1.5 | 부적합/정상을 옳게 가르는가 |",
          "| 추출정확도 | 1.0 | OCR 오독 교정·문서번호 추출 |", "",
          "> **환각저항에서 하나라도 틀리면 점수와 무관하게 '부적합'** 입니다.",
          "> 이 도구는 잘못된 판정이 금액 오류가 되므로 그렇게 잡았습니다.", ""]
    L += ["## 케이스별 결과", ""]
    for c in out["cases"]:
        g = c.get("gold", "-")
        L.append(f"### {c['i']+1}. [{c['category']}] {c['name']}")
        L.append(f"- stage `{c['stage']}` / 정답 `{g}` / 모델간 일치 "
                 f"{c['agreement']['agree']}/{c['agreement']['pairs']}")
        for p, r in c["results"].items():
            if not r.get("ok"):
                L.append(f"  - `{p}` 오류: {r.get('error','')[:80]}")
            else:
                L.append(f"  - `{p}` → `{KEY_FN.get(c['stage'], lambda x: x)(r.get('parsed'))}` "
                         f"({r['ms']} ms)")
        L.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")
    return path

def _self_test():
    """가짜 서버 2대로 하네스 로직만 확인 (사외)."""
    import threading
    from http.server import ThreadingHTTPServer
    import tests.fake_hcx_server as fake
    from app.config import load_yaml
    srvs = []
    ports = []
    for _ in range(2):
        s = ThreadingHTTPServer(("127.0.0.1", 0), fake.Handler)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        srvs.append(s); ports.append(s.server_address[1])
    load_yaml.cache_clear(); cfg = load_yaml("hcx.yaml")
    cfg["providers"] = {
        "hcx":   {"base_url": f"http://127.0.0.1:{ports[0]}", "api_style": "v3", "token_env": "NDT_HCX_TOKEN"},
        "gemma": {"base_url": f"http://127.0.0.1:{ports[1]}", "api_style": "openai", "model": "gemma-4-31b", "token_env": "NDT_HCX_TOKEN"},
    }
    os.environ.update({"NDT_HCX_MOCK": "0", "NDT_HCX_BUDGET_BYPASS": "1", "NDT_HCX_TOKEN": "k"})
    cases = [{"stage": "matching_judge", "payload": {"billing_row": {"joint_no": "J1"}, "candidates": []}},
             {"stage": "code_lookup", "payload": {"question": "VT 100%?"}}]
    res = run(cases, ["hcx", "gemma"])
    for s in srvs: s.shutdown()
    print(json.dumps(res["summary"], ensure_ascii=False, indent=2))
    print("\n[self-test] 하네스 로직 정상 — 사내에서 실 endpoint 로 --providers 지정해 실행하세요.")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", nargs="+", help="쉼표 또는 공백 구분 (예: hcx007,gemma gptoss). 배치가 쉼표를 쪼개 넘겨도 된다")
    ap.add_argument("--cases", help="테스트 케이스 JSON [{stage, payload}, ...]")
    ap.add_argument("--gold", help="정답 라벨 JSON {case_index: 판정값}")
    ap.add_argument("--out", default="data/outputs/ab_result.json")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--report", default="data/outputs/ab_report.md",
                    help="사람이 읽는 비교 보고서(.md) 저장 위치")
    args = ap.parse_args(argv)

    if args.self_test:
        _self_test(); return
    if not (args.providers and args.cases):
        ap.error("--providers 와 --cases 필요 (또는 --self-test)")
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    # --gold 를 안 줘도 정답 파일이 있으면 자동으로 쓴다.
    # 사내에 이미 결재된 run_model_ab.bat 은 --gold 를 넘기지 않으므로,
    # .bat 을 새로 반입하지 않고도 채점이 되게 하려는 것이다. (2026-09-03)
    gold_path = Path(args.gold) if args.gold else Path("scripts/ab_gold_ndt.json")
    gold = json.loads(gold_path.read_text(encoding="utf-8")) if gold_path.exists() else None
    if gold and not args.gold:
        print(f"[i] 정답 라벨 자동 사용: {gold_path}")
    providers = split_providers(" ".join(args.providers) if isinstance(args.providers, list) else args.providers)
    bad = unknown_providers(providers)
    if bad:
        from app.config import load_yaml
        known = ", ".join((load_yaml("hcx.yaml").get("providers") or {}).keys())
        raise SystemExit(f"[ERROR] hcx.yaml providers 에 없는 이름: {', '.join(bad)} — 알려진 provider: {known}. "
                         f"run_model_ab.bat 의 목록에서 빼거나 --providers 로 직접 지정하세요.")
    res = run(cases, providers, gold)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(res["summary"], ensure_ascii=False, indent=2))
    rep = write_report(res, Path(args.report))
    print(f"\n상세(JSON): {args.out}")
    print(f"보고서(읽기용): {rep}")


if __name__ == "__main__":
    main()
