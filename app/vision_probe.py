"""사내 모델별 이미지 입력 지원 여부 프로브.

배경 (2026-09-07): 표 페이지 전사 정확도가 판정 근거의 질을 좌우하는데, 지금은 비전 모델이 HCX-005
하나뿐이라고 **가정**하고 있다. 근거는 Studio XP 가이드의 "텍스트 모델 관련 필드만 지원" 한 문장이다.
gemma4-31b·qwen36-27b 는 상위 계열에 비전판이 있으므로 실제로 받을 수도 있다 — 추측하지 말고 물어본다.

작은 그림 하나(빨간 사각형)를 보내고 "무엇이 보이나"를 묻는다. 대답이 오면 비전 가능,
파라미터 오류(40001 등)면 불가, 일시 오류면 판정 보류(None).
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Optional

import app.hcx_client as hc          # 모듈로 참조 — 테스트가 _post_chat 을 갈아끼울 수 있게
from app.config import hcx_config
from app.hcx_client import HCXClientError, HCXError

logger = logging.getLogger(__name__)

_PROMPT = ("You are shown one small image. Answer in one short sentence: what shape and colour is it? "
           "If you cannot see any image, reply exactly: NO IMAGE RECEIVED.")


def probe_image_png() -> bytes:
    """빨간 사각형 한 개짜리 작은 PNG. 정답이 자명해서 '봤는지'를 대답만으로 가릴 수 있다."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (160, 120), "white")
    ImageDraw.Draw(img).rectangle([40, 30, 120, 90], fill="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def run(providers: Optional[list[str]] = None) -> list[dict]:
    """provider 목록마다 이미지 1장을 보내고 결과를 모은다. 서버를 부르므로 사내에서만 의미가 있다."""
    cfg = hcx_config()
    names = providers or sorted((cfg.get("providers") or {}).keys())
    png = probe_image_png()
    b64 = base64.b64encode(png).decode("ascii")
    out: list[dict] = []
    for name in names:
        row = {"provider": name, "model": None, "vision": None, "answer": "", "error": ""}
        try:
            p = hc.resolve_provider_by_name(name)      # 기본값(retry·timeout) 채움 — 날것 dict 금지
        except KeyError as e:
            row["error"] = str(e).strip("'\"")
            out.append(row)
            continue
        row["model"] = p.get("model")
        try:
            api = hc._post_chat(p.get("model"), _PROMPT, "{}", [b64], stage="hcx_check", provider=p)
            answer = (hc._extract_content(api) or "").strip()
            row["answer"] = answer[:200]
            row["vision"] = bool(answer) and "NO IMAGE RECEIVED" not in answer.upper()
            if not row["vision"]:
                row["error"] = "이미지를 못 봤다고 응답"
        except HCXClientError as e:          # 파라미터·형식 거부 = 비전 미지원
            row["vision"] = False
            row["error"] = str(e)[:200]
        except HCXError as e:                # 일시 오류 = 판정 보류
            row["vision"] = None
            row["error"] = f"일시 오류(재시도 필요): {str(e)[:160]}"
        except Exception as e:               # noqa: BLE001
            row["vision"] = None
            row["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        logger.info("비전 프로브 %s(%s): vision=%s %s", name, row["model"], row["vision"],
                    row["answer"] or row["error"])
        out.append(row)
    return out
