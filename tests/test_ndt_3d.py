"""ndt_3d 단위 테스트 — 회귀 안전망."""
from __future__ import annotations

from pathlib import Path

import pytest

from ndt_3d.coord_extractor import (
    RE_BEND,
    RE_BEND_FALLBACK,
    RE_DRAWING_NO,
    RE_SPOOL,
    RE_TIE_X,
    RE_TIE_Y,
    RE_TIE_Z,
    _extract_bends,
    _extract_spools,
    _extract_tie_ins,
    extract_drawing,
)


def test_drawing_no_with_ampersands():
    """1ULD 케이스 — & 포함 도면번호 매치."""
    text = "NP.D.P000.9.1ULD&&GMM91&.052.DC.0001.E Revision C01"
    m = RE_DRAWING_NO.search(text)
    assert m is not None
    assert m.group(1) == "NP.D.P000.9.1ULD&&GMM91&.052.DC.0001.E"


def test_drawing_no_without_ampersands():
    """일반 케이스 — & 없는 도면번호도 매치."""
    text = "NP.D.P000.1.0UGB95GML90&.052.DC.0001.E"
    m = RE_DRAWING_NO.search(text)
    assert m is not None


def test_spool_pattern():
    """파이프 spec 표 라인 매치."""
    text = "1 GOST 8732-78 Pipe 108x6 20 TU 14-3-190-2004 1114 mm 15.09 16.81"
    m = RE_SPOOL.search(text)
    assert m is not None
    assert m.group(1) == "108"
    assert m.group(2) == "6"
    assert m.group(3) == "1114"


def test_bend_with_quantity():
    """Bend Quantity 컬럼 추출 — '4 GOST 17375-2001 Bend 90-108x6 20 GOST 1050-2013 3 3.60 10.80'."""
    text = "4 GOST 17375-2001 Bend 90-108x6 20 GOST 1050-2013 3 3.60 10.80"
    m = RE_BEND.search(text)
    assert m is not None
    assert int(m.group(1)) == 90    # angle
    assert int(m.group(2)) == 108   # OD
    assert int(m.group(3)) == 6     # wall
    assert int(m.group(4)) == 3     # quantity


def test_bend_fallback():
    """Quantity 캡처 실패 시 fallback regex 매치."""
    text = "Bend 45-57x4"
    m = RE_BEND_FALLBACK.search(text)
    assert m is not None
    assert int(m.group(1)) == 45


def test_tie_in_xyz():
    """X/Y/Z 좌표 3개 모두 매치."""
    text = "X- 77050\nY+ 127452\nZ-2340"
    x = RE_TIE_X.search(text)
    y = RE_TIE_Y.search(text)
    z = RE_TIE_Z.search(text)
    assert x and int(x.group(2)) * (-1 if x.group(1) == "-" else 1) == -77050
    assert y and int(y.group(2)) * (-1 if y.group(1) == "-" else 1) == 127452
    assert z and int(z.group(2)) * (-1 if z.group(1) == "-" else 1) == -2340


def test_extract_bends_quantity_multiply():
    """Quantity=3 인 bend 1행 → Bend 객체 3개 생성."""
    text = "4 GOST 17375-2001 Bend 90-108x6 20 GOST 1050-2013 3 3.60 10.80"
    bends = _extract_bends(text, 0)
    assert len(bends) == 3
    assert all(b.angle_deg == 90 and b.od_mm == 108 for b in bends)


def test_extract_tie_ins_dedup():
    """동일 (X,Y,Z) 좌표는 1개로."""
    text = """
    X- 77050 Y+ 127452 Z-2340
    For continuation see XXX
    X- 77050 Y+ 127452 Z-2340
    """
    tie_ins = _extract_tie_ins(text, 0)
    # 같은 좌표 → 1개만
    coords = {(t.x, t.y, t.z) for t in tie_ins}
    assert len(coords) == len(tie_ins)


def test_extract_spools_dedup_same_combo():
    """동일 (OD, wall, length) spool 은 1개 (spec 표 안에서 중복 가능성)."""
    text = "Pipe 108x6 ... 1114 mm\nPipe 108x6 ... 1114 mm"
    spools = _extract_spools(text, 0)
    assert len(spools) == 1


@pytest.mark.skipif(
    not Path("samples/drawings/NP.D.P000.9.1ULD&&GMM91&.052.DC.0001.E.pdf").exists(),
    reason="1ULD sample PDF not present",
)
def test_extract_1uld_native_pdf():
    """1ULD native PDF — 회귀 안전망: 핵심 수치 유지."""
    pdf = Path("samples/drawings/NP.D.P000.9.1ULD&&GMM91&.052.DC.0001.E.pdf")
    extract = extract_drawing(pdf)
    assert extract.drawing_no == "NP.D.P000.9.1ULD&&GMM91&.052.DC.0001.E"
    assert extract.elevation_plan == -5.2
    assert extract.north_bearing_deg == 18.9
    # isometric 시트 정확 10개 (v0.1.2 strict 분류 후)
    assert len(extract.sheets) == 10
    # spool 합계 ≥ 25 (개선되면 늘 수 있으나 회귀 방지)
    total_spools = sum(len(s.spools) for s in extract.sheets)
    assert total_spools >= 25
    # bend 합계 ≥ 8 (Quantity 반영 후)
    total_bends = sum(len(s.bends) for s in extract.sheets)
    assert total_bends >= 8
