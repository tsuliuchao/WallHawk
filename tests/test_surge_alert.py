# -*- coding: utf-8 -*-
"""surge_alert 价格环比检查逻辑（mock 掉 _send 与状态/埋点文件）。"""
import json
import os
from types import SimpleNamespace

import pytest

import surge_alert


def _quote(price, change_pct, market_state="REGULAR"):
    return SimpleNamespace(price=price, change_pct=change_pct, market_state=market_state)


@pytest.fixture
def surge_env(monkeypatch, tmp_path):
    """隔离状态/埋点文件并拦截发送，返回 (sent, data_dir)。"""
    monkeypatch.setattr(surge_alert, "STATE_PATH", str(tmp_path / "surge_state.json"))
    monkeypatch.setattr(surge_alert, "DATA_DIR", str(tmp_path / "watch_track"))
    monkeypatch.setattr(surge_alert.Config, "SURGE_DELTA_PCT", 1.0)
    monkeypatch.setattr(surge_alert.Config, "SURGE_SESSION_ONLY", 1)
    surge_alert._state = None
    sent = []

    def fake_send(triggered):
        sent.append([dict(t) for t in triggered])
        return True

    monkeypatch.setattr(surge_alert, "_send", fake_send)
    return sent, str(tmp_path / "watch_track")


def _daily_file(data_dir):
    path = os.path.join(data_dir, surge_alert._today() + ".jsonl")
    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_first_round_only_builds_baseline(surge_env):
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    out = surge_alert.check(watch, {"NVDA": _quote(120.0, 2.7)})
    assert out == []
    assert sent == []


def test_delta_beyond_threshold_triggers(surge_env):
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120.0, 1.5)})
    out = surge_alert.check(watch, {"NVDA": _quote(121.4, 2.6)})
    assert len(out) == 1
    assert out[0]["symbol"] == "NVDA"
    assert out[0]["price"] == 121.4
    assert out[0]["change_pct"] == 2.6
    assert out[0]["delta"] == pytest.approx(1.17)
    assert out[0]["minutes"] >= 0
    assert len(sent) == 1  # 合并一条


def test_negative_delta_accelerating_drop_triggers(surge_env):
    """加速下跌同样触发。"""
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120.0, 1.0)})
    out = surge_alert.check(watch, {"NVDA": _quote(118.0, -0.6)})
    assert len(out) == 1
    assert out[0]["delta"] == pytest.approx(-1.67)
    assert len(sent) == 1


def test_small_delta_no_push(surge_env):
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120.0, 1.5)})
    out = surge_alert.check(watch, {"NVDA": _quote(120.5, 2.0)})
    assert out == []
    assert sent == []


def test_price_move_without_pct_change_triggers(surge_env):
    """涨幅分母换挡（口径跳变）但现价真实变动时仍触发：现价口径不受影响。"""
    sent, _ = surge_env
    watch = [("MRNA", "莫德纳")]
    # 两轮涨幅相同（-0.5% == -0.5%）但现价变了 2% —— 旧涨幅环比口径会漏报
    surge_alert.check(watch, {"MRNA": _quote(200.0, -0.5)})
    out = surge_alert.check(watch, {"MRNA": _quote(204.0, -0.5)})
    assert len(out) == 1
    assert out[0]["delta"] == pytest.approx(2.0)


def test_close_transition_no_false_signal(surge_env):
    """收盘切换（REGULAR→POST）不再产生假异动。

    旧口径实测案例：10-10 03:58 MRNA 涨 +13.98%，04:08（收盘后）盘后价
    224.87 相对昨收仍涨，但涨幅口径换成相对今收 → 显示 -0.06%，环比推了
    "10分钟 -14.04pp" 的假暴跌。现价口径下 224.54→224.87 只 +0.15%，
    不触发。
    """
    sent, _ = surge_env
    watch = [("MRNA", "莫德纳")]
    # 收盘前最后一轮 REGULAR：现价 224.54，涨 +13.98%
    surge_alert.check(watch, {"MRNA": _quote(224.54, 13.98)})
    # 收盘后首轮 POST：现价 224.87（盘后成交，价格连续），涨幅口径归位显示 -0.06%
    out = surge_alert.check(watch, {"MRNA": _quote(224.87, -0.06, market_state="POST")})
    assert out == []
    assert sent == []
    # 盘后真实拉升仍能触发
    out = surge_alert.check(watch, {"MRNA": _quote(228.0, 1.3, market_state="POST")})
    assert len(out) == 1
    assert out[0]["delta"] == pytest.approx((228.0 - 224.87) / 224.87 * 100, abs=0.05)


