"""NDT-3D — 시공 보조용 per-drawing 3D 시각화.

DC 등각도 (isometric) PDF → 좌표·스풀·곡관 추출 → trimesh 3D → glb + HTML.

목적: 검토 도구가 아닌 시공·QC 보조. 작업자가 휴대폰·태블릿에서 회전·확대로
모양 확인 후 fabrication·welding 작업.

정확도 한계는 다음 메모리 원칙대로 needs_review 로 노출:
- OCR 추출 실패한 좌표·길이 → 빨강 표시 (3D 안 만들고 경고)
- bend 각도 ≠ 90° 인데 카탈로그 미확인 → 회색 placeholder
"""
from __future__ import annotations

__version__ = "0.1.0"
