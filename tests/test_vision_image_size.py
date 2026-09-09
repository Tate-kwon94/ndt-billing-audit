"""HCX-005 'Invalid image size' (code=40063) — 이미지가 크면 거부한다 (2026-09-08 사내 확정).

증거: table-bench 가 **PNG 1.41MB** 를 보내자 `{"code":"40063","message":"Invalid image size"}`.
같은 원인으로 사내 ② 도면4 에서 **표 전사 vlm 실패 30건**이 났다 (표페이지 34 중 전사 4). 운영 경로가 막혀 있었다.

한계값은 아직 모른다 — **추측해서 상한을 박지 않는다.** 대신 거부당하면 더 작게 줄여 다시 보내고,
성공한 크기를 로그에 남긴다. 그 숫자가 쌓이면 config 에 상한을 넣는다.
"""
import pytest

import app.hcx_client as hc
from app.extractors import vision_render as vr


def test_render_fits_a_byte_budget(tmp_path):
    import pypdfium2 as pdfium
    pdf = tmp_path / "d.pdf"
    doc = pdfium.PdfDocument.new(); doc.new_page(1200, 850); doc.save(str(pdf)); doc.close()
    big = vr.render_png(pdf, 0, scale=4.0)
    small = vr.render_png(pdf, 0, scale=4.0, max_bytes=len(big) // 4)
    assert len(small) <= len(big) // 4 < len(big)


def test_send_retries_smaller_on_invalid_image_size():
    sizes = []

    def send(png: bytes):
        sizes.append(len(png))
        if len(sizes) < 3:
            raise hc.HCXClientError('HCX 클라이언트 오류 http=400 code=40063: {"message":"Invalid image size"}')
        return "OK"

    out, used = vr.send_with_shrink(lambda mb: b"x" * (1_400_000 // (2 ** len(sizes))), send)
    assert out == "OK"
    assert len(sizes) == 3 and sizes[0] > sizes[-1]
    assert used == sizes[-1]


def test_send_does_not_retry_other_errors():
    def send(png: bytes):
        raise hc.HCXClientError("HCX 인증 실패 (code=40103)")
    with pytest.raises(hc.HCXClientError, match="40103"):
        vr.send_with_shrink(lambda mb: b"x" * 1000, send)


def test_send_gives_up_after_the_ladder_and_says_the_last_size():
    def send(png: bytes):
        raise hc.HCXClientError('code=40063: {"message":"Invalid image size"}')
    with pytest.raises(hc.HCXClientError) as e:
        vr.send_with_shrink(lambda mb: b"x" * 500, send, attempts=3)
    assert "3회" in str(e.value) and "줄여" in str(e.value)


def test_transcribe_page_shrinks_the_image_when_rejected(monkeypatch):
    """운영 경로(사내 ② 도면4: 표페이지 34 중 vlm 실패 30) 도 같은 재시도를 타야 한다."""
    from app.extractors import table_transcriber as tt
    sent = []

    class R:
        content = "| a |\n|---|\n| 1 |"
        model = "HCX-005"

    def fake_call_vision(stage, payload, images):
        sent.append(len(images[0]))
        if len(sent) == 1:
            raise hc.HCXClientError('http=400 code=40063: {"message":"Invalid image size"}')
        return R()

    monkeypatch.setattr(hc, "call_vision", fake_call_vision)
    from PIL import Image
    import io as _io
    buf = _io.BytesIO()
    Image.new("RGB", (2400, 1700), "white").save(buf, format="PNG")
    out = tt.transcribe_page(buf.getvalue(), doc_hint="d", page_no=13)
    assert out["markdown"] and out["error"] is None
    assert len(sent) == 2 and sent[1] < sent[0], sent


def test_transcribe_page_still_swallows_other_failures(monkeypatch):
    from app.extractors import table_transcriber as tt
    monkeypatch.setattr(hc, "call_vision", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = tt.transcribe_page(b"not a png", doc_hint="d", page_no=1)
    assert out["markdown"] is None and "boom" in out["error"]
