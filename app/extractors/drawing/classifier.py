"""도면 파일을 DC/SD/BG 로 분류 + 도면번호·rev 추출.

전략: 파일명 정규식이 명확히 식별하면 그대로 채택 (LLM 호출 없음).
모호하면 LLM(drawing_classify) 호출.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.extractors.pdf_extractor import extract
from app.hcx_client import call

# 원전 도면 파일명 패턴
#   예 (scan, rev 있음): NP.D.P000.1.0UGB95GML90&.052.DC.0001.E_C03_scan.pdf
#   예 (native, rev 없음): NP.D.P000.9.0UTF&&GQA49&.052.DC.0002.E.pdf
#
# 구조: NP.D.P000.<unit>.<KKS&masking>.052.<type>.<seq>.<lang>[_<rev>][_scan].pdf
#   drawing_no = 도면번호 (type 직전까지). KKS 코드 일부가 '&' 로 마스킹됨.
#   type       = DC | SD | BG
#   rev        = 'C01' 등 (옵션)
#   scan       = '_scan' 접미사 있으면 스캔본 (텍스트 레이어 없을 가능성 큼)
_FILENAME_PATTERN = re.compile(
    r"""
    ^
    (?:\[[^\]]*\]\s*)?                     # 대괄호 태그 접두어 ('[ABD]') — 사내 파일명 (2026-09-07 ② 로그)
    (?:\d+\.\s*)?                          # 다운로드 순번 접두어 ('95.', '106.', '4. ') — 번호가 아니다 (2026-09 실 샘플)
    (?P<drawing_no>NP\..+?)                # 'NP.' 로 시작, type 직전까지 (원전 도면 명명규칙)
    \.
    (?P<type>DC|SD|BG|DK|KE)               # 도면 = DC/SD/BG/DK (DK: 시공 관련, 2026-09-07 사용자), KE = SCWEP(도면 아님)
    \.
    (?P<seq>\d{1,5})
    \.
    (?P<lang>[A-Z])
    (?:[_=](?P<rev>C\d+))?                 # rev: '_C03' 또는 '=C01' (2026-09 실 SCWEP 파일명)
    (?:_(?P<scan>scan))?                   # scan 접미사 옵션
    (?:[ ,].*?)?                           # 자유 꼬리 (', 10UMA HC WEP CW Pipe 설치') — 무시
    \.pdf$
    """,
    re.VERBOSE,
)

# 표제란(1~2쪽 텍스트)에서 찾는 도면번호. KKS 12자 마디는 '&' 마스킹 포함. 대소문자 무시, 공백 제거 후 비교.
_TITLE_BLOCK_NO = re.compile(r"NP\.D\.[A-Z]\d{3}\.\d\.[0-9A-Z&]{12}\.\d{3}\.(?:DC|SD|BG|KE)\.\d{4}", re.IGNORECASE)


def _title_block_no(file_path: Path) -> Optional[str]:
    """파일 안 표제란의 도면번호 (없거나 못 읽으면 None). 실패가 분류를 막지 않는다."""
    try:
        extracted = extract(file_path)
        text = "".join((p.text or "") for p in extracted.pages[: min(2, len(extracted.pages))])
    except Exception:       # noqa: BLE001 - 대조는 보조 정보다
        return None
    m = _TITLE_BLOCK_NO.search(re.sub(r"\s+", "", text))
    return m.group(0).upper() if m else None
# KE 명명규칙은 WEP·SCWEP 둘 다 공유 — 둘 다 시공 절차 관련 서류로 도면 아님.
# 청구 엑셀의 Detailed Drawing 컬럼에 들어가면 시공사의 오기.
# 사용자 정책 (2026-05-21 확인): 모체 도면이 별도로 존재하므로 시공사에 수정 요청 필요.
_NON_DRAWING_TYPES = {"KE"}
# Fallback pattern — 다른 명명규칙(예: 향후 다른 원전 프로젝트)에 대비
_FILENAME_PATTERN_FALLBACK = re.compile(
    r"""
    ^
    (?P<drawing_no>[A-Z0-9][A-Z0-9\-\.&]*?)
    [-_\.]
    (?P<type>DC|SD|BG|DK)
    (?:[-_\.](?:rev|REV|Rev|r|C)\.?(?P<rev>[A-Za-z0-9]+))?
    \.pdf$
    """,
    re.VERBOSE,
)


@dataclass
class Classification:
    drawing_type: Optional[str]      # "DC" | "SD" | "BG" | None (재확인 필요)
    drawing_no: Optional[str]
    revision: Optional[str]
    confidence: float
    source: str                     # "filename" | "llm" | "unknown"
    reasoning: Optional[str] = None
    needs_review: bool = False
    review_reasons: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.review_reasons is None:
            self.review_reasons = []


def classify(file_path: Path) -> Classification:
    """파일 1개 분류. **추론으로 채우지 않음** — 모호하면 needs_review 표시."""
    file_path = Path(file_path)

    fn_match = _FILENAME_PATTERN.match(file_path.name) or _FILENAME_PATTERN_FALLBACK.match(file_path.name)
    if fn_match:
        dtype = fn_match.group("type")
        needs_review = dtype in _NON_DRAWING_TYPES
        review_reasons = (
            [
                f"'{dtype}' 식별자 = WEP 또는 SCWEP (시공 절차 관련 서류, 도면 아님). "
                f"청구의 Detailed Drawing 컬럼에 들어가면 시공사의 오기 — 모체 도면번호로 수정 요청 필요"
            ]
            if needs_review else []
        )
        # 표제란 교차검증 — 파일명과 파일 안 번호가 다르면 두 값을 남기고 재확인 (사용자 2026-09-06).
        # 파일명 값은 바꾸지 않는다 (추론으로 채우지 않음). 표제란이 없으면 대조 생략.
        fn_full = f"{fn_match.group('drawing_no')}.{dtype}.{fn_match.group('seq')}".upper()
        tb = _title_block_no(file_path)
        if tb and tb != fn_full:
            needs_review = True
            review_reasons = review_reasons + [
                f"파일명 도면번호 {fn_full} ≠ 표제란 도면번호 {tb} — 어느 쪽이 맞는지 확인 필요"
            ]
        return Classification(
            drawing_type=dtype,
            drawing_no=fn_match.group("drawing_no"),
            revision=fn_match.group("rev"),
            confidence=0.99,
            source="filename",
            reasoning=f"Filename pattern matched: {file_path.name}",
            needs_review=needs_review,
            review_reasons=review_reasons,
        )

    # LLM fallback — title block 텍스트로 분류
    extracted = extract(file_path)
    title_block_text = "\n".join(
        p.text for p in extracted.pages[: min(2, len(extracted.pages))]
    )[:2000]
    resp = call(
        "drawing_classify",
        {
            "file_name": file_path.name,
            "text_excerpt": title_block_text,
            "page_count": len(extracted.pages),
        },
    )

    if resp.parsed is None:
        return Classification(
            drawing_type=None,
            drawing_no=None,
            revision=None,
            confidence=0.0,
            source="unknown",
            reasoning="LLM 분류 응답 파싱 실패",
            needs_review=True,
            review_reasons=["LLM 응답이 JSON 으로 파싱되지 않음 — 파일명·내용 재확인 필요"],
        )

    p = resp.parsed
    confidence = float(p.get("confidence", 0.0))
    review_reasons = list(p.get("review_reasons") or [])
    needs_review = bool(p.get("needs_review")) or confidence < 0.8

    if needs_review and not review_reasons:
        review_reasons.append(f"분류 신뢰도 낮음 ({confidence:.2f}) — 도면 종류·번호 재확인 필요")

    if p.get("drawing_type") not in ("DC", "SD", "BG", "DK", None):
        needs_review = True
        review_reasons.append(f"알 수 없는 도면 종류: {p.get('drawing_type')!r}")

    return Classification(
        drawing_type=p.get("drawing_type"),
        drawing_no=p.get("drawing_no"),
        revision=p.get("revision"),
        confidence=confidence,
        source="llm",
        reasoning=p.get("reasoning"),
        needs_review=needs_review,
        review_reasons=review_reasons,
    )
