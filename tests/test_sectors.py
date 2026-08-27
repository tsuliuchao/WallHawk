# -*- coding: utf-8 -*-
"""sectors.all_symbols 去重与板块结构完整性。"""
from sectors import SECTORS, all_symbols


def test_all_symbols_deduped():
    syms = all_symbols()
    # 无重复
    assert len(syms) == len(set(syms))
    # NVDA 同时出现在半导体与人工智能概念，只应保留一次
    assert syms.count("NVDA") == 1
    # 与集合大小一致
    assert len(syms) == len({s["symbol"] for sec in SECTORS for s in sec["stocks"]})


def test_all_symbols_nonempty_and_uppercase():
    syms = all_symbols()
    assert len(syms) > 50
    assert all(s == s.upper() for s in syms)


def test_every_sector_has_unique_key_and_stocks():
    keys = [sec["key"] for sec in SECTORS]
    assert len(keys) == len(set(keys))
    for sec in SECTORS:
        assert sec["key"]
        assert sec["name"]
        # 每只股票必有 symbol
        assert all(s["symbol"] for s in sec["stocks"])
