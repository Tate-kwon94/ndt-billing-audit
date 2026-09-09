"""HCX 사용량 이력 — 오늘·이번 주·이번 달·전체 (2026-09-07, 사용자 요청).

예전 data/hcx_daily_count.json 은 {"date","count"} 하나뿐이라 어제 것이 사라졌다. 이제 날짜별
{calls, tokens} 를 쌓고, 옛 모양 파일도 그대로 읽는다. 사내는 호출량을 부서에 통보하므로
"이번 달 몇 회" 가 바로 나와야 한다 (한도가 아니라 확인용).
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import app.hcx_client as hc


@pytest.fixture()
def daily_file(tmp_path, monkeypatch):
    p = tmp_path / "hcx_daily_count.json"
    monkeypatch.setattr(hc, "_daily_count_path", lambda: p)
    return p


def test_bump_accumulates_calls_and_tokens_per_day(daily_file):
    hc._bump_daily(calls=1, tokens=100, day=dt.date(2026, 9, 1))
    hc._bump_daily(calls=1, tokens=150, day=dt.date(2026, 9, 1))
    hc._bump_daily(calls=1, tokens=50, day=dt.date(2026, 9, 7))
    data = json.loads(daily_file.read_text(encoding="utf-8"))
    assert data["days"]["2026-09-01"] == {"calls": 2, "tokens": 250}
    assert data["days"]["2026-09-07"] == {"calls": 1, "tokens": 50}
    # 옛 필드도 유지 — 예전 코드·문서가 읽는다
    assert data["date"] == "2026-09-07" and data["count"] == 1


def test_summary_today_week_month_all(daily_file):
    for d, c, t in [(dt.date(2026, 8, 30), 4, 400), (dt.date(2026, 9, 1), 5, 100),
                    (dt.date(2026, 9, 6), 7, 200), (dt.date(2026, 9, 7), 3, 50)]:
        hc._bump_daily(calls=c, tokens=t, day=d)
    s = hc.usage_summary(today=dt.date(2026, 9, 7))
    assert s["today"] == {"calls": 3, "tokens": 50}
    assert s["week"] == {"calls": 15, "tokens": 350}        # 최근 7일 (09-01 ~ 09-07), 08-30 제외
    assert s["month"] == {"calls": 15, "tokens": 350}       # 2026-09
    assert s["all"] == {"calls": 19, "tokens": 750}
    assert s["days"] == 4


def test_legacy_file_shape_is_still_read(daily_file):
    daily_file.write_text(json.dumps({"date": "2026-09-07", "count": 108}), encoding="utf-8")
    assert hc._load_daily_count(today=dt.date(2026, 9, 7)) == 108
    s = hc.usage_summary(today=dt.date(2026, 9, 7))
    assert s["today"]["calls"] == 108 and s["today"]["tokens"] == 0
    hc._bump_daily(calls=1, tokens=10, day=dt.date(2026, 9, 7))     # 이어서 쌓인다
    assert hc._load_daily_count(today=dt.date(2026, 9, 7)) == 109


def test_load_daily_count_is_zero_on_a_new_day(daily_file):
    hc._bump_daily(calls=5, tokens=1, day=dt.date(2026, 9, 6))
    assert hc._load_daily_count(today=dt.date(2026, 9, 7)) == 0


def test_usage_cli_prints_periods(daily_file):
    from typer.testing import CliRunner
    from app.main import app
    hc._bump_daily(calls=12, tokens=3456, day=dt.date.today())
    r = CliRunner().invoke(app, ["usage"])
    assert r.exit_code == 0, r.output
    assert "오늘" in r.output and "이번 주" in r.output and "이번 달" in r.output and "12" in r.output