def test_multiple_triggered_merged_into_one_message(surge_env):
    sent, _ = surge_env
    watch = [("NVDA", "英伟达"), ("AAPL", "苹果"), ("TSLA", "特斯拉")]
    quotes = {"NVDA": _quote(120, 1.0), "AAPL": _quote(190, 0.5), "TSLA": _quote(250, 3.0)}
    surge_alert.check(watch, quotes)
    out = surge_alert.check(watch, {
        "NVDA": _quote(121.5, 2.3),   # +1.25% 触发
        "AAPL": _quote(190.2, 0.6),   # +0.11% 不触发
        "TSLA": _quote(252.5, 4.1),   # +1.0% 触发
    })
    assert [t["symbol"] for t in out] == ["NVDA", "TSLA"]
    assert len(sent) == 1
    assert [t["symbol"] for t in sent[0]] == ["NVDA", "TSLA"]


def test_baseline_updates_after_push_so_no_repeat(surge_env):
    """推送后基线更新为本轮现价，价格维持不变不再推送。"""
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0)})
    surge_alert.check(watch, {"NVDA": _quote(121.5, 2.3)})
    out = surge_alert.check(watch, {"NVDA": _quote(121.6, 2.4)})
    assert out == []
    assert len(sent) == 1


def test_closed_market_skipped_when_session_only(surge_env):
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    # 首轮 REGULAR 建立基线，随后休市 → 跳过且不更新基线
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0)})
    out = surge_alert.check(watch, {"NVDA": _quote(118, -2.0, market_state="CLOSED")})
    assert out == []
    assert sent == []
    # 恢复交易时段后，仍与休市前基线比较
    out = surge_alert.check(watch, {"NVDA": _quote(117, -3.0)})
    assert len(out) == 1


def test_session_only_disabled_checks_all_day(surge_env, monkeypatch):
    monkeypatch.setattr(surge_alert.Config, "SURGE_SESSION_ONLY", 0)
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0)})
    out = surge_alert.check(watch, {"NVDA": _quote(118.5, -1.2, market_state="CLOSED")})
    assert len(out) == 1  # -1.25%，全天检查模式下休市也推


def test_date_rollover_resets_baseline(surge_env, monkeypatch):
    """跨美东交易日后基线清空（新交易日昨收换挡），首轮只重建基线。"""
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120, 3.0)})
    fake_dates = iter(["2000-01-01"])
    monkeypatch.setattr(surge_alert, "_today", lambda: next(fake_dates))
    # 新交易日开盘跳空低开，无基线可比较 → 不推送
    out = surge_alert.check(watch, {"NVDA": _quote(115, -4.2)})
    assert out == []
    assert sent == []


def test_send_failure_keeps_baseline_for_retry(surge_env, monkeypatch):
    """发送失败时保留旧基线，下一轮以累计环比再次触发。"""
    sent, _ = surge_env
    monkeypatch.setattr(surge_alert, "_send", lambda triggered: False)
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0)})
    out = surge_alert.check(watch, {"NVDA": _quote(121.5, 2.3)})
    assert out == []  # 未送达
    # 恢复发送：价格继续走高，与旧基线(120)的累计环比仍超阈值
    monkeypatch.setattr(surge_alert, "_send", lambda triggered: True)
    out = surge_alert.check(watch, {"NVDA": _quote(122.5, 3.1)})
    assert len(out) == 1
    assert out[0]["delta"] == pytest.approx(2.08)


