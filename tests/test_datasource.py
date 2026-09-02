# -*- coding: utf-8 -*-
"""datasource 纯逻辑单元测试（无网络）。"""
from datetime import datetime
from zoneinfo import ZoneInfo

from datasource import (
    SinaProvider,
    _classify_ext,
    _diff,
    _ext_session,
    _f,
    _norm_state,
    _parse_sina_ts,
    _pct,
    _resolve_active,
    _us_session_state,
    Quote,
)

ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def _ts(y, mo, d, h, mi=0):
    return _et(y, mo, d, h, mi).timestamp()


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


# ---------------- 美股时段判定（含午夜后隔夜段） ----------------
def test_session_state_regular_weekday():
    assert _us_session_state(_et(2026, 9, 1, 12, 0)) == "REGULAR"


def test_session_state_pre_weekday():
    assert _us_session_state(_et(2026, 9, 1, 7, 0)) == "PRE"
    assert _us_session_state(_et(2026, 9, 1, 4, 0)) == "PRE"       # 边界：含 04:00


def test_session_state_post_evening_and_after_midnight():
    assert _us_session_state(_et(2026, 9, 1, 20, 0)) == "POST"
    # 午夜后仍属盘后（隔夜电子盘），不再回退 CLOSED —— 本次修复的核心
    assert _us_session_state(_et(2026, 9, 2, 0, 30)) == "POST"
    assert _us_session_state(_et(2026, 9, 2, 3, 59)) == "POST"


def test_session_state_weekend():
    assert _us_session_state(_et(2026, 9, 5, 12, 0)) == "CLOSED"   # 周六
    assert _us_session_state(_et(2026, 9, 6, 12, 0)) == "CLOSED"   # 周日


def test_session_state_friday_after_close_into_saturday_small_hours():
    # 周五 16:00 后是盘后；周六凌晨延续周五盘后（隔夜），周日凌晨起休市
    assert _us_session_state(_et(2026, 9, 4, 18, 0)) == "POST"     # 周五傍晚
    assert _us_session_state(_et(2026, 9, 5, 1, 0)) == "POST"      # 周六凌晨
    assert _us_session_state(_et(2026, 9, 6, 1, 0)) == "CLOSED"    # 周日凌晨


# ---------------- 新浪时间戳解析 ----------------
def test_parse_sina_ts_accepts_seconds_and_millis():
    assert _parse_sina_ts(1787913862) == 1787913862.0
    assert _parse_sina_ts("1787913862") == 1787913862.0
    assert _parse_sina_ts(1787913862123) == 1787913862.123
    assert _parse_sina_ts(None) is None
    assert _parse_sina_ts("") is None
    assert _parse_sina_ts("abc") is None
    assert _parse_sina_ts(0) is None          # 占位 0 无效


# ---------------- 延展成交时段归属 ----------------
def test_ext_session_pre_post_and_overnight():
    assert _ext_session(_ts(2026, 9, 1, 7, 0)) == "PRE"            # 盘前清晨
    assert _ext_session(_ts(2026, 9, 1, 20, 0)) == "POST"          # 盘后傍晚
    assert _ext_session(_ts(2026, 9, 2, 1, 0)) == "POST"           # 午夜后隔夜
    assert _ext_session(_ts(2026, 9, 1, 12, 0)) is None            # 常规时段
    assert _ext_session(None) is None


def test_classify_ext_by_timestamp_not_wall_clock():
    # 当前是盘后傍晚，但延展成交发生在盘前清晨 -> 应归 pre 而非 post
    pre, post = _classify_ext(101.0, _ts(2026, 9, 1, 7, 0), "POST")
    assert (pre, post) == (101.0, None)
    # 当前是午夜后，延展成交在昨晚盘后 -> 归 post
    pre, post = _classify_ext(206.0, _ts(2026, 9, 1, 20, 0), "POST")
    assert (pre, post) == (None, 206.0)
    # 无时间戳 -> 退回当前墙钟时段兜底
    pre, post = _classify_ext(101.0, None, "POST")
    assert (pre, post) == (None, 101.0)
    # 无时间戳且当前是常规时段 -> 丢弃（历史行为：不猜）
    pre, post = _classify_ext(101.0, None, "REGULAR")
    assert (pre, post) == (None, None)
    # 无延展价
    assert _classify_ext(None, _ts(2026, 9, 1, 20, 0), "POST") == (None, None)
    assert _classify_ext(0, _ts(2026, 9, 1, 20, 0), "POST") == (None, None)


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
        price, change, pct, pc = _resolve_active(state, 100.0, 2.0, 2.0, 98.0, 101.0, None)
        assert (price, change, pct, pc) == (100.0, 2.0, 2.0, 98.0)


