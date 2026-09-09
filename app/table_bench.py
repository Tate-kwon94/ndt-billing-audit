"""표 전사 정확도 벤치 — 정답표로 모델을 재는 도구.

2026-09-07: "멀티모달이 tesseract 보다 낫다" 는 사외에서 사람이 읽은 결과였고, 사내 HCX-005 가 같은지는
확인된 적이 없다. 도면1 p.13 (검사 범위표) 의 정답을 만들어 두었으므로 이제 측정이 가능하다.
vision-probe 로 비전 가능한 모델을 찾고, 그 모델들에 같은 이미지를 보내 이 표로 채점한다.

채점 기준:
  kks_found    정답 KKS 10개 중 전사본에서 찾은 수 (1↔I, 0↔O 혼동은 정규화 후 비교)
  rows_matched 그 줄의 값 8개가 **전부** 맞은 행 수 (부분 정답은 안 쳐준다 — 판정에 쓰이는 값이다)
  wrong_cells  값이 있는데 틀린 칸 수 (빠뜨린 것보다 나쁘다 — 조용히 틀린 근거가 된다)
  accuracy     맞은 칸 / 전체 칸
"""
from __future__ import annotations

import json
import logging
import pathlib
import re
from pathlib import Path
from typing import Optional

from app.extractors.pdf_extractor import extract

logger = logging.getLogger(__name__)

_VALUE_COLS = ("dout_x_thickness_mm", "document", "vt_pct", "pt_or_mt_pct",
               "rt_pct", "ut_pct", "aux_vt_pct", "aux_pt_or_mt_pct")


