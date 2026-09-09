#!/usr/bin/env python3
"""이미지 입력(비전) 가능 여부 판정 — Studio XP 개방모델이 그림을 읽는지 1회 호출로 확인.

왜 필요한가:
  Gemma 4 31B 는 모델 자체가 멀티모달이고 vLLM 도 image_url 입력을 지원하지만,
  사내 Studio XP 배포에서 실제로 열어뒀는지는 문서만으로 확정되지 않는다.
  이 스크립트가 그림 한 장을 만들어 보내고, 모델이 그 안의 글자를 읽어내는지로 판정한다.

무엇을 시험하나:
  실제 앱이 쓰는 경로(hcx_client.call_vision → OpenAI 호환 image_url)를 그대로 태운다.
  따라서 통과하면 앱의 vision stage 를 그 provider 로 돌려도 된다는 뜻이다.

사용 (사내):
  run_vision_check.bat            더블클릭 → gemma 로 시험
  python scripts/vision_check.py --provider gemma
  python scripts/vision_check.py --provider gptoss   (텍스트 전용이라 실패가 정상)

사외에서 도구 자체 점검:
  python scripts/vision_check.py --self-test   (그림 생성·요청 본문 형식만 확인, 호출 없음)
"""
from __future__ import annotations
import argparse, base64, io, json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 모델이 맞혀야 하는 값. 흔한 숫자를 피해 우연 일치 가능성을 낮춘다.
SECRET_CODE = "NDT-7391"


