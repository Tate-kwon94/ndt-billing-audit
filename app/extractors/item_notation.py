"""청구서 Item(조인트) 칸과 Weld Map No. 칸의 압축 표기를 펼친다.

1항차 실 데이터 4,354행 실측 (2026-09-07):
  · Item 이 한 칸에 여러 개인 행 1,397 (32%)
  · 단순 콤마 분리로 수량과 안 맞는 504행이 **전부** 아래 3종 — '기타' 0건. 닫힌 문법이다.
  · Weld Map 은 1,437행이 한 칸에 여러 개, 7행은 엑셀 앞따옴표, 27행은 공백 오염.

문법:
  a÷b            범위          Sp11.1÷6           → Sp11.1 … Sp11.6
  (a÷b)본체      곱셈          (1÷65)SC2-1,2      → 1..65 × {SC2-1, SC2-2}
  머리-접미,접미  접미사 곱셈   R.10,11,12-up,down → {R.10,R.11,R.12} × {up,down}
  본체-(그룹)    괄호 접미사   S.1E-(up-1,2,dn-1,2) → S.1E-up-1 …

해석 못 하는 문자열은 **원문 한 개**로 남긴다 (추론 금지 — 검토자가 본다).
"""
from __future__ import annotations

import re
from typing import Optional

_TRAIL_NUM = re.compile(r"^(?P<pre>.*?)(?P<num>\d+)$")
_MULT = re.compile(r"^\(\s*(?P<a>\d+)\s*÷\s*(?P<b>\d+)\s*\)\s*(?P<body>.+)$", re.S)
_PARENS = re.compile(r"^(?P<head>[^()]+?)\(\s*(?P<inner>.+?)\s*\)$", re.S)
# 뒤에 붙는 괄호 범위: '10GML90BR002-G01.(1÷4)' → …G01.1 … …G01.4 (실측 107행)
_PAREN_RANGE = re.compile(r"\(\s*(?P<a>\d+)\s*÷\s*(?P<b>\d+)\s*\)")
_SUFFIX = re.compile(r"^(?P<head>.+?)-(?P<tail>[^-]*(?:,[^-]*)+)$", re.S)
_SPLIT = re.compile(r"[,\n]")


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        x = x.strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _expand_one(text: str) -> list[str]:
    """구분자 없는 조각 하나를 펼친다."""
    t = text.strip()
    if not t:
        return []
    if "÷" in t:                            # Sp11.1÷6 / 1/1÷1/2 (끝 숫자 범위)
        left, _, right = t.partition("÷")
        ml, mr = _TRAIL_NUM.match(left.strip()), _TRAIL_NUM.match(right.strip())
        if ml and mr:
            pre_l, pre_r = ml.group("pre"), mr.group("pre")
            a, b = int(ml.group("num")), int(mr.group("num"))
            # 오른쪽 접두가 없거나(Sp11.1÷6) 왼쪽과 같으면(1/1÷1/2) 끝 숫자 범위로 읽는다.
            if b > a and (not pre_r or pre_r == pre_l):
                # 자릿수를 보존한다: SW0105÷0107 → SW0105·0106·0107 (SW105 가 되면 성적서와 안 맞는다).
                # 왼쪽이 0 으로 시작할 때만 채운다 — Sp11.1÷6 을 Sp11.01 로 만들면 안 된다.
                num = ml.group("num")
                width = len(num) if num.startswith("0") else 0
                return [f"{pre_l}{str(i).zfill(width) if width else i}" for i in range(a, b + 1)]
    return [t]


def _expand_group(text: str) -> list[str]:
    """괄호 안처럼 접두가 반복되는 목록: 'up-1,2,dn-1,2' → up-1,up-2,dn-1,dn-2."""
    out: list[str] = []
    prefix = ""
    for part in _SPLIT.split(text):
        p = part.strip()
        if not p:
            continue
        if "-" in p:
            prefix = p.rsplit("-", 1)[0]
            out.append(p)
        else:
            out.append(f"{prefix}-{p}" if prefix else p)
    # 조각마다 끝 숫자 범위(1/1÷1/2, Sp11.1÷6)를 마저 펼친다
    return _dedupe([x for q in out for x in _expand_one(q)])


