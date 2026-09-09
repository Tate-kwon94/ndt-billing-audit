"""2026-09-08 사내에서 k 의 새 명령 둘이 죽었다 — 둘 다 사외 테스트가 진짜 경로를 건너뛰어 못 잡은 결함.

1. vision-probe: 모든 모델이 `KeyError: 'retry'`. providers 설정을 기본값(retry·timeout) 채우기 없이 날것으로
   _post_chat 에 넘겼다. 테스트는 _post_chat 자체를 가짜로 바꿔서 그 안의 재시도 데코레이터를 안 탔다.
   → 이름으로 provider 를 해석하는 공개 함수를 두고, 테스트는 전송 함수(_do_post_*)만 가짜로 한다.
2. table-bench: FileNotFoundError. 정답 파일의 source 가 사외 Mac 의 파일명('4. NP.D…')인데 사내는 '[ABD]NP.D…' 다.
   → 파일명이 아니라 **도면번호**로 찾는다.
"""
from pathlib import Path

import pytest

import app.hcx_client as hc


def _fake_transport(monkeypatch, answer_by_model):
    def v3(url, body, provider=None):
        model = url.rsplit("/", 1)[-1]
        return {"status": {"code": "20000"}, "result": {"message": {"content": answer_by_model[model]}}}
    def oa(url, body, provider=None):
        return {"choices": [{"message": {"content": answer_by_model[body["model"]]}, "finish_reason": "stop"}]}
    monkeypatch.setattr(hc, "_do_post_v3", v3)
    monkeypatch.setattr(hc, "_do_post_openai", oa)


def test_resolve_provider_by_name_fills_defaults():
    p = hc.resolve_provider_by_name("hcx005")
    assert p["model"] == "HCX-005" and "retry" in p and "timeout_seconds" in p and "chat_path" in p


def test_resolve_provider_by_name_refuses_unknown():
    with pytest.raises(KeyError, match="알 수 없는 provider"):
        hc.resolve_provider_by_name("nope")


def test_vision_probe_goes_through_the_real_post_chat(monkeypatch):
    """전송만 가짜 — provider 기본값·재시도 데코레이터·본문 조립은 진짜로 돈다 (사내 KeyError 재현 방지)."""
    from app import vision_probe
    monkeypatch.setenv("NDT_HCX_TOKEN", "t"); monkeypatch.setenv("NDT_STUDIO_TOKEN", "t")
    _fake_transport(monkeypatch, {"HCX-005": "a red square", "gemma4-31b": "NO IMAGE RECEIVED"})
    out = {r["provider"]: r for r in vision_probe.run(["hcx005", "gemma"])}
    assert out["hcx005"]["vision"] is True
    assert out["gemma"]["vision"] is False and "못 봤다" in out["gemma"]["error"]


def test_table_bench_locates_pdf_by_drawing_number(tmp_path):
    from app import table_bench as tb
    d = tmp_path / "samples" / "drawings"; d.mkdir(parents=True)
    target = d / "[ABD]NP.D.N000.1.0UMA&&BBB&&&.021.DC.0001.E_C02.pdf"; target.write_bytes(b"%PDF")
    (d / "[ABD]NP.D.N000.2.0UMA&&BBB&&&.021.DC.0001.E_C01.pdf").write_bytes(b"%PDF")
    # source 는 사외 Mac 의 경로 — 사내에는 없다 (이 테스트를 프로젝트 폴더에서 돌려도 실재하지 않는 경로여야 한다)
    truth = {"source": str(tmp_path / "mac" / "4. NP.D.N000.1.0UMA&&BBB&&&.021.DC.0001.E_C02.pdf"), "page": 13}
    assert tb.locate_pdf(truth, search_dirs=[d]) == target


def test_table_bench_prefers_existing_source_path(tmp_path):
    from app import table_bench as tb
    src = tmp_path / "x.pdf"; src.write_bytes(b"%PDF")
    assert tb.locate_pdf({"source": str(src)}, search_dirs=[tmp_path]) == src


def test_table_bench_explains_when_pdf_is_missing(tmp_path):
    from app import table_bench as tb
    with pytest.raises(FileNotFoundError, match="--pdf"):
        tb.locate_pdf({"source": "samples/drawings/4. NP.D.N000.9.X.DC.0001.E.pdf"}, search_dirs=[tmp_path])


def test_table_bench_goes_through_the_real_post_chat(monkeypatch, tmp_path):
    """사내 2차: table-bench 도 KeyError 'retry'. 같은 치환이 이 파일에서는 안 먹었고 확인이 없었다.
    전송만 가짜로 두고 provider 해석·본문 조립·재시도 데코레이터는 진짜로 돈다."""
    from app import table_bench as tb
    import pypdfium2 as pdfium
    monkeypatch.setenv("NDT_HCX_TOKEN", "t"); monkeypatch.setenv("NDT_STUDIO_TOKEN", "t")
    # 한 쪽짜리 진짜 PDF 를 만들어 렌더 경로까지 실제로 태운다
    pdf = tmp_path / "[ABD]NP.D.N000.1.0UMA&&BBB&&&.021.DC.0001.E_C02.pdf"
    doc = pdfium.PdfDocument.new(); doc.new_page(200, 100); doc.save(str(pdf)); doc.close()
    truth = tmp_path / "truth.json"
    truth.write_text('{"source": "%s", "page": 1, "rows": [{"kks": "10AAA02BR005", "dout_x_thickness_mm": "88.9×7.1",'
                     ' "document": "SPEC 100-80", "vt_pct": "100", "pt_or_mt_pct": "-", "rt_pct": "-", "ut_pct": "100",'
                     ' "aux_vt_pct": "100", "aux_pt_or_mt_pct": "-"}]}' % str(pdf).replace("\\", "/"), encoding="utf-8")
    md = "| 10AAA02BR005 | 88.9×7.1 | SPEC 100-80 | 100 | - | - | 100 | 100 | - |"
    _fake_transport(monkeypatch, {"HCX-005": md, "gemma4-31b": md})
    rows = {r["provider"]: r for r in tb.run(truth, ["hcx005", "gemma", "nope"])}
    assert rows["hcx005"]["error"] == "" and rows["hcx005"]["accuracy"] == 1.0, rows["hcx005"]
    assert rows["gemma"]["accuracy"] == 1.0
    assert "알 수 없는 provider" in rows["nope"]["error"] and "gemma" in rows["nope"]["error"]   # 아는 이름을 알려준다
