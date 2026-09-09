"""텍스트레이어 품질 채점 — Adobe-good 신뢰 / mediocre 재OCR 자동 판정.

근거: 배포본 CRST 성적서는 텍스트레이어가 있으나 KKS 코드 대부분 깨짐 (측정값).
"있으면 신뢰"가 아니라 품질로 판정해야 정확도 최대화.
"""
from __future__ import annotations

from app.extractors.pdf_extractor import (
    _RE_KKS_CLEAN,
    _RE_STRUCT_COLLAPSE,
)


def test_clean_kks_matches():
    assert _RE_KKS_CLEAN.search("4. KKS code: 10GUC11BR002")
    assert _RE_KKS_CLEAN.search("90GMM91BR001")


def test_clean_kks_multiformat():
    """다중에이전트 분석 확인 케이스 — 과제약이던 패턴들 (4자 시스템코드, MR 접미)."""
    assert _RE_KKS_CLEAN.search("10GMMA01XX001")          # 시스템코드 4자
    assert _RE_KKS_CLEAN.search("11AB01CD001")            # 2자
    assert _RE_KKS_CLEAN.search("10PAB22BR005MR008")      # MR 접미 (용접부 연장)
    assert _RE_KKS_CLEAN.search("20PAB23BR005MR005+20PAB23BR005MR006")  # 복수 병기


def test_garbled_kks_does_not_match():
    # 측정된 실제 깨짐 패턴들
    assert not _RE_KKS_CLEAN.search("KKS code: IOPAB24BR005MR0021")  # I→1, O→0
    assert not _RE_KKS_CLEAN.search("KKS code: BROOS")               # 0→O
    assert not _RE_KKS_CLEAN.search("-------,-oo_u_C_l_l_B_R_00_2_")  # 구조 붕괴
    assert not _RE_KKS_CLEAN.search("90GMLl9BR003MR002")             # 소문자 l 혼입


def test_struct_collapse_detected():
    assert _RE_STRUCT_COLLAPSE.search("-------,-oo_u_C_l_l_B_R_00_2_")
    assert _RE_STRUCT_COLLAPSE.search("_______ ______ __")
    assert _RE_STRUCT_COLLAPSE.search("10 _G_M_L_I _Il 3-l-l(-l5-0")
    # 정상 라인은 붕괴 신호 없음
    assert not _RE_STRUCT_COLLAPSE.search("4. KKS code: 10GUC11BR002")
    assert not _RE_STRUCT_COLLAPSE.search("No. 77-584VMC dated 12.09.2024")
