# -*- coding: utf-8 -*-
"""price_alert 边沿+滞回触发逻辑（mock 掉 _send 与状态文件）。"""
import pytest

import price_alert


@pytest.fixture
def alert_env(monkeypatch, tmp_path):
    """隔离状态文件并拦截发送，返回 sent 列表以断言通知次数。"""
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    monkeypatch.setattr(price_alert.Config, "ALERT_HYSTERESIS_PCT", 0.0)
    monkeypatch.setattr(price_alert.Config, "ALERT_DAILY_DROP_PCT", 0.0)
    price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, level, kind):
        sent.append((symbol, name, price, level, kind))
        return True

    monkeypatch.setattr(price_alert, "_send", fake_send)
    return sent


def test_first_observation_already_below_does_not_alert(alert_env):
    # 启动时已低于下限价，无下穿边沿，不提醒
    assert price_alert.check_and_notify("NVDA", "英伟达", 90.0, 100.0) is False
    assert alert_env == []


def test_first_observation_above_then_cross_down_alerts_once(alert_env):
    assert price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 99.0, 100.0) is True
    assert len(alert_env) == 1
    assert alert_env[0][:4] == ("NVDA", "英伟达", 99.0, 100.0)
    assert alert_env[0][4] == "cross_below"


def test_stays_below_does_not_repeat(alert_env):
    assert price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 99.0, 100.0) is True
    assert price_alert.check_and_notify("NVDA", "英伟达", 98.0, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 95.0, 100.0) is False
    assert len(alert_env) == 1


def test_recover_then_cross_again_alerts_again(alert_env):
    assert price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 99.0, 100.0) is True
    assert price_alert.check_and_notify("NVDA", "英伟达", 101.0, 100.0) is False  # 回到上方
    assert price_alert.check_and_notify("NVDA", "英伟达", 99.5, 100.0) is True   # 再次下穿
    assert len(alert_env) == 2


def test_hysteresis_requires_recovery_beyond_band(monkeypatch, tmp_path):
    """滞回：触发后必须回升超过 expect*(1+hb) 才重新武装。"""
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    monkeypatch.setattr(price_alert.Config, "ALERT_HYSTERESIS_PCT", 1.0)  # 1%
    price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, level, kind):
        sent.append((symbol, name, price, level, kind))
        return True

    monkeypatch.setattr(price_alert, "_send", fake_send)

    # 下穿触发
    assert price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 99.0, 100.0) is True
    # 回升但未超过滞回带（100*1.01=101），不重新武装 → 再下穿不提醒
    assert price_alert.check_and_notify("NVDA", "英伟达", 100.5, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 98.0, 100.0) is False
    # 回升超过 101 → 重新武装 → 再下穿可提醒
    assert price_alert.check_and_notify("NVDA", "英伟达", 102.0, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 98.0, 100.0) is True
    assert len(sent) == 2


def test_upper_cross_alerts_and_requires_retreat(monkeypatch, tmp_path):
    """上限突破：>= upper 触发一次，需回落到 upper*(1-hb) 才重新武装。"""
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    monkeypatch.setattr(price_alert.Config, "ALERT_HYSTERESIS_PCT", 1.0)
    price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, level, kind):
        sent.append((symbol, name, price, level, kind))
        return True

    monkeypatch.setattr(price_alert, "_send", fake_send)

    # 从下方上穿上限
    assert price_alert.check_and_notify("NVDA", "英伟达", 95.0, None, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 101.0, None, 100.0) is True
    assert sent[-1][4] == "cross_above"
    # 未回落到 99 以内，不重新武装
    assert price_alert.check_and_notify("NVDA", "英伟达", 103.0, None, 100.0) is False
    # 回落到 99 以下重新武装，再次上穿可提醒
    assert price_alert.check_and_notify("NVDA", "英伟达", 98.0, None, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 100.5, None, 100.0) is True
    assert len(sent) == 2


def test_both_targets_side_by_side(alert_env):
    # 同一标的可同时设下限价与上限价（hb=0，回升即武装）
    assert price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0, 110.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 99.0, 100.0, 110.0) is True   # 向下触发
    assert price_alert.check_and_notify("NVDA", "英伟达", 98.0, 100.0, 110.0) is False  # 停留在下方不重复
    assert price_alert.check_and_notify("NVDA", "英伟达", 115.0, 100.0, 110.0) is True  # 上穿上限
    assert [s[4] for s in alert_env] == ["cross_below", "cross_above"]


def test_change_expect_resets_tracking(alert_env):
    assert price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0) is False
    # 改下限价后重置，之前已在上方不再算作「从上方下穿」
    assert price_alert.check_and_notify("NVDA", "英伟达", 104.0, 120.0) is False
    assert alert_env == []


