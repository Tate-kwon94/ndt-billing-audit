"""186쪽 전문 1회 호출(=40003) 대신 NDT 페이지만 창 단위로. LLM 은 봉인."""
from __future__ import annotations

from pathlib import Path

from app.extractors import scwep_parser


class P:
    def __init__(self, i, text): self.page_index, self.text = i, text


def test_select_pages_keyword_plus_neighbors():
    pages = [P(0, "cover"), P(1, "x"), P(2, "liquid penetrant testing"), P(3, "y"), P(4, "z"), P(5, "NDT scope")]
    assert scwep_parser.select_pages(pages) == [1, 2, 3, 4, 5]


def test_windows_respect_limit():
    pages = [P(i, "a" * 1000) for i in range(10)]
    w = scwep_parser._windows(pages, list(range(10)), limit=3500)
    assert len(w) == 4 and all(len(x) <= 3500 for x in w)


def test_merge_dedups_by_method_page_quote():
    a = {"ndt_method": "PT", "page": 25, "quote": "…removed shall be subjected…", "trigger": "t"}
    m = scwep_parser.merge_results([
        {"document_no": "D", "conditional_ndt_requirements": [a], "general_rules": []},
        {"document_no": "D", "conditional_ndt_requirements": [dict(a)], "general_rules": [{"ndt_method": "RT", "page": 3, "quote": "q"}]},
    ])
    assert len(m["conditional_ndt_requirements"]) == 1 and len(m["general_rules"]) == 1 and m["document_no"] == "D"


def test_ingest_calls_per_window(monkeypatch, tmp_path):
    sent = []
    class R: parsed = {"document_no": "x", "conditional_ndt_requirements": [], "general_rules": []}
    class X: pages = [P(i, ("penetrant " if i % 40 == 0 else "") + "b" * 30000) for i in range(120)]
    monkeypatch.setattr(scwep_parser, "extract", lambda p: X())
    monkeypatch.setattr(scwep_parser, "call", lambda stage, payload, **k: (sent.append(payload), R())[1])
    monkeypatch.setattr(scwep_parser, "upsert_standard", lambda *a, **k: None)
    monkeypatch.setattr(scwep_parser, "init_db", lambda: None)
    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass
    monkeypatch.setattr(scwep_parser, "get_session", lambda: S())
    out = scwep_parser.ingest(tmp_path / "NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E.pdf")
    assert 2 <= len(sent) <= 9 and all(len(p["text_full"]) <= 60_000 for p in sent)
    assert out["_windows"] == len(sent) and out["applicable_scope"]["kks"]["systems"] == ["LAH"]


def test_skip_reason_only_pairs_scan_with_text_twin():
    """스캔본끼리 접두 관계이거나 무관한 짧은 stem 이 있어도 텍스트판이 없으면 건너뛰지 않는다."""
    from app.main import _skip_reason
    assert _skip_reason(Path("X_scan_v2.pdf"), {"X_scan", "X_scan_v2"}) is None
    assert _skip_reason(Path("NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E_C01_scan.pdf"),
                        {"NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001", "NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E_C01_scan"}) is None
    assert _skip_reason(Path("X.E_C01_scan.pdf"), {"X.E", "X.E_C01_scan"}) == "스캔본 (텍스트판 있음)"
    assert _skip_reason(Path("lone_scan.pdf"), {"lone_scan"}) is None


# ─────────── C3: 파싱 실패한 창은 조용히 사라지면 안 된다 (2026-09-05 전체점검) ───────────

def _stub_session(monkeypatch):
    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass
    monkeypatch.setattr(scwep_parser, "upsert_standard", lambda *a, **k: None)
    monkeypatch.setattr(scwep_parser, "init_db", lambda: None)
    monkeypatch.setattr(scwep_parser, "get_session", lambda: S())


def test_unparsed_window_forces_needs_review(monkeypatch, tmp_path):
    """3창 중 1창이 파싱 실패하면 '문서가 침묵했다' 고 말할 자격이 없다 → needs_review."""
    _stub_session(monkeypatch)
    class X: pages = [P(i, "penetrant " + "b" * 25000) for i in range(6)]
    monkeypatch.setattr(scwep_parser, "extract", lambda p: X())
    seen = []

    def _call(stage, payload, **k):
        seen.append(payload)
        class R: parsed = None if len(seen) == 2 else {
            "document_no": "x", "conditional_ndt_requirements": [], "general_rules": []}
        return R()
    monkeypatch.setattr(scwep_parser, "call", _call)

    out = scwep_parser.ingest(tmp_path / "NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E.pdf")
    assert len(seen) == 3
    assert out["_windows_attempted"] == 3 and out["_windows"] == 2
    assert out["needs_review"] is True
    assert any("파싱 실패" in r for r in out["review_reasons"])


