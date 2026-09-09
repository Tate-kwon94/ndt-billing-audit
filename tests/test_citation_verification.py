"""code_lookup 인용문이 실제 원문에서 확인되는지 — 근거 게이트가 믿기 전에."""
import app.analyzers.compliance as comp


def test_code_lookup_marks_fabricated_quote(monkeypatch):
    snippets = [{"doc": "SP 70", "page": 88, "file": "sp70.pdf",
                 "text": "VT of welded joints shall be 100 % for all types.",
                 "chunk_source": "text", "confidence": 0.9}]
    monkeypatch.setattr(comp.code_indexer, "search", lambda q, top_k=3: snippets)

    class R:
        parsed = {"found_in_context": True, "citations": [
            {"doc": "SP 70", "page": 88, "quote": "VT of welded joints shall be 100 % for all types"},
            {"doc": "SP 70", "page": 88, "quote": "PT of every joint is mandatory without exception"},
        ]}
    monkeypatch.setattr(comp, "call", lambda stage, payload, **k: R())
    refs = comp._code_lookup({"ndt_method": "VT", "joint_no": "J1"})
    assert refs[0]["quote_verified"] is True and refs[0]["needs_review"] is False
    assert refs[1]["quote_verified"] is False and refs[1]["needs_review"] is True
