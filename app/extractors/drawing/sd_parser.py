"""SD 도면 파일을 HCX-007 로 구조화 추출."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.extractors.drawing.dc_parser import _join_pages
from app.extractors.pdf_extractor import extract
from app.hcx_client import call


def parse_sd(file_path: Path, *, drawing_no: str, revision: Optional[str]) -> dict | None:
    extracted = extract(file_path)
    resp = call(
        "drawing_sd",
        {
            "drawing_no": drawing_no,
            "revision": revision,
            "text_full": _join_pages(extracted.pages),
            "language": "english",
        },
    )
    return resp.parsed
