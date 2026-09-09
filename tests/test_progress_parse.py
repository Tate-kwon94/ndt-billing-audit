"""진행률·사용량 표시 — 런처가 로그에서 읽는 한 줄 형식 (2026-09-07, 사용자 요청).

첫 실행이 몇 시간이라 런처의 빙글빙글 바만으로는 멈춘 건지 알 수 없다. 규칙은 셋:
  1) 형식은 app.progress_fmt 한 곳에서 만든다 — 어느 단계가 찍든 런처가 반드시 알아본다.
  2) 런처는 그 형식만 파싱한다 (app.gui.progress_parse, tkinter 없이 import 가능).
  3) 사용량(호출·토큰)도 같은 방식 — hcx_client 가 주기적으로, 명령 끝에 합계.
"""
from __future__ import annotations

from app import progress_fmt as pf
from app.gui import progress_parse as pp


def test_progress_line_round_trips_through_parser():
    line = pf.progress_line("OCR", 27, 51, "NP.D.N000.4.pdf")
    p = pp.parse_progress(line)
    assert p is not None
    assert (p.stage, p.k, p.n, p.note) == ("OCR", 27, 51, "NP.D.N000.4.pdf")
    assert pp.parse_progress(pf.progress_line("행", 250, 4395)) == pp.Progress("행", 250, 4395, "")


def test_parser_ignores_ordinary_log_lines():
    for line in ["INFO app.hcx_client: HCX code_lookup | model=HCX-007 | mode=live | payload=930B",
                 "  • gost 10922-2012_en.pdf", "✓ 4개 적재 완료", "Grouped into 8 drawing sets (unclassified=0)"]:
        assert pp.parse_progress(line) is None and pp.parse_usage(line) is None


def test_parser_accepts_logger_prefix():
    """런처 창에는 'INFO app.extractors.pdf_extractor: ' 같은 접두어가 붙어서 온다."""
    line = "INFO app.extractors.pdf_extractor: " + pf.progress_line("OCR", 3, 3, "blank3.pdf")
    assert pp.parse_progress(line) == pp.Progress("OCR", 3, 3, "blank3.pdf")


def test_usage_line_round_trips():
    line = pf.usage_line(calls=1234, day=5678, tokens=9_876_543)
    u = pp.parse_usage("INFO app.hcx_client: " + line)
    assert u == pp.Usage(calls=1234, day=5678, tokens=9_876_543)


def test_tracker_gives_percent_and_eta_and_resets_on_new_stage():
    t = pp.ProgressTracker()
    s1 = t.update(pp.Progress("OCR", 10, 100, "a.pdf"), now=100.0)
    s2 = t.update(pp.Progress("OCR", 50, 100, "a.pdf"), now=140.0)     # 40초에 40쪽 → 남은 50쪽 ≈ 50초
    assert "OCR 50/100" in s2 and "50%" in s2
    assert t.eta_seconds is not None and 45 <= t.eta_seconds <= 55
    assert t.fraction == 0.5
    s3 = t.update(pp.Progress("행", 1, 4395), now=200.0)                 # 단계가 바뀌면 시계도 새로
    assert "행 1/4395" in s3 and t.eta_seconds is None
    assert "a.pdf" not in s3


def test_tracker_status_text_is_human_readable_korean():
    t = pp.ProgressTracker()
    t.update(pp.Progress("행", 0, 4395), now=0.0)
    s = t.update(pp.Progress("행", 2000, 4395), now=3600.0)
    assert "남은 예상" in s and "경과 60분" in s
