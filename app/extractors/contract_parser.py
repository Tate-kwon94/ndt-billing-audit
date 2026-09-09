"""계약서 핵심 조항 추출 (HCX-007, 권위계층 4순위)."""
from __future__ import annotations

from pathlib import Path

from app.database.models import get_session, init_db
from app.database.repository import upsert_standard
from app.extractors.pdf_extractor import extract
from app.hcx_client import call


def ingest(file_path: Path) -> dict:
    file_path = Path(file_path)
    extracted = extract(file_path)
    text_full = "\n\n".join(f"---PAGE {p.page_index + 1}---\n{p.text}" for p in extracted.pages)
    resp = call(
        "contract_extract",
        {
            "contract_no": file_path.stem,
            "parties": [],
            "effective_date": None,
            "text_full": text_full,
        },
    )
    parsed = resp.parsed or {}

    init_db()
    with get_session() as s:
        upsert_standard(
            s,
            file_path=str(file_path),
            doc_type="contract",
            document_no=parsed.get("contract_no"),
            revision=None,
            extracted_json=parsed,
        )
        s.commit()
    return parsed
