"""SCWEP 근거 판정기 — "이 청구를 과다청구라고 말해도 되는가".

왜 이 모듈이 있는가 (2026-09-04)
    SCWEP 은 **특수 공정·특수 상황의 비파괴 요구**를 담는다. 사용자 설명 그대로
    "러그를 떼고 나면 그 자리에 PT 를 해야 한다" 같은 것이다. 이런 요구는
    **Detailed Drawing 에 없다.** 도면은 설계 형상을 그린 것이지 가설재를 떼는
    공정을 적은 문서가 아니기 때문이다.

    그런데 지금까지 compliance 는 "도면에 그 검사가 없다" 는 사실 하나만으로
    `billed_ndt_not_in_requirements` (하드 위반 → NONCOMPLIANT) 를 확정했다.
    러그 제거 후 PT 를 정당하게 청구해도 과다청구로 지목된다.
    **분쟁 문서에서 시공사를 잘못 지목하는 것은 과다분을 놓치는 것보다 나쁘다.**

원칙
    근거 사슬이 완전할 때까지 "과다청구" 라고 말하지 않는다.
    부적합 확정은 **긍정적 사실**을 요구한다 —
    "당신이 이 분야 절차서를 제출했고, 거기에 이 검사가 없다."

    SCWEP 은 시공사가 **제출해야** 검토 대상이 된다(사용자 확인, 2026-09-04).
    발주자가 상시 보유·색인하는 문서가 아니다. 그래서 "SCWEP 0건" 은 결함이
    아니라 정상 상태이며, 그 상태에서는 고발이 아니라 **제출 요구**가 맞다.

네 상태 — 순서 자체가 안전장치다
    covered        조건부 요구가 이 검사를 지목. 조건·인용문이 있고 신뢰도 충분.
    unclear        방법만 겹침(일반규칙·샘플링률) 또는 저신뢰 조건부 히트.
    no_basis_found 제출됐고·범위 맞고·새 형식이고·조항이 실제로 뽑혔는데 침묵.
                   **여기서만 과다청구를 확정할 수 있다.**
    not_submitted  그 외 전부.

    일반규칙·샘플링률은 스키마상 "어떤 사건이 일어나면" 을 표현할 수 없다.
    그래서 그쪽에서만 걸린 것은 '정당' 이 아니라 '확인 필요' 다. 이것이 SCWEP
    한 건이 회차 전체를 조용히 면책시키는 것을 막는 유일한 장치다.

경계 조건은 전부 **보수적**으로 — 모르면 고발하지 않는다
    - 신뢰도 값이 없거나 숫자가 아니면 **임계 미달로 취급**한다. 절대 1.0 기본값을
      주지 않는다 (StandardDocument 에는 신뢰도 컬럼이 없고 extracted_json 안에만
      있으므로, 없는 경우가 실제로 생긴다).
    - needs_review 키가 없으면 True 로 본다.
    - 적용 범위가 비어 있으면 'unknown' 이고, unknown 은 **면책은 가능하지만
      고발은 불가**하다. 이 비대칭은 의도된 것이다.

이 모듈은 순수하다. classify() 는 DB 도 LLM 도 만지지 않는다 (테스트가 쉬워야
실제로 테스트된다). DB 접근은 load_docs() 하나에 가둔다.
"""
from __future__ import annotations

import re

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────── 임계값 ───────────────────────────
# 4단계에서 config/matching_rules.yaml 로 뺀다. 지금은 코드 상수로 자족한다.

MIN_RULE_CONFIDENCE = 0.7        # 조건부 요구 항목 자체의 신뢰도
MIN_DOC_CONFIDENCE = 0.7         # 문서 추출 신뢰도 (고발 경로에서만 요구)
MIN_SCHEMA_VERSION = 2           # 조건부 인지 프롬프트로 뽑힌 문서만 고발 근거

# general_rules 중 "이 검사를 언급한 것으로 볼" topic
_MEANINGFUL_TOPICS = {"procedure", "sampling", "re-examination", "acceptance"}

STATE_COVERED = "covered"
STATE_UNCLEAR = "unclear"
STATE_NO_BASIS = "no_basis_found"
STATE_NOT_SUBMITTED = "not_submitted"


