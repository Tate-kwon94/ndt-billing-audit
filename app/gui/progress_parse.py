# -*- coding: utf-8 -*-
"""런처가 로그 줄에서 진행률·사용량을 읽는 쪽. tkinter 없이 import 된다 (테스트용).

형식은 app.progress_fmt 가 만든다. 여기서는 그 형식만 알아본다. 로거 접두어
('INFO app.extractors.pdf_extractor: ') 가 앞에 붙어도 된다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_PROGRESS = re.compile(r"진행 \[(?P<stage>[^\]]+)\] (?P<k>\d+)/(?P<n>\d+)(?: — (?P<note>.*))?\s*$")
_USAGE = re.compile(r"사용량 \[HCX\] 호출 (?P<calls>\d+) · 오늘 (?P<day>\d+) · 토큰 (?P<tokens>\d+)"
                    r"(?: · 이번주 (?P<week>\d+))?(?: · 이번달 (?P<month>\d+))?")


@dataclass(frozen=True)
class Progress:
    stage: str
    k: int
    n: int
    note: str = ""


@dataclass(frozen=True)
class Usage:
    calls: int
    day: int
    tokens: int
    week: Optional[int] = None
    month: Optional[int] = None


def parse_progress(line: str) -> Optional[Progress]:
    m = _PROGRESS.search(line or "")
    if not m:
        return None
    return Progress(m.group("stage"), int(m.group("k")), int(m.group("n")), (m.group("note") or "").strip())


def parse_usage(line: str) -> Optional[Usage]:
    m = _USAGE.search(line or "")
    if not m:
        return None
    w, mo = m.group("week"), m.group("month")
    return Usage(int(m.group("calls")), int(m.group("day")), int(m.group("tokens")),
                 int(w) if w else None, int(mo) if mo else None)


class ProgressTracker:
    """단계별 시계. 같은 단계 안에서 처리 속도로 남은 시간을 추정하고, 단계가 바뀌면 새로 잰다."""

    def __init__(self):
        self.stage: Optional[str] = None
        self.n = 0
        self.k = 0
        self.t0 = 0.0
        self.k0 = 0
        self.now = 0.0
        self.eta_seconds: Optional[float] = None

    @property
    def fraction(self) -> float:
        return (self.k / self.n) if self.n else 0.0

    def update(self, p: Progress, now: float) -> str:
        if p.stage != self.stage or p.n != self.n:
            self.stage, self.n, self.t0, self.k0 = p.stage, p.n, now, p.k
            self.eta_seconds = None
        self.k, self.now = p.k, now
        elapsed = max(0.0, now - self.t0)
        done_since = p.k - self.k0
        if done_since > 0 and elapsed > 0 and p.n > p.k:
            rate = done_since / elapsed
            self.eta_seconds = (p.n - p.k) / rate
        elif p.k >= p.n:
            self.eta_seconds = 0.0
        pct = int(100 * self.fraction)
        s = f"{p.stage} {p.k}/{p.n} ({pct}%) · 경과 {int(elapsed // 60)}분"
        if self.eta_seconds is not None and p.k < p.n:
            s += f" · 남은 예상 {max(1, int(round(self.eta_seconds / 60)))}분"
        if p.note:
            s += f" · {p.note}"
        return s
