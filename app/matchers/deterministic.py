"""1단계 매칭: 결정 키 정확 일치."""
from __future__ import annotations

from typing import Iterable, Optional

from app.config import matching_rules


def _normalize(s) -> str:
    """공백·하이픈·언더스코어 제거 후 대문자. CRST 'No. 77-005PT' ↔ 엑셀 '77-005PT' 매칭용."""
    if s is None:
        return ""
    # 'No. ' 접두 제거 (PDF 헤더에서 자주 등장)
    text = str(s).strip()
    if text.lower().startswith("no."):
        text = text[3:].strip()
    return text.upper().replace(" ", "").replace("-", "").replace("_", "")


_JOINT_FIELDS = ("joint_no", "welder_id")


def _cand_values(cand: dict, field: str) -> list:
    """후보 성적서에서 필드 값 후보들. 최상위에 있으면 그것, joint 필드면 joints[*] 에서.

    파이프라인의 _to_report_dict 는 report_no/ndt_method/inspection_date/drawing_no 만
    최상위로 올리고 joint_no·welder_id 는 joints 안에 남긴다 (2026-09-04 감사 ②).
    """
    if cand.get(field) not in (None, ""):
        return [cand.get(field)]
    if field in _JOINT_FIELDS:
        return [j.get(field) for j in (cand.get("joints") or []) if j.get(field) not in (None, "")]
    return []


def _rows_for(cand: dict, fields: list[str]) -> list[dict]:
    """규칙의 필드를 어디서 읽을지. joint 필드가 있으면 joints 의 각 행이 하나의 후보 행이다 —
    joint_no 는 A행, welder_id 는 B행에서 맞추는 식의 교차 일치는 인정하지 않는다."""
    joint_fields = [f for f in fields if f in _JOINT_FIELDS]
    top_has_all = all(cand.get(f) not in (None, "") for f in joint_fields)
    if not joint_fields or top_has_all:
        return [cand]
    rows = []
    for j in cand.get("joints") or []:
        merged = {**cand, **{f: j.get(f) for f in _JOINT_FIELDS if j.get(f) not in (None, "")}}
        rows.append(merged)
    return rows


def find_match(billing_row: dict, candidate_reports: Iterable[dict]) -> Optional[dict]:
    """deterministic_keys 를 순서대로 시도. 한 규칙에 **정확히 하나**의 후보만 남을 때 확정한다.

    둘 이상 남으면(같은 Joint·방법의 성적서가 여럿) 더 엄격한 다음 규칙으로 넘기고, 끝까지 하나로
    좁혀지지 않으면 None — fuzzy/LLM/재확인 단계가 맡는다. 첫 후보를 집는 것은 추측이다 (2026-09-05 리뷰:
    용접사만 다른 성적서 둘 중 앞의 것을 100점으로 확정했다). 청구측 키가 비어 있는 규칙은 건너뛴다.
    """
    rules = matching_rules().get("deterministic_keys", [])
    candidates = list(candidate_reports)
    for rule in rules:
        fields = list(rule["fields"])
        wanted = {f: _normalize(billing_row.get(f)) for f in fields}
        if any(v == "" for v in wanted.values()):
            continue
        hits: list[dict] = []
        for cand in candidates:
            for row in _rows_for(cand, fields):
                if all(_normalize(row.get(f)) == v for f, v in wanted.items()):
                    hit = dict(cand)
                    if "joint_no" in fields:
                        hit["joint_no"] = row.get("joint_no")
                    hits.append(hit)
                    break                       # 같은 성적서는 한 번만 센다
        if len(hits) == 1:
            out = hits[0]
            out["_match_rule"] = rule["name"]
            out["_match_score"] = 100.0
            return out
        # 0개면 다음 규칙, 2개 이상이면(모호) 다음 규칙이 더 엄격하므로 역시 다음 규칙
    return None
