"""표 페이지 전사 파이프라인 — 분류 → VLM 전사 → OCR 숫자 대조 → 신뢰도.

왜 (2026-09-03 실측)
    규격서·도면·성적서 모두에서 글자 인식은 충분한데 **표가 조용히 깨진다.**
    PNAEG 7-010-89 p.46: tesseract psm3 가 9열 중 8열을 오류 없이 떨어뜨렸다.
    psm6 는 숫자를 다 살렸지만 한 줄로 뭉개 열 의미를 잃었다. Adobe 도 표를 평탄화한다.
    → 표는 이미지 입력 모델(HCX-005 / gemma4-31b)이 셀 단위로 옮기고,
      그 결과의 숫자를 OCR 변형들의 숫자 집합과 대조해 환각을 막는다.

세 단계
    1. table_score : tesseract 단어 박스로 "칸 나뉜 줄" 비율. 실측 —
       본문 0.00 · 표지 0.11 · 양식 0.33~0.42 · 표/등각도 0.67~0.81.
    2. transcribe  : call_vision("table_transcribe") → Markdown 표.
    3. digit_agreement : 전사문의 숫자 토큰이 OCR 변형(psm6 등)의 숫자 집합에
       들어 있는 비율. 없는 숫자 = 지어낸 숫자 → needs_review.

원칙
    - VLM 이 "표 아님" 이라 하면 OCR 텍스트를 그대로 둔다.
    - 임베딩·VLM 어느 것이 죽어도 적재는 멈추지 않는다 (OCR 텍스트로 후퇴).
    - 낮은 신뢰도의 표에서 나온 인용은 판정 근거가 아니라 needs_review 다.
"""
from __future__ import annotations

import logging
import re
import statistics
from collections import Counter, defaultdict
from typing import Optional

from app.config import hcx_config

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "enabled": True,
    "min_table_score": 0.5,       # 이 이상이면 표 페이지로 보고 VLM 전사
    "min_digit_agreement": 0.9,   # 이 미만이면 needs_review
    "min_multiword_lines": 6,     # 다단어 줄이 이보다 적으면 점수 신뢰 안 함(빈 페이지 등)
    "score_dpi": 150,
    "lang": "eng",
    "keep_ocr_text": True,        # 전사 뒤에 OCR 평문을 덧붙여 검색 재현율 확보
    "ocr_tail_chars": 1500,
    "reference_ocr": True,        # 표 페이지는 텍스트레이어가 있어도 OCR 을 한 번 더 (아래 주석)
    "reference_psms": ["6", "4"],
}


def config() -> dict:
    return {**_DEFAULTS, **(hcx_config().get("table_pipeline") or {})}


# ─────────────────────────── 1. 분류 ───────────────────────────

def table_score(pil_image, *, lang: Optional[str] = None) -> dict:
    """페이지가 표처럼 생겼는가. {score, gappy_lines, multiword_lines}.

    줄 안에서 단어 사이 간격이 중앙 글자높이의 3배를 넘는 곳이 2개 이상이면
    '칸 나뉜 줄'. 본문은 양쪽 정렬이라 간격이 고르고, 표·양식은 칸마다 비어 있다.
    """
    from app.extractors.ocr_engine import _configure_pytesseract
    _configure_pytesseract()
    import pytesseract

    c = config()
    d = pytesseract.image_to_data(pil_image, lang=lang or c["lang"], config="--psm 6",
                                  output_type=pytesseract.Output.DICT)
    lines: dict = defaultdict(list)
    heights: list[int] = []
    for i, txt in enumerate(d["text"]):
        if not str(txt).strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines[key].append((d["left"][i], d["left"][i] + d["width"][i]))
        heights.append(d["height"][i])
    if not lines:
        return {"score": 0.0, "gappy_lines": 0, "multiword_lines": 0}
    ch = statistics.median(heights) or 10
    gappy = multi = 0
    for words in lines.values():
        if len(words) < 3:
            continue
        multi += 1
        words.sort()
        gaps = sum(1 for a, b in zip(words, words[1:]) if b[0] - a[1] > 3 * ch)
        if gaps >= 2:
            gappy += 1
    return {"score": (gappy / multi) if multi else 0.0, "gappy_lines": gappy, "multiword_lines": multi}


# ─────────────────────────── 3. 숫자 대조 ───────────────────────────

_NUM_RE = re.compile(r"(?<![\d.])[+-]?\d+(?:[.,]\d+)*(?!\d)")