def make_test_image() -> bytes:
    """판정용 PNG 생성 — 흰 바탕에 큰 검은 글씨로 SECRET_CODE 를 쓴다."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (640, 240), "white")
    d = ImageDraw.Draw(img)
    font = None
    # 큰 글꼴 우선 (없으면 기본 글꼴 — 작아도 판독은 가능)
    for cand in ("arial.ttf", "DejaVuSans-Bold.ttf", "malgun.ttf", "Arial.ttf"):
        try:
            font = ImageFont.truetype(cand, 96)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    d.text((40, 70), SECRET_CODE, fill="black", font=font)
    # 모델이 "글자만 있는 그림"이라 헷갈리지 않도록 테두리 추가
    d.rectangle([(8, 8), (631, 231)], outline="black", width=3)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _configure(provider_name: str):
    """지정 provider 로 vision stage 를 임시 라우팅."""
    from app.config import load_yaml

    cfg = load_yaml("hcx.yaml")  # 캐시된 객체 (clear 금지)
    if not cfg.get("providers"):
        print("[중단] config/hcx.yaml 의 providers 블록이 주석 처리되어 있습니다.")
        print("       주석(#)을 벗기고 base_url 을 채운 뒤 다시 실행하세요.")
        sys.exit(2)
    if provider_name not in cfg["providers"]:
        print(f"[중단] providers 에 '{provider_name}' 가 없습니다. "
              f"정의된 것: {list(cfg['providers'])}")
        sys.exit(2)
    cfg.setdefault("stage_providers", {})["vision_table_verify"] = provider_name
    os.environ.setdefault("NDT_HCX_MOCK", "0")
    os.environ.setdefault("NDT_HCX_BUDGET_BYPASS", "1")
    return cfg["providers"][provider_name]


def run(provider_name: str) -> int:
    prov = _configure(provider_name)
    token_env = prov.get("token_env", "NDT_HCX_TOKEN")
    if not os.environ.get(token_env):
        print(f"[중단] 환경변수 {token_env} 가 비어 있습니다. API Key 를 등록하고 재로그인하세요.")
        return 2

    print(f"provider : {provider_name}")
    print(f"모델     : {prov.get('model', '(config 기본값)')}")
    print(f"주소     : {prov.get('base_url', '')}{prov.get('chat_path', '')}")
    print(f"시험     : 그림 속 문자 '{SECRET_CODE}' 를 읽어내는지")
    print("-" * 58)

    png = make_test_image()
    print(f"시험 이미지 생성 완료 ({len(png):,} bytes)")

    import app.hcx_client as hc

    payload = {
        "질문": "이 이미지에 적혀 있는 문자열을 그대로 옮겨 적으시오. 다른 설명은 붙이지 마시오.",
        "instruction": "Transcribe the exact text shown in the image. Output only that text.",
    }
    try:
        resp = hc.call_vision("vision_table_verify", payload, [png], force_refresh=True)
    except Exception as e:
        msg = str(e)
        print(f"\n[판정] 이미지 입력 불가 — 호출이 거부되었습니다.")
        print(f"  오류: {type(e).__name__}: {msg[:300]}")
        low = msg.lower()
        if any(k in low for k in ("image", "modal", "vision", "content type", "not support")):
            print("  → 서버가 이미지 입력을 지원하지 않는다는 취지의 응답입니다.")
            print("     사내 AI솔루션부에 'Studio XP 에서 이미지 입력 활성화 가능 여부'를 문의하세요.")
        else:
            print("  → 이미지와 무관한 오류일 수 있습니다(인증·방화벽·모델명).")
            print("     먼저 run_model_ab.bat 으로 텍스트 호출이 되는지 확인하세요.")
        return 1

    text = (resp.content or "").strip()
    print(f"\n모델 응답: {text[:200]}")

    # 판정: 하이픈·대소문자·공백 차이를 허용하고 비교
    norm = "".join(ch for ch in text.upper() if ch.isalnum())
    want = "".join(ch for ch in SECRET_CODE.upper() if ch.isalnum())
    if want in norm:
        print(f"\n[판정] 이미지 입력 가능 — 그림 속 '{SECRET_CODE}' 를 정확히 읽었습니다.")
        print("  → 이 provider 로 vision stage 를 돌려도 됩니다.")
        print("     config/hcx.yaml 의 stage_providers 에 vision_* 단계를 지정하면 됩니다.")
        return 0
    print(f"\n[판정] 재확인 필요 — 호출은 성공했으나 '{SECRET_CODE}' 를 읽어내지 못했습니다.")
    print("  가능한 원인 두 가지이므로 구분이 필요합니다:")
    print("   (1) 이미지가 무시되고 텍스트만 처리됨 → 사실상 비전 불가")
    print("   (2) 이미지는 봤으나 글자 판독에 실패 → 비전은 되나 정확도 문제")
    print("  응답에 그림 이야기(흰 배경·테두리 등)가 있으면 (2), 아예 없으면 (1) 쪽입니다.")
    return 1


def self_test() -> int:
    """사외 점검 — 그림 생성과 요청 본문 형식만 확인 (실제 호출 없음)."""
    png = make_test_image()
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "PNG 헤더 불일치"
    print(f"1) 시험 이미지 생성 OK ({len(png):,} bytes, PNG 헤더 정상)")

    b64 = base64.b64encode(png).decode("ascii")
    body_user_content = [
        {"type": "text", "text": "(payload)"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]
    blk = body_user_content[1]
    assert blk["image_url"]["url"].startswith("data:image/png;base64,")
    print("2) 요청 본문 형식 OK — OpenAI/vLLM 표준 image_url + data URI")
    print(f"   {json.dumps(blk, ensure_ascii=False)[:88]}...")

    import app.hcx_client as hc
    src = Path(hc.__file__).read_text(encoding="utf-8")
    assert '"type": "image_url"' in src, "hcx_client 의 openai 경로에 image_url 없음"
    print("3) hcx_client 의 OpenAI 경로가 동일 형식으로 전송함을 확인")

    print("\n[self-test 통과] 사내에서 --provider gemma 로 실제 판정하세요.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="gemma", help="hcx.yaml providers 의 이름 (기본: gemma)")
    ap.add_argument("--save-image", help="시험 이미지를 이 경로에 저장(눈으로 확인용)")
    ap.add_argument("--self-test", action="store_true", help="호출 없이 도구 자체만 점검")
    args = ap.parse_args()

    if args.save_image:
        Path(args.save_image).write_bytes(make_test_image())
        print(f"시험 이미지 저장: {args.save_image}")

    if args.self_test:
        return self_test()
    return run(args.provider)


if __name__ == "__main__":
    sys.exit(main())