def test_removed_from_watch_prunes_baseline(surge_env):
    """移出「今日关注」后陈旧基线被清理，重新加入时重新建基线。"""
    sent, _ = surge_env
    surge_alert.check([("NVDA", "英伟达"), ("AAPL", "苹果")],
                      {"NVDA": _quote(120, 1.0), "AAPL": _quote(190, 0.5)})
    surge_alert.check([("NVDA", "英伟达")], {"NVDA": _quote(121, 2.0)})
    assert "AAPL" not in surge_alert._load_state()["pcts"]


def test_daily_file_records_snapshots_with_pushed_flag(surge_env):
    sent, data_dir = surge_env
    watch = [("NVDA", "英伟达"), ("AAPL", "苹果")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0), "AAPL": _quote(190, 0.5)})
    surge_alert.check(watch, {"NVDA": _quote(121.5, 2.3), "AAPL": _quote(190.2, 0.6)})
    rows = _daily_file(data_dir)
    assert len(rows) == 4
    by_round = [r for r in rows if r["symbol"] == "NVDA"]
    assert by_round[0]["delta"] is None and by_round[0]["pushed"] is False
    assert by_round[1]["delta"] == pytest.approx(1.25) and by_round[1]["pushed"] is True
    aapl_last = [r for r in rows if r["symbol"] == "AAPL"][-1]
    assert aapl_last["pushed"] is False
    for r in rows:
        assert {"ts", "symbol", "name", "price", "change_pct", "delta", "minutes", "pushed"} <= set(r)


def test_pushed_only_true_after_send_success(surge_env, monkeypatch):
    """发送失败时快照 pushed 保持 False，如实记录漏推。"""
    sent, data_dir = surge_env
    monkeypatch.setattr(surge_alert, "_send", lambda triggered: False)
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0)})
    out = surge_alert.check(watch, {"NVDA": _quote(121.5, 2.3)})
    assert out == []
    rows = _daily_file(data_dir)
    pushed_rows = [r for r in rows if r["pushed"]]
    assert pushed_rows == []


def test_disabled_by_zero_threshold(surge_env, monkeypatch):
    monkeypatch.setattr(surge_alert.Config, "SURGE_DELTA_PCT", 0.0)
    sent, data_dir = surge_env
    surge_alert.check([("NVDA", "英伟达")], {"NVDA": _quote(120, 1.0)})
    out = surge_alert.check([("NVDA", "英伟达")], {"NVDA": _quote(121.5, 2.5)})
    assert out == []
    assert sent == []
    assert not os.path.exists(data_dir)  # 关闭时不写埋点


def test_state_persisted_across_reload(surge_env, monkeypatch):
    """重启（_state 置空重载）后沿用已持久化基线。"""
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0)})
    surge_alert._state = None  # 模拟重启
    out = surge_alert.check(watch, {"NVDA": _quote(121.5, 2.3)})
    assert len(out) == 1
    assert out[0]["delta"] == pytest.approx(1.25)


def test_legacy_pct_baseline_discarded(surge_env):
    """旧版基线（存涨幅数值 float）载入时被丢弃，按首轮重建。"""
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0)})
    # 手工把基线改成旧版 float 格式，模拟升级场景
    surge_alert._load_state()["pcts"]["NVDA"] = 1.0
    surge_alert._save_state()
    surge_alert._state = None  # 模拟重启重载
    out = surge_alert.check(watch, {"NVDA": _quote(121.5, 2.3)})
    assert out == []  # 旧基线被丢弃 → 本轮只重建
    assert sent == []


def test_missing_quote_or_pct_skipped(surge_env):
    sent, _ = surge_env
    watch = [("NVDA", "英伟达"), ("BAD", "无行情")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0), "BAD": _quote(None, None)})
    out = surge_alert.check(watch, {"NVDA": _quote(121.5, 2.3), "BAD": _quote(None, None)})
    assert len(out) == 1
