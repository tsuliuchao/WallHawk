# -*- coding: utf-8 -*-
"""recommend_sector 关键词匹配推荐。"""
from app import recommend_sector


def test_chinese_name_match():
    assert recommend_sector("英伟达", "NVDA") == "semiconductors"


def test_short_symbol_exact_match_scores_higher():
    # "AMD" 为短代码，仅当与 symbol 精确相等时命中（+2）
    assert recommend_sector("", "AMD") == "semiconductors"


def test_english_long_keyword_substring():
    # "Applied Materials" 长度>=4，做子串匹配
    assert recommend_sector("Applied Materials Inc.", "AMAT") == "semi_equipment"


def test_ev_chinese_match():
    assert recommend_sector("特斯拉", "TSLA") == "ev"


def test_no_match_returns_none():
    assert recommend_sector("某个不存在的公司", "ZZZZZ") is None
    assert recommend_sector("", "") is None


def test_case_insensitive():
    assert recommend_sector("nvidia corp", "nvda") == "semiconductors"


def test_short_code_not_substring_matched():
    # "AI" 短代码不应因出现在英文名里就误判（这里只做精确匹配）
    assert recommend_sector("C3.ai", "AI") is None
