# -*- coding: utf-8 -*-
"""datasource 纯逻辑单元测试（无网络）。"""
from datasource import Quote, _diff, _f, _norm_state, _pct, _resolve_active


# ---------------- 工具函数 ----------------
def test_f_parses_and_guards():
    assert _f("12.5") == 12.5
    assert _f(7) == 7.0
    assert _f(None) is None
    assert _f("abc") is None
    assert _f("") is None


def test_pct_and_diff():
    assert _pct(101, 100) == 1.0
    assert _pct(90, 100) == -10.0
    assert _pct(None, 100) is None
    assert _pct(100, None) is None
    assert _pct(100, 0) is None          # 昨收为 0 视为无效
    assert _diff(101, 100) == 1.0
    assert _diff(None, 100) is None


def test_norm_state():
    assert _norm_state("PRE") == "PRE"
    assert _norm_state("prepre") == "PREPRE"
    assert _norm_state("POSTPOST") == "POST"
    assert _norm_state("REGULAR") == "REGULAR"
    assert _norm_state("CLOSED") == "CLOSED"
    assert _norm_state("") == "UNKNOWN"
    assert _norm_state(None) == "UNKNOWN"
    assert _norm_state("bogus") == "UNKNOWN"


# ---------------- _resolve_active 时段解析 ----------------
def test_resolve_pre_with_pre_price():
    price, change, pct, pc = _resolve_active("PRE", 100.0, 1.0, 1.0, 99.0, 101.0, None)
    assert price == 101.0
    assert change == _diff(101.0, 100.0)      # 盘前价相对昨收
    assert pct == _pct(101.0, 100.0)
    assert pc == 100.0                        # 昨收即常规价


def test_resolve_pre_without_pre_price_falls_back_to_zero():
    price, change, pct, pc = _resolve_active("PRE", 100.0, 1.0, 1.0, 99.0, None, None)
    assert (price, change, pct, pc) == (100.0, 0.0, 0.0, 100.0)


def test_resolve_pre_zero_price_treated_as_no_trade():
    price, change, pct, pc = _resolve_active("PRE", 100.0, 1.0, 1.0, 99.0, 0, None)
    assert (price, change, pct, pc) == (100.0, 0.0, 0.0, 100.0)


def test_resolve_post_with_post_price():
    price, change, pct, pc = _resolve_active("POST", 100.0, 1.0, 1.0, 99.0, None, 98.0)
    assert price == 98.0
    assert change == _diff(98.0, 100.0)
    assert pct == _pct(98.0, 100.0)
    assert pc == 100.0


def test_resolve_regular_and_closed_use_regular_quote():
    for state in ("REGULAR", "CLOSED"):
        price, change, pct, pc = _resolve_active(state, 100.0, 2.0, 2.0, 98.0, 101.0, 99.0)
        assert (price, change, pct, pc) == (100.0, 2.0, 2.0, 98.0)


# ---------------- Quote.to_dict ----------------
def test_quote_to_dict_roundtrip():
    q = Quote(symbol="NVDA", name="英伟达", price=123.45, prev_close=120.0,
              change=3.45, change_pct=2.88, regular_price=123.45,
              regular_change_pct=2.88, pre_price=None, pre_change_pct=None,
              post_price=None, post_change_pct=None, market_state="REGULAR",
              currency="USD", last_updated=1700000000, source="SINA")
    d = q.to_dict()
    assert d["symbol"] == "NVDA"
    assert d["price"] == 123.45
    assert d["market_state"] == "REGULAR"
    assert d["source"] == "SINA"
    # asdict 应包含全部字段
    expected = {"symbol", "name", "price", "prev_close", "change", "change_pct",
                "regular_price", "regular_change_pct", "pre_price",
                "pre_change_pct", "post_price", "post_change_pct",
                "market_state", "currency", "last_updated", "source"}
    assert set(d) == expected
