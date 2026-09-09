"""추출 근거 검증 — LLM 이 원문에 없는 숫자를 냈으면 무효화한다.

**왜 필요한가 (2026-09-07 사내 실사례)**
사내 ② 도면 등록에서 검사표가 `vt/pt/rt/ut 전부 100` 으로 적재됐다. 실제 표는 VT 100 · UT 100 · PT '-' · RT '-' 다.
원인은 모델의 무능이 아니라 **빈 입력**이었다. tesseract 가 그 표에서 숫자를 0개 뽑았고(사외 실측: KKS 0/10, 값 0/30),
아무것도 못 받은 HCX-007 이 그럴듯한 값을 채웠다. 프롬프트에는 이미 "추정 금지"가 있으므로 프롬프트로는 못 막는다.

이 결함이 특히 나쁜 이유: PT 가 '요구 없음'인데 '100% 요구'로 뒤집히면, PT 청구가 **정당한 것으로 판정된다**.
틀린 값보다 **근거 없는 값**이 위험하다 — 아무도 의심하지 않기 때문이다.

**무엇을 잡고 무엇을 못 잡는가**
- 잡는다: 원문에 숫자가 아예 없는데 나온 값, 원문에 있는 횟수보다 많이 주장한 값.
- 못 잡는다: 100 을 0 으로 잘못 읽은 것처럼 **원문에 그 숫자가 있는** 오독. 그건 표 전사(HCX-005)와
  숫자 대조가 맡는다. 이 가드는 마지막 그물이지 유일한 그물이 아니다.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_NUMERIC = ("vt_pct", "pt_or_mt_pct", "rt_pct", "ut_pct",
                    "auxiliary_vt_pct", "auxiliary_pt_or_mt_pct")
_DIGIT = re.compile(r"\d")


def _norm(s) -> str:
    return re.sub(r"[\s,]", "", str(s))


def _count_in(value, source: str) -> int:
    """source 안에 value 가 **독립된 수**로 몇 번 나오는가.

    원문의 공백을 지우면 안 된다 — '527-80' 과 '100' 이 붙어 '80100' 이 되면 경계가 사라진다.
    """
    v = _norm(value)
    if not v:
        return 0
    return len(re.findall(rf"(?<![\d.]){re.escape(v)}(?![\d.])", str(source)))


def verify_matrix(rows: Optional[Iterable[dict]], page_texts: Optional[dict],
                  *, numeric_fields: tuple = _DEFAULT_NUMERIC) -> tuple[list[dict], list[str]]:
    """행마다 숫자 항목이 그 쪽 원문에 실재하는지 확인하고, 없으면 None + needs_review.

    page_texts: {쪽번호(1-base): 그 쪽의 원문 텍스트(OCR 또는 전사)}
    반환: (검증된 행 목록, 경고 문자열 목록)
    """
    out: list[dict] = []
    warnings: list[str] = []
    for row in list(rows or []):
        r = dict(row)
        page = r.get("page_in_drawing")
        src = (page_texts or {}).get(page)
        claimed = [f for f in numeric_fields if r.get(f) is not None]
        if not claimed:
            out.append(r)
            continue
        kks = r.get("kks_code") or "?"
        if page is None:
            # 모델이 쪽을 안 댔다 = **대조를 못 한 것**이지 값이 틀린 것이 아니다. 값을 비우면 정상 추출까지
            # 지워진다 (2026-09-08 사내 ②: 40PAB02BR005 6개가 이 경로로 비워졌다). 표시만 남긴다.
            r["needs_review"] = True
            r["verification"] = "unverifiable"
            reason = (f"{kks}: 검사율 {len(claimed)}개가 쪽을 대지 않아 원문 대조 불가 — 값은 남기되 "
                      f"검토자가 도면에서 직접 확인할 것")
            r.setdefault("review_reasons", []).append(reason)
            warnings.append(reason)
            out.append(r)
            continue
        if not src:
            for f in claimed:
                r[f] = None
            r["needs_review"] = True
            r["verification"] = "unsupported"
            reason = (f"{kks}: 쪽 {page} 을 문서에서 못 찾아 검사율 {len(claimed)}개가 근거 없음 — 값을 비웠다")
            r.setdefault("review_reasons", []).append(reason)
            warnings.append(reason)
            out.append(r)
            continue
        # 원문에 있는 횟수만큼만 인정한다. 원문에 0번이면 '근거 없음', 있긴 한데 모자라면 '횟수 초과'.
        budget: dict[str, int] = {}
        absent: list[str] = []
        over: list[str] = []
        for f in claimed:
            v = _norm(r[f])
            if v not in budget:
                budget[v] = _count_in(r[f], src)
            if budget[v] > 0:
                budget[v] -= 1
            else:
                (absent if _count_in(r[f], src) == 0 else over).append(f)
                r[f] = None
        r["verification"] = "verified" if not (absent or over) else "unsupported"
        if absent or over:
            r["needs_review"] = True
            if absent:
                reason = (f"{kks}: 쪽 {page} 원문에 없는 검사율 {', '.join(absent)} 를 비웠다 — "
                          f"근거 없음 (표 전사 실패 또는 모델이 지어낸 값)")
                r.setdefault("review_reasons", []).append(reason)
                warnings.append(reason)
            if over:
                reason = (f"{kks}: 쪽 {page} 원문에 나오는 횟수를 초과한 검사율 {', '.join(over)} 를 비웠다")
                r.setdefault("review_reasons", []).append(reason)
                warnings.append(reason)
        out.append(r)
    if warnings:
        logger.warning("추출 근거 검증: %d건 무효화 — %s", len(warnings), warnings[0])
    return out, warnings


# ─────────────── 동일 구조 도면 간 일관성 ───────────────

def _strip_unit(kks: str) -> str:
    """KKS 앞 두 자리 호기 접두를 뗀다: 10PAB02BR005 · 40PAB02BR005 → PAB02BR005."""
    s = str(kks or "").strip().upper()
    return s[2:] if re.match(r"^\d{2}[A-Z]", s) else s


def consistency_report(sets_by_drawing: dict, *, fields: tuple = _DEFAULT_NUMERIC) -> dict:
    """구조가 같은 도면들의 추출 결과가 서로 같은지 본다.

    1항차 4개 도면(1~4호기)의 검사표는 호기 접두만 다르고 값이 **완전히 동일**하다 (사외 직독 확인).
    따라서 이 4건은 내장된 시험지다. 결과가 도면마다 다르면 값이 맞고 틀리고를 따지기 전에
    **표를 읽지 못했다**는 뜻이다 (2026-09-07 사내: 어떤 도면은 전부 100, 어떤 도면은 값 없음).
    """
    names = [n for n, rows in (sets_by_drawing or {}).items() if rows]
    if len(names) < 2:
        return {"consistent": None, "differing_fields": [], "message": "비교할 도면이 2건 미만"}
    by_name = {}
    for n in names:
        by_name[n] = {_strip_unit(r.get("kks_code")): r for r in sets_by_drawing[n]}
    common = set.intersection(*(set(v) for v in by_name.values()))
    differing: set = set()
    for key in sorted(common):
        for f in fields:
            vals = {by_name[n][key].get(f) for n in names}
            if len(vals) > 1:
                differing.add(f)
    ok = not differing
    msg = ("구조가 같은 도면들의 추출값이 일치" if ok else
           f"같은 구조인데 추출값이 도면마다 다르다 ({', '.join(sorted(differing))}) — "
           f"표를 읽지 못했을 가능성이 높다. 공통 라인 {len(common)}개, 도면 {len(names)}건")
    return {"consistent": ok, "differing_fields": sorted(differing), "message": msg,
            "common_lines": len(common), "drawings": names}


# ─────────────── 인용문 검증 ───────────────
#
# 근거 게이트와 SCWEP 면책은 "원문 인용이 있다" 를 조건으로 삼는다. 그런데 지금까지 인용문이 **그 원문에
# 실재하는지** 는 아무도 확인하지 않았다 — (문서, 쪽) 이 검색 결과에 있는지만 봤다. 모양은 맞고 내용은
# 지어낸 인용이 통과하면, 도면 검사표가 전부 100 으로 들어온 것과 같은 종류의 조용한 실패가 된다.
# 차이는 결과다: 그쪽은 요구사항을 뒤집고, 이쪽은 **면책을 만들어 낸다**.
#
# OCR 원문은 깨져 있으므로 정확 일치를 요구하면 정직한 인용까지 떨어진다. 부분 유사도로 본다.

_QUOTE_MIN_RATIO = 0.75


def _flatten(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def verify_quote(quote, source, *, min_ratio: float = _QUOTE_MIN_RATIO) -> tuple[bool, float]:
    """인용문이 원문 안에 (OCR 잡음을 감안해) 실제로 있는가. 둘 중 하나라도 비면 False."""
    q, s = _flatten(quote), _flatten(source)
    if not q or not s:
        return False, 0.0
    try:
        from rapidfuzz import fuzz
        ratio = fuzz.partial_ratio(q, s) / 100.0
    except ImportError:                      # rapidfuzz 없으면 보수적으로 정확 포함만 인정
        ratio = 1.0 if q in s else 0.0
    return ratio >= min_ratio, round(ratio, 4)


def verify_citations(citations: Optional[Iterable[dict]], snippets: Optional[Iterable[dict]],
                     *, min_ratio: float = _QUOTE_MIN_RATIO) -> list[dict]:
    """인용 목록을 검색 스니펫 원문과 대조해 quote_verified 를 붙인다.

    (문서, 쪽) 을 못 찾거나 인용문이 그 원문에 없으면 needs_review — 근거로 쓰기 전에 사람이 본다.
    """
    by_key = {(s.get("doc"), s.get("page")): s for s in (snippets or [])}
    out: list[dict] = []
    for c in list(citations or []):
        r = dict(c)
        src = by_key.get((r.get("doc"), r.get("page")))
        ok, ratio = verify_quote(r.get("quote"), (src or {}).get("text"), min_ratio=min_ratio)
        r["quote_verified"] = bool(ok)
        r["quote_match_ratio"] = ratio
        if not ok:
            r["needs_review"] = True
            why = ("검색 결과에 없는 (문서, 쪽)" if src is None
                   else f"인용문이 원문에서 확인되지 않음 (유사도 {ratio:.2f})")
            r.setdefault("review_reasons", []).append(
                f"{r.get('doc')} p.{r.get('page')}: {why} — 근거로 쓰기 전에 원문 대조 필요")
        out.append(r)
    return out