def numeric_tokens(text: str) -> Counter:
    """텍스트의 숫자 토큰 다중집합.
    '1,250' → '1250' (천단위: 선행 1~3자리 + 3자리 그룹, 선행이 0 이 아님).
    '0,125' → '0.125' (러시아 소수점 쉼표 — 선행 0 이거나 그룹이 3자리가 아니면 소수점).
    끝 '.' 제거."""
    out: Counter = Counter()
    text = text or ""
    for m in _NUM_RE.finditer(text):
        before = text[m.start() - 1] if m.start() > 0 else ""
        after = text[m.end()] if m.end() < len(text) else ""
        if before.isalpha() and after.isalpha():
            # 글자 사이에 낀 숫자(UGB95GML, KE0001E)는 식별자의 일부지 측정값이 아니다 — 대조 제외.
            # 한쪽만 글자인 것(DN100, 25mm)은 단위·접두가 붙은 측정값이라 남긴다. (2026-09-05 리뷰)
            continue
        t = m.group().lstrip("+")
        if re.fullmatch(r"-?[1-9]\d{0,2}(,\d{3})+(\.\d+)?", t):
            t = t.replace(",", "")
        else:
            t = t.replace(",", ".")
        t = t.rstrip(".")
        if t and t != "-":
            out[t] += 1
    return out


_SEP_ROW = re.compile(r"^\s*\|?\s*:?-{2,}")


def _strip_table_headers(md: str) -> str:
    """Markdown 표의 헤더 행(구분선 `|---|` 바로 위 행)을 뺀다.
    '(continued)' 표는 열 번호 1, 2, 3… 을 제목으로 쓰는데 이건 OCR 평문에 없어
    억울하게 '지어낸 숫자' 로 잡히기 때문이다. 본문 행의 숫자는 그대로 대조한다."""
    lines = (md or "").splitlines()
    drop = {i - 1 for i, ln in enumerate(lines) if i > 0 and _SEP_ROW.match(ln)}
    return "\n".join(ln for i, ln in enumerate(lines) if i not in drop)


def digit_agreement(candidate: str, references: list[str]) -> dict:
    """전사문(candidate)의 숫자가 OCR 변형들(references)의 숫자 집합에 있는 비율.

    reference 는 변형별 다중집합의 **최댓값 합집합** — psm6 가 살린 숫자와 psm3 가
    살린 숫자를 모두 인정한다. candidate 에만 있는 숫자 = 지어낸 후보.
    """
    cand = numeric_tokens(_strip_table_headers(candidate))
    ref: Counter = Counter()
    for r in references or []:
        for tok, n in numeric_tokens(r).items():
            ref[tok] = max(ref[tok], n)
    if not cand:
        return {"ratio": 1.0, "candidate_total": 0, "matched": 0, "invented": [], "missing": [],
                "unverified": True}
    matched = sum(min(n, ref.get(tok, 0)) for tok, n in cand.items())
    total = sum(cand.values())
    invented = sorted(tok for tok, n in cand.items() if ref.get(tok, 0) < n)
    missing = sorted(tok for tok, n in ref.items() if cand.get(tok, 0) < n)
    return {"ratio": matched / total, "candidate_total": total, "matched": matched,
            "invented": invented, "missing": missing, "unverified": False}


def _ocr_reference(pil_image, *, lang: str, psms) -> list[str]:
    """표 페이지 숫자 대조용 **2차 읽기**. 텍스트레이어가 있어도 반드시 한 번 더 OCR 한다.

    왜 (2026-09-03 사내 첫 실행이 드러낸 것)
        GOST 23118-2019 p35 전사가 conf=0.16, "지어낸 후보 43개" 로 찍혔다.
        같은 페이지를 사외에서 재현해 보니 —
            텍스트레이어 숫자  20개
            tesseract 숫자   147개
        레이어 자체가 표의 숫자를 떨어뜨리고 있었다. 그게 이 파이프라인을 만든 이유인데,
        정작 대조 기준을 그 레이어로만 잡아서 **VLM 이 제대로 읽은 숫자를 환각으로 몰았다.**
        기준이 눈을 감고 있으면 신뢰도는 신뢰도가 아니다. 그래서 표 페이지에서는
        레이어와 OCR 을 **합집합**으로 기준 삼는다 (레이어가 더 좋은 페이지도 있다 —
        같은 문서 p27 은 레이어 45 vs OCR 31).
    """
    from app.extractors.ocr_engine import _configure_pytesseract
    _configure_pytesseract()
    import pytesseract

    out: list[str] = []
    for psm in psms or ("6",):
        try:
            out.append(pytesseract.image_to_string(pil_image, lang=lang, config=f"--psm {psm}"))
        except Exception as e:      # noqa: BLE001 - 기준이 하나라도 있으면 진행한다
            logger.warning("대조용 OCR 실패 (psm %s): %s: %s", psm, type(e).__name__, e)
    return out


# ─────────────────────────── 2. VLM 전사 ───────────────────────────

_FENCE_RE = re.compile(r"```(?:markdown|md)?\s*\n(.*?)```", re.S | re.I)