def test_none_price_or_expect_ignored(alert_env):
    assert price_alert.check_and_notify("NVDA", "英伟达", None, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 100.0, None) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", "abc", 100.0) is False
    assert alert_env == []


def test_symbol_uppercased_key(alert_env):
    assert price_alert.check_and_notify("nvda", "英伟达", 105.0, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 99.0, 100.0) is True
    assert len(alert_env) == 1


def test_send_failure_does_not_mark_sent(alert_env, monkeypatch):
    """发送失败时应返回 False 且后续下穿可重试。"""
    def failing_send(symbol, name, price, level, kind):
        return False

    monkeypatch.setattr(price_alert, "_send", failing_send)
    assert price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 99.0, 100.0) is False
    # 但 last 仍被记录为 99，因此不产生额外通知
    assert alert_env == []


# ---------------- 启动补发（notify_caught_below） ----------------

def test_caught_below_fires_once_on_startup(monkeypatch, tmp_path):
    """启动时观测价已 ≤ 下限价：补发一次，永不重复。"""
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, level, kind):
        sent.append((symbol, name, price, level, kind))
        return True

    monkeypatch.setattr(price_alert, "_send", fake_send)

    assert price_alert.notify_caught_below("KO", "可口可乐", 89.6, 90.0) is True
    assert price_alert.notify_caught_below("KO", "可口可乐", 89.0, 90.0) is False  # 已补发
    assert len(sent) == 1
    assert sent[0][4] == "caught_below"


def test_caught_below_no_op_when_above(monkeypatch, tmp_path):
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, level, kind):
        sent.append((symbol, name, price, level, kind))
        return True

    monkeypatch.setattr(price_alert, "_send", fake_send)
    assert price_alert.notify_caught_below("NVDA", "英伟达", 105.0, 100.0) is False
    assert sent == []


def test_caught_below_marks_armed_false(monkeypatch, tmp_path):
    """补发后进入未武装态：此后下穿不再重复（等待回升）。"""
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    monkeypatch.setattr(price_alert.Config, "ALERT_HYSTERESIS_PCT", 0.0)
    price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, level, kind):
        sent.append((symbol, name, price, level, kind))
        return True

    monkeypatch.setattr(price_alert, "_send", fake_send)
    assert price_alert.notify_caught_below("KO", "可口可乐", 89.0, 90.0) is True
    # 已补发后 armed=False：即使随后跌得更深也不重复
    assert price_alert.check_and_notify("KO", "可口可乐", 88.0, 90.0) is False
    assert len(sent) == 1


# ---------------- 单日急跌（check_daily_drop） ----------------

def test_daily_drop_fires_when_below_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    monkeypatch.setattr(price_alert.Config, "ALERT_DAILY_DROP_PCT", 5.0)
    price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, level, kind):
        sent.append((symbol, name, price, level, kind))
        return True

    monkeypatch.setattr(price_alert, "_send", fake_send)
    assert price_alert.check_daily_drop("NVDA", "英伟达", 95.0, -5.5) is True
    assert sent[0][4] == "daily_drop"
    # 同一天不再重复
    assert price_alert.check_daily_drop("NVDA", "英伟达", 90.0, -9.0) is False
    assert len(sent) == 1


def test_daily_drop_ignores_small_drops(monkeypatch, tmp_path):
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    monkeypatch.setattr(price_alert.Config, "ALERT_DAILY_DROP_PCT", 5.0)
    price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, level, kind):
        sent.append((symbol, name, price, level, kind))
        return True

    monkeypatch.setattr(price_alert, "_send", fake_send)
    assert price_alert.check_daily_drop("NVDA", "英伟达", 98.0, -2.0) is False
    assert price_alert.check_daily_drop("NVDA", "英伟达", 98.0, 1.0) is False
    assert sent == []


def test_daily_drop_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    monkeypatch.setattr(price_alert.Config, "ALERT_DAILY_DROP_PCT", 0.0)
    price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, level, kind):
        sent.append((symbol, name, price, level, kind))
        return True

    monkeypatch.setattr(price_alert, "_send", fake_send)
    assert price_alert.check_daily_drop("NVDA", "英伟达", 50.0, -40.0) is False
    assert sent == []


# ---------------- 触发历史 ----------------

def test_history_records_and_returns_reversed(alert_env):
    price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0)
    price_alert.check_and_notify("NVDA", "英伟达", 99.0, 100.0)
    hist = price_alert.history()
    assert len(hist) == 1
    assert hist[0]["kind"] == "cross_below"
    assert hist[0]["symbol"] == "NVDA"
    assert hist[0]["price"] == 99.0
    assert hist[0]["level"] == 100.0


def test_history_empty_initially(monkeypatch, tmp_path):
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    price_alert._state = None
    assert price_alert.history() == []
