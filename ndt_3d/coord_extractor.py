"""DC PDF → 구조화 JSON 추출기.

1ULD native DC 샘플 분석 결과 반영:
- 페이지마다 "Isometric installation diagram" 시트인지 확인
- "Specification of spools and parts" 표에서 pipe segment 길이
- bend 표준 (GOST 17375-2001 Bend 90-XXxX) 패턴
- tie-in 좌표 (X- 71350 / Y+ 144691 / Z-1892) 패턴
- KKS code (90GMM91BR001 등) 추출
- "For continuation see" 참조 (전 시스템 chain 가능성)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import pdfplumber

logger = logging.getLogger(__name__)


# ─────────────────────────── 정규식 패턴 ───────────────────────────


# tie-in 절대 좌표 — X- 71350 / Y+ 127452 / Z-2340 (mm)
# pdfplumber 출력에서 "X- 71350" / "Y+ 144691" / "Z-1892" 형태로 나옴 (- 와 + 가 부호)
RE_TIE_X = re.compile(r"\bX\s*([+\-])\s*(\d{3,6})\b")
RE_TIE_Y = re.compile(r"\bY\s*([+\-])\s*(\d{3,6})\b")
RE_TIE_Z = re.compile(r"\bZ\s*([+\-])\s*(\d{2,6})\b")

# spool 패턴 — "Pipe [재질토큰들 0~2개] ODxWALL ... LEN mm"
# wall thickness 는 소수 가능 (2.7 등 PP-H 파이프)
# 예: "1 GOST 8732-78 Pipe 108x6 ... 1114 mm"
#     "1 GOST 32414-2013 Pipe PP-H 110x2.7 L=500 mm"
RE_SPOOL_WITH_POS = re.compile(
    r"^[\s]*(\d{1,3})\s+"                                          # Pos
    r"GOST\s*\d{2,5}[-]?[A-Z]?\s*\d{2,5}\s+"                       # GOST std
    r"(?:Pipe|Tube)(?:\s+[A-Z][A-Z0-9-]*)*"                        # Pipe + 재질코드 토큰
    r"\s+(\d{2,4})\s*[x×]\s*(\d{1,3}(?:\.\d+)?)"                  # OD x wall (소수 가능)
    r"[^\n]*?"
    r"(?:L\s*=\s*)?(\d{2,5})\s*mm",                                # length (L= 옵션)
    re.IGNORECASE | re.MULTILINE,
)
RE_SPOOL = re.compile(
    r"(?:Pipe|Tube)(?:\s+[A-Z][A-Z0-9-]*)*"
    r"\s+(\d{2,4})\s*[x×]\s*(\d{1,3}(?:\.\d+)?)"
    r"[^\n]*?"
    r"(?:L\s*=\s*)?(\d{2,5})\s*mm",
    re.IGNORECASE,
)

# bend 표준 — "Pos GOST-std Bend 90-108x6 ... [material] 3" 형태 (마지막 숫자 = Quantity)
# 예: "4 GOST 17375-2001 Bend 90-108x6 20 GOST 1050-2013 3 3.60 10.80"
RE_BEND_WITH_POS = re.compile(
    r"^[\s]*(\d{1,3})\s+"                                     # Pos
    r"GOST\s*\d{2,5}[-]?[A-Z]?\s*\d{2,5}\s+"                            # GOST std
    r"Bend\s+(\d{2,3})\s*-\s*(\d{2,4})\s*[x×]\s*(\d{1,3})"   # angle-OD x wall
    r"(?:\s+\S+){1,6}?"
    r"\s+(\d{1,2})\s+\d",                                     # Quantity
    re.IGNORECASE | re.MULTILINE,
)
RE_BEND = re.compile(
    r"Bend\s+(\d{2,3})\s*-\s*(\d{2,4})\s*[x×]\s*(\d{1,3})"
    r"(?:\s+\S+){1,6}?"
    r"\s+(\d{1,2})\s+\d",
    re.IGNORECASE,
)
RE_BEND_FALLBACK = re.compile(
    r"Bend\s+(\d{2,3})\s*-\s*(\d{2,4})\s*[x×]\s*(\d{1,3})",
    re.IGNORECASE,
)

# Component 의 Pos 번호 추출 — "Pos KKS Name -" 형태
# 예: "6 90GMM91AA001 Valve - 1 - -"
#     "5 90GMM91BQ2002 Penetration - 1 - -"
RE_COMPONENT_WITH_POS = re.compile(
    r"^[\s]*(\d{1,3})\s+"
    r"(\d{2}[A-Z]{2,3}\d{1,2}[A-Z]{2}\d{3,4})\s+"     # KKS
    r"(Valve|Support|Penetration|Cap|Flange|Nozzle|Pump|Bend|Pipe|Tube)",
    re.IGNORECASE | re.MULTILINE,
)

# Fitting 패턴들 — 모두 Pos + GOST 표준 + 종류 + 치수 + Quantity
# Tee 단순: "Tee 108x6 ... 2 3.30 6.60"
RE_TEE = re.compile(
    r"^[\s]*(\d{1,3})\s+"
    r"GOST\s*\d{2,5}[-]?[A-Z]?\s*\d{2,5}\s+"
    r"Tee\s+(\d{2,4})\s*[x×]\s*(\d{1,3}(?:\.\d+)?)"
    r"(?:\s+\S+){1,6}?"
    r"\s+(\d{1,2})\s+\d",
    re.IGNORECASE | re.MULTILINE,
)
# Reducing tee — 두 직경: "Reducing tee 57x5-32x4 ... 1"
RE_REDUCING_TEE = re.compile(
    r"^[\s]*(\d{1,3})\s+"
    r"GOST\s*\d{2,5}[-]?[A-Z]?\s*\d{2,5}\s+"
    r"Reducing\s+tee\s+(\d{2,4})\s*[x×]\s*(\d{1,3}(?:\.\d+)?)"
    r"\s*-\s*(\d{2,4})\s*[x×]\s*(\d{1,3}(?:\.\d+)?)"
    r"(?:\s+\S+){1,6}?"
    r"\s+(\d{1,2})\s+\d",
    re.IGNORECASE | re.MULTILINE,
)
# Reducer 단순 — "Reducer 100x50"
RE_REDUCER = re.compile(
    r"^[\s]*(\d{1,3})\s+"
    r"GOST\s*\d{2,5}[-]?[A-Z]?\s*\d{2,5}\s+"
    r"Reducer\s+(\d{2,4})\s*[x×]\s*(\d{2,4})"
    r"(?:\s+\S+){1,6}?"
    r"\s+(\d{1,2})\s+\d",
    re.IGNORECASE | re.MULTILINE,
)
# Branch — "Branch 57x5.5-100-PN16"
RE_BRANCH = re.compile(
    r"^[\s]*(\d{1,3})\s+"
    r"(?:GOST|TU)\s*[\d\-/A-Z\s]{3,30}\s+"
    r"Branch\s+(\d{2,4})\s*[x×]\s*(\d{1,3}(?:\.\d+)?)"
    r"(?:[\-\s]+(\d{2,4}))?",
    re.IGNORECASE | re.MULTILINE,
)
# Flange — "Flange 100-16-01-1-B-20-IV" → DN 100, PN 16
RE_FLANGE = re.compile(
    r"^[\s]*(\d{1,3})\s+"
    r"GOST\s*\d{2,5}[-]?[A-Z]?\s*\d{2,5}\s+"
    r"Flange\s+(\d{2,4})\s*-\s*(\d{1,3})",  # DN-PN
    re.IGNORECASE | re.MULTILINE,
)
# Cap — "Cap CW-C-14x2"
RE_CAP = re.compile(
    r"^[\s]*(\d{1,3})\s+"
    r"TU\s+[\d\-]+\s+"
    r"Cap\s+\S+",
    re.IGNORECASE | re.MULTILINE,
)

# Slope — "1mm/m", "12mm/m" 등 (도면 안 기울기 표시)
RE_SLOPE = re.compile(r"(\d{1,3})\s*mm\s*/\s*m", re.IGNORECASE)

# Fallback (Pos 번호 없이) — 키워드만 보고 fitting 카운트
# multi-line 으로 펼쳐진 spec 표에서 Pos 가 분리된 케이스 처리
RE_TEE_SIMPLE = re.compile(
    r"\bTee\s+(\d{2,4})\s*[x×]\s*(\d{1,3}(?:\.\d+)?)"
    r"(?:\s+\S+){1,8}?"
    r"\s+(\d{1,2})\s+\d",  # quantity
    re.IGNORECASE,
)
RE_REDUCING_TEE_SIMPLE = re.compile(
    r"\bReducing\s+tee\s+(\d{2,4})\s*[x×]\s*(\d{1,3}(?:\.\d+)?)"
    r"\s*-\s*(\d{2,4})\s*[x×]\s*(\d{1,3}(?:\.\d+)?)"
    r"(?:\s+\S+){1,8}?"
    r"\s+(\d{1,2})\s+\d",
    re.IGNORECASE,
)
RE_BRANCH_SIMPLE = re.compile(
    r"\bBranch\s+(\d{2,4})\s*[x×]\s*(\d{1,3}(?:\.\d+)?)"
    r"(?:[\-\s]+(\d{2,4}))?",
    re.IGNORECASE,
)
RE_FLANGE_SIMPLE = re.compile(
    r"\bFlange\s+(\d{2,4})\s*-\s*(\d{1,3})",  # DN-PN
    re.IGNORECASE,
)
RE_CAP_SIMPLE = re.compile(r"\bCap\s+[A-Z]+(?:[-]\S+)?", re.IGNORECASE)

# valve, fitting, support, penetration, pump — pos type 식별
# KKS 명명규칙: AA=Valve, BQ=Support 류 (BQ2xxx=Penetration, BQ4xxx=Support), CP=Control Point/Nozzle, AP=Pump
RE_VALVE = re.compile(r"\b(\w+AA\d{3,4})\b\s+Valve", re.IGNORECASE)
RE_SUPPORT = re.compile(r"\b(\w+BQ4\d{2,3})\b\s+Support", re.IGNORECASE)
RE_PENETRATION = re.compile(r"\b(\w+BQ2\d{2,3})\b\s+Penetration", re.IGNORECASE)
RE_NOZZLE = re.compile(r"\b(\w+CP\d{3,4})\b", re.IGNORECASE)
RE_PUMP = re.compile(r"\b(\w+AP\d{3,4})\b", re.IGNORECASE)

# KKS code — 90GMM91BR001 형태 (system_unit + element)
RE_KKS = re.compile(r"\b(\d{2}[A-Z]{2,3}\d{1,2}[A-Z]{2}\d{3,4})\b")

# 페이지 분류 hints
RE_ISO_DIAGRAM = re.compile(r"Isometric installation diagram", re.IGNORECASE)
RE_PLAN_ELEVATION = re.compile(r"Plan at elevation\s*([+\-]?\d+[.,]?\d*)", re.IGNORECASE)
RE_NORTH_BEARING = re.compile(r"N\s+(\d{1,2}[.,]\d+)°", re.IGNORECASE)
# Continuation 패턴 — KKS branch (90XXX91BR001-MLV0001) 또는 ED 도면번호만 인정 (단순 숫자 거부)
RE_CONTINUATION = re.compile(
    r"For continuation see\s+([A-Z0-9.&\-_]{5,})",
    re.IGNORECASE,
)
# 'Connected to' 변형도 별도 추출
RE_CONNECTED_TO = re.compile(
    r"Connected to\s+([A-Z0-9.&\-_]{5,})",
    re.IGNORECASE,
)

# 도면번호 — & 등 특수문자 포함 가능, 마지막 언어코드 .E 는 optional (참조 시 생략됨)
# 예: NP.D.P000.9.1ULD&&GMM91&.052.DC.0001.E (자기 자신, 풀)
#     NP.D.P000.1.0UKZ&&GMM&&&.021.DC.0001    (참조, 언어 생략)
RE_DRAWING_NO = re.compile(
    r"(NP\.[A-Z]\.[A-Z0-9]{4}\.\d\.[A-Z0-9&]{12}\.\d{3}\.[A-Z]{2}\.\d{4}(?:\.[A-Z])?)"
)


# ─────────────────────────── 데이터 구조 ───────────────────────────


@dataclass
class TieIn:
    """도면 경계 또는 분기점의 절대 좌표 (mm)."""

    x: Optional[int] = None
    y: Optional[int] = None
    z: Optional[int] = None
    label: Optional[str] = None  # "For continuation see XXX" 의 XXX
    page: int = 0


@dataclass
class Spool:
    """직선 파이프 세그먼트 1개."""

    od_mm: float  # outside diameter (e.g. 108, 57, 32)
    wall_mm: float  # wall thickness (e.g. 6, 4)
    length_mm: float  # segment length
    material: Optional[str] = None
    pos_no: Optional[int] = None  # spec 표 Position 번호 — routing 순서
    page: int = 0


@dataclass
class Bend:
    """곡관 1개 (GOST 17375-2001 표준)."""

    angle_deg: int  # 90, 45, 30
    od_mm: float
    wall_mm: float
    radius_mm: Optional[float] = None  # 표준에서 계산 (LR = 1.5*DN)
    pos_no: Optional[int] = None  # spec 표 Position 번호
    page: int = 0


@dataclass
class Fitting:
    """Tee / Reducer / Branch / Flange / Cap / Reducing tee.

    spool/bend 와 달리 길이는 적거나 표준 카탈로그 dimension 사용.
    Tee 는 1개의 분기, Reducer 는 직경 변화, Branch 는 측면 분기.
    """

    type: str            # "tee" / "reducer" / "reducing_tee" / "branch" / "flange" / "cap" / "cross"
    od_main_mm: float    # 메인 OD
    wall_main_mm: float
    od_branch_mm: Optional[float] = None  # Reducing tee/branch 의 분기 OD
    wall_branch_mm: Optional[float] = None
    quantity: int = 1
    pos_no: Optional[int] = None
    page: int = 0


@dataclass
class Component:
    """valve / support / nozzle / equipment 1개."""

    type: str  # "valve" / "support" / "nozzle" / "pump" 등
    kks: str  # KKS code
    pos_no: Optional[int] = None  # spec 표 Position 번호 — 도면 routing 상 위치
    page: int = 0


@dataclass
class IsometricSheet:
    """1개 isometric 시트 = 1개 파이프 어셈블리 = 1개 3D 모델."""

    page: int
    branch_kks: Optional[str] = None  # 이 시트가 표현하는 BR (e.g. 90GMM91BR001)
    spools: list[Spool] = field(default_factory=list)
    bends: list[Bend] = field(default_factory=list)
    fittings: list[Fitting] = field(default_factory=list)  # NEW: Tee/Reducer/Branch/Flange/Cap
    components: list[Component] = field(default_factory=list)
    tie_ins: list[TieIn] = field(default_factory=list)
    continuations: list[str] = field(default_factory=list)
    slopes_mm_per_m: list[float] = field(default_factory=list)  # NEW: 기울기 표시 (1mm/m, 12mm/m)
    needs_review: list[str] = field(default_factory=list)  # 추출 모호 사유


@dataclass
class DrawingExtract:
    """DC PDF 1개 = 1개 도면 세트의 모든 시트."""

    drawing_no: Optional[str]
    elevation_plan: Optional[float] = None  # "Plan at elevation -5.200" 의 -5.200
    north_bearing_deg: Optional[float] = None  # "N 18.9°"
    sheets: list[IsometricSheet] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "drawing_no": self.drawing_no,
            "elevation_plan": self.elevation_plan,
            "north_bearing_deg": self.north_bearing_deg,
            "sheets": [
                {
                    "page": s.page,
                    "branch_kks": s.branch_kks,
                    "spools": [asdict(sp) for sp in s.spools],
                    "bends": [asdict(b) for b in s.bends],
                    "fittings": [asdict(f) for f in s.fittings],
                    "components": [asdict(c) for c in s.components],
                    "tie_ins": [asdict(t) for t in s.tie_ins],
                    "continuations": s.continuations,
                    "slopes_mm_per_m": s.slopes_mm_per_m,
                    "needs_review": s.needs_review,
                }
                for s in self.sheets
            ],
            "needs_review": self.needs_review,
        }


# ─────────────────────────── 추출 함수 ───────────────────────────


def _extract_tie_ins(text: str, page_idx: int) -> list[TieIn]:
    """페이지 텍스트에서 X/Y/Z 좌표 그룹 추출.

    한 페이지에 여러 tie-in 이 있을 수 있음. X/Y/Z 가 같은 인근에서 묶여서 나오면 1개 tie-in.
    단순화: X 매치 위치 ± 200자 안에서 Y/Z 찾기. (X,Y,Z) 동일한 좌표는 dedup.
    """
    tie_ins: list[TieIn] = []
    seen_coords: set[tuple] = set()
    # 모든 X 좌표 매치 시작점 기록
    x_matches = list(RE_TIE_X.finditer(text))
    for m in x_matches:
        x_sign = -1 if m.group(1) == "-" else 1
        x_val = int(m.group(2)) * x_sign
        # 인근 ±200자 안의 Y/Z
        win_start = max(0, m.start() - 100)
        win_end = min(len(text), m.end() + 200)
        win = text[win_start:win_end]
        y_m = RE_TIE_Y.search(win)
        z_m = RE_TIE_Z.search(win)
        y_val = int(y_m.group(2)) * (-1 if y_m.group(1) == "-" else 1) if y_m else None
        z_val = int(z_m.group(2)) * (-1 if z_m.group(1) == "-" else 1) if z_m else None
        key = (x_val, y_val, z_val)
        if key in seen_coords:
            continue
        seen_coords.add(key)
        tie = TieIn(x=x_val, y=y_val, z=z_val, page=page_idx + 1)
        # continuation 라벨 결합 (의미 있는 KKS·도면번호 패턴만 — 단순 숫자 제외)
        cont_m = RE_CONTINUATION.search(win)
        if cont_m:
            label = cont_m.group(1)
            if len(label) >= 3 and not label.isdigit():
                tie.label = label
        tie_ins.append(tie)
    # X 없이 Z 만 있는 케이스 — Z 만 따로
    if not tie_ins:
        seen_z: set[int] = set()
        for m in RE_TIE_Z.finditer(text):
            z_val = int(m.group(2)) * (-1 if m.group(1) == "-" else 1)
            if z_val in seen_z:
                continue
            seen_z.add(z_val)
            tie_ins.append(TieIn(z=z_val, page=page_idx + 1))
    return tie_ins


def _extract_spools(text: str, page_idx: int) -> list[Spool]:
    """Specification of spools and parts 표에서 파이프 세그먼트 추출.

    Pos 번호 우선 매칭, 실패 시 fallback. Pos 순서가 도면 routing 순서.
    """
    spools: list[Spool] = []
    seen_at: dict[tuple, int] = {}  # key → list index (Pos 우선)
    matched_spans: list[tuple[int, int]] = []

    # 1차: Pos 포함 매칭 (정확)
    for m in RE_SPOOL_WITH_POS.finditer(text):
        pos = int(m.group(1))
        od = float(m.group(2))
        wall = float(m.group(3))
        length = float(m.group(4))
        if length < 50:
            continue
        key = (od, wall, length)
        if key in seen_at:
            continue
        seen_at[key] = len(spools)
        spools.append(Spool(
            od_mm=od, wall_mm=wall, length_mm=length,
            pos_no=pos, page=page_idx + 1,
        ))
        matched_spans.append((m.start(), m.end()))

    # 2차: Pos 없이 — 이미 매칭된 영역 외 추가
    for m in RE_SPOOL.finditer(text):
        if any(s <= m.start() < e for s, e in matched_spans):
            continue
        od = float(m.group(1))
        wall = float(m.group(2))
        length = float(m.group(3))
        if length < 50:
            continue
        key = (od, wall, length)
        if key in seen_at:
            continue
        seen_at[key] = len(spools)
        spools.append(Spool(
            od_mm=od, wall_mm=wall, length_mm=length,
            pos_no=None, page=page_idx + 1,
        ))

    # Pos 번호 있는 것 먼저, 그 안에서 Pos 오름차순
    spools.sort(key=lambda s: (s.pos_no is None, s.pos_no or 999))
    return spools


def _extract_bends(text: str, page_idx: int) -> list[Bend]:
    """GOST 17375 Bend + Quantity. Pos 번호 우선 매칭."""
    bends: list[Bend] = []
    matched_spans: list[tuple[int, int]] = []

    # 1차: Pos 포함
    for m in RE_BEND_WITH_POS.finditer(text):
        pos = int(m.group(1))
        angle = int(m.group(2))
        od = float(m.group(3))
        wall = float(m.group(4))
        qty = int(m.group(5))
        if qty > 20:
            qty = 1
        dn = od - wall * 2
        radius = dn * 1.5
        for k in range(qty):
            # 첫 bend 만 Pos 번호 부여, 나머지는 Pos.subscript 으로
            bends.append(Bend(
                angle_deg=angle, od_mm=od, wall_mm=wall, radius_mm=radius,
                pos_no=pos if k == 0 else None, page=page_idx + 1,
            ))
        matched_spans.append((m.start(), m.end()))

    # 2차: Pos 없는 RE_BEND (Quantity 만)
    for m in RE_BEND.finditer(text):
        if any(s <= m.start() < e for s, e in matched_spans):
            continue
        angle = int(m.group(1))
        od = float(m.group(2))
        wall = float(m.group(3))
        qty = int(m.group(4))
        if qty > 20:
            qty = 1
        dn = od - wall * 2
        radius = dn * 1.5
        for _ in range(qty):
            bends.append(Bend(
                angle_deg=angle, od_mm=od, wall_mm=wall, radius_mm=radius,
                pos_no=None, page=page_idx + 1,
            ))
        matched_spans.append((m.start(), m.end()))

    # 3차: fallback
    for m in RE_BEND_FALLBACK.finditer(text):
        if any(s <= m.start() < e for s, e in matched_spans):
            continue
        angle = int(m.group(1))
        od = float(m.group(2))
        wall = float(m.group(3))
        dn = od - wall * 2
        radius = dn * 1.5
        bends.append(Bend(
            angle_deg=angle, od_mm=od, wall_mm=wall, radius_mm=radius,
            pos_no=None, page=page_idx + 1,
        ))
    return bends


def _extract_fittings(text: str, page_idx: int) -> list[Fitting]:
    """Tee / Reducing tee / Reducer / Branch / Flange / Cap 추출."""
    fittings: list[Fitting] = []
    matched_spans: list[tuple[int, int]] = []

    # 1. Reducing tee (가장 specific 먼저)
    for m in RE_REDUCING_TEE.finditer(text):
        pos, od_m, w_m, od_b, w_b, qty = (
            int(m.group(1)), float(m.group(2)), float(m.group(3)),
            float(m.group(4)), float(m.group(5)), int(m.group(6)),
        )
        if qty > 20:
            qty = 1
        fittings.append(Fitting(
            type="reducing_tee", od_main_mm=od_m, wall_main_mm=w_m,
            od_branch_mm=od_b, wall_branch_mm=w_b,
            quantity=qty, pos_no=pos, page=page_idx + 1,
        ))
        matched_spans.append((m.start(), m.end()))

    # 2. Tee 단순
    for m in RE_TEE.finditer(text):
        if any(s <= m.start() < e for s, e in matched_spans):
            continue
        pos, od, w, qty = int(m.group(1)), float(m.group(2)), float(m.group(3)), int(m.group(4))
        if qty > 20:
            qty = 1
        fittings.append(Fitting(
            type="tee", od_main_mm=od, wall_main_mm=w,
            quantity=qty, pos_no=pos, page=page_idx + 1,
        ))
        matched_spans.append((m.start(), m.end()))

    # 3. Reducer
    for m in RE_REDUCER.finditer(text):
        if any(s <= m.start() < e for s, e in matched_spans):
            continue
        pos = int(m.group(1))
        od_m = float(m.group(2))
        od_b = float(m.group(3))
        qty = int(m.group(4))
        if qty > 20:
            qty = 1
        fittings.append(Fitting(
            type="reducer", od_main_mm=od_m, wall_main_mm=0,
            od_branch_mm=od_b, wall_branch_mm=0,
            quantity=qty, pos_no=pos, page=page_idx + 1,
        ))
        matched_spans.append((m.start(), m.end()))

    # 4. Branch
    for m in RE_BRANCH.finditer(text):
        if any(s <= m.start() < e for s, e in matched_spans):
            continue
        pos = int(m.group(1))
        od_m = float(m.group(2))
        w_m = float(m.group(3))
        fittings.append(Fitting(
            type="branch", od_main_mm=od_m, wall_main_mm=w_m,
            quantity=1, pos_no=pos, page=page_idx + 1,
        ))
        matched_spans.append((m.start(), m.end()))

    # 5. Flange
    for m in RE_FLANGE.finditer(text):
        if any(s <= m.start() < e for s, e in matched_spans):
            continue
        pos = int(m.group(1))
        dn = float(m.group(2))
        pn = float(m.group(3))
        fittings.append(Fitting(
            type="flange", od_main_mm=dn, wall_main_mm=pn,  # wall_main 에 PN 임시 저장
            quantity=1, pos_no=pos, page=page_idx + 1,
        ))
        matched_spans.append((m.start(), m.end()))

    # 6. Cap
    for m in RE_CAP.finditer(text):
        if any(s <= m.start() < e for s, e in matched_spans):
            continue
        pos = int(m.group(1))
        fittings.append(Fitting(
            type="cap", od_main_mm=0, wall_main_mm=0,
            quantity=1, pos_no=pos, page=page_idx + 1,
        ))
        matched_spans.append((m.start(), m.end()))

    # Fallback patterns (Pos 없이) — spec 표 multi-line 분리된 경우
    # 1차 매칭에 잡힌 영역 외에서만 추가
    def _try_fallback(pat, kind, parse_fn):
        for m in pat.finditer(text):
            if any(s <= m.start() < e for s, e in matched_spans):
                continue
            try:
                parsed = parse_fn(m)
                if parsed:
                    fittings.append(parsed)
                    matched_spans.append((m.start(), m.end()))
            except Exception:
                continue

    def _parse_reducing_tee_simple(m):
        od_m, w_m, od_b, w_b, qty = (
            float(m.group(1)), float(m.group(2)),
            float(m.group(3)), float(m.group(4)), int(m.group(5)),
        )
        if qty > 20:
            qty = 1
        return Fitting(
            type="reducing_tee", od_main_mm=od_m, wall_main_mm=w_m,
            od_branch_mm=od_b, wall_branch_mm=w_b,
            quantity=qty, page=page_idx + 1,
        )

    def _parse_tee_simple(m):
        od, w, qty = float(m.group(1)), float(m.group(2)), int(m.group(3))
        if qty > 20:
            qty = 1
        return Fitting(
            type="tee", od_main_mm=od, wall_main_mm=w,
            quantity=qty, page=page_idx + 1,
        )

    def _parse_branch_simple(m):
        od = float(m.group(1))
        w = float(m.group(2))
        return Fitting(
            type="branch", od_main_mm=od, wall_main_mm=w,
            quantity=1, page=page_idx + 1,
        )

    def _parse_flange_simple(m):
        dn = float(m.group(1))
        pn = float(m.group(2))
        return Fitting(
            type="flange", od_main_mm=dn, wall_main_mm=pn,
            quantity=1, page=page_idx + 1,
        )

    def _parse_cap_simple(m):
        return Fitting(
            type="cap", od_main_mm=0, wall_main_mm=0,
            quantity=1, page=page_idx + 1,
        )

    _try_fallback(RE_REDUCING_TEE_SIMPLE, "reducing_tee", _parse_reducing_tee_simple)
    _try_fallback(RE_TEE_SIMPLE, "tee", _parse_tee_simple)
    _try_fallback(RE_BRANCH_SIMPLE, "branch", _parse_branch_simple)
    _try_fallback(RE_FLANGE_SIMPLE, "flange", _parse_flange_simple)
    _try_fallback(RE_CAP_SIMPLE, "cap", _parse_cap_simple)

    # Pos 번호 오름차순
    fittings.sort(key=lambda f: (f.pos_no is None, f.pos_no or 999))
    return fittings


def _extract_slopes(text: str) -> list[float]:
    """도면 안 'Xmm/m' 표기 추출 (시공 시 기울기). Unique 값만."""
    seen: set[float] = set()
    for m in RE_SLOPE.finditer(text):
        v = float(m.group(1))
        if 0 < v < 100:  # 100mm/m 이상은 비현실적
            seen.add(v)
    return sorted(seen)


def _extract_components(text: str, page_idx: int) -> list[Component]:
    """valve / support / penetration / nozzle / pump 추출.

    1차: Pos 번호 함께 매칭 (정확한 routing 위치).
    2차: Pos 없이 (kind 별).
    """
    comps: list[Component] = []
    seen: set[str] = set()

    # 1차: Pos 함께
    for m in RE_COMPONENT_WITH_POS.finditer(text):
        pos = int(m.group(1))
        kks = m.group(2)
        kind_raw = m.group(3).lower()
        if kks in seen:
            continue
        # kind 정규화
        kind_map = {
            "valve": "valve", "support": "support", "penetration": "penetration",
            "nozzle": "nozzle", "pump": "pump", "cap": "cap", "flange": "flange",
        }
        kind = kind_map.get(kind_raw, kind_raw)
        seen.add(kks)
        comps.append(Component(type=kind, kks=kks, pos_no=pos, page=page_idx + 1))

    # 2차: Pos 없이 — kind 별 (KKS 패턴 기반)
    def _add(pattern, kind):
        for m in pattern.finditer(text):
            kks = m.group(1)
            if kks not in seen:
                seen.add(kks)
                comps.append(Component(type=kind, kks=kks, pos_no=None, page=page_idx + 1))

    _add(RE_VALVE, "valve")
    _add(RE_PENETRATION, "penetration")
    _add(RE_SUPPORT, "support")
    _add(RE_NOZZLE, "nozzle")
    _add(RE_PUMP, "pump")

    # Pos 번호 있는 것 우선, Pos 오름차순
    comps.sort(key=lambda c: (c.pos_no is None, c.pos_no or 999))
    return comps


def _branch_kks_for_sheet(text: str) -> Optional[str]:
    """이 시트가 표현하는 메인 branch KKS 추출 (90XXX91BR001 형태)."""
    for m in RE_KKS.finditer(text):
        k = m.group(1)
        if "BR" in k:  # branch
            return k
    return None


def extract_drawing(pdf_path: Path) -> DrawingExtract:
    """DC PDF 한 개 → DrawingExtract."""
    extract = DrawingExtract(drawing_no=None)
    if not pdf_path.exists():
        extract.needs_review.append(f"파일 없음: {pdf_path}")
        return extract

    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""

            # 메타데이터: 첫 페이지에서 도면번호
            if extract.drawing_no is None:
                dm = RE_DRAWING_NO.search(text)
                if dm:
                    extract.drawing_no = dm.group(1)

            # plan elevation / north bearing (보통 plan 페이지에서)
            if extract.elevation_plan is None:
                em = RE_PLAN_ELEVATION.search(text)
                if em:
                    try:
                        extract.elevation_plan = float(em.group(1).replace(",", "."))
                    except ValueError:
                        pass
            if extract.north_bearing_deg is None:
                nm = RE_NORTH_BEARING.search(text)
                if nm:
                    try:
                        extract.north_bearing_deg = float(nm.group(1).replace(",", "."))
                    except ValueError:
                        pass

            # isometric 시트 분류 — strict:
            # (1) Specification of spools 표 **필수** (실제 어셈블리에만 존재)
            # + (2) "Isometric installation diagram" 키워드 또는 BR KKS branch 중 하나
            has_iso_keyword = bool(RE_ISO_DIAGRAM.search(text))
            has_spec_table = "Specification of spools" in text
            branch = _branch_kks_for_sheet(text)
            if not has_spec_table:
                continue
            if not (has_iso_keyword or branch is not None):
                continue

            sheet = IsometricSheet(page=i + 1)
            sheet.branch_kks = branch
            sheet.spools = _extract_spools(text, i)
            sheet.bends = _extract_bends(text, i)
            sheet.fittings = _extract_fittings(text, i)
            sheet.components = _extract_components(text, i)
            sheet.tie_ins = _extract_tie_ins(text, i)
            sheet.slopes_mm_per_m = _extract_slopes(text)
            # continuations — 페이지 안 모든 (a) BR sub-ref (90XXX91BR003-MLV0001)
            # 와 (b) ED 도면번호 자기 자신 외 모두 dedup 수집
            seen_cont: set[str] = set()
            self_drawing = extract.drawing_no or ""
            # (a) BR sub-ref 패턴
            for m in re.finditer(r"\b(\w{2,}BR\d{2,3}-MLV\d{3,4})\b", text):
                label = m.group(1)
                if label not in seen_cont:
                    seen_cont.add(label)
                    sheet.continuations.append(label)
            # (b) ED 도면 ref — 자기 자신 (언어코드 .E 유무 모두) 제외
            self_base = self_drawing.rstrip(".E").rstrip(".") if self_drawing else ""
            for m in RE_DRAWING_NO.finditer(text):
                label = m.group(1)
                label_base = label.rstrip(".E").rstrip(".")
                if label_base == self_base:
                    continue
                if label not in seen_cont:
                    seen_cont.add(label)
                    sheet.continuations.append(label)

            # 자가 검증 — 추출이 빈약하면 needs_review
            # (spool 다수인데 bend 0 = 직선 어셈블리 정상 가능, 경고 안 함)
            if not sheet.spools:
                sheet.needs_review.append("spool 미추출 — OCR 또는 패턴 미스 가능")
            if not sheet.tie_ins and not sheet.spools:
                sheet.needs_review.append("좌표·spool 모두 0 — 시트 분류 오류 가능")

            extract.sheets.append(sheet)

    if not extract.sheets:
        extract.needs_review.append(
            "isometric 시트 0개 — 'Isometric installation diagram' 키워드 없음. "
            "도면이 scan 본일 경우 Adobe OCR 처리 후 재시도 권장."
        )

    return extract


def extract_to_json(pdf_path: Path, output_path: Optional[Path] = None) -> dict:
    """CLI 진입점 — DC PDF → JSON."""
    extract = extract_drawing(pdf_path)
    data = extract.to_dict()
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Wrote %s", output_path)
    return data
