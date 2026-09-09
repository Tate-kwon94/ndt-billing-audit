"""PDF 텍스트 추출 — 텍스트 레이어 우선, 부재 시 OCR 으로 폴백.

per-page 결과를 dict 로 반환해 후속 모듈(분할기, 정규화기)이 자유롭게 사용한다.

캐시 2단계:
  1. 메모리 (한 run 안 재호출)
  2. 디스크 (data/ocr_cache/<sha256>.json) — OCR 비용이 거대(수시간) 하므로 영구 저장
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PageText:
    page_index: int                  # 0-based
    text: str                        # 추출된 텍스트
    source: str                      # "text_layer" | "ocr"
    ocr_variants: list[str] = field(default_factory=list)  # 다중 파라미터 OCR 결과
    confidence: Optional[float] = None  # OCR 신뢰도 평균


@dataclass
class ExtractedPDF:
    path: Path
    pages: list[PageText]
    text_layer_present: bool


# ─────────────────────────── Detection ───────────────────────────


def _has_meaningful_text_layer(pdf_path: Path, min_chars_per_page: int = 40) -> bool:
    """첫 3페이지를 샘플링해 텍스트 레이어 유의미 여부 판단."""
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        sample_count = min(3, len(pdf.pages))
        if sample_count == 0:
            return False
        char_counts = []
        for i in range(sample_count):
            text = pdf.pages[i].extract_text() or ""
            char_counts.append(len(text.strip()))
    return any(c >= min_chars_per_page for c in char_counts)


# ── 텍스트레이어 품질 채점 (Adobe-good 신뢰 / mediocre 깨진 레이어 재OCR) ──
# 측정 근거: 배포본 CRST 성적서는 텍스트레이어가 있으나 KKS 코드 82% 깨짐
# (IOPAB→10PAB, BROOS→BR005, 밑줄 붕괴). "있으면 신뢰" 가 아니라 품질로 판정해야 함.
import re as _re

# 정상(canonical) 단축코드 패턴
# rule_engine.KKS_CODE_RE 와 정합: 시스템코드 2~4자 허용 + MR 접미(용접부 연장) 허용.
# 과제약 시 정상 KKS 를 garbled 로 오분류 → 불필요한 재OCR 트리거 (다중에이전트 분석 확인).
_RE_KKS_CLEAN = _re.compile(r"\b\d{2}[A-Z]{2,4}\d{2,3}[A-Z]{2}\d{3}(?:MR\d{3})?\b")
_RE_KKS_LABEL = _re.compile(r"KKS\s*code", _re.I)
# 구조 붕괴 신호: 밑줄/대시 런, _X_X_X 같은 단일문자 분절
_RE_STRUCT_COLLAPSE = _re.compile(r"_{2,}|-{4,}|(?:_\w){3,}")


def _text_layer_quality(pdf_path: Path, sample: int = 12) -> tuple[float, dict]:
    """임베드 텍스트레이어 신뢰도 0~1 (낮을수록 재OCR 권장).

    도메인 신호: KKS code 필드의 well-formed 비율 + 구조붕괴(밑줄/대시) 감점.
    Adobe-good 본은 ~0.9+, 배포본 mediocre OCR 은 ~0.1~0.3.
    """
    import pdfplumber

    clean = garbled = 0
    collapse_lines = total_lines = 0
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        if n == 0:
            return 0.0, {"reason": "empty"}
        k = min(sample, n)
        idxs = sorted({int(i * n / k) for i in range(k)})
        for i in idxs:
            text = pdf.pages[i].extract_text() or ""
            for line in text.splitlines():
                total_lines += 1
                if _RE_STRUCT_COLLAPSE.search(line):
                    collapse_lines += 1
                if _RE_KKS_LABEL.search(line):
                    if _RE_KKS_CLEAN.search(line):
                        clean += 1
                    else:
                        garbled += 1

    field_total = clean + garbled
    collapse_ratio = (collapse_lines / total_lines) if total_lines else 0.0
    if field_total >= 3:
        q = (clean / field_total) - min(0.3, collapse_ratio)
    else:
        # KKS 필드 못 찾음 (도면·표준 등) → 구조붕괴만으로 추정
        q = 1.0 - min(1.0, collapse_ratio * 4)
    q = max(0.0, min(1.0, q))
    return q, {
        "clean_kks": clean, "garbled_kks": garbled,
        "collapse_ratio": round(collapse_ratio, 3),
        "sampled_pages": len(idxs),
    }


def _has_scan_images(pdf_path: Path, sample: int = 6) -> bool:
    """샘플 페이지에 (스캔으로 추정되는) 이미지가 있는가.

    born-digital(벡터 텍스트) PDF 는 이미지 없음 → 재OCR 무의미.
    OCR-over-scan PDF 는 페이지마다 full-page 이미지 존재.
    """
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        if n == 0:
            return False
        k = min(sample, n)
        idxs = sorted({int(i * n / k) for i in range(k)})
        pages_with_img = sum(1 for i in idxs if pdf.pages[i].images)
    return pages_with_img >= max(1, len(idxs) // 2)


# ─────────────────────────── Extract ───────────────────────────


# 한 run 동안 동일 PDF 재추출 방지 (segmenter + normalizer 가 같은 파일 두 번 읽음)
_extract_cache: dict[tuple[str, float, bool], "ExtractedPDF"] = {}

# 캐시 키 구성이 바뀌면 올린다 — 옛 캐시는 자연히 miss (감사 ⑥)
OCR_CACHE_SCHEMA = 2


def _ocr_cache_path(pdf_path: Path) -> Path:
    """파일 내용 sha256 기반 캐시 경로. 파일 자체가 안 바뀌면 영구 hit."""
    from app.config import DATA_DIR
    cache_dir = DATA_DIR / "ocr_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 큰 파일은 hash 가 비싸므로 (path, size, mtime) 으로 빠른 키 + 충돌 안전성을 위해 path basename 도 포함
    h = hashlib.sha256()
    h.update(str(pdf_path.resolve()).encode("utf-8"))
    st = pdf_path.stat()
    h.update(f"{st.st_size}:{st.st_mtime_ns}".encode("utf-8"))
    # OCR 백엔드/언어/스키마가 바뀌면 키도 바뀌어야 한다 — 아니면 고친 OCR 이 옛 캐시에 가려진다 (감사 ⑥)
    h.update(f"|schema={OCR_CACHE_SCHEMA}|backend={os.environ.get('NDT_OCR_BACKEND', 'tesseract').lower()}"
             f"|langs={os.environ.get('NDT_OCR_LANGS', '')}".encode("utf-8"))
    return cache_dir / f"{pdf_path.stem}.{h.hexdigest()[:16]}.json"


def _load_ocr_cache(pdf_path: Path) -> Optional["ExtractedPDF"]:
    cache = _ocr_cache_path(pdf_path)
    if not cache.exists():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        pages = [PageText(**p) for p in data["pages"]]
        return ExtractedPDF(path=pdf_path, pages=pages, text_layer_present=data["text_layer_present"])
    except Exception as e:
        logger.warning("OCR 디스크 캐시 로드 실패 (%s): %s", cache, e)
        return None


def _save_ocr_cache(pdf_path: Path, result: "ExtractedPDF") -> None:
    if any(p.source == "ocr_unavailable" for p in result.pages):
        logger.info("OCR 실패 페이지 포함 — 디스크 캐시에 저장하지 않음: %s", pdf_path.name)
        return
    cache = _ocr_cache_path(pdf_path)
    try:
        cache.write_text(json.dumps({
            "path": str(pdf_path),
            "text_layer_present": result.text_layer_present,
            "pages": [asdict(p) for p in result.pages],
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("OCR 디스크 캐시 저장 실패 (%s): %s", cache, e)


def extract(pdf_path: Path, *, force_ocr: bool = False) -> ExtractedPDF:
    """PDF 1개 → 페이지별 텍스트.

    force_ocr=True 인 경우 텍스트 레이어를 무시하고 OCR.
    동일 (경로, mtime, force_ocr) 조합은 메모리·디스크 캐시 둘 다 검사.
    OCR 비용이 거대(수시간) 하므로 디스크 캐시가 핵심.
    """
    pdf_path = Path(pdf_path)
    key = (str(pdf_path.resolve()), pdf_path.stat().st_mtime, force_ocr)
    cached = _extract_cache.get(key)
    if cached is not None:
        return cached

    # 디스크 캐시 hit 검사 (OCR 결과만 의미 있음 — 텍스트레이어 추출은 빠르므로 캐시 불필요)
    # 단, force_ocr=False 이고 텍스트 레이어 없을(또는 품질 낮을) 때만 디스크 캐시 본다.
    import os as _os
    force_ocr = force_ocr or _os.environ.get("NDT_FORCE_OCR") == "1"
    has_layer = (not force_ocr) and _has_meaningful_text_layer(pdf_path)
    text_layer = has_layer

    # 텍스트레이어가 있어도 품질이 낮으면 (배포본 mediocre OCR) 재OCR 강제.
    # NDT_TRUST_TEXT_LAYER=1 이면 무조건 신뢰(Adobe-good 확신 시), NDT_FORCE_OCR=1 이면 무조건 재OCR.
    if has_layer and _os.environ.get("NDT_TRUST_TEXT_LAYER") != "1":
        q, qinfo = _text_layer_quality(pdf_path)
        min_q = float(_os.environ.get("NDT_TEXT_LAYER_MIN_QUALITY", "0.6"))
        if q < min_q and _has_scan_images(pdf_path):
            # 품질 낮음 + 스캔 이미지 존재 = OCR-over-scan 깨짐 → 재OCR 이 도움
            logger.warning(
                "텍스트레이어 품질 낮음 (q=%.2f < %.2f) — 재OCR: %s %s",
                q, min_q, pdf_path.name, qinfo,
            )
            text_layer = False
        elif q < min_q:
            # 품질 낮으나 born-digital (스캔 이미지 없음) → 재OCR 무의미, 레이어 유지
            logger.info(
                "텍스트레이어 품질 낮으나 born-digital (스캔 없음) → 레이어 유지: %s", pdf_path.name)
        else:
            logger.info("텍스트레이어 신뢰 (q=%.2f, Adobe-good 추정): %s", q, pdf_path.name)

    if not text_layer:
        disk_cached = _load_ocr_cache(pdf_path)
        if disk_cached is not None:
            logger.info("OCR 디스크 캐시 HIT: %s (%d 페이지 — Tesseract 호출 생략)",
                        pdf_path.name, len(disk_cached.pages))
            _extract_cache[key] = disk_cached
            return disk_cached

    if text_layer:
        pages = _extract_text_layer(pdf_path)
    else:
        pages = _extract_via_ocr(pdf_path)

    result = ExtractedPDF(path=pdf_path, pages=pages, text_layer_present=text_layer)
    _extract_cache[key] = result

    # OCR 결과만 디스크에 저장 (텍스트 레이어는 매번 빠르게 재추출)
    if not text_layer:
        _save_ocr_cache(pdf_path, result)

    return result


def _extract_text_layer(pdf_path: Path) -> list[PageText]:
    import pdfplumber

    pages: list[PageText] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            pages.append(PageText(page_index=i, text=text, source="text_layer"))
    return pages


def _worker_diagnostics() -> dict:
    """워커가 죽었을 때 부모가 로그에 남기는 진단 — 사내 원인(경로·PATH·--version 원문)을 다음 실행에서 본다.
    2026-09-07 사내 ②: 워커 안의 pytesseract 가 'Invalid tesseract version: ""' (SystemExit) 로 죽었다."""
    import subprocess
    from app.extractors import ocr_engine
    try:
        ocr_engine._configure_pytesseract()
    except Exception:       # noqa: BLE001
        pass
    try:
        import pytesseract
        cmd = str(pytesseract.pytesseract.tesseract_cmd)
    except Exception as e:  # noqa: BLE001
        cmd = f"(pytesseract import 실패: {e})"
    cmd_dir = str(Path(cmd).parent) if cmd and os.path.sep in cmd else ""
    d = {"tesseract_cmd": cmd, "tessdata_prefix": os.environ.get("TESSDATA_PREFIX", ""),
         "path_has_cmd_dir": bool(cmd_dir) and cmd_dir in os.environ.get("PATH", "").split(os.pathsep),
         "version_rc": None, "version_raw": ""}
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, text=True, errors="replace", timeout=30)
        d["version_rc"] = r.returncode
        d["version_raw"] = (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        d["version_rc"] = -1
        d["version_raw"] = f"{type(e).__name__}: {e}"
    return d


def _init_worker() -> None:
    """ProcessPool 워커 초기화 — 부모와 같은 순서(env → 번들 → PATH)로 tesseract 경로를 잡는다.
    Windows 는 spawn 이라 워커가 새 인터프리터로 뜨고 부모의 pytesseract 설정을 모른다."""
    try:
        from app.extractors import ocr_engine
        ocr_engine._configure_pytesseract()
    except Exception:       # noqa: BLE001 - 초기화 실패는 페이지 처리에서 다시 드러난다
        pass


def _ocr_one_page(args: tuple) -> dict:
    """워커 진입점. pytesseract 는 버전 문자열을 못 읽으면 SystemExit 를 던진다(Exception 아님) —
    그대로 두면 워커가 통째로 죽고 문서 전체가 실패한다. 여기서 잡아 진단과 함께 돌려보낸다."""
    import multiprocessing
    if os.environ.get("NDT_OCR_TEST_FAIL_WORKER") == "1" and multiprocessing.parent_process() is not None:
        raise SystemExit('Invalid tesseract version: ""')        # 테스트 전용: 사내 워커 죽음 그대로 재현
    try:
        return _ocr_one_page_impl(args)
    except SystemExit as e:
        return {"page_index": args[1], "fatal": f"SystemExit: {e}", "diag": _worker_diagnostics()}


def _ocr_one_page_impl(args: tuple) -> dict:
    """Multi-processing worker — 페이지 1개 렌더링 + OCR.

    Backend 선택 (env NDT_OCR_BACKEND):
      - "tesseract" (기본)
      - "paddle"  — PP-OCR + PP-Structure (사내 Windows 권장, mac M-series 미지원)
      - "both"    — 둘 다 호출 후 diff 정보 첨부

    워커별로 pdfium handle 새로 열어서 page 추출.
    """
    pdf_path_str, page_index = args
    backend = os.environ.get("NDT_OCR_BACKEND", "tesseract").lower()

    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(pdf_path_str)
        try:
            page = pdf[page_index]
            w_pt, h_pt = page.get_size()
            target_pixels = 100_000_000
            base_scale = 4.0
            pixels_at_4 = (w_pt * base_scale) * (h_pt * base_scale)
            downscaled_to = None
            if pixels_at_4 > target_pixels:
                base_scale = max(2.0, (target_pixels / (w_pt * h_pt)) ** 0.5)
                downscaled_to = base_scale
            pil_image = page.render(scale=base_scale).to_pil()
        finally:
            pdf.close()

        tess_result = None
        paddle_result = None

        if backend in ("tesseract", "both"):
            from app.extractors.ocr_engine import ocr_page
            tess_result = ocr_page(pil_image)

        if backend in ("paddle", "both"):
            try:
                from app.extractors.ocr_paddle import ocr_page_paddle
                paddle_result = ocr_page_paddle(pil_image)
            except ImportError:
                # Paddle 미설치 (예: mac M-series) — Tesseract 폴백
                if tess_result is None:
                    from app.extractors.ocr_engine import ocr_page
                    tess_result = ocr_page(pil_image)

        # 결과 합성
        if backend == "paddle" and paddle_result is not None:
            primary_text = paddle_result.text
            confidence = paddle_result.mean_confidence
            source = "ocr_paddle"
            extras = {"paddle_tables": paddle_result.tables}
        elif backend == "both" and paddle_result is not None and tess_result is not None:
            from app.extractors.ocr_paddle import diff_text_lines
            diff = diff_text_lines(tess_result.best_text, paddle_result.text)
            # 다수 동의 — agreement 높으면 paddle 우선, 낮으면 두 텍스트 모두 보존
            primary_text = paddle_result.text if diff["agreement_ratio"] >= 0.7 else (
                tess_result.best_text + "\n\n[PADDLE]\n" + paddle_result.text
            )
            confidence = paddle_result.mean_confidence or tess_result.mean_confidence
            source = "ocr_both"
            extras = {
                "paddle_tables": paddle_result.tables,
                "diff": diff,
                "tesseract_text": tess_result.best_text,
            }
        else:
            # tesseract only (기본, 또는 paddle 폴백)
            primary_text = tess_result.best_text if tess_result else ""
            confidence = tess_result.mean_confidence if tess_result else 0.0
            source = "ocr"
            extras = {}

        return {
            "page_index": page_index,
            "text": primary_text,
            "source": source,
            "ocr_variants": tess_result.variants if tess_result else [],
            "confidence": confidence,
            "downscaled_to": downscaled_to,
            "w_pt": int(w_pt), "h_pt": int(h_pt),
            "pixels_at_4_mp": int(pixels_at_4 / 1e6),
            "backend": backend,
            "extras": extras,
        }
    except Exception as e:
        return {
            "page_index": page_index,
            "text": f"[OCR_ERROR] {e}",
            "source": "ocr_error",
            "ocr_variants": [],
            "confidence": 0.0,
            "error": str(e),
        }


def _extract_via_ocr(pdf_path: Path) -> list[PageText]:
    """렌더링 → Tesseract OCR (다중 파라미터 재시도).

    NDT_OCR_WORKERS 환경변수로 페이지 단위 multi-processing.
      - 1 (기본): 직렬 (안전)
      - 2~N: ProcessPoolExecutor 로 페이지 병렬 (메모리 부담 N배)

    Tesseract 미설치 시 빈 페이지 리스트 반환 + 경고. 호출부가 후속 LLM 단계에서
    needs_review 로 분류하도록 함 (추정 금지 원칙).
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        logger.error("pypdfium2 미설치 — scan PDF OCR 불가 (%s)", pdf_path)
        return [_empty_ocr_page(0, f"pypdfium2 미설치: {e}")]

    # Tesseract 가용성 사전 체크 — pytesseract 가 바이너리 못 찾으면 즉시 needs_review
    if not _tesseract_available():
        logger.error("Tesseract 바이너리 미설치 — scan PDF OCR 불가: %s", pdf_path)
        return [_empty_ocr_page(0, "Tesseract 바이너리 미설치 — 사외(mac) 환경에서는 'brew install tesseract' 또는 사내 설치본 사용")]

    # 페이지 수 파악 (handle 잠깐 열어보고 닫음)
    pdf = pdfium.PdfDocument(pdf_path)
    n_pages = len(pdf)
    pdf.close()

    workers = int(os.environ.get("NDT_OCR_WORKERS", "1"))
    pdf_path_str = str(pdf_path)
    tasks = [(pdf_path_str, i) for i in range(n_pages)]

    def _to_pages(results: list[dict]) -> list[PageText]:
        out: list[PageText] = []
        for r in sorted(results, key=lambda r: r["page_index"]):
            if r.get("downscaled_to"):
                logger.info("Page %d of %s downscaled to scale %.2f",
                            r["page_index"] + 1, pdf_path.name, r["downscaled_to"])
            out.append(PageText(
                page_index=r["page_index"], text=r["text"], source=r["source"],
                ocr_variants=r["ocr_variants"], confidence=r["confidence"],
            ))
        return out

    def _serial() -> list[dict]:
        logger.info("OCR (serial): %s (%d pages)", pdf_path.name, n_pages)
        return [_ocr_one_page(args) for args in tasks]

    results: Optional[list[dict]] = None
    if workers > 1:
        # 병렬 — 페이지를 worker pool 에 분산. 워커가 어떤 이유로든 죽으면 이 문서는 직렬로 마저 한다.
        # 2026-09-07 사내 ②: 워커의 pytesseract 가 SystemExit('Invalid tesseract version: ""') 로 죽어
        # 문서 전체가 실패했다. 원인은 워커 진단을 로그로 남겨 다음 실행에서 본다.
        from concurrent.futures import ProcessPoolExecutor, as_completed
        from app.progress_fmt import progress_line
        logger.info("OCR (parallel x%d): %s (%d pages)", workers, pdf_path.name, n_pages)
        every = max(1, n_pages // 10)
        got: list[dict] = []
        try:
            with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as ex:
                futures = [ex.submit(_ocr_one_page, t) for t in tasks]
                for done, fut in enumerate(as_completed(futures), start=1):
                    got.append(fut.result())
                    if done % every == 0 or done == n_pages:
                        logger.info(progress_line("OCR", done, n_pages, pdf_path.name))
            fatal = [r for r in got if r.get("fatal")]
            if fatal:
                logger.warning("병렬 OCR 워커 실패 (%s) — %s 는 직렬로 재시도합니다. 워커 진단: %s",
                               fatal[0]["fatal"], pdf_path.name, fatal[0].get("diag"))
            else:
                results = got
        except (Exception, SystemExit) as e:       # 워커가 통째로 죽으면 부모에 SystemExit/BrokenProcessPool 로 온다
            logger.warning("병렬 OCR 실패 (%s: %s) — %s 는 직렬로 재시도합니다. 진단(부모 기준): %s",
                           type(e).__name__, str(e)[:200], pdf_path.name, _worker_diagnostics())
    if results is None:
        results = _serial()
    pages = _to_pages(results)
    return pages


_tesseract_check_cache: Optional[bool] = None


def _tesseract_available() -> bool:
    """ocr_engine.resolve_tesseract 와 같은 순서(env → 번들 → PATH)로 본다.

    예전엔 env+PATH 만 봐서 hcx-check/ocr-check 는 ✓ 인데 실제 추출은
    [OCR_UNAVAILABLE] 이 되는 불일치가 있었다 (2026-09-04 감사 ⑤).
    """
    global _tesseract_check_cache
    if _tesseract_check_cache is not None:
        return _tesseract_check_cache
    from app.extractors import ocr_engine
    _tesseract_check_cache = ocr_engine.tesseract_available()
    return _tesseract_check_cache


def _empty_ocr_page(page_index: int, reason: str) -> PageText:
    return PageText(
        page_index=page_index,
        text=f"[OCR_UNAVAILABLE] {reason}",
        source="ocr_unavailable",
        confidence=0.0,
    )
