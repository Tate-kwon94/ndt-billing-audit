"""분류된 도면 파일을 도면번호 기준으로 DC+SD+BG 3파일 세트로 묶는다.

세트 누락 시 경고와 함께 부분 세트로 진행 (이후 단계가 누락을 인지하도록 표시).
rev 표기가 다양할 수 있어 normalize 후 비교.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from app.extractors.drawing.classifier import Classification


# 사용자 규칙 (2026-09-07): 시공 관련 도면은 DC·DK 뿐. SD·BG 는 구매 관련 도면이라 시공 스펙에 없어도 된다.
# 그래서 세트의 완전성은 DC 하나로 판정하고, 나머지는 있으면 쓰는 선택 종류다.
# (DK 는 아직 샘플이 없어 파서가 없다 — 세트에 묶이고 파일로 기록되며, 내용 추출은 후속.)
REQUIRED_TYPES = ("DC",)
OPTIONAL_TYPES = ("SD", "BG", "DK")
ALL_TYPES = REQUIRED_TYPES + OPTIONAL_TYPES


@dataclass
class DrawingSet:
    drawing_no: str
    revision: Optional[str]
    files: dict[str, Classification] = field(default_factory=dict)   # "DC"|"SD"|"BG"|"DK" → Classification

    @property
    def is_complete(self) -> bool:
        return all(t in self.files for t in REQUIRED_TYPES)

    @property
    def missing(self) -> list[str]:
        return [t for t in REQUIRED_TYPES if t not in self.files]


@dataclass
class GroupingResult:
    sets: list[DrawingSet]
    unclassified: list[Classification]   # drawing_no 또는 drawing_type 미확정 → 검토자 확인 필요
    duplicate_conflicts: list[dict]      # 동일 (no, rev, type) 에 여러 파일이 들어온 경우


def _normalize_rev(rev: Optional[str]) -> Optional[str]:
    if rev is None:
        return None
    return re.sub(r"[^A-Za-z0-9]", "", str(rev)).lower() or None


def group(classifications: list[Classification]) -> GroupingResult:
    """동일 drawing_no 별로 묶음 — 사용자 정책 (2026-05-21):
    rev 는 종류별로 독립 진화하므로 (예: BG=C01 인데 DC/SD=None) drawing_no 만으로 1세트.

    같은 (drawing_no, drawing_type) 에 여러 rev 파일이 있으면 최신 rev 채택 + 충돌 기록.
    분류 미확정(drawing_no 또는 drawing_type 이 None)인 파일은 `unclassified` 로 분리.
    """
    buckets: dict[str, DrawingSet] = {}
    unclassified: list[Classification] = []
    duplicates: list[dict] = []

    for c in classifications:
        if not c.drawing_no or not c.drawing_type:
            unclassified.append(c)
            continue
        ds = buckets.get(c.drawing_no)
        if ds is None:
            ds = DrawingSet(drawing_no=c.drawing_no, revision=None)
            buckets[c.drawing_no] = ds
        existing = ds.files.get(c.drawing_type)
        if existing is None:
            ds.files[c.drawing_type] = c
        else:
            # 같은 종류에 여러 rev 가 있는 경우 — 최신 rev 채택
            chosen_path = existing.__dict__.get("_file_path")
            new_path = c.__dict__.get("_file_path")
            new_is_newer = _rev_rank(c.revision) > _rev_rank(existing.revision)
            duplicates.append({
                "drawing_no": c.drawing_no,
                "drawing_type": c.drawing_type,
                "existing_rev": existing.revision,
                "new_rev": c.revision,
                "files": [str(chosen_path), str(new_path)],
                "chosen_rev": (c.revision if new_is_newer else existing.revision),
                "reason": "동일 (도면번호, 종류) 에 rev 다른 파일 다수 — 최신 rev 자동 채택, 검토자 확인 권장",
            })
            if new_is_newer:
                ds.files[c.drawing_type] = c

    # 세트의 set_revision 표시 — 종류별 rev 합본 (예: 'DC=-,SD=-,BG=C01')
    for ds in buckets.values():
        parts = []
        for t in ALL_TYPES:
            if t in ds.files:
                parts.append(f"{t}={ds.files[t].revision or '-'}")
        ds.revision = ",".join(parts) if parts else None

    return GroupingResult(sets=list(buckets.values()), unclassified=unclassified, duplicate_conflicts=duplicates)


def _rev_rank(rev: Optional[str]) -> int:
    """rev 우선순위: None/빈 = 0 (rev 표기 누락 = C00 으로 간주, 사용자 정책)."""
    if rev is None or str(rev).strip() == "":
        return 0
    # 'C01' → 1, 'C03' → 3, 'A' → 1, '2' → 2 등 마지막 숫자/알파벳 사용
    m = re.search(r"(\d+)$", str(rev))
    if m:
        return int(m.group(1))
    # 알파벳 rev (A, B, C ...) → 1, 2, 3 ...
    m = re.search(r"([A-Za-z])$", str(rev))
    if m:
        return ord(m.group(1).upper()) - ord("A") + 1
    return 0