def test_resolve_closed_or_prepre_keeps_last_post_price():
    # 休市/盘前未开但存在盘后价：最新价应保留最后盘后成交，而非跳回常规收盘
    for state in ("CLOSED", "PREPRE"):
        price, change, pct, pc = _resolve_active(state, 200.0, 2.0, 1.0, 198.0, None, 206.0)
        assert price == 206.0
        assert change == _diff(206.0, 200.0)
        assert pct == _pct(206.0, 200.0)
        assert pc == 200.0


def test_resolve_post_without_post_price_falls_back_to_zero():
    price, change, pct, pc = _resolve_active("POST", 100.0, 1.0, 1.0, 99.0, None, None)
    assert (price, change, pct, pc) == (100.0, 0.0, 0.0, 100.0)


# ---------------- 新浪 _parse：核心回归场景（无网络） ----------------
def _sina_line(sym, name, reg, reg_pct, reg_chg, ext, ext_ts, prev_close):
    """构造一条新浪 gb_ 返回行（其余字段留空，仅解析用到的位次）。"""
    f = [name, str(reg), str(reg_pct), "", str(reg_chg)] + [""] * 16
    f += [str(ext), "", "", str(ext_ts), "", str(prev_close)]
    f += [""] * 9   # 补到 36 字段
    return f'var hq_str_gb_{sym}="{",".join(f)}";'


def test_sina_parse_after_midnight_shows_post_price():
    """MRVL 场景：美东午夜后（北京时间白天），最新价应显示昨晚盘后价 206。"""
    txt = _sina_line("MRVL", "迈威尔科技", 200.0, 1.0, 2.0, 206.0,
                     _ts(2026, 9, 1, 20, 0), 198.0)
    q = SinaProvider()._parse(txt, state="POST")["MRVL"]
    assert q.price == 206.0                    # 最新价 = 盘后价，不再是常规收盘 200
    assert q.post_price == 206.0
    assert q.pre_price is None
    assert q.change == 6.0                     # 206 - 200，相对昨收（常规收盘）
    assert q.change_pct == 3.0
    assert q.market_state == "POST"


def test_sina_parse_pre_market_morning_shows_pre_price():
    """美东盘前清晨：延展价按时间戳归为盘前价。"""
    txt = _sina_line("NVDA", "英伟达", 100.0, 1.0, 1.0, 101.5,
                     _ts(2026, 9, 1, 7, 30), 99.0)
    q = SinaProvider()._parse(txt, state="PRE")["NVDA"]
    assert q.price == 101.5
    assert q.pre_price == 101.5
    assert q.post_price is None


def test_sina_parse_morning_residual_overnight_price_is_post():
    """盘前清晨但 [21] 仍是昨晚盘后价（时间戳在昨晚）-> 归 post，不误判为盘前。"""
    txt = _sina_line("AAPL", "苹果", 100.0, 1.0, 1.0, 99.0,
                     _ts(2026, 8, 31, 21, 0), 99.0)
    q = SinaProvider()._parse(txt, state="PRE")["AAPL"]
    assert q.post_price == 99.0
    assert q.pre_price is None
    # 盘前时段以盘前价优先；无盘前成交时价格回落到常规收盘
    assert q.price == 100.0


def test_sina_parse_regular_session_ignores_ext():
    """常规时段：最新价即常规价，延展价不计入 pre/post 字段。"""
    txt = _sina_line("TSLA", "特斯拉", 250.0, 2.0, 5.0, 251.0,
                     _ts(2026, 9, 1, 12, 0), 245.0)
    q = SinaProvider()._parse(txt, state="REGULAR")["TSLA"]
    assert q.price == 250.0
    assert q.pre_price is None and q.post_price is None


def test_sina_parse_weekend_keeps_friday_post_price():
    """周末：时间戳归 post 的延展价保留，最新价显示周五最后一笔盘后成交。"""
    txt = _sina_line("WDC", "西部数据", 459.0, 2.0, 9.0, 462.0,
                     _ts(2026, 9, 4, 19, 30), 450.0)
    q = SinaProvider()._parse(txt, state="CLOSED")["WDC"]
    assert q.price == 462.0
    assert q.post_price == 462.0


def test_sina_parse_no_ts_falls_back_to_wall_clock():
    """[24] 时间戳缺失/为 0：退回按当前时段归位（历史兜底行为）。"""
    txt = _sina_line("KO", "可口可乐", 90.0, 0.5, 0.45, 90.2, 0, 89.5)
    q = SinaProvider()._parse(txt, state="POST")["KO"]
    assert q.post_price == 90.2
    assert q.price == 90.2


def test_sina_parse_skips_malformed_lines():
    txt = 'var hq_str_gb_bogus="too,few,fields";\nnot a line\n'
    assert SinaProvider()._parse(txt, state="POST") == {}


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