def test_all_windows_unparsed_still_returns_a_flagged_stub(monkeypatch, tmp_path):
    _stub_session(monkeypatch)
    class X: pages = [P(i, "penetrant " + "b" * 25000) for i in range(6)]
    monkeypatch.setattr(scwep_parser, "extract", lambda p: X())
    class R: parsed = None
    monkeypatch.setattr(scwep_parser, "call", lambda stage, payload, **k: R())
    out = scwep_parser.ingest(tmp_path / "NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E.pdf")
    assert out["needs_review"] is True and out["_schema_version"] == scwep_parser.SCWEP_SCHEMA_VERSION
    assert out["document_no"] == "NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E"


def test_extraction_confidence_is_the_minimum_across_windows():
    parts = [{"document_no": "D", "extraction_confidence": 0.9},
             {"document_no": "D", "extraction_confidence": 0.4},
             {"document_no": "D", "extraction_confidence": None}]
    assert scwep_parser.merge_results(parts, attempted=3)["extraction_confidence"] == 0.4


def test_windows_split_a_single_oversized_page():
    """한 쪽이 창 크기를 넘으면 그 쪽을 쪼갠다 — 예전엔 창 하나가 limit 을 넘겨 40003 이 났다."""
    pages = [P(0, "c" * 3000)]
    w = scwep_parser._windows(pages, [0], limit=1000)
    assert len(w) >= 3 and all(len(x) <= 1000 for x in w)
    assert all("---PAGE 1---" in x for x in w)
    assert "".join(x.split("---PAGE 1---")[1].strip() for x in w).replace("(part", "").count("c") >= 3000


def test_scan_skip_applies_only_to_scwep(monkeypatch, tmp_path):
    """M9: 스캔본/러시아어판 건너뛰기는 SCWEP 전제 규칙이다. 규격·계약 폴더까지 지우면 안 된다."""
    from app import main as cli
    from app.extractors import code_indexer

    folder = tmp_path / "codes"
    folder.mkdir()
    for n in ("X.E.pdf", "X.E_C01_scan.pdf", "X.R.pdf"):
        (folder / n).write_bytes(b"%PDF-1.4\n")
    seen = []
    monkeypatch.setattr(code_indexer, "ingest", lambda f: (seen.append(f.name), {})[1])
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda **k: "t")
    cli.ingest_standards(folder=folder, doc_type="code", verbose=False)
    assert sorted(seen) == ["X.E.pdf", "X.E_C01_scan.pdf", "X.R.pdf"]

    seen_scwep = []
    monkeypatch.setattr(scwep_parser, "ingest", lambda f: (seen_scwep.append(f.name), {})[1])
    cli.ingest_standards(folder=folder, doc_type="scwep", verbose=False)
    assert sorted(seen_scwep) == ["X.E.pdf"]


def test_non_dict_applicable_scope_is_replaced_before_stamping_kks(monkeypatch, tmp_path):
    """M10: LLM 이 applicable_scope 를 문자열로 내도 죽지 않고 kks 를 각인한다."""
    class R: parsed = {"document_no": "x", "applicable_scope": "all mechanical works",
                       "conditional_ndt_requirements": [], "general_rules": []}
    class X: pages = [P(0, "liquid penetrant testing")]
    monkeypatch.setattr(scwep_parser, "extract", lambda p: X())
    monkeypatch.setattr(scwep_parser, "call", lambda stage, payload, **k: R())
    monkeypatch.setattr(scwep_parser, "upsert_standard", lambda *a, **k: None)
    monkeypatch.setattr(scwep_parser, "init_db", lambda: None)
    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass
    monkeypatch.setattr(scwep_parser, "get_session", lambda: S())
    out = scwep_parser.ingest(tmp_path / "NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E.pdf")
    assert out["applicable_scope"]["kks"]["systems"] == ["LAH"]
