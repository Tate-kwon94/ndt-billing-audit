"""실 SCWEP 샘플(사외 전용) — 러그 제거 PT 문장이 LLM 창에 실제로 들어가는지, 스캔·러문이 제외되는지."""
from __future__ import annotations

from pathlib import Path
import pytest

from app.extractors import scwep_parser

SAMPLE_DIR = Path("samples/scwep")
LAH = SAMPLE_DIR / "NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E.pdf"
pytestmark = pytest.mark.skipif(not LAH.exists(), reason="실 SCWEP 샘플 없음")


def test_lug_removal_sentence_reaches_a_window(monkeypatch):
    sent = []
    class R: parsed = {"document_no": "x", "conditional_ndt_requirements": [], "general_rules": []}
    monkeypatch.setattr(scwep_parser, "call", lambda stage, payload, **k: (sent.append(payload["text_full"]), R())[1])
    monkeypatch.setattr(scwep_parser, "upsert_standard", lambda *a, **k: None)
    monkeypatch.setattr(scwep_parser, "init_db", lambda: None)
    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass
    monkeypatch.setattr(scwep_parser, "get_session", lambda: S())
    # 표 전사(HCX-005)는 이 테스트의 관심사가 아니다 — 켜 두면 실 25쪽을 렌더·점수 매기고 서버 없는 vision 호출을 기다린다.
    monkeypatch.setattr(scwep_parser, "page_records", lambda pdf_path, extracted, **kw: (
        [{"page": p.page_index + 1, "text": p.text, "source": p.source} for p in extracted.pages],
        {"pages": len(extracted.pages), "table_like": 0, "transcribed": 0, "needs_review": 0, "vlm_failed": 0}))
    scwep_parser.ingest(LAH)
    assert 1 <= len(sent) <= 8 and all(len(t) <= 60_000 for t in sent)
    assert any("temporary process devices have been removed" in t for t in sent)


def test_folder_dedup_keeps_only_english_text_versions():
    """규칙을 단언한다 — 건수를 못 박지 않는다 (2026-09-06 새 SCWEP 'PAB …E=C01, 10UMA HC WEP CW Pipe 설치.pdf' 추가로 4건)."""
    from app.main import _skip_reason
    files = sorted(SAMPLE_DIR.glob("*.pdf"))
    stems = {p.stem for p in files}
    kept = [p.name for p in files if _skip_reason(p, stems) is None]
    assert kept, "영문 텍스트판이 하나는 남아야 한다"
    assert all("_scan" not in n for n in kept)                 # 스캔본은 텍스트판이 있으면 건너뜀
    assert all(not n.endswith(".R.pdf") for n in kept)          # 러시아어판은 영문판이 있으면 건너뜀
    assert all(".KE.0001.E" in n for n in kept)                 # 남은 건 전부 영문 텍스트판 ('.E' 또는 '.E=C01, …' 꼬리)
    systems = [n.split("&&")[1][:3] for n in kept]              # LAH / LBA / LCA / PAB — 문서마다 하나만
    assert len(systems) == len(set(systems))

