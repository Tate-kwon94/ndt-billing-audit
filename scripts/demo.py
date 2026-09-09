"""합성 샘플로 도구의 동작을 한 번에 보여준다 — LLM 호출 없이(NDT_HCX_MOCK=1) 결정론 경로만.

    python scripts/make_synthetic_samples.py
    NDT_HCX_MOCK=1 python scripts/demo.py

보여주는 것: 청구서 파싱 → 조인트 압축 표기 해석과 수량 대조 → 도면 OCR·검사 범위표 위치 →
성적서 헤더·표1 추출 → 근거 사슬(청구 ↔ 성적서 ↔ 도면 ↔ 절차서) 연결.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("NDT_HCX_MOCK", "1")

from app.extractors import item_notation as it              # noqa: E402
from app.extractors.excel_parser import parse_billing_xlsx   # noqa: E402
from app.extractors.pdf_extractor import extract             # noqa: E402

SAMPLES = Path("samples")


def head(t: str) -> None:
    print(f"\n{'─' * 74}\n{t}\n{'─' * 74}")


def main() -> None:
    if not SAMPLES.exists():
        raise SystemExit("샘플이 없습니다. 먼저: python scripts/make_synthetic_samples.py")

    head("1. 청구서 — 양식을 헤더로 찾아 읽는다 (열 위치가 회차마다 바뀐다)")
    xlsx = next(SAMPLES.glob("billing/*.xlsx"))
    billing = parse_billing_xlsx(xlsx, discipline_hint="CP-P1").rows
    print(f"  {xlsx.name} → {len(billing)}행")
    for r in billing:
        print(f"    {r['report_no']:10s} {r['ndt_method']:3s} {str(r['joint_no']):20s} "
              f"수량 {r['quantity']:.0f}  {r['inspection_date']}")

    head("2. 조인트 압축 표기 — 펼친 개수를 청구 수량과 대조한다")
    print("  청구서는 한 칸에 여러 조인트를 압축해 적는다. 문법을 알면 개수가 검증된다.")
    bad = 0
    for r in billing:
        res = it.reconcile(r["joint_no"], r["quantity"])
        mark = "OK" if res["ok"] else "불일치"
        bad += not res["ok"]
        print(f"    {str(r['joint_no']):20s} → {it.expand(r['joint_no'])}  수량 {res['billed']:.0f}  {mark}")
    print(f"  결과: {len(billing) - bad}/{len(billing)} 일치" + ("" if bad else " — 수량 이상 없음"))

    head("3. 도면 — 스캔본을 OCR 하고 검사 범위표가 있는 쪽을 찾는다")
    dwg = sorted(SAMPLES.glob("drawings/*.pdf"))[0]
    ex = extract(dwg)
    print(f"  {dwg.name}")
    print(f"    {len(ex.pages)}쪽 · 텍스트레이어 {ex.text_layer_present} · 판독 {ex.pages[0].source}")
    for p in ex.pages:
        t = re.sub(r"\s+", " ", p.text or "")
        tag = "  ← 검사 범위표" if "scope of welded joint inspection" in t.lower() else ""
        print(f"    p{p.page_index + 1}: {len(t):4d}자  {t[:52]}{tag}")
    scope = next((p for p in ex.pages if "scope of welded joint inspection" in (p.text or "").lower()), None)
    if scope:
        t = re.sub(r"\s+", " ", scope.text)
        print("\n  이 표가 요구사항의 1차 근거다. 도면 본문이 아니라 별도 문서(MJZ0001)에 있다.")
        print(f"    발췌: …{t[t.lower().find('kks code'):][:120]}…")

    head("4. 성적서 — OCR 이 표를 통째로 놓친다. 이것이 이 도구를 만든 이유다")
    rep = next(SAMPLES.glob("NDT reports/*.pdf"))
    rex = extract(rep)
    from app.extractors.report_segmenter import _extract_crst_header
    print(f"  {rep.name}: {len(rex.pages)}쪽 (스캔본)\n")
    ok = rows_found = 0
    for p in rex.pages:
        t = re.sub(r"\s+", " ", p.text or "")
        raw = re.search(r"No\.\s*(\S+)\s+dated", t)
        m = _extract_crst_header(p.text or "")
        got = m["report_no_normalized"] if m else None
        ok += got is not None
        tail = t[t.lower().find("testing results"):] if "testing results" in t.lower() else ""
        has_rows = bool(re.search(r"(FW|SW|E)\d", tail)) or "removing" in tail.lower()
        rows_found += has_rows
        print(f"    p{p.page_index + 1}: 헤더 OCR '{raw.group(1) if raw else '?'}'"
              f"{'':>{max(0, 13 - len(raw.group(1) if raw else '?'))}} → 복원 {str(got):9s}"
              f" · 표1 데이터 {'읽힘' if has_rows else '**소실**'}")
    print(f"\n  헤더는 {ok}/{len(rex.pages)} 복원했다 — '7/7-O02UT'(슬래시·O→0), '77-ООЗРТ'(키릴 О·З·Р) 를 견딘다.")
    print(f"  그런데 표1 데이터 행은 {rows_found}/{len(rex.pages)} 만 남았다. 조인트 번호와 검사 사유가 통째로 사라진다.")
    print("""
  tesseract 는 격자 안의 짧은 문자열을 잘 놓친다. 실물에서도 성적서 121건의 표1 을
  한 줄도 못 읽었다. 그래서 이 도구는 두 가지를 한다.

    · 표처럼 생긴 쪽을 골라 **멀티모달로 다시 전사**하고 OCR 숫자와 대조한다
      (app/extractors/table_pages.py · table_transcriber.py)
    · 그렇게 얻은 값도 **원문에 실재하는지 확인**한다. 없으면 비우고 재확인 표시를 붙인다
      (app/analyzers/extraction_guard.py)

  근거 없는 값이 조용히 통과하면 '요구되지 않은 검사' 가 '요구된 검사' 로 뒤집힌다.""")

    head("5. 근거 사슬 — 청구 한 행이 서 있으려면 넷이 이어져야 한다")
    print("""
    청구서(행) ──성적서번호──▶ 성적서 ──5항 도면번호──▶ 도면 검사 범위표
         │                        │
         │                        └── 표1 '검사 사유' ──▶ 절차서(SCWEP) 조항
         └── 용접도 번호

    · 도면 범위표가 UT 100% 를 요구하면 UT 청구는 근거가 있다.
    · 도면이 PT 를 요구하지 않아도, 성적서 표1 이 '임시 부착물 제거부' 라고 적고
      절차서가 그 경우 PT 를 요구하면 PT 청구도 근거가 있다.
    · 어느 고리든 끊기면 '과다청구' 가 아니라 '판정 불가' 다 — 근거 없이 단정하지 않는다.""")

    scwep = next(SAMPLES.glob("scwep/*.pdf"), None)
    if scwep:
        s = re.sub(r"\s+", " ", " ".join(p.text or "" for p in extract(scwep).pages))
        i = s.lower().find("temporary process devices")
        if i > 0:
            print(f"\n  절차서 근거 발췌:\n    …{s[max(0, i - 90):i + 150]}…")

    head("끝 — 판정(적합성)은 LLM 이 아니라 규칙이 내린다")
    print("  이 시연은 LLM 없이(mock) 결정론 경로만 돌렸다.")
    print("  전체 검토는:  python -m app.main review --help")


if __name__ == "__main__":
    main()
