"""① 검토결과를 답안지와 행 단위로 대조해 점수를 낸다.

사내에서 필요한 것은 답안지를 눈으로 훑는 게 아니라 **숫자**다. 답안지는 JSON 으로 패치에 실리고
(엑셀은 반입 정책상 못 싣는다), 검토결과 xlsx 는 사내 출력물이므로 여기서 둘을 맞춘다.

채점을 셋으로 나눈다 — 섞으면 무엇이 좋아졌는지 알 수 없다.
  matching   성적서를 붙였는가 (결정론 매칭의 성적)
  verdict    판정이 답과 같은가 (LLM·게이트의 성적)
  unjudgeable 판정할 수 없는 행(성적서없음·도면미보유)에 **재확인 표시를 했는가**
              — 이것을 OK 로 통과시키면 근거 없이 승인한 것이다. 놓치면 오답으로 센다.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Optional

import openpyxl

UNJUDGEABLE = ("성적서없음", "도면미보유")


def _norm(s) -> str:
    return re.sub(r"[\s\-_]", "", str(s or "")).upper()


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "예", "y", "yes", "1")


def _read_result(path) -> dict:
    ws = openpyxl.load_workbook(Path(path), read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hi = next((i for i, r in enumerate(rows) if r and "적합성_판정" in [str(c) for c in r]), None)
    if hi is None:
        raise ValueError(f"검토결과에서 '적합성_판정' 헤더를 못 찾음: {path}")
    head = [str(c) for c in rows[hi]]
    ix = {k: i for i, k in enumerate(head)}
    need = ("Report Number", "Item", "매칭_방법", "적합성_판정")
    missing = [k for k in need if k not in ix]
    if missing:
        raise ValueError(f"검토결과에 필요한 열이 없음: {', '.join(missing)}")
    out = {}
    for r in rows[hi + 1:]:
        if not r or r[ix["Item"]] is None:
            continue
        out[(_norm(r[ix["Report Number"]]), str(r[ix["Item"]]).strip())] = {
            "매칭": str(r[ix["매칭_방법"]]),
            "판정": str(r[ix["적합성_판정"]]),
            "재확인": _truthy(r[ix["재확인_필요"]]) if "재확인_필요" in ix else False,
        }
    return out


def score(key: dict, result_path) -> dict:
    got = _read_result(result_path)
    # 0 인 항목도 화면에 나와야 한다 — 없는 칸과 0 인 칸은 다르다
    m = Counter({"일치": 0, "불일치": 0}); v = Counter(); u = Counter({"표시함": 0, "놓침": 0})
    missing = 0; scored = 0
    for row in key.get("rows") or []:
        k = (_norm(row.get("성적서번호")), str(row.get("Item") or "").strip())
        g = got.get(k)
        if g is None:
            missing += 1
            continue
        scored += 1
        truth = row.get("최종정답")
        matched = g["매칭"] != "none"
        m["일치" if matched == _truthy(row.get("성적서_있음")) else "불일치"] += 1
        if truth in UNJUDGEABLE:
            # 판정할 수 없는 행 — 재확인 표시가 있어야 맞은 것이다
            u["표시함" if (g["재확인"] or g["판정"] != "OK") else "놓침"] += 1
        else:
            v[f"{truth}→{g['판정']}"] += 1
    return {"rows_scored": scored, "rows_missing_from_result": missing,
            "matching": dict(m), "verdict": dict(v), "unjudgeable": dict(u)}