def load_truth(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _norm_kks(s: str) -> str:
    """KKS 비교용 정규화: 글꼴 혼동(I→1, O→0, l→1)과 공백 제거."""
    return (str(s).upper().replace(" ", "").replace("I", "1").replace("L", "1")
            .replace("O", "0").replace("|", "1"))


def _norm_val(s) -> str:
    """값 비교용: 공백·대시 종류·곱셈기호를 통일하고 소문자화."""
    t = str(s).strip().lower().replace(" ", "")
    t = t.replace("—", "-").replace("–", "-").replace("~", "-").replace("−", "-")
    t = t.replace("×", "x").replace("*", "x")
    return t


# 규격 이름만 뽑아 비교한다 — 'SN 527-80 / SNIP 3.05.05-84/VV' 와 'SN 527-80 SNiP 3.05.05-84/ VV' 는
# 같은 두 규격을 가리킨다. 구분자·대소문자 차이로 모델을 깎으면 채점기가 거짓말을 한다 (2026-09-08 실물).
_CODE_TOKEN = re.compile(r"[A-Za-z]{2,6}\s*[\d.]{2,}(?:[-–]\d+)?")


def _doc_codes(text) -> set:
    return {re.sub(r"\s+", "", c).lower() for c in _CODE_TOKEN.findall(str(text or ""))}


def _row_cells(line: str) -> list[str]:
    """마크다운 표 한 줄 → 칸 목록. 파이프가 없으면 공백 2칸 이상으로 나눈다."""
    if "|" in line:
        return [c.strip() for c in line.strip().strip("|").split("|")]
    return [c for c in re.split(r"\s{2,}", line.strip()) if c]


def score(transcript: str, truth: dict) -> dict:
    """전사본(마크다운 표 또는 평문)을 정답과 대조."""
    rows = truth["rows"]
    lines = [l for l in (transcript or "").splitlines() if l.strip()]
    by_kks: dict[str, list[str]] = {}
    for line in lines:
        cells = _row_cells(line)
        for c in cells:
            k = _norm_kks(c)
            if re.fullmatch(r"\d{2}[A-Z]{3}\d{2}BR\d{3}", k):
                by_kks.setdefault(k, cells)
                break
    kks_found = rows_matched = wrong = right = 0
    total = len(rows) * len(_VALUE_COLS)
    by_col = {c: 0 for c in _VALUE_COLS}
    for r in rows:
        cells = by_kks.get(_norm_kks(r["kks"]))
        if cells is None:
            continue
        kks_found += 1
        # 칸 수가 정답과 같으면 **위치로** 비교한다. 집합 비교는 PT 를 100 으로 잘못 읽어도
        # 옆 칸의 '-' 때문에 맞았다고 세어 버린다 (조용한 오답 = 이 프로그램이 잡으려는 바로 그 결함).
        kpos = next((i for i, c in enumerate(cells)
                     if re.fullmatch(r"\d{2}[A-Z]{3}\d{2}BR\d{3}", _norm_kks(c))), 0)
        positional = cells[kpos + 1: kpos + 1 + len(_VALUE_COLS)]
        got = {_norm_val(c) for c in cells}
        ok = 0
        for i, col in enumerate(_VALUE_COLS):
            want = _norm_val(r[col])
            if len(positional) == len(_VALUE_COLS):
                g = _norm_val(positional[i])
                if col == "document":
                    want_codes, got_codes = _doc_codes(r[col]), _doc_codes(positional[i])
                    hit = bool(want_codes) and want_codes <= got_codes
                else:
                    hit = g == want
            else:   # 칸 수가 다르면 순서를 믿을 수 없다 — 느슨하게 집합 비교
                if col == "document":
                    want_codes = _doc_codes(r[col])
                    hit = bool(want_codes) and want_codes <= _doc_codes(" ".join(cells))
                else:
                    hit = want in got
            if hit:
                ok += 1
                by_col[col] += 1
            else:
                wrong += 1
        right += ok
        if ok == len(_VALUE_COLS):
            rows_matched += 1
    # 실제로 행을 잃은 열만 (다 맞은 열을 '틀린 열' 로 보여주면 화면이 거짓말을 한다)
    worst = [c for c in sorted(_VALUE_COLS, key=lambda c: by_col[c]) if by_col[c] < len(rows)]
    return {"kks_found": kks_found, "rows_matched": rows_matched, "wrong_cells": wrong,
            "right_cells": right, "total_cells": total,
            "accuracy": round(right / total, 4) if total else 0.0,
            # 열별로 몇 행이 맞았나. 특정 열만 통째로 틀리면 모델 오독이 아니라 **표 자체가 다른 것**
            # (개정 차이) 이거나 표기 정규화 문제다 — 2026-09-08 사내 gemma 37.5% 진단용.
            "by_column": by_col, "worst_columns": worst[:3]}


_DRAWING_CORE = re.compile(r"NP\.D\.[A-Z0-9&.]+?\.DC\.\d{4}")


def locate_pdf(truth: dict, *, search_dirs=None) -> Path:
    """정답 파일이 가리키는 도면 PDF 를 찾는다 — 경로가 그대로 있으면 그것, 없으면 **도면번호**로 검색.

    정답 파일의 source 는 사외 Mac 의 파일명('4. NP.D…')이고 사내는 '[ABD]NP.D…' 처럼 접두가 다르다 (2026-09-08).
    파일명이 아니라 도면번호 핵심(NP.D.….DC.0001)이 들어 있는 파일을 찾는다.
    """
    src = Path(str(truth.get("source") or ""))
    if src.is_file():
        return src
    core = _DRAWING_CORE.search(src.name)
    dirs = [Path(d) for d in (search_dirs or [Path("samples/drawings"), Path("samples")])]
    if core:
        key = core.group(0).lower()
        for d in dirs:
            if not d.exists():
                continue
            for cand in sorted(d.rglob("*.pdf")):
                if key in cand.name.lower():
                    return cand
    raise FileNotFoundError(
        f"정답 파일의 도면 PDF 를 못 찾음: {src.name} (도면번호 {core.group(0) if core else '?'} 를 "
        f"{', '.join(str(d) for d in dirs)} 에서 검색). --pdf 로 파일을 직접 지정할 것.")


_DOC_CODE = re.compile(r"M[A-Z]{2}\d{4}")


def locate_page(pdf_path, truth: dict) -> int:
    """정답이 가리키는 **쪽**을 찾는다 — 쪽 번호가 아니라 문서코드(MJZ0001)와 표 제목으로.

    2026-09-08 사내: (1) 도면 개정이 달라 같은 13쪽이 다른 표였고, (2) 교체한 C02 파일에는 13쪽이 아예 없어
    pdfium 이 `Failed to load page` 로 죽었다. 쪽 번호는 문서마다 다르다 — 내용으로 찾고, 못 찾으면
    **어떤 문서를 봤는지 알려주고 멈춘다**. 엉뚱한 쪽을 읽어 0% 를 내는 것보다 낫다.
    """
    marker = str(truth.get("page_marker") or "").upper().replace(" ", "")
    title = str(truth.get("title") or "").lower()
    fallback = int(truth.get("page") or 1)
    try:
        ex = extract(pathlib.Path(pdf_path))
    except Exception as e:      # noqa: BLE001
        logger.warning("쪽 표식 검색 실패 (%s) — 정답의 쪽 번호 %d 사용", e, fallback)
        return fallback
    pages = ex.pages
    n = len(pages)
    seen: set = set()
    for pg in pages:
        t = (pg.text or "")
        flat = t.upper().replace(" ", "")
        seen.update(_DOC_CODE.findall(flat))
        if marker and marker in flat:
            return pg.page_index + 1
    # 문서코드가 OCR 에서 깨졌을 수 있다 — 표 제목으로 한 번 더
    if title:
        key = re.sub(r"\s*\(.*?\)\s*", "", title).strip()
        for pg in pages:
            if key and key in (pg.text or "").lower():
                logger.info("문서코드 %s 는 못 찾았으나 표 제목으로 %d쪽 확인", marker, pg.page_index + 1)
                return pg.page_index + 1
    if 1 <= fallback <= n:
        logger.warning("쪽 표식 %s 를 못 찾음 — 정답의 쪽 번호 %d 사용 (그 문서에서 본 문서코드: %s)",
                       marker, fallback, ", ".join(sorted(seen)) or "없음")
        return fallback
    logger.warning("쪽 표식 %s 를 못 찾음. 그 문서에서 본 문서코드: %s",
                   marker, ", ".join(sorted(seen)) or "없음")
    raise ValueError(
        f"{pathlib.Path(pdf_path).name} 은 {n}쪽뿐이라 정답의 {fallback}쪽이 없고, 표식 {marker} 도 못 찾았다. "
        f"그 문서에서 본 문서코드: {', '.join(sorted(seen)) or '없음'}. "
        f"같은 도면이 맞는지 확인하거나 --page 로 쪽을 직접 지정할 것.")


def run(truth_path, providers: list[str], *, pdf: Optional[str] = None,
        page: Optional[int] = None, dpi: int = 300, out_dir=None) -> list[dict]:
    """정답표의 원본 쪽을 렌더해 provider 마다 전사시키고 채점한다. 사내에서만 의미가 있다."""
    import base64
    import time

    import app.hcx_client as hc
    from app.config import hcx_config
    from app.extractors.table_transcriber import extract_markdown_table

    truth = load_truth(truth_path)
    src = Path(pdf) if pdf else locate_pdf(truth)
    pno = int(page) if page else locate_page(src, truth)
    # 운영과 **같은 렌더 경로**를 쓴다 — 벤치가 다른 크기를 보내면 운영에서 나는 오류를 못 재현한다.
    from app.extractors.vision_verifier import render_page_png
    png = render_page_png(src, pno - 1)
    if png is None:
        raise RuntimeError(f"쪽 렌더 실패: {src.name} p{pno}")
    b64 = base64.b64encode(png).decode("ascii")
    logger.info("표 벤치 입력: %s p%d · PNG %.1fMB", src.name, pno, len(png) / 1e6)
    from app.config import DATA_DIR
    dest = pathlib.Path(out_dir) if out_dir else (DATA_DIR / "outputs" / "table_bench")
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as e:      # noqa: BLE001
        logger.warning("전사본 저장 폴더를 못 만듦 (%s) — 화면 출력만", e)
        dest = None
    prompt = hc.load_prompt("table_transcribe")
    cfg = hcx_config()
    out = []
    for name in providers:
        row = {"provider": name, "model": None, "ms": None, "error": "", "png_mb": round(len(png) / 1e6, 2)}
        try:
            p = hc.resolve_provider_by_name(name)      # 기본값(retry·timeout) 채움 — 사내 2차 KeyError 'retry' 의 원인
        except KeyError as e:
            row["error"] = str(e).strip("'\"")
            out.append(row)
            continue
        row["model"] = p.get("model")
        t0 = time.time()
        try:
            from app.extractors.vision_render import send_with_shrink, shrink_png
            import base64 as _b64
            api, used = send_with_shrink(
                lambda budget: shrink_png(png, budget),
                lambda img: hc._post_chat(p.get("model"), prompt,
                                          json.dumps({"page": pno}, ensure_ascii=False),
                                          [_b64.b64encode(img).decode("ascii")],
                                          stage="table_transcribe", provider=p),
            )
            row["png_mb"] = round(used / 1e6, 2)
            content = hc._extract_content(api) or ""
            md = extract_markdown_table(content) or content
            row.update(score(md, truth))
            row["head"] = md[:120]
            if dest is not None:      # 전사본 원문을 남긴다 — 정답과 눈으로 대조해야 개정 차이를 가린다
                f = dest / f"{name}.md"
                f.write_text(md, encoding="utf-8")
                row["transcript"] = str(f)
        except Exception as e:      # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {e}"
        row["ms"] = round((time.time() - t0) * 1000)
        logger.info("표 벤치 %s: %s", name, row.get("accuracy", row["error"]))
        out.append(row)
    return out
