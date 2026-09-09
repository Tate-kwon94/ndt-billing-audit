"""위험도 점수 산정 (0~100). matching_rules.yaml 의 weights 기반."""
from __future__ import annotations

import logging

from app.config import matching_rules

logger = logging.getLogger(__name__)

# yaml 에 없을 때의 코드 레벨 fallback. matching_rules.yaml 은 운영자가 편집 가능한 파일이라
# 갱신본이 안 깔리면 새 rule 이 조용히 0점(=대시보드 맨 아래, 기본 필터 밖)이 된다.
# 근거 게이트 rule 이 화면에서 사라지는 것은 "아무도 고발하지 않게 되는" 실패라 눈에 안 띈다.
_FALLBACK_WEIGHTS = {
    "billed_ndt_basis_not_submitted": 45,
    "billed_ndt_basis_unclear": 50,
    "billed_ndt_covered_by_scwep": 35,
    "pipeline_row_failed": 30,
    "low_confidence_code_ref": 20,
}
_warned: set[str] = set()


def compute(findings: list[dict]) -> int:
    """compliance.findings 리스트를 받아 0~100 위험도 반환."""
    weights = matching_rules().get("risk_score", {}).get("weights", {})
    total = 0
    for f in findings:
        rule = f["rule"]
        if rule in weights:
            total += int(weights[rule])
        elif rule in _FALLBACK_WEIGHTS:
            total += _FALLBACK_WEIGHTS[rule]
            if rule not in _warned:
                _warned.add(rule)
                logger.warning("risk_score.weights 에 %s 가 없어 코드 기본값 %d 사용 — config/matching_rules.yaml 갱신 필요",
                               rule, _FALLBACK_WEIGHTS[rule])
    return min(100, max(0, total))
