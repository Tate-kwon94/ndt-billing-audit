"""Tesseract OCR 래퍼 — 최상 강도, 다중 파라미터 재시도.

전제: 설치된 Tesseract 5 + 영어/한국어/러시아어 traineddata.
폐쇄망 PC 에서는 installer/tesseract/ 의 포터블 바이너리·언어모델을 사용.
환경변수 NDT_TESSERACT_CMD, NDT_TESSDATA_PREFIX 로 경로 override 가능.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────── Setup ───────────────────────────


def resolve_tesseract() -> tuple[Optional[str], Optional[str]]:
    """(바이너리 경로, tessdata 경로). 못 찾으면 (None, None).

    2026-09-03 사내 실측: `call set_env.bat` 을 했는데도 NDT_TESSERACT_CMD 가 비어
    표 분류 테스트 6건이 조용히 건너뛰어졌다. 번들에 tesseract.exe 는 분명히 있는데
    환경변수 하나에 전부를 걸고 있던 것이 문제였다. 그래서 순서를 셋으로 늘린다.
      1) 환경변수 (사용자가 명시한 것이 항상 우선)
      2) 번들 포터블 (installer/tesseract/) — set_env 없이도 동작
      3) PATH
    """
    import shutil
    from pathlib import Path

    from app.config import PROJECT_ROOT

    cmd = os.environ.get("NDT_TESSERACT_CMD") or None
    if cmd and not Path(cmd).exists():
        # 실패가 아니다 — 아래에서 번들/PATH 로 다시 찾는다. 사용자가 이 줄만 보고
        # 고장으로 오해했으므로(2026-09-03) 무엇을 할지까지 한 줄에 적는다.
        logger.info("NDT_TESSERACT_CMD 경로에 파일이 없어 무시하고 번들에서 찾습니다: %s", cmd)
        cmd = None

    # 번들 후보는 **이 플랫폼에서 실행 가능한 이름만** 본다. 사외(macOS)에서 Windows용
    # tesseract.exe 를 집어 들면 PermissionError 로 죽는다 (실제로 그렇게 5건 깨졌다).
    bundled_dir = PROJECT_ROOT / "installer" / "tesseract"
    if not cmd:
        cand = bundled_dir / ("tesseract.exe" if os.name == "nt" else "tesseract")
        if cand.exists() and (os.name == "nt" or os.access(cand, os.X_OK)):
            cmd = str(cand)
    if not cmd:
        cmd = shutil.which("tesseract")

    tessdata = os.environ.get("NDT_TESSDATA_PREFIX") or None
    if tessdata and not Path(tessdata).exists():
        tessdata = None
    if not tessdata and (bundled_dir / "tessdata").exists():
        tessdata = str(bundled_dir / "tessdata")
    return cmd, tessdata


def tesseract_available() -> bool:
    return resolve_tesseract()[0] is not None


def _configure_pytesseract():
    """pytesseract 의 바이너리·tessdata 경로를 정한다 (환경변수 → 번들 → PATH)."""
    import pytesseract

    cmd, tessdata = resolve_tesseract()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata


# ─────────────────────────── Data ───────────────────────────


@dataclass
class OCRResult:
    best_text: str
    variants: list[str] = field(default_factory=list)
    mean_confidence: Optional[float] = None
    chosen_params: Optional[dict] = None


# 다중 파라미터 후보 — 정확도 우선 (속도 손해 감수)
# 거대 도면(예: A1/A0 scan)은 upscale2x 가 메모리 초과 가능 → 크기 임계 초과 시 자동 skip
_LARGE_IMAGE_PIXEL_THRESHOLD = 50_000_000   # 50 megapixel 이상이면 upscale variant skip

# OCR 언어 모델 — 사용자 확인 (2026-05-22): 원전 도면·성적서에 한국어 없음.
# eng+rus 만으로 충분, ~30% 빠름. 환경변수 NDT_OCR_LANGS 로 override (예: 'eng+rus+kor').
def _ocr_langs() -> str:
    return os.environ.get("NDT_OCR_LANGS", "eng+rus")


def _param_variants() -> list[dict]:
    """매 호출 시점에 langs 평가 (환경변수 override 반영)."""
    langs = _ocr_langs()
    return [
        {"psm": 6,  "oem": 3, "langs": langs, "preprocess": "none"},
        {"psm": 6,  "oem": 3, "langs": langs, "preprocess": "binarize"},
        {"psm": 4,  "oem": 3, "langs": langs, "preprocess": "upscale2x_binarize"},
        {"psm": 11, "oem": 3, "langs": langs, "preprocess": "binarize"},
    ]


# ─────────────────────────── Preprocess ───────────────────────────


def _preprocess(pil_image, mode: str):
    from PIL import ImageOps

    img = pil_image
    if mode == "none":
        return img
    if mode == "binarize":
        return ImageOps.grayscale(img).point(lambda p: 255 if p > 180 else 0)
    if mode == "upscale2x_binarize":
        gray = ImageOps.grayscale(img)
        upsized = gray.resize((gray.width * 2, gray.height * 2))
        return upsized.point(lambda p: 255 if p > 180 else 0)
    return img


# ─────────────────────────── OCR ───────────────────────────


def ocr_page(pil_image) -> OCRResult:
    """단일 이미지에 대해 다중 파라미터로 OCR 수행 → 최선의 결과 + 변종 목록."""
    _configure_pytesseract()
    import pytesseract

    variants: list[str] = []
    best_text = ""
    best_confidence = -1.0
    best_params: Optional[dict] = None

    # 거대 도면(예: 19064×40376 = ~770MP) 에서 upscale2x 는 OOM 위험
    base_pixels = pil_image.width * pil_image.height
    skip_upscale = base_pixels >= _LARGE_IMAGE_PIXEL_THRESHOLD

    for params in _param_variants():
        if skip_upscale and "upscale" in params["preprocess"]:
            logger.debug("거대 이미지 (%dMP) — upscale variant skip", base_pixels // 1_000_000)
            continue
        try:
            img = _preprocess(pil_image, params["preprocess"])
            config = f"--psm {params['psm']} --oem {params['oem']}"
            text = pytesseract.image_to_string(img, lang=params["langs"], config=config) or ""
            variants.append(text)

            # 신뢰도 추정: pytesseract image_to_data 의 conf 평균
            data = pytesseract.image_to_data(
                img, lang=params["langs"], config=config, output_type=pytesseract.Output.DICT
            )
            confs = [int(c) for c in data.get("conf", []) if str(c).lstrip("-").isdigit() and int(c) >= 0]
            mean_conf = sum(confs) / len(confs) if confs else 0.0

            if mean_conf > best_confidence:
                best_confidence = mean_conf
                best_text = text
                best_params = params
        except Exception as e:
            logger.warning("OCR variant failed (%s): %s", params, e)

    return OCRResult(
        best_text=best_text,
        variants=variants,
        mean_confidence=best_confidence if best_confidence >= 0 else None,
        chosen_params=best_params,
    )
