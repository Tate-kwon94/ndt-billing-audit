"""SCWEP 시공·검사 절차서 추출 (HCX-007, 권위계층 2순위).

실 문서(2026-09-05 샘플)는 159~236쪽이고 NDT 관련 페이지는 그중 15쪽 안팎이다. 전문을 한 번에
보내면 40003(Context length) 이 확정이라, NDT 키워드 페이지(±1쪽)만 골라 창 단위로 여러 번 부르고
결과를 합친다. 조건부 요구의 실제 형태: §7.3.4.7 "the places from which temporary process devices
have been removed shall be subjected to 100 % visual inspection and measurements and liquid penetrant
testing (LPT)" (LAH·LBA p25, LCA p24).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from app.config import hcx_config
from app.database.models import get_session, init_db
from app.database.repository import upsert_standard
from app.extractors.pdf_extractor import PageText, extract
from app.extractors.table_pages import page_records
from app.hcx_client import call

logger = logging.getLogger(__name__)

SCWEP_SCHEMA_VERSION = 2

_NDT_KW = re.compile(
    r"non-?destructive|\bNDT\b|penetrant|\bLPT\b|ultrasonic|radiograph|magnetic particle|"
    r"visual inspection|temporary process device|have been removed", re.I)
_MERGE_KEYS = ("conditional_ndt_requirements", "general_rules", "default_sampling_rates")


def select_pages(pages, *, keywords=_NDT_KW) -> list[int]:
    n = len(pages)
    hit = {p.page_index for p in pages if keywords.search(p.text or "")}
    keep: set[int] = set()
    for i in hit:
        keep.update(j for j in (i - 1, i, i + 1) if 0 <= j < n)
    return sorted(keep)


def _split_page(i: int, text, limit: int) -> list[str]:
    """한 쪽을 limit 이하 조각으로. 한 쪽이 창보다 크면 그 쪽만 쪼갠다.

    예전엔 쪽 하나가 limit 을 넘으면 그대로 한 창이 되어 40003(Context length) 이 났다.
    (2026-09-05 리뷰) 조각마다 같은 쪽 머리말 + "(part k)" 를 달아 인용 쪽수를 지킨다.
    """
    text = text or ""
    plain = f"---PAGE {i + 1}---\n{text}\n\n"
    if len(plain) <= limit:
        return [plain]
    out: list[str] = []
    pos, k = 0, 1
    while pos < len(text):
        head = f"---PAGE {i + 1}--- (part {k})\n"
        room = max(1, limit - len(head) - 2)
        out.append(head + text[pos:pos + room] + "\n\n")
        pos += room; k += 1
    return out


def _windows(pages, idxs: list[int], limit: int) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for i in idxs:
        for chunk in _split_page(i, pages[i].text, limit):
            if buf and size + len(chunk) > limit:
                out.append("".join(buf)); buf, size = [], 0
            buf.append(chunk); size += len(chunk)
    if buf:
        out.append("".join(buf))
    return out


def _as_conf(value):
    """신뢰도를 float 로. 숫자가 아니면 None (bool 은 배제 — True → 1.0 사고 방지)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def merge_results(parts: list[dict], attempted: Optional[int] = None) -> dict:
    """창별 결과를 합친다. **파싱 실패한 창은 조용히 사라지면 안 된다.**

    예전엔 응답 파싱에 실패한 창을 그냥 버리고 살아남은 창만으로 needs_review 를 셌다.
    절차서를 절반만 읽고도 "이 문서는 PT 에 침묵한다" 는 결론이 나와 고발 근거로 쓰였다.
    attempted(시도한 창 수)를 받아 하나라도 잃었으면 재확인 대상으로 못 박는다. (2026-09-05 리뷰)
    """
    lost = (attempted - len(parts)) if attempted is not None else 0
    fail_reasons: list[str] = []
    if lost > 0:
        fail_reasons.append(
            f"LLM 응답 파싱 실패 창 {lost}/{attempted} — 문서 일부를 읽지 못했으므로 근거 판정에 쓰지 않는다")
    if not parts:
        return {"needs_review": True,
                "review_reasons": fail_reasons or [
                    "LLM 응답을 한 창도 파싱하지 못했다 — 문서를 읽지 못했으므로 근거 판정에 쓰지 않는다"]}
    merged = dict(parts[0])
    for key in _MERGE_KEYS:
        seen: set = set()
        items: list[dict] = []
        for p in parts:
            for r in p.get(key) or []:
                if not isinstance(r, dict):
                    continue
                k = (str(r.get("ndt_method")).upper(), r.get("page"), (r.get("quote") or "")[:40])
                if k in seen:
                    continue
                seen.add(k); items.append(r)
        merged[key] = items
    merged["needs_review"] = bool(lost) or any(bool(p.get("needs_review")) for p in parts)
    merged["review_reasons"] = fail_reasons + [r for p in parts for r in (p.get("review_reasons") or [])]
    confs = [c for c in (_as_conf(p.get("extraction_confidence")) for p in parts) if c is not None]
    if confs:
        merged["extraction_confidence"] = min(confs)   # 가장 낮은 창이 문서 전체의 신뢰도
    return merged


