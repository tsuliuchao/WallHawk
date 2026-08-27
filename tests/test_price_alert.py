# -*- coding: utf-8 -*-
"""price_alert.check_and_notify 边沿触发逻辑（mock 掉 _send 与状态文件）。"""
import pytest

import price_alert


@pytest.fixture
def alert_env(monkeypatch, tmp_path):
    """隔离状态文件并拦截发送，返回 sent 列表以断言通知次数。"""
    monkeypatch.setattr(price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, expect):
        sent.append((symbol, price, expect))
        return True

    monkeypatch.setattr(price_alert, "_send", fake_send)
    return sent


def test_first_observation_already_below_does_not_alert(alert_env):
    # 启动时已低于预期价，无下穿边沿，不提醒
    assert price_alert.check_and_notify("NVDA", "英伟达", 90.0, 100.0) is False
    assert alert_env == []


def test_first_observation_above_then_cross_down_alerts_once(alert_env):
    assert price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 99.0, 100.0) is True
    assert len(alert_env) == 1
    assert alert_env[0] == ("NVDA", 99.0, 100.0)


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


def test_change_expect_resets_tracking(alert_env):
    assert price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0) is False
    # 改预期价后重置，之前已在上方不再算作「从上方下穿」
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
    def failing_send(symbol, name, price, expect):
        return False

    monkeypatch.setattr(price_alert, "_send", failing_send)
    assert price_alert.check_and_notify("NVDA", "英伟达", 105.0, 100.0) is False
    assert price_alert.check_and_notify("NVDA", "英伟达", 99.0, 100.0) is False
    # 但 last 仍被记录为 99，因此不产生额外通知
    assert alert_env == []