@dataclass
class BasisAssessment:
    """근거 판정 결과.

    state          위 네 상태 중 하나
    refs           인용 가능한 근거 (authority_level 2 로 auth_refs 에 실린다)
    reasons        운영자가 읽는 한국어 사유 (검토 엑셀·재확인 사유로 전파)
    covering_docs  covered 를 만든 문서번호들 (한 문서가 회차를 지배하는지 감시용)
    detail         finding details 에 실을 기계 판독용 부가정보
    """

    state: str
    refs: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    covering_docs: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def may_claim_overbilling(self) -> bool:
        """과다청구를 확정해도 되는 상태인가. 이 한 줄이 이 모듈의 존재 이유다."""
        return self.state == STATE_NO_BASIS


# ─────────────────────────── 보수적 읽기 도우미 ───────────────────────────

def _norm_method(value: Any) -> str:
    """NDT 방법 정규화. 'pt ' → 'PT'."""
    return str(value or "").strip().upper()


def _conf(value: Any) -> Optional[float]:
    """신뢰도를 float 로. **숫자가 아니면 None** — 호출부는 None 을 임계 미달로 다룬다.

    bool 은 float() 가 받아버리므로 명시적으로 배제한다 (True → 1.0 사고 방지).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _conf_ok(value: Any, threshold: float) -> bool:
    c = _conf(value)
    return c is not None and c >= threshold


def _doc_flagged(extracted: dict) -> bool:
    """문서가 재확인 대상인가. **키가 없으면 True** (모르면 신뢰하지 않는다)."""
    return bool(extracted.get("needs_review", True))


def _schema_version(extracted: dict) -> int:
    v = extracted.get("_schema_version", extracted.get("schema_version", 1))
    try:
        return int(v)
    except (TypeError, ValueError):
        return 1


def _text(value: Any) -> str:
    return str(value or "").strip()


# ─────────────────────────── 적용 범위 ───────────────────────────

def scope_state(extracted: dict, discipline: Optional[str], *, drawing_no: Optional[str] = None) -> str:
    """이 문서가 이 공종에 적용되는가 — 'yes' | 'no' | 'unknown'.

    2026-09-05 해소: 표지에 CP-M1 없음 → KKS 로 대조. 'CP-M1' 같은 발주자측 공종 코드는
    발주처·시공사 계약 세부내역 항목이라 노형 공급사 문서(시공사 SCWEP·도면)에는 나오지 않는다
    (사용자 확인). 시공사 문서와 도면이 공유하는 범위 키는 문서번호 다섯째 마디(KKS 건물·
    계통, 고정폭 12자) 뿐이다 — 이것을 먼저 본다. KKS 로 못 정하면(unknown) 기존
    discipline 경로로 내려간다 (구 형식 문서·합성 테스트 데이터 호환).
    """
    from app.extractors import kks

    scope = extracted.get("applicable_scope") or {}
    if not isinstance(scope, dict):
        scope = {}
    kks_field = scope.get("kks")
    # LLM 이 {"raw": ...} 대신 문자열을 내도 죽지 않는다 — 문자열이면 그것을 raw 로 본다 (2026-09-05 리뷰)
    kks_raw = kks_field.get("raw") if isinstance(kks_field, dict) else (kks_field if isinstance(kks_field, str) else None)
    doc_kks = kks.parse(extracted.get("document_no")) or kks.parse_segment(kks_raw)
    kks_state = kks.same_scope(doc_kks, kks.parse(drawing_no))
    if kks_state != "unknown":
        return kks_state

    if not discipline:
        return "unknown"
    disciplines = scope.get("disciplines") or []
    if not isinstance(disciplines, (list, tuple)) or not disciplines:
        return "unknown"
    want = _text(discipline).upper()
    have = {_text(d).upper() for d in disciplines if _text(d)}
    if not have:
        return "unknown"
    return "yes" if want in have else "no"


# ─────────────────────────── 규칙 매칭 ───────────────────────────

_STOP = {"후", "전", "및", "the", "of", "and", "shall", "be", "to", "in", "a", "is", "for", "제거", "자리"}


def context_matches(rule: dict, report_context) -> bool:
    """성적서가 적은 검사 사유가 이 규칙의 조건과 맞는가.

    1항차 실물: SCWEP 은 "임시 공정 장치를 제거한 자리" 에 PT 를 요구하고, 성적서 표1 의 '용접부 특성' 칸에는
    "Removing temporary plate fit-up" 이라고 적혀 있다. 둘을 이으면 그 행의 PT 는 근거가 있는 것이고,
    사유를 안 적은 행은 자동 면책이 아니라 검토자가 본다.

    trigger_keywords 가 있으면 그것으로, 없으면 trigger 문장의 낱말로 대조한다 (부분 일치, 대소문자 무시).
    """
    ctx = _text(report_context).lower()
    if not ctx:
        return False
    kws = [str(k).strip().lower() for k in (rule.get("trigger_keywords") or []) if str(k).strip()]
    if not kws:
        kws = [w for w in re.split(r"[\s,·/]+", _text(rule.get("trigger")).lower())
               if len(w) >= 2 and w not in _STOP]
    return any(k in ctx for k in kws)


def _conditional_hits(extracted: dict, method: str) -> tuple[list[dict], list[dict]]:
    """조건부 요구 중 이 방법을 지목한 것. (충분한 것, 약한 것) 으로 나눠 돌려준다.

    '충분' 의 조건 — 넷 다 만족해야 한다:
      · 조건(trigger) 문구가 있다        ← 없으면 무엇을 근거로 면책하는지 알 수 없다
      · 원문 인용(quote) 이 있다          ← 사람이 대조할 수 없는 면책은 면책이 아니다
      · 그 인용문이 원문에서 **틀리지 않았다** ← quote_verified=False 면 지어낸 문장이므로 제외.
                                           None(확인 못 함)은 제외하지 않고 needs_review 로 표시만 한다 —
                                           '모른다' 를 '틀렸다' 로 바꿔 옛 적재본의 면책을 뒤집지 않는다
      · 방법이 정확히 일치한다            ← 'ALL' 은 인정하지 않는다. 전부를 뜻하는
                                           값으로 특수공정 면책을 만들 수 없다
      · 항목 신뢰도가 임계 이상이다
    """
    strong: list[dict] = []
    weak: list[dict] = []
    for rule in extracted.get("conditional_ndt_requirements") or []:
        if not isinstance(rule, dict):
            continue
        if _norm_method(rule.get("ndt_method")) != method:
            continue
        trigger = _text(rule.get("trigger"))
        quote = _text(rule.get("quote"))
        if not trigger or not quote:
            weak.append(rule)
            continue
        # 인용문 검증 (2026-09-07 파생 검토). **실패한 것**과 **못 한 것**을 구분한다.
        #   False → 원문과 대조했는데 그 문장이 없다. 지어낸 인용이므로 면책 근거로 쓰지 않는다.
        #   None  → 대조 자체를 못 했다 (쪽 미기재, 또는 이 검증 이전에 적재된 문서).
        #           '모른다' 를 '틀렸다' 로 바꾸면 옛 적재본의 정당한 면책이 한꺼번에 뒤집힌다 —
        #           판정을 바꾸는 대신 근거에 표시만 남기고 검토자가 보게 한다.
        if rule.get("quote_verified") is False:
            weak.append(rule)
            continue
        (strong if _conf_ok(rule.get("confidence"), MIN_RULE_CONFIDENCE) else weak).append(rule)
    return strong, weak


def _weak_mentions(extracted: dict, method: str) -> list[dict]:
    """일반 규칙·샘플링률에서 이 방법이 언급된 것.

    이 두 배열은 스키마상 "어떤 사건이 일어나면" 을 표현할 수 없다. 그래서 여기서
    걸린 것은 정당화가 아니라 **확인 필요**다. 다만 '침묵했다'고 말할 수도 없으므로
    고발도 막는다.
    """
    out: list[dict] = []
    for rule in extracted.get("general_rules") or []:
        if not isinstance(rule, dict):
            continue
        rule_method = _norm_method(rule.get("ndt_method"))
        if rule_method in (method, "ALL") and _text(rule.get("topic")) in _MEANINGFUL_TOPICS:
            out.append(rule)
    for rule in extracted.get("default_sampling_rates") or []:
        if not isinstance(rule, dict):
            continue
        if _norm_method(rule.get("ndt_method")) != method:
            continue
        if _conf(rule.get("rate_pct")) not in (None, 0.0):
            out.append(rule)
    return out


def _has_real_extraction(extracted: dict) -> bool:
    """이 문서에서 규칙이 실제로 뽑혔는가 — '침묵했다' 고 말할 자격이 있는지.

    **default_sampling_rates 는 세지 않는다.** RT 샘플링률 한 줄만 들어 있는 문서가
    PT 에 대한 침묵의 증거가 될 수는 없다.
    """
    for key in ("conditional_ndt_requirements", "general_rules"):
        items = extracted.get(key)
        if isinstance(items, (list, tuple)) and len(items) > 0:
            return True
    return False


def _ref(doc_no: str, rule: dict, kind: str) -> dict:
    return {
        "authority_level": 2,
        "doc": doc_no,
        "page": rule.get("page"),
        "section": _text(rule.get("applies_to")) or _text(rule.get("topic")) or None,
        "trigger": _text(rule.get("trigger")) or None,
        "quote": _text(rule.get("quote")) or None,
        "ndt_method": _norm_method(rule.get("ndt_method")) or None,
        "kind": kind,
        # 인용문이 원문에서 확인됐는가. None 이면 확인 못 한 것 — 근거로는 쓰되 검토자에게 보인다.
        "quote_verified": rule.get("quote_verified"),
        "needs_review": rule.get("quote_verified") is not True,
    }


# ─────────────────────────── 판정 ───────────────────────────

def classify(docs: Iterable[dict], method: Any, discipline: Optional[str], *,
            drawing_no: Optional[str] = None, report_context: Optional[str] = None) -> BasisAssessment:
    """제출된 SCWEP 들을 놓고 이 청구의 근거 상태를 정한다.

    docs  : load_docs() 형식 — [{"document_no": str, "extracted": dict}, ...]
    method: 청구된 NDT 방법
    discipline: 청구 회차의 공종 (BillingRound.discipline) — KKS 로 못 정할 때의 폴백
    drawing_no: 청구·성적서의 도면번호 — scope_state 가 KKS 대조에 먼저 쓴다

    순서가 곧 정책이다. 면책이 먼저, 고발이 맨 마지막.
    """
    m = _norm_method(method)
    if not m:
        return BasisAssessment(
            state=STATE_NOT_SUBMITTED,
            reasons=["청구 NDT 방법이 비어 있어 SCWEP 근거를 대조할 수 없음"],
            detail={"reason": "billed_method_empty"},
        )

    doc_list = [d for d in docs if isinstance(d, dict)]
    if not doc_list:
        return BasisAssessment(
            state=STATE_NOT_SUBMITTED,
            reasons=["제출된 SCWEP 없음 — 시공사에 근거 절차서 제출 요청"],
            detail={"reason": "scwep_not_submitted"},
        )

    strong_refs: list[dict] = []
    weak_refs: list[dict] = []
    covering: list[str] = []
    accuse_candidates: list[str] = []      # 고발 자격을 갖춘 문서
    blocked_reasons: list[str] = []

    for d in doc_list:
        extracted = d.get("extracted") or {}
        doc_no = _text(d.get("document_no")) or _text(d.get("file_path")) or "(문서번호 없음)"
        scope = scope_state(extracted, discipline, drawing_no=drawing_no)
        flagged = _doc_flagged(extracted)

        # 1) 면책 방향 — scope 'no' 인 문서만 배제한다. unknown 은 인정한다.
        if scope != "no":
            strong, weak = _conditional_hits(extracted, m)
            if flagged:
                # 문서 자체가 재확인 대상이면 강한 히트도 '확인 필요' 로 강등
                weak = weak + strong
                strong = []
            for r in strong:
                # 성적서가 검사 사유를 적었으면 그 사유와 맞는 규칙만 면책으로 인정한다.
                # (성적서가 아무 말도 안 했으면 예전대로 — 있는 근거를 사유 기재 누락으로 없애지 않는다.)
                if report_context and not context_matches(r, report_context):
                    ref = _ref(doc_no, r, "conditional_weak")
                    ref["needs_review"] = True
                    ref["mismatch"] = (f"성적서가 적은 검사 사유 '{_text(report_context)[:60]}' 가 "
                                       f"이 규칙의 조건 '{_text(r.get('trigger'))[:40]}' 와 맞지 않음")
                    weak_refs.append(ref)
                    continue
                strong_refs.append(_ref(doc_no, r, "conditional"))
                covering.append(doc_no)
            for r in weak:
                weak_refs.append(_ref(doc_no, r, "conditional_weak"))
            for r in _weak_mentions(extracted, m):
                weak_refs.append(_ref(doc_no, r, "general"))

        # 2) 고발 방향 — 넷 다 만족해야 이 문서가 '침묵했다' 고 말할 수 있다
        if scope != "yes":
            blocked_reasons.append("scwep_scope_unknown" if scope == "unknown" else "scwep_out_of_scope")
        elif _schema_version(extracted) < MIN_SCHEMA_VERSION:
            blocked_reasons.append("scwep_legacy_schema")
        elif flagged:
            blocked_reasons.append("scwep_doc_needs_review")
        elif not _conf_ok(extracted.get("extraction_confidence"), MIN_DOC_CONFIDENCE):
            blocked_reasons.append("scwep_low_confidence")
        elif not _has_real_extraction(extracted):
            blocked_reasons.append("scwep_extraction_empty")
        else:
            accuse_candidates.append(doc_no)

    # ── 면책이 우선 ──
    if strong_refs:
        uniq = sorted(set(covering))
        return BasisAssessment(
            state=STATE_COVERED,
            refs=strong_refs,
            covering_docs=uniq,
            reasons=[
                f"SCWEP 조건부 요구에 근거 있음 ({', '.join(uniq)}) — "
                f"조건 '{strong_refs[0].get('trigger')}' 발생 사실을 1회 대조 후 종결"
            ],
            detail={"reason": "covered_by_conditional", "docs": uniq,
                    "trigger": strong_refs[0].get("trigger")},
        )

    if weak_refs:
        docs_seen = sorted({r["doc"] for r in weak_refs})
        return BasisAssessment(
            state=STATE_UNCLEAR,
            refs=weak_refs,
            reasons=[
                f"SCWEP 에 {m} 언급은 있으나 적용 조건이 확정되지 않음 "
                f"({', '.join(docs_seen)}) — 해당 조항 원문 확인 필요"
            ],
            detail={"reason": "mentioned_without_condition", "docs": docs_seen},
        )

    # ── 면책 근거가 없다. 고발해도 되는가 ──
    if accuse_candidates:
        return BasisAssessment(
            state=STATE_NO_BASIS,
            covering_docs=[],
            reasons=[
                f"제출된 SCWEP ({', '.join(sorted(set(accuse_candidates)))}) 에 "
                f"{m} 요구가 없음 — 도면·절차서 어디에도 근거 없음"
            ],
            detail={"reason": "no_basis_found", "docs": sorted(set(accuse_candidates))},
        )

    reason = blocked_reasons[0] if blocked_reasons else "scwep_not_submitted"
    return BasisAssessment(
        state=STATE_NOT_SUBMITTED,
        reasons=[_BLOCKED_TEXT.get(reason, "SCWEP 근거를 확인할 수 없음 — 시공사에 제출 요청")],
        detail={"reason": reason, "blocked": sorted(set(blocked_reasons))},
    )


_BLOCKED_TEXT = {
    "scwep_not_submitted": "제출된 SCWEP 없음 — 시공사에 근거 절차서 제출 요청",
    "scwep_scope_unknown": "제출된 SCWEP 의 적용 공종이 확인되지 않음 — 과다청구로 단정하지 않음",
    "scwep_out_of_scope": "제출된 SCWEP 이 이 공종 범위가 아님 — 해당 공종 절차서 제출 요청",
    "scwep_legacy_schema": "SCWEP 이 구 형식으로 적재됨 — 조건부 요구가 추출되지 않았을 수 있어 재적재 필요",
    "scwep_doc_needs_review": "SCWEP 추출 결과가 재확인 대상 — 원문 확인 후 재판정",
    "scwep_low_confidence": "SCWEP 추출 신뢰도 낮음 — 과다청구로 단정하지 않음",
    "scwep_extraction_empty": "SCWEP 에서 규칙이 추출되지 않음 — 재적재 필요",
    "billed_method_empty": "청구 NDT 방법이 비어 있음",
}


# ─────────────────────────── DB 경계 ───────────────────────────

_CACHE: Optional[list[dict]] = None


def load_docs(session) -> list[dict]:
    """제출된 SCWEP 문서를 판정기가 먹는 형식으로. 회차 1건 안에서는 캐시한다.

    검토 1회에 청구 행이 수천 건이고 행마다 같은 SCWEP 를 읽는다. 캐시가 없으면
    같은 질의를 수천 번 반복한다. pipeline.run() 진입 시 reset_cache() 를 부른다.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    from sqlalchemy import select

    from app.database.models import StandardDocument

    out: list[dict] = []
    for d in session.scalars(select(StandardDocument).where(StandardDocument.doc_type == "scwep")):
        out.append({
            "document_no": d.document_no or d.file_path,
            "file_path": d.file_path,
            "extracted": d.extracted_json or {},
        })
    _CACHE = out
    logger.info("SCWEP 근거 문서 %d건 적재", len(out))
    return out


def reset_cache() -> None:
    """회차 시작 시 호출. 적재 직후 재검토에서 옛 목록을 쓰지 않도록."""
    global _CACHE
    _CACHE = None