def verify_quotes(parsed: dict, page_texts: dict) -> dict:
    """규칙마다 인용문이 그 쪽 원문에 실재하는지 확인해 quote_verified 를 붙인다.

    면책(과다청구 아님)의 근거가 되는 문장이므로 적재 시점에 한 번 대조해 둔다 — 판정 때는 원문이 없다.
    쪽을 안 댔거나 그 쪽 원문이 없으면 None(모름). 확인 실패는 False 이고 재확인 사유가 붙는다.
    (2026-09-07 파생 검토: 지어낸 인용으로 면책이 성립하던 구멍)
    """
    from app.analyzers.extraction_guard import verify_quote
    if not isinstance(parsed, dict):
        return parsed
    reasons: list[str] = []
    for key in ("conditional_ndt_requirements", "default_sampling_rates", "general_rules"):
        for rule in parsed.get(key) or []:
            if not isinstance(rule, dict):
                continue
            src = (page_texts or {}).get(rule.get("page"))
            if not rule.get("quote") or src is None:
                rule["quote_verified"] = None
                continue
            ok, ratio = verify_quote(rule.get("quote"), src)
            rule["quote_verified"] = bool(ok)
            rule["quote_match_ratio"] = ratio
            if not ok:
                reasons.append(
                    f"{rule.get('rule_id') or rule.get('ndt_method')}: 쪽 {rule.get('page')} 원문에서 "
                    f"인용문을 확인하지 못함 (유사도 {ratio:.2f}) — 면책 근거로 쓰지 않는다")
    if reasons:
        parsed["needs_review"] = True
        parsed["review_reasons"] = list(parsed.get("review_reasons") or []) + reasons
        logger.warning("SCWEP 인용문 검증 실패 %d건: %s", len(reasons), reasons[0])
    return parsed


def ingest(file_path: Path) -> dict:
    file_path = Path(file_path)
    extracted = extract(file_path)
    # 표 페이지(검사 범위·샘플링률 표)는 HCX-005 전사 + OCR 꼬리로 대체 — 규격(③)과 같은 규칙 (2026-09-07 감사 2번)
    records, table_stats = page_records(file_path, extracted)
    by_idx = {r["page"] - 1: r for r in records if r.get("source") == "vlm_table"}
    pages = [PageText(page_index=p.page_index,
                      text=(by_idx[p.page_index].get("text") or p.text) if p.page_index in by_idx else p.text,
                      source=("vlm_table" if p.page_index in by_idx else getattr(p, "source", "ocr")),
                      ocr_variants=list(getattr(p, "ocr_variants", None) or []),
                      confidence=getattr(p, "confidence", None)) for p in extracted.pages]
    if table_stats.get("table_like"):
        logger.info("표 합계 %s: 표페이지 %d · 전사 %d · 재확인 %d · vlm 실패 %d", file_path.name,
                    table_stats.get("table_like", 0), table_stats.get("transcribed", 0),
                    table_stats.get("needs_review", 0), table_stats.get("vlm_failed", 0))
    limit = int((hcx_config().get("scwep") or {}).get("window_chars", 60_000))
    idxs = select_pages(pages) or list(range(min(len(pages), 30)))
    windows = _windows(pages, idxs, limit)
    parts: list[dict] = []
    for i, text_full in enumerate(windows):
        resp = call("scwep_extract", {"document_no": file_path.stem, "revision": None,
                                      "text_full": text_full, "window": i + 1})
        if resp.parsed:
            parts.append(resp.parsed)
    parsed = merge_results(parts, attempted=len(windows))
    if parsed:
        parsed = verify_quotes(parsed, {p.page_index + 1: (p.text or "") for p in pages})
        parsed.setdefault("_schema_version", SCWEP_SCHEMA_VERSION)   # 파서가 직접 각인 — 근거 게이트의 열쇠
        parsed.setdefault("document_no", file_path.stem)
        from app.extractors import kks
        k = kks.parse(file_path.stem)
        if k:
            # LLM 이 applicable_scope 를 문자열·리스트로 내도 죽지 않는다 (2026-09-05 리뷰)
            scope = parsed.get("applicable_scope")
            if not isinstance(scope, dict):
                scope = {}
                parsed["applicable_scope"] = scope
            scope["kks"] = {"raw": k["raw"], "building": k["building"], "systems": [k["system_code"]]}
        parsed["_windows"] = len(parts)
        parsed["_windows_attempted"] = len(windows)
        parsed["_pages_sent"] = [i + 1 for i in idxs]

    init_db()
    with get_session() as s:
        upsert_standard(s, file_path=str(file_path), doc_type="scwep",
                        document_no=parsed.get("document_no"), revision=parsed.get("revision"),
                        extracted_json=parsed)
        s.commit()
    return parsed
