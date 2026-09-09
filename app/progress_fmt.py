# -*- coding: utf-8 -*-
"""진행률·사용량 한 줄 형식 — 만드는 곳은 여기 하나, 읽는 곳은 app.gui.progress_parse 하나.

왜 (2026-09-07): 첫 실행이 몇 시간이라 런처의 빙글빙글 바만으로는 멈춘 건지 알 수 없다. 어느 단계가
찍든 런처가 반드시 알아보려면 형식이 한 곳에서 나와야 한다. 숫자는 천 단위 구분 없이 찍는다(파싱 단순).
"""
from __future__ import annotations

from typing import Optional


def progress_line(stage: str, k: int, n: int, note: str = "") -> str:
    """예: '진행 [OCR] 27/51 — NP.D.N000.4.pdf'  /  '진행 [행] 250/4395'"""
    s = f"진행 [{stage}] {int(k)}/{int(n)}"
    return f"{s} — {note}" if note else s


def usage_line(*, calls: int, day: int, tokens: int,
               week: Optional[int] = None, month: Optional[int] = None) -> str:
    """예: '사용량 [HCX] 호출 1234 · 오늘 5678 · 토큰 9876543 · 이번주 7000 · 이번달 20000'"""
    s = f"사용량 [HCX] 호출 {int(calls)} · 오늘 {int(day)} · 토큰 {int(tokens)}"
    if week is not None:
        s += f" · 이번주 {int(week)}"
    if month is not None:
        s += f" · 이번달 {int(month)}"
    return s
