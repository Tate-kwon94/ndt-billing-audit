"""CEB Fabrication 검사기준 근거(col27) 파서 + 기준 매트릭스.

배경 (2026-07-06 원문 검증 완료):
- CEB 청구 엑셀 col27 '기준 근거' 는 자유텍스트 25종 — 규격·조항 인용(A급)부터
  근거 없는 "100%" 주장(B급)까지 혼재.
- 검증된 코드 요구 (보유 원문 대조):
  * SP 70.13330.2012 §10.4 Table 10.6: VT — all types — 100% (물리 p139)
  * GOST 23118(-2019 §6.4.1 Table 4): VT — all — 100% + protocol 문서화 의무
    (2012판 인용 조번호는 §6.18 — 적용판 질의 중)
- 정책: 위험은 과다청구 단방향. 미청구(UT/RT 등) = 비용절감 → 정보성 기록만.

파서는 결정론 (LLM 미사용) — 25종 실측 패턴 기반. 미지 패턴은 needs_review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────── 등급 정의 ───────────────────────────

# A1: SP 70.13330 §10.4 T10.6 (조항 인용, 원문 검증 통과)
# A2: GOST 23118 (§6.18/2012판) (조항 인용, 실질 요구 검증 통과 — 적용판 질의 중)
# A3: SP 은폐작업 확인서   A4: SP 중요구조물 확인서   A5: SP 매입철물
# A6: SP 일반 준수 선언 (조항 미특정 — 검사범위 근거로 불충분)
# B1: 도면 노트 참조 (예: E/5.2) — 참조 문서 확인 필요
# B2: 근거 미인용 "100%" 주장   B3: 근거 미인용 + 범위도 없음
# UNKNOWN: 미지 패턴 — needs_review

GRADE_INFO = {
    "A1": "SP 70.13330.2012 §10.4 Table 10.6 — 원문 검증 통과 (VT all 100%)",
    "A2": "GOST 23118 — 실질 요구 검증 통과 (VT all 100% + protocol 의무). 적용판 질의 중",
    "A3": "SP 은폐작업 확인서 조항 — 실존 확인",
    "A4": "SP 중요구조물 확인서 조항 — 실존 확인",
    "A5": "SP 매입철물 조항 — 실존 확인",
    "A6": "SP 일반 준수 선언 — 조항 미특정, 검사범위 근거로 불충분",
    "B1": "도면 노트 참조 — 참조 문서 미보유, 확인 필요",
    "B2": "근거 미인용 — 방법·범위만 주장 (코드화 요구 대상)",
    "B3": "근거 미인용 + 검사범위도 미기재 (최하위)",
    "UNKNOWN": "미지 패턴 — 검토자 확인 필요",
    "EMPTY": "기준 근거 미기재",
}

# 검증된 기준 요구 매트릭스 (보유 코드 원문 확인분).
# required=True 인 방법이 청구에 있으면 근거 성립. 청구에 없으면 미청구=비용절감(정보성).
VERIFIED_REQUIREMENTS = {
    "SP 70.13330.2012 §10.4 T10.6": {
        "VT": {"scope_pct": 100, "basis": "Table 10.6 row 1 — all types of structure joints"},
        "UT": {"scope_pct": 0.5, "basis": "Table 10.6 row 2 — ≥0.5% of weld length (+도면 지시)",
                "watch_only": True},  # 미청구 = 감시 포인트
    },
    "GOST 23118 T4": {
        "VT": {"scope_pct": 100, "basis": "2019판 §6.4.1 Table 4 — all, 100% + protocol 문서화"},
        "UT_RT": {"scope_pct": 100, "basis": "Table 4 — seam types 1·2 는 UT 또는 RT 100%",
                   "watch_only": True},
    },
}


@dataclass
class CriteriaParse:
    """col27 1건의 파싱 결과."""

    raw: str
    grade: str                       # A1..B3 / UNKNOWN / EMPTY
    standard: Optional[str] = None   # "SP 70.13330.2012" / "GOST 23118-2012"
    clause: Optional[str] = None     # "10.4/T10.6" / "6.18" 등
    method: Optional[str] = None     # "VT" 등
    scope_pct: Optional[float] = None
    note_ref: Optional[str] = None   # "(E/5.2)" 의 E/5.2
    needs_review: bool = False
    review_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "grade": self.grade,
            "standard": self.standard,
            "clause": self.clause,
            "method": self.method,
            "scope_pct": self.scope_pct,
            "note_ref": self.note_ref,
            "needs_review": self.needs_review,
            "review_reason": self.review_reason,
            "grade_info": GRADE_INFO.get(self.grade, ""),
        }


# ─────────────────────────── 패턴 ───────────────────────────

_RE_SP = re.compile(r"(?:SP\s*)?70\.13330[.\-]?(\d{4})?", re.I)
_RE_SP_CLAUSE = re.compile(r"clause\s*10\.4|table\s*10\.6", re.I)
_RE_GOST_23118 = re.compile(r"GOST\s*23118(?:-(\d{4}))?", re.I)
_RE_GOST_CLAUSE = re.compile(r"\b6\.18\b")
_RE_NOTE_REF = re.compile(r"\(([A-Z]/[\d.]+)\)")
_RE_SCOPE_100 = re.compile(r"100\s*%|each\s+weld|entire\s+length|all\s+(?:the\s+)?(?:welded\s+)?joints",
                            re.I)
_RE_VISUAL = re.compile(r"visual|VT\b", re.I)
_RE_CONCEALED = re.compile(r"concealed\s+works?", re.I)
_RE_CRITICAL = re.compile(r"critical\s+st(?:r)?uctures?|examination\s+certificates?", re.I)
_RE_EMBEDDED = re.compile(r"embedded\s+parts?", re.I)
_RE_GENERAL_COMPLY = re.compile(r"shall\s+be\s+implemented\s+and\s+accepted|in\s+compliance\s+with",
                                 re.I)


def parse_criteria(raw: Optional[str]) -> CriteriaParse:
    """col27 '기준 근거' 1건 → 구조화. 결정론 — 25종 실측 패턴 기반."""
    text = (str(raw).strip() if raw is not None else "")
    if not text or text.lower() in ("none", "nan", "-"):
        return CriteriaParse(raw=text, grade="EMPTY", needs_review=True,
                             review_reason="기준 근거 미기재")

    has_visual = bool(_RE_VISUAL.search(text))
    has_100 = bool(_RE_SCOPE_100.search(text))
    sp_m = _RE_SP.search(text)
    gost_m = _RE_GOST_23118.search(text)
    note_m = _RE_NOTE_REF.search(text)

    method = "VT" if has_visual else None
    scope = 100.0 if has_100 else None

    # A1 — SP §10.4/T10.6 (검사범위 조항)
    if sp_m and _RE_SP_CLAUSE.search(text):
        return CriteriaParse(
            raw=text, grade="A1", standard="SP 70.13330.2012",
            clause="10.4/T10.6", method=method or "VT", scope_pct=scope or 100.0,
        )
    # A2 — GOST 23118 (조항 6.18 유무 무관하게 규격 인용이면 — 실측상 6.18 동반)
    if gost_m:
        yr = gost_m.group(1)
        return CriteriaParse(
            raw=text, grade="A2",
            standard=f"GOST 23118-{yr}" if yr else "GOST 23118",
            clause="6.18(2012판)" if _RE_GOST_CLAUSE.search(text) else None,
            method=method or "VT", scope_pct=scope or 100.0,
            needs_review=(yr == "2012"),
            review_reason="적용판 확인 중 (보유 2019판, 인용 2012판)" if yr == "2012" else None,
        )
    # A3~A5 — SP 절차 조항 (검사범위가 아닌 문서화 요구)
    if sp_m and _RE_CONCEALED.search(text):
        return CriteriaParse(raw=text, grade="A3", standard="SP 70.13330.2012",
                             clause="concealed works acceptance")
    if _RE_CRITICAL.search(text):
        return CriteriaParse(raw=text, grade="A4", standard="SP 70.13330.2012",
                             clause="critical structures acceptance")
    if sp_m and _RE_EMBEDDED.search(text):
        return CriteriaParse(raw=text, grade="A5", standard="SP 70.13330.2012",
                             clause="embedded parts")
    # A6 — SP 일반 준수 (조항 미특정)
    if sp_m and _RE_GENERAL_COMPLY.search(text):
        return CriteriaParse(raw=text, grade="A6", standard="SP 70.13330.2012",
                             needs_review=True,
                             review_reason="조항 미특정 — 검사범위 근거로 불충분")
    # B1 — 도면 노트 참조
    if note_m:
        return CriteriaParse(raw=text, grade="B1", note_ref=note_m.group(1),
                             method=method, scope_pct=scope,
                             needs_review=True,
                             review_reason=f"참조 문서({note_m.group(1)}) 미보유 — 확인 필요")
    # B2 — 무인용, 방법+범위 주장
    if has_visual and has_100:
        return CriteriaParse(raw=text, grade="B2", method="VT", scope_pct=100.0,
                             needs_review=True,
                             review_reason="근거 문서 미인용 — 코드화 요구 대상")
    # B3 — 무인용 + 범위도 없음
    if has_visual:
        return CriteriaParse(raw=text, grade="B3", method="VT",
                             needs_review=True,
                             review_reason="근거·검사범위 모두 미기재")
    # UNKNOWN
    return CriteriaParse(raw=text, grade="UNKNOWN", needs_review=True,
                         review_reason="미지 패턴 — 검토자 확인 필요")


# ─────────────────────────── 기준 매트릭스 판정 ───────────────────────────


def judge_billing_against_criteria(
    parse: CriteriaParse, billed_methods: dict[str, float],
) -> list[dict]:
    """행의 기준 파싱 + 청구된 검사방법·수량 → findings (과다 중심).

    billed_methods: {"VT": 224, "PT": 0, ...}  (0/None 은 미청구 취급)
    판정:
      - 기준이 요구하는 방법을 청구 → OK (근거 성립)
      - 기준 근거가 없는데(B2/B3/EMPTY) 청구 → needs_review (근거 요구)
      - 기준이 요구하지 않는 방법을 청구 → 과다 의심
      - 기준이 요구하는데 미청구 → 정보성 (비용절감 — 감시 기록만)
    """
    findings: list[dict] = []
    billed = {m.upper(): q for m, q in (billed_methods or {}).items() if q}

    if parse.grade in ("A1", "A2"):
        # 검증된 기준 — VT 100% 성립
        for m, q in billed.items():
            if m == "VT":
                continue  # 근거 성립
            if m in ("PT", "LT", "KT", "UT", "RT"):
                findings.append({
                    "rule": "billed_method_not_in_cited_criteria",
                    "severity": "review",
                    "details": {
                        "method": m, "qty": q,
                        "cited": f"{parse.standard} {parse.clause or ''}".strip(),
                        "note": f"인용 기준은 VT 100% (+UT 감시) — '{m}' 청구의 근거 조항 확인 필요",
                    },
                })
    elif parse.grade in ("B2", "B3", "EMPTY", "UNKNOWN"):
        if billed:
            findings.append({
                "rule": "billing_without_cited_criteria",
                "severity": "review",
                "details": {
                    "methods": sorted(billed),
                    "grade": parse.grade,
                    "note": "검사기준 근거 미인용 상태의 청구 — 보완지시 6(코드화) 회신 필요",
                },
            })
    elif parse.grade == "B1":
        findings.append({
            "rule": "criteria_is_unresolved_note_ref",
            "severity": "review",
            "details": {"note_ref": parse.note_ref,
                        "note": "도면 노트 참조 — 대상 문서 특정·제출 요청"},
        })
    # A3~A6: 절차 조항 — 검사범위 판정에는 미사용 (문서화 요구 확인용)

    # 감시 (정보성): 검증된 기준의 watch_only 방법 미청구 → 비용절감, 기록만
    if parse.grade == "A1" and "UT" not in billed:
        findings.append({
            "rule": "required_method_unbilled_info",
            "severity": "info",
            "details": {"method": "UT", "basis": "SP T10.6 ≥0.5%",
                        "note": "미청구 = 비용절감 (문제 아님). 소급 청구 시 엄격 대조"},
        })
    if parse.grade == "A2" and not ({"UT", "RT"} & set(billed)):
        findings.append({
            "rule": "required_method_unbilled_info",
            "severity": "info",
            "details": {"method": "UT/RT", "basis": "GOST 23118 T4 — seam type 1·2 는 100%",
                        "note": "미청구 = 비용절감 (문제 아님). 소급 청구 시 엄격 대조"},
        })
    return findings