def extract_markdown_table(content: str) -> Optional[str]:
    """응답에서 Markdown 표 부분만. '표 여부: 아니오' 면 None."""
    if not content:
        return None
    head = content[:200]
    if re.search(r"표\s*여부\s*[:：]\s*아니", head):
        return None
    blocks = [b.strip() for b in _FENCE_RE.findall(content) if "|" in b]
    if blocks:
        return "\n\n".join(blocks)
    rows = [ln for ln in content.splitlines() if ln.strip().startswith("|")]
    return "\n".join(rows) if len(rows) >= 2 else None


def transcribe_page(png_bytes: bytes, *, doc_hint: str, page_no: int, ocr_text: str = "") -> dict:
    """VLM 호출 1회. 실패하면 markdown=None 으로 돌려주고 예외를 삼킨다."""
    import app.hcx_client as hc
    from app.extractors.vision_render import send_with_shrink, shrink_png
    payload = {"doc_hint": doc_hint, "page": page_no, "ocr_text": (ocr_text or "")[:3000]}
    try:
        # HCX-005 는 큰 이미지를 code=40063 "Invalid image size" 로 거부한다 (2026-09-08 사내 확정:
        # 1.41MB 거부, 도면4 에서 34쪽 중 30쪽이 이 이유로 실패). 거부당하면 줄여서 다시 보낸다.
        resp, used = send_with_shrink(
            lambda budget: shrink_png(png_bytes, budget),
            lambda png: hc.call_vision("table_transcribe", payload, [png]),
        )
    except Exception as e:      # noqa: BLE001 - 적재를 멈추지 않는다
        logger.warning("표 전사 VLM 호출 실패 p%s: %s: %s", page_no, type(e).__name__, e)
        return {"markdown": None, "raw": "", "model": None, "error": f"{type(e).__name__}: {e}"}
    md = extract_markdown_table(resp.content or "")
    return {"markdown": md, "raw": resp.content or "", "model": resp.model, "error": None}


# ─────────────────────────── 조립 ───────────────────────────

def process_page(pil_image, png_bytes: Optional[bytes], ocr_variants: list[str], *,
                 doc_hint: str, page_no: int, ocr_text: str = "",
                 score: Optional[dict] = None) -> dict:
    """한 페이지에 대해 표 파이프라인 전체. 항상 dict 를 돌려주며 예외를 밖으로 내지 않는다.

    score: table_score() 결과를 이미 갖고 있으면 넘긴다 (적재 경로는 PNG 렌더 전에
           점수를 먼저 보므로 두 번 계산하지 않는다).
    반환 키: is_table_like, table_score, markdown, confidence, needs_review, invented, model, note
    """
    c = config()
    base = {"is_table_like": False, "table_score": 0.0, "markdown": None, "confidence": None,
            "needs_review": False, "invented": [], "model": None, "note": ""}
    if not c.get("enabled"):
        return {**base, "note": "table_pipeline 비활성"}
    if score is None:
        try:
            score = table_score(pil_image, lang=c["lang"])
        except Exception as e:      # noqa: BLE001
            return {**base, "note": f"table_score 실패: {type(e).__name__}: {e}"}
    sc = score
    base["table_score"] = round(sc["score"], 3)
    if sc["multiword_lines"] < int(c["min_multiword_lines"]) or sc["score"] < float(c["min_table_score"]):
        return base
    base["is_table_like"] = True
    if not png_bytes:
        return {**base, "note": "페이지 이미지 없음 — OCR 텍스트 유지"}

    tr = transcribe_page(png_bytes, doc_hint=doc_hint, page_no=page_no, ocr_text=ocr_text)
    base["model"] = tr.get("model")
    if tr.get("error"):
        return {**base, "note": f"VLM 실패 — OCR 텍스트 유지: {tr['error']}"}
    if not tr.get("markdown"):
        return {**base, "note": "VLM 판단: 표 아님 — OCR 텍스트 유지"}

    refs = list(ocr_variants or [])
    if ocr_text and ocr_text not in refs:
        refs.append(ocr_text)
    if c.get("reference_ocr", True):
        refs.extend(_ocr_reference(pil_image, lang=c["lang"], psms=c.get("reference_psms")))
    ag = digit_agreement(tr["markdown"], refs)
    conf = round(ag["ratio"], 3)
    unverified = bool(ag.get("unverified"))
    return {**base, "markdown": tr["markdown"], "confidence": (0.0 if unverified else conf),
            "needs_review": unverified or conf < float(c["min_digit_agreement"]),
            "invented": ag["invented"][:20],
            "note": ("숫자 없음 — OCR 대조 불가, 재확인 대상" if unverified else
                     f"숫자 대조 {ag['matched']}/{ag['candidate_total']}"
                     + (f", 지어낸 후보 {len(ag['invented'])}개" if ag["invented"] else ""))}