def _expand_paren_ranges(text: str) -> list[str]:
    """문자열 어디에 있든 `(a÷b)` 를 그 자리에서 숫자로 펼친다 — 앞·중간·뒤가 모두 같은 규칙.

    (1÷65)SC2-1,2 / R.4.(1÷3)-dn,up / …G01.(1÷4) 세 형태가 이 한 줄로 처리된다.
    """
    m = _PAREN_RANGE.search(text)
    if not m:
        return [text]
    a, b = int(m.group("a")), int(m.group("b"))
    if b < a:
        return [text]
    width = len(m.group("a")) if m.group("a").startswith("0") else 0
    out: list[str] = []
    for i in range(a, b + 1):
        num = str(i).zfill(width) if width else str(i)
        out += _expand_paren_ranges(text[:m.start()] + num + text[m.end():])
    return out


def expand(text) -> list[str]:
    """Item 칸 → 조인트 목록. 해석 못 하면 원문 한 개 (추론 금지)."""
    if text is None:
        return []
    t = str(text).strip()
    if not t:
        return []
    out: list[str] = []
    for variant in _expand_paren_ranges(t):
        out += _expand_variant(variant)
    return _dedupe(out)


def _expand_variant(t: str) -> list[str]:
    """괄호 범위가 이미 펼쳐진 문자열 하나 → 조인트 목록."""
    m = _PARENS.match(t)                    # S.1E-(up-1,2,dn-1,2) / R.07-(dn,up)
    if m:
        head = m.group("head").rstrip("-")
        inner = _expand_group(m.group("inner"))
        if inner:
            return [f"{head}-{x}" for x in inner]
    m = _SUFFIX.match(t)                    # R.10,11,12-up,down
    if m and _SPLIT.search(m.group("head") or "") and _SPLIT.search(m.group("tail")):
        heads = _expand_heads(m.group("head"))
        tails = [x.strip() for x in _SPLIT.split(m.group("tail")) if x.strip()]
        return [f"{h}-{s}" for h in heads for s in tails]
    if _SPLIT.search(t):
        return _expand_group(t)             # 접두 물려주기 포함
    return _expand_one(t)


def _expand_heads(head: str) -> list[str]:
    """'R.10,11,12' → R.10, R.11, R.12 (첫 조각의 문자 접두를 나머지에 물려준다)."""
    parts = [p.strip() for p in _SPLIT.split(head) if p.strip()]
    if not parts:
        return []
    m = re.match(r"^(?P<pre>.*?)(?P<num>\d+)$", parts[0])
    pre = m.group("pre") if m else ""
    out = [parts[0]]
    for p in parts[1:]:
        out.append(p if not p.isdigit() else f"{pre}{p}")
    return _dedupe([x for h in out for x in _expand_one(h)])


def reconcile(text, billed_qty: Optional[float]) -> dict:
    """펼친 조인트 개수와 청구 수량을 대조. ok=None 이면 수량이 없어 판단 보류."""
    n = len(expand(text))
    if billed_qty is None:
        return {"expanded": n, "billed": None, "ok": None, "delta": None}
    delta = float(billed_qty) - n
    return {"expanded": n, "billed": float(billed_qty), "ok": abs(delta) < 0.01, "delta": delta}


def weld_maps(cell) -> list[str]:
    """Weld Map No. 칸 → 도면번호 목록. 엑셀 앞따옴표·공백·콤마/줄바꿈 혼용을 정리한다."""
    if cell is None:
        return []
    return _dedupe([p.strip().lstrip("'").strip() for p in _SPLIT.split(str(cell)) if p.strip()])
