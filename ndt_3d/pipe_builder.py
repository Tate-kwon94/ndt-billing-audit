"""DC 추출 JSON → trimesh 3D 모델 (glb).

v0.1.2 한계 (정직 명시):
- spool/bend 의 **순서**가 spec 표의 등장 순서라고 가정
- 각 segment 의 공간 방향은 **bend 각도 반영 + 3축 cyclic 회전** — 정확한 routing 은 등각도 추출 필요
- tie-in 절대 좌표는 **라벨로만** 표시 (검증·정합 용)
- 모양·치수 정확도 ≈ 85%, routing 방향 정확도 ≈ 40% (v0.2 vision 필요)
- 향후 v0.2 에서 HCX-005 vision 으로 segment 방향 추정 보강 필요

목적: 시공자가 회전·확대로 어셈블리 모양 확인 + 치수 검증.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np
import trimesh

from ndt_3d.router import reconstruct_routing, axis_vector

logger = logging.getLogger(__name__)


# ─────────────────────────── 색상 (시공자 친화) ───────────────────────────


COLOR_PIPE_HI = [180, 180, 180, 255]    # 회색 (high confidence routing)
COLOR_PIPE_LO = [200, 200, 100, 200]    # 옅은 황색 + 반투명 (low confidence — fallback)
COLOR_PIPE = COLOR_PIPE_HI              # 기본
COLOR_BEND = [220, 200, 100, 255]       # 황색
COLOR_VALVE = [200, 50, 50, 255]        # 빨강
COLOR_NOZZLE = [50, 150, 200, 255]      # 청색
COLOR_NEEDS_REVIEW = [128, 128, 128, 180]  # 반투명 회색

CONFIDENCE_HI = 0.7  # 이상이면 결정론 매칭 신뢰


# Fallback cyclic directions (router 가 못 풀 때)
DIRECTIONS = [
    np.array([1.0, 0.0, 0.0]),
    np.array([0.0, 1.0, 0.0]),
    np.array([0.0, 0.0, 1.0]),
]


def _cylinder_segment(start: np.ndarray, end: np.ndarray, radius: float, color: list) -> trimesh.Trimesh:
    """두 점 사이를 잇는 원기둥 메쉬."""
    length = float(np.linalg.norm(end - start))
    if length < 1e-3:
        return trimesh.Trimesh()
    direction = (end - start) / length
    # trimesh.creation.cylinder 는 z축 기준 — 회전 변환 필요
    cyl = trimesh.creation.cylinder(radius=radius, height=length, sections=24)
    # z축(0,0,1) 을 direction 으로 회전
    z = np.array([0.0, 0.0, 1.0])
    if np.allclose(direction, z):
        rot = np.eye(4)
    elif np.allclose(direction, -z):
        rot = trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0])
    else:
        axis = np.cross(z, direction)
        angle = math.acos(float(np.clip(np.dot(z, direction), -1.0, 1.0)))
        rot = trimesh.transformations.rotation_matrix(angle, axis)
    cyl.apply_transform(rot)
    # 위치: start + length/2 (cylinder 는 중심 기준)
    midpoint = (start + end) / 2
    cyl.apply_translation(midpoint)
    cyl.visual.face_colors = color
    return cyl


def _bend_segment(
    pivot: np.ndarray, dir_in: np.ndarray, dir_out: np.ndarray, radius: float, bend_radius: float, color: list
) -> trimesh.Trimesh:
    """곡관 — pivot 기준 dir_in → dir_out 으로 호를 그리는 torus 일부."""
    # 단순화: torus 일부 대신 짧은 cylinder 들 연결로 근사 (10 segment)
    angle = math.acos(float(np.clip(np.dot(-dir_in, dir_out), -1.0, 1.0)))
    if angle < 1e-3:
        return trimesh.Trimesh()
    n = 10
    # 회전 평면의 법선
    normal = np.cross(dir_in, dir_out)
    n_norm = np.linalg.norm(normal)
    if n_norm < 1e-6:
        # 동일 방향 또는 정반대 — bend 없음
        return trimesh.Trimesh()
    normal = normal / n_norm
    # bend 중심 = pivot 에서 -dir_in 방향으로 bend_radius
    center = pivot + (-dir_in) * 0  # pivot 이 곡선 시작점, 중심은 옆으로
    # 더 정확히: 곡관 중심은 dir_in 과 dir_out 모두에 직각인 위치
    # 단순화: bend_radius 가 작으므로 짧은 호로 처리
    pieces = []
    prev = pivot.copy()
    for i in range(1, n + 1):
        t = i / n
        # angle 만큼 점진 회전
        a = angle * t
        rot = trimesh.transformations.rotation_matrix(a, normal, point=pivot)
        # 새 방향 단위벡터
        v = (rot[:3, :3] @ dir_in)
        curr = pivot + v * (bend_radius * math.sin(a))
        # 단순화: 직선 segment 들로 호 근사
        seg = _cylinder_segment(prev, curr, radius, color)
        if len(seg.vertices) > 0:
            pieces.append(seg)
        prev = curr
    if not pieces:
        return trimesh.Trimesh()
    return trimesh.util.concatenate(pieces)


def _make_scale_reference(extent: float, step: float, bbox: np.ndarray) -> trimesh.Trimesh:
    """1m grid + XYZ 축 표시 — 시공자가 축척 가늠하도록.

    Returns: 단일 메쉬 (회색 grid + RGB 축 line)
    """
    pieces = []
    # 1m grid 바닥 — 얇은 박스 (Y=bbox 최저점에)
    floor_z = float(bbox[0][2]) if bbox is not None else 0.0
    # Grid lines — X 방향 / Y 방향
    line_thickness = 10.0  # 10mm 두께
    n_steps = int(extent / step) + 1
    grid_color = [200, 200, 200, 120]  # 연회색 반투명

    # X 방향 grid line (Y 축 따라 반복)
    for i in range(n_steps + 1):
        y = i * step
        line = trimesh.creation.box(extents=[extent, line_thickness, line_thickness])
        line.apply_translation([extent / 2, y, floor_z - 100])
        line.visual.face_colors = grid_color
        pieces.append(line)
    # Y 방향 grid line
    for i in range(n_steps + 1):
        x = i * step
        line = trimesh.creation.box(extents=[line_thickness, extent, line_thickness])
        line.apply_translation([x, extent / 2, floor_z - 100])
        line.visual.face_colors = grid_color
        pieces.append(line)

    # XYZ 축 표시 (원점 기준 1m) — 시공자가 어느 축인지 즉시 파악
    axis_len = step  # 1m
    axis_thick = 40.0
    # X = red
    ax_x = trimesh.creation.box(extents=[axis_len, axis_thick, axis_thick])
    ax_x.apply_translation([axis_len / 2, 0, floor_z + 50])
    ax_x.visual.face_colors = [255, 80, 80, 255]
    # Y = green
    ax_y = trimesh.creation.box(extents=[axis_thick, axis_len, axis_thick])
    ax_y.apply_translation([0, axis_len / 2, floor_z + 50])
    ax_y.visual.face_colors = [80, 255, 80, 255]
    # Z = blue
    ax_z = trimesh.creation.box(extents=[axis_thick, axis_thick, axis_len])
    ax_z.apply_translation([0, 0, floor_z + axis_len / 2 + 50])
    ax_z.visual.face_colors = [80, 80, 255, 255]
    pieces.extend([ax_x, ax_y, ax_z])

    return trimesh.util.concatenate(pieces)


def _fitting_marker(pos: np.ndarray, radius: float, ftype: str,
                    od_main: float = 100, od_branch: Optional[float] = None) -> trimesh.Trimesh:
    """Fitting (Tee/Reducer/Branch/Flange/Cap) 의 시각적 표현.

    - tee          → 메인 원기둥 + 직각 분기 원기둥 (T)
    - reducing_tee → T 자, 분기 작음
    - reducer      → 절단된 콘 (큰 D → 작은 d)
    - branch       → 짧은 측면 원기둥
    - flange       → 두꺼운 원판
    - cap          → 반구 (말단 마개)
    """
    pieces = []
    main_r = max(20.0, od_main * 0.5) if od_main else radius
    branch_r = max(15.0, (od_branch or od_main * 0.5) * 0.5) if (od_branch or od_main) else radius * 0.6

    if ftype in ("tee", "reducing_tee"):
        # 메인 원기둥 (수평 X)
        main = trimesh.creation.cylinder(radius=main_r, height=main_r * 4)
        main.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
        # 분기 원기둥 (수직 Y)
        branch = trimesh.creation.cylinder(radius=branch_r, height=main_r * 3)
        branch.apply_translation([0, main_r * 1.5, 0])
        pieces = [main, branch]
        color = [200, 150, 80, 255]  # 황갈색
    elif ftype == "reducer":
        # 절단된 콘 — trimesh.creation.cone 이 base→tip 인데 절단된 콘은 cylinder 양끝 다른 반경으로
        # 단순화: 두 cylinder 의 평균 — 또는 frustum
        big_r = main_r
        small_r = max(10.0, (od_branch or main_r) * 0.5)
        # frustum 직접 만들기 (8각 둘레)
        h = main_r * 2
        n = 12
        verts = []
        for i in range(n):
            theta = 2 * np.pi * i / n
            verts.append([big_r * np.cos(theta), big_r * np.sin(theta), 0])
        for i in range(n):
            theta = 2 * np.pi * i / n
            verts.append([small_r * np.cos(theta), small_r * np.sin(theta), h])
        faces = []
        for i in range(n):
            a, b = i, (i + 1) % n
            c, d = n + b, n + a
            faces.append([a, b, c])
            faces.append([a, c, d])
        try:
            m_red = trimesh.Trimesh(vertices=verts, faces=faces)
            m_red.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
            pieces = [m_red]
        except Exception:
            pieces = [trimesh.creation.cylinder(radius=main_r, height=main_r * 2)]
        color = [180, 130, 80, 255]
    elif ftype == "branch":
        # 측면 분기 짧은 cylinder
        b = trimesh.creation.cylinder(radius=branch_r, height=main_r * 2)
        b.apply_translation([0, main_r, 0])
        pieces = [b]
        color = [180, 180, 80, 255]
    elif ftype == "flange":
        # 두꺼운 원판
        f = trimesh.creation.cylinder(radius=main_r * 1.3, height=main_r * 0.4)
        pieces = [f]
        color = [120, 100, 200, 255]
    elif ftype == "cap":
        # 반구 (말단)
        c = trimesh.creation.icosphere(subdivisions=2, radius=main_r * 1.1)
        # 위 절반만? — 일단 sphere 단순화
        pieces = [c]
        color = [220, 100, 100, 255]
    else:
        pieces = [trimesh.creation.icosphere(subdivisions=2, radius=radius)]
        color = [160, 160, 160, 200]

    if not pieces:
        return trimesh.Trimesh()
    m = trimesh.util.concatenate(pieces)
    m.apply_translation(pos)
    m.visual.face_colors = color
    return m


def _marker(pos: np.ndarray, radius: float, color: list, kind: str = "sphere",
            valve_subtype: Optional[str] = None) -> trimesh.Trimesh:
    """component 마커 — 종류별 모양 구분.

    valve 의 경우 valve_subtype (gate/check/ball/globe/butterfly) 으로 더 세분:
    - gate   → body 큐브 + 위쪽 stem (rising stem)
    - check  → 디스크 + 화살표 모양 (방향성)
    - ball   → 구 + 작은 핸들
    - globe  → 구형 body + stem
    - butterfly → 얇은 디스크
    """
    pieces: list[trimesh.Trimesh] = []
    if kind == "valve":
        st = (valve_subtype or "gate").lower()
        if st == "gate":
            # Body 박스 + 위쪽 stem (cylinder) + handle (cylinder)
            body = trimesh.creation.box(extents=[radius * 2.4, radius * 2.4, radius * 1.6])
            stem = trimesh.creation.cylinder(radius=radius * 0.3, height=radius * 2.0)
            stem.apply_translation([0, 0, radius * 1.8])
            handle = trimesh.creation.cylinder(radius=radius * 1.5, height=radius * 0.25)
            handle.apply_translation([0, 0, radius * 3.0])
            pieces = [body, stem, handle]
        elif st == "check":
            # 디스크 (납작 cylinder) — 방향성 표현
            disk = trimesh.creation.cylinder(radius=radius * 1.3, height=radius * 0.8)
            arrow = trimesh.creation.cone(radius=radius * 0.8, height=radius * 1.2)
            arrow.apply_translation([radius * 1.2, 0, 0])
            arrow.apply_transform(trimesh.transformations.rotation_matrix(
                np.pi / 2, [0, 1, 0], point=[radius * 1.2, 0, 0]))
            pieces = [disk, arrow]
        elif st == "ball":
            sphere = trimesh.creation.icosphere(subdivisions=2, radius=radius * 1.2)
            handle = trimesh.creation.cylinder(radius=radius * 0.25, height=radius * 1.5)
            handle.apply_translation([0, 0, radius * 1.5])
            pieces = [sphere, handle]
        elif st == "globe":
            body = trimesh.creation.icosphere(subdivisions=2, radius=radius * 1.2)
            stem = trimesh.creation.cylinder(radius=radius * 0.3, height=radius * 2.0)
            stem.apply_translation([0, 0, radius * 1.6])
            pieces = [body, stem]
        elif st == "butterfly":
            pieces = [trimesh.creation.cylinder(radius=radius * 1.5, height=radius * 0.3)]
        else:
            pieces = [trimesh.creation.box(extents=[radius * 2, radius * 2, radius * 1.5])]
    elif kind == "nozzle":
        pieces = [trimesh.creation.cone(radius=radius * 1.2, height=radius * 2.2)]
    elif kind == "support":
        pieces = [trimesh.creation.cylinder(radius=radius * 0.5, height=radius * 3.0)]
    elif kind == "penetration":
        pieces = [trimesh.creation.cylinder(radius=radius * 1.5, height=radius * 0.3)]
    elif kind == "pump":
        pieces = [trimesh.creation.box(extents=[radius * 3, radius * 3, radius * 3])]
    else:
        pieces = [trimesh.creation.icosphere(subdivisions=2, radius=radius)]

    if not pieces:
        return trimesh.Trimesh()
    m = trimesh.util.concatenate(pieces)
    m.apply_translation(pos)
    m.visual.face_colors = color
    return m


def build_sheet_mesh(
    sheet_data: dict, pdf_path: Optional[Path] = None,
) -> tuple[trimesh.Scene, dict]:
    """1 isometric 시트의 spool + bend + component → trimesh.Scene.

    router 가 결정한 spool 방향을 사용 (v0.3 vector graphics 우선).
    """
    scene = trimesh.Scene()
    meta = {
        "branch_kks": sheet_data.get("branch_kks"),
        "page": sheet_data.get("page"),
        "spool_count": len(sheet_data.get("spools", [])),
        "bend_count": len(sheet_data.get("bends", [])),
        "component_count": len(sheet_data.get("components", [])),
        "tie_in_count": len(sheet_data.get("tie_ins", [])),
        "needs_review": sheet_data.get("needs_review", []),
        "total_length_mm": 0.0,
        "warnings": [],
    }

    spools = sheet_data.get("spools", [])
    bends = sheet_data.get("bends", [])
    fittings = sheet_data.get("fittings", [])  # NEW
    components = sheet_data.get("components", [])
    meta["fitting_count"] = sum(f.get("quantity", 1) for f in fittings)
    meta["slope_count"] = len(sheet_data.get("slopes_mm_per_m", []))

    if not spools:
        meta["warnings"].append("spool 0개 — 3D 모델 생성 불가")
        return scene, meta

    # 결정론 routing — vector graphics (v0.3) 우선, tie-in delta (v0.2) fallback
    placements, router_meta = reconstruct_routing(sheet_data, pdf_path=pdf_path)
    meta["routing_method"] = router_meta["method"]
    meta["routing_matched"] = router_meta["matched_count"]
    meta["routing_fallback"] = router_meta["fallback_count"]
    if router_meta["fallback_count"]:
        meta["warnings"].append(
            f"⚠ routing {router_meta['fallback_count']}/{router_meta['spool_count']} spool 추정 (tie-in delta 매칭 실패)"
        )

    current_pos = np.array([0.0, 0.0, 0.0])
    pieces = []
    bend_iter = iter(bends)
    prev_dir: Optional[np.ndarray] = None

    for i, spool in enumerate(spools):
        od = spool["od_mm"]
        length = spool["length_mm"]
        radius = od / 2.0
        # router 결정 방향
        p = placements[i] if i < len(placements) else None
        if p:
            direction = axis_vector(p.axis, p.sign)
            color = COLOR_PIPE_HI if p.confidence >= CONFIDENCE_HI else COLOR_PIPE_LO
        else:
            direction = DIRECTIONS[i % 3]
            color = COLOR_PIPE_LO

        # 이전 방향과 다르면 bend 1개 (router 가 결정한 게 자연스럽게 bend 위치)
        if prev_dir is not None and not np.allclose(direction, prev_dir):
            try:
                bend = next(bend_iter)
                br = bend.get("radius_mm") or (od * 1.5)
                bend_mesh = _bend_segment(current_pos, prev_dir, direction, radius, br, COLOR_BEND)
                if len(bend_mesh.vertices) > 0:
                    pieces.append(bend_mesh)
                # bend arc 길이만큼 current_pos 전진
                arc = br * math.pi / 2.0  # 90°
                current_pos = current_pos + direction * arc * 0.3
            except StopIteration:
                pass

        end_pos = current_pos + direction * length
        cyl = _cylinder_segment(current_pos, end_pos, radius, color)
        if len(cyl.vertices) > 0:
            pieces.append(cyl)
        meta["total_length_mm"] += length
        current_pos = end_pos
        prev_dir = direction

    # 모든 spool + bend 합쳐서 scene 에 추가
    if pieces:
        combined = trimesh.util.concatenate(pieces)
        scene.add_geometry(combined, geom_name="pipeline")
        meta["bbox_size_mm"] = float(np.max(combined.bounds[1] - combined.bounds[0]))

    # component 마커 — Pos 번호 기반 spool 사이 정확 배치
    # 도면 spec 표에서 Pos N spool 뒤에 Pos N+1 component 가 오면, 그 spool 끝점에 마커
    base_radius = max(50.0, (spools[0]["od_mm"] if spools else 100) * 0.8)

    # spool 의 cumulative end position 계산
    spool_endpoints: list[tuple[Optional[int], np.ndarray]] = [(None, np.array([0.0, 0.0, 0.0]))]
    pos = np.array([0.0, 0.0, 0.0])
    for i, sp in enumerate(spools):
        p = placements[i] if i < len(placements) else None
        if p:
            direction = axis_vector(p.axis, p.sign)
        else:
            direction = DIRECTIONS[i % 3]
        pos = pos + direction * sp["length_mm"]
        spool_endpoints.append((sp.get("pos_no"), pos.copy()))

    # 각 component 의 위치 = 가장 가까운 작은 Pos 번호 spool 의 끝점
    if components:
        for comp in components:
            cpos = comp.get("pos_no")
            marker_pos = current_pos.copy()  # default: 끝
            if cpos is not None:
                # 이 component 의 Pos 보다 작거나 같은 spool 중 가장 가까운 Pos 의 endpoint
                best = None
                for sp_pos, ep in spool_endpoints:
                    if sp_pos is not None and sp_pos <= cpos:
                        best = ep
                if best is not None:
                    marker_pos = best.copy()
            color = {
                "valve": COLOR_VALVE,
                "nozzle": COLOR_NOZZLE,
                "support": COLOR_PIPE,
                "penetration": [160, 160, 200, 255],
                "pump": [100, 180, 100, 255],
                "flange": [120, 100, 200, 255],
                "cap": [220, 100, 100, 255],
            }.get(comp["type"], COLOR_NEEDS_REVIEW)
            valve_subtype = comp.get("valve_subtype")
            sd = comp.get("sd_spec") or {}
            dn = sd.get("dn_mm")
            radius = base_radius
            if comp["type"] == "valve" and dn:
                radius = base_radius * max(0.4, min(1.5, dn / 100.0))
            marker = _marker(marker_pos, radius, color,
                              kind=comp["type"], valve_subtype=valve_subtype)
            scene.add_geometry(marker, geom_name=f"{comp['type']}_{comp['kks']}")

    # Fitting 마커 — Pos 번호 기반 spool 끝점 (component 와 동일 방식)
    if fittings:
        for i, fit in enumerate(fittings):
            ftype = fit.get("type", "unknown")
            qty = fit.get("quantity", 1)
            pos_no = fit.get("pos_no")
            # Pos 기반 위치 또는 cycle 분산
            if pos_no is not None:
                best = None
                for sp_pos, ep in spool_endpoints:
                    if sp_pos is not None and sp_pos <= pos_no:
                        best = ep
                marker_pos = best.copy() if best is not None else current_pos.copy()
            else:
                # fallback — fitting 별 distinct 위치 (i 번째 fitting → i/총 fitting 비율)
                t = (i + 1) / (len(fittings) + 1)
                marker_pos = current_pos * t
            # 같은 spec quantity > 1 → 살짝 다른 위치에 복제
            for q in range(qty):
                offset = np.array([0.0, 0.0, q * base_radius * 0.6])  # Z 축 살짝 옆
                m = _fitting_marker(
                    marker_pos + offset, base_radius, ftype,
                    od_main=fit.get("od_main_mm", 100),
                    od_branch=fit.get("od_branch_mm"),
                )
                scene.add_geometry(m, geom_name=f"{ftype}_{pos_no or i}_{q}")

    return scene, meta


def build_drawing_glb(
    extract_data: dict, output_dir: Path, pdf_path: Optional[Path] = None,
) -> dict:
    """DC PDF 1개의 모든 isometric 시트 → 시트별 .glb 파일들.

    pdf_path 주어지면 vector graphics 분석 활성 (v0.3, 정확도 ↑).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    drawing_no = extract_data.get("drawing_no") or "unknown"
    safe_name = drawing_no.replace("&", "_").replace(".", "_")
    result = {}
    for sheet in extract_data.get("sheets", []):
        scene, meta = build_sheet_mesh(sheet, pdf_path=pdf_path)
        if len(scene.geometry) == 0:
            logger.warning("Sheet page=%s: empty scene — 3D 생성 안 함", sheet.get("page"))
            result[sheet["page"]] = {"glb_path": None, "meta": meta}
            continue
        glb_path = output_dir / f"{safe_name}_p{sheet['page']}_{sheet.get('branch_kks', 'unknown')}.glb"
        try:
            data = scene.export(file_type="glb")
            glb_path.write_bytes(data)
            logger.info("Wrote %s (%d KB)", glb_path, len(data) // 1024)
            result[sheet["page"]] = {"glb_path": str(glb_path), "meta": meta}
        except Exception as e:
            logger.error("glb export 실패 page=%s: %s", sheet.get("page"), e)
            result[sheet["page"]] = {"glb_path": None, "meta": meta, "error": str(e)}
    return result
