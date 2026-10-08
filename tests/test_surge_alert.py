# -*- coding: utf-8 -*-
"""surge_alert 涨幅环比检查逻辑（mock 掉 _send 与状态/埋点文件）。"""
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
    out = surge_alert.check(watch, {"NVDA": _quote(122.4, 2.7)})
    assert len(out) == 1
    assert out[0]["symbol"] == "NVDA"
    assert out[0]["price"] == 122.4
    assert out[0]["change_pct"] == 2.7
    assert out[0]["delta"] == pytest.approx(1.2)
    assert len(sent) == 1  # 合并一条


def test_negative_delta_accelerating_drop_triggers(surge_env):
    """加速下跌同样触发。"""
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120.0, 1.0)})
    out = surge_alert.check(watch, {"NVDA": _quote(118.0, -0.5)})
    assert len(out) == 1
    assert out[0]["delta"] == pytest.approx(-1.5)
    assert len(sent) == 1


def test_small_delta_no_push(surge_env):
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120.0, 1.5)})
    out = surge_alert.check(watch, {"NVDA": _quote(120.5, 2.1)})
    assert out == []
    assert sent == []


def test_multiple_triggered_merged_into_one_message(surge_env):
    sent, _ = surge_env
    watch = [("NVDA", "英伟达"), ("AAPL", "苹果"), ("TSLA", "特斯拉")]
    quotes = {"NVDA": _quote(120, 1.0), "AAPL": _quote(190, 0.5), "TSLA": _quote(250, 3.0)}
    surge_alert.check(watch, quotes)
    out = surge_alert.check(watch, {
        "NVDA": _quote(121.5, 2.5),   # +1.5 触发
        "AAPL": _quote(190.2, 0.7),   # +0.2 不触发
        "TSLA": _quote(252, 4.4),     # +1.4 触发
    })
    assert [t["symbol"] for t in out] == ["NVDA", "TSLA"]
    assert len(sent) == 1
    assert [t["symbol"] for t in sent[0]] == ["NVDA", "TSLA"]


def test_baseline_updates_after_push_so_no_repeat(surge_env):
    """推送后基线更新为本轮涨幅，涨幅维持不变不再推送。"""
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0)})
    surge_alert.check(watch, {"NVDA": _quote(121.5, 2.5)})
    out = surge_alert.check(watch, {"NVDA": _quote(121.6, 2.6)})
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
    out = surge_alert.check(watch, {"NVDA": _quote(118, -2.0, market_state="CLOSED")})
    assert len(out) == 1


def test_date_rollover_resets_baseline(surge_env, monkeypatch):
    """跨日后基线清空（当日常规涨幅归零），首轮只重建基线。"""
    sent, _ = surge_env
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120, 3.0)})
    fake_dates = iter(["2000-01-01"])
    monkeypatch.setattr(surge_alert, "_today", lambda: next(fake_dates))
    # 昨天收在 +3%，今天开盘 -1%（相对昨收跌幅），无基线可比较 → 不推送
    out = surge_alert.check(watch, {"NVDA": _quote(115, -1.0)})
    assert out == []
    assert sent == []


def test_send_failure_keeps_baseline_for_retry(surge_env, monkeypatch):
    """发送失败时保留旧基线，下一轮以累计环比再次触发。"""
    sent, _ = surge_env
    monkeypatch.setattr(surge_alert, "_send", lambda triggered: False)
    watch = [("NVDA", "英伟达")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0)})
    out = surge_alert.check(watch, {"NVDA": _quote(121.5, 2.5)})
    assert out == []  # 未送达
    # 恢复发送：涨幅继续走高，与旧基线(1.0)的累计环比 2.0 仍超阈值
    monkeypatch.setattr(surge_alert, "_send", lambda triggered: True)
    out = surge_alert.check(watch, {"NVDA": _quote(122.0, 3.0)})
    assert len(out) == 1
    assert out[0]["delta"] == pytest.approx(2.0)


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
    surge_alert.check(watch, {"NVDA": _quote(121.5, 2.5), "AAPL": _quote(190.2, 0.7)})
    rows = _daily_file(data_dir)
    assert len(rows) == 4
    by_round = [r for r in rows if r["symbol"] == "NVDA"]
    assert by_round[0]["delta"] is None and by_round[0]["pushed"] is False
    assert by_round[1]["delta"] == pytest.approx(1.5) and by_round[1]["pushed"] is True
    aapl_last = [r for r in rows if r["symbol"] == "AAPL"][-1]
    assert aapl_last["pushed"] is False
    for r in rows:
        assert {"ts", "symbol", "name", "price", "change_pct", "delta", "pushed"} <= set(r)


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
    out = surge_alert.check(watch, {"NVDA": _quote(121.5, 2.5)})
    assert len(out) == 1
    assert out[0]["delta"] == pytest.approx(1.5)


def test_missing_quote_or_pct_skipped(surge_env):
    sent, _ = surge_env
    watch = [("NVDA", "英伟达"), ("BAD", "无行情")]
    surge_alert.check(watch, {"NVDA": _quote(120, 1.0), "BAD": _quote(None, None)})
    out = surge_alert.check(watch, {"NVDA": _quote(121.5, 2.5), "BAD": _quote(None, None)})
    assert len(out) == 1
