"""CRST Civil 성적서 (VMC — 구조물 용접부 육안검사) 파서.

폼: "Conclusion on Visual and Measuring Inspection of Quality of Welded Joints
     of Metal Structures Building" (Form C-21, 검사기관)

실측 기반 (2026-07-06 파일럿, sample 20sheets + Tesseract 재OCR):
- 헤더: "No. 77-056VMC dated 23.04.2024 20UMA"  (성적서번호 + 검사일 + 건물)
- 5. Drawing: NP.D.N000.2.0UMA95&&&&&&.012.DC.0002.E_C02(RC4)
- 6. Number of welding ...: "15 Cages(RC4)"  (부재 수)
- 결과표: "RC-4 15pec ... No defects found A 056 23.04.2024"  (부재유형+수량+판정)
- ⚠ Civil 묶음에 타 검사기관(NIKIMT 등, 번호 VT-XXX) 성적서 혼재 — CRST(77-XXX)만
  시공사→CRST 물량표 대상.
- ⚠ Adobe OCR 본 일부 페이지가 역순(거울) 텍스트 — 복원 필요.
- 수량 단위: 성적서 = 부재 수(pcs). 물량표 VT = 용접점 수.
  → 대사는 부재수 × CEB 부재당 용접점(unit Q'ty) 로 계산 (ceb_crosscheck 참조).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────── 패턴 ───────────────────────────

# CRST 성적서 헤더 — "No. 77-056VMC dated 23.04.2024 20UMA"
# 건물 토큰은 실제 형식(20UMA/10UGB/91ULD 등)만 — 느슨하면 'S'/'B'/'100' 같은 오탐 (실측 11건)
RE_CRST_HEADER = re.compile(
    r"No\.?\s*№?\s*(77-\d+\s?[A-Z]{1,4})\s+dated\s+([\d.]+)"
    r"(?:\s+([0-9IlO]{1,2}\.?[0-9IlO]?\s?U[A-Z]{2}\d{0,2}))?"
)
# 타 기관 (NIKIMT 등) — "No. VT-712 dated 12.09.2023"
RE_OTHER_HEADER = re.compile(r"No\.?\s*([A-Z]{2,4}-\d+)\s+dated\s+([\d.]+)")
RE_DRAWING = re.compile(r"Drawing:?\s*(NP\.[DС]\.[^\s]{8,70})")
RE_KKS = re.compile(r"KKS code:?\s*([0-9A-Z&,\- ]{0,50})")
# 수량 단위 — 영문 변형 + 러시아어 OCR 혼입 (рс/рсз/шт — 77-452VMC 실측: 'MD-10 (1рс.)')
_UNIT = r"(?:pcs|pes|pec|pcs?\b|pc\b|EA|рсз?|pcз|шт)"
# 부재유형+수량 — "RC-4 15pec", "MD-2 (8pcs.)", "MD-10 (1рс.)", "MC-18-60 (6PCS)" (이중 하이픈)
_PROD = r"[A-Z]{1,4}(?:-?\d+){1,2}[a-zA-Z]?"
RE_ITEM_QTY = re.compile(
    r"(" + _PROD + r")\s*\(?\s*(\d{1,4})\s*" + _UNIT, re.I)
RE_QTY_ITEM = re.compile(
    r"(\d{1,4})\s*(?:" + _UNIT + r"|Cages?|plates?)\s*\(?(" + _PROD + r")?", re.I)
RE_REJECT = re.compile(r"\bR-?not\s*accepted|\bnot\s+accepted\b", re.I)

# 역순(거울) 텍스트 신호 — Adobe OCR 회전 문제
_MIRROR_SIGNS = (".oN", "gnidlew", "noitceps", "gniward", "yrotarobal")


@dataclass
class CivilReport:
    """성적서 1건 파싱 결과."""

    report_no: str
    org: str                          # "CRST" | "OTHER"
    date: Optional[str] = None
    facility: Optional[str] = None    # 예: "20UMA"
    drawing: Optional[str] = None
    kks: Optional[str] = None
    items: list[tuple[str, int]] = field(default_factory=list)  # [(부재유형, 수량)]
    rejected: bool = False
    start_page: int = 0               # 1-based
    needs_review: list[str] = field(default_factory=list)

    @property
    def total_pieces(self) -> int:
        return sum(q for _, q in self.items)

    def to_dict(self) -> dict:
        return {
            "report_no": self.report_no, "org": self.org, "date": self.date,
            "facility": self.facility, "drawing": self.drawing, "kks": self.kks,
            "items": self.items, "total_pieces": self.total_pieces,
            "rejected": self.rejected, "start_page": self.start_page,
            "needs_review": self.needs_review,
        }


def fix_mirrored(text: str) -> tuple[str, bool]:
    """역순(거울) 텍스트 감지·라인 단위 복원."""
    score = sum(text.count(s) for s in _MIRROR_SIGNS)
    if score >= 2:
        return "\n".join(line[::-1] for line in text.splitlines()), True
    return text, False


def normalize_report_no(s: str) -> str:
    return str(s or "").upper().replace(" ", "").replace("\n", "").strip()


def normalize_product(s: str) -> str:
    """부재유형 정규화 — 'RC-4' == 'RC4' == 'rc 4'."""
    return re.sub(r"[\s\-_]", "", str(s or "").upper())


def parse_page(text: str, page_index: int = 0) -> Optional[CivilReport]:
    """1 페이지 텍스트 → 성적서 첫장이면 CivilReport, 아니면 None."""
    text, _ = fix_mirrored(text)

    m = RE_CRST_HEADER.search(text)
    if m:
        fac = (m.group(3) or "").replace(" ", "").strip() or None
        if not fac:
            # 2차 fallback — 헤더 인근이 아니어도 본문에서 건물 패턴 (줄바꿈 분리 케이스)
            fm = re.search(r"\b([0-9IlO]{1,2}\.?[0-9IlO]?U[A-Z]{2})\d{0,2}\b", text)
            if fm:
                fac = fm.group(1)
        if fac:
            # OCR 문자 혼동 정규화 — I0USJ→10USJ, 2OUMA→20UMA
            fac = fac.translate(str.maketrans({"I": "1", "l": "1", "O": "0"}))
        rep = CivilReport(
            report_no=normalize_report_no(m.group(1)),
            org="CRST",
            date=m.group(2).strip("."),
            facility=fac,
            start_page=page_index + 1,
        )
    else:
        m2 = RE_OTHER_HEADER.search(text)
        if not m2:
            return None
        rep = CivilReport(
            report_no=normalize_report_no(m2.group(1)),
            org="OTHER", date=m2.group(2).strip("."),
            start_page=page_index + 1,
        )

    dm = RE_DRAWING.search(text)
    if dm:
        rep.drawing = dm.group(1).rstrip(".,;")
    km = RE_KKS.search(text)
    if km:
        kv = km.group(1).strip().rstrip(".,;")
        if kv and "not required" not in kv.lower():
            rep.kks = kv

    # 부재유형+수량 — 두 패턴 (유형 먼저 / 수량 먼저), dedup
    seen: set[tuple[str, int]] = set()
    for pm in RE_ITEM_QTY.finditer(text):
        key = (normalize_product(pm.group(1)), int(pm.group(2)))
        if key not in seen and 0 < key[1] < 5000:
            seen.add(key)
    for pm in RE_QTY_ITEM.finditer(text):
        prod = normalize_product(pm.group(2)) if pm.group(2) else ""
        if prod:
            key = (prod, int(pm.group(1)))
            if key not in seen and 0 < key[1] < 5000:
                seen.add(key)
    rep.items = sorted(seen)

    if RE_REJECT.search(text):
        rep.rejected = True
        rep.needs_review.append("불합격(R) 포함 — 재검·후속 성적서 확인")
    if rep.org == "CRST" and not rep.items:
        rep.needs_review.append("부재 수량 미추출 — OCR 품질 또는 폼 변형 (원문 확인)")
    if rep.org == "CRST" and not rep.drawing:
        rep.needs_review.append("도면번호 미추출")
    return rep


def extract_reports(pages: list[str]) -> list[CivilReport]:
    """페이지 텍스트 목록 → 성적서 목록 (첫장 기준 세그멘테이션).

    성적서 첫장(헤더 매치)만 신규로 인식. 후속 페이지(첨부·연속)는
    직전 성적서의 보조 정보로 스캔 (drawing·items 보강).
    """
    reports: list[CivilReport] = []
    seen_no: dict[str, int] = {}   # report_no → reports 인덱스 (다중페이지 헤더 반복 dedup)
    for i, t in enumerate(pages):
        r = parse_page(t, i)
        if r is not None:
            if r.report_no in seen_no:
                # 같은 성적서의 연속 페이지 (헤더 반복) — 신규 생성 대신 기존에 병합
                cur = reports[seen_no[r.report_no]]
                if not cur.drawing and r.drawing:
                    cur.drawing = r.drawing
                if not cur.facility and r.facility:
                    cur.facility = r.facility
                for it in r.items:
                    if it not in cur.items:
                        cur.items.append(it)
                continue
            seen_no[r.report_no] = len(reports)
            reports.append(r)
        elif reports:
            # 연속 페이지 — 직전 성적서 필드 보강
            cur = reports[-1]
            txt, _ = fix_mirrored(t)
            if not cur.drawing:
                dm = RE_DRAWING.search(txt)
                if dm:
                    cur.drawing = dm.group(1).rstrip(".,;")
            if not cur.items:
                for pm in RE_ITEM_QTY.finditer(txt):
                    q = int(pm.group(2))
                    if 0 < q < 5000:
                        cur.items.append((normalize_product(pm.group(1)), q))
                if cur.items and "부재 수량 미추출" in " ".join(cur.needs_review):
                    cur.needs_review = [x for x in cur.needs_review if "부재 수량" not in x]
    return reports
