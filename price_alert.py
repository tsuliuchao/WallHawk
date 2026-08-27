# -*- coding: utf-8 -*-
"""价格触达提醒。

「今日关注」板块中设置了预期价(expect_price)的标的，当最新价从预期价**上方下穿**
到预期价（<= 预期价）那一刻，通过 utils/weichat_notify.py（默认 pushplus 通道）
推送一条通知。

边沿触发（edge-trigger）：只在"下穿"瞬间提醒一次，之后价格一直低于预期价也不会
重复轰炸；只有当价格重新回到预期价上方、再次下穿时才再次提醒。状态持久化到
alert_state.json，重启不重复提醒；首次观察时已低于预期价不提醒（避免误报历史状态）。
"""
import json
import logging
import os
import threading

from config import Config
from utils.weichat_notify import Notifier

logger = logging.getLogger("price_alert")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "alert_state.json")

_lock = threading.Lock()
_state = None


def _load_state() -> dict:
    """载入状态 {SYMBOL: {"expect": 预期价, "last": 上次观测价}}。"""
    global _state
    if _state is None:
        with _lock:
            if _state is None:
                try:
                    with open(STATE_PATH, "r", encoding="utf-8") as f:
                        _state = json.load(f)
                except Exception:
                    _state = {}
    return _state


def _save_state():
    with _lock:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)


def _send(symbol: str, name: str, price: float, expect: float) -> bool:
    """通过 Notifier 发送一条价格触达通知，返回是否成功。"""
    title = f"📉 {symbol} 价格触达"
    body = f"**{name} ({symbol})** 现价 {price} 已 ≤ 预期价 {expect}"
    try:
        notifier = Notifier()
        fn = getattr(notifier, Config.ALERT_CHANNEL, None)
        if fn is None:
            logger.warning("未知提醒通道 %s", Config.ALERT_CHANNEL)
            return False
        ok, resp = fn(title, body)
        if ok:
            logger.info("价格触达通知已发送: %s (%.2f <= %.2f)", symbol, price, expect)
        else:
            logger.warning("价格触达通知失败 %s: %s", symbol, resp)
        return bool(ok)
    except Exception as e:
        logger.warning("价格触达通知异常 %s: %s", symbol, e)
        return False


def check_and_notify(symbol: str, name: str, price, expect) -> bool:
    """边沿触发：仅在价格从上方向下穿越预期价（<=）时发送一次通知。

    返回是否在本次调用中发出通知。首次观测到已低于预期价时不提醒（无穿越边沿）。
    """
    if price is None or expect is None:
        return False
    try:
        price = float(price)
        expect = float(expect)
    except (TypeError, ValueError):
        return False

    key = symbol.upper()
    st = _load_state()
    entry = dict(st.get(key, {}) or {})

    # 预期价变更 → 重置穿越状态（新预期价重新开始追踪）
    dirty = False
    if entry.get("expect") != expect:
        entry = {"expect": expect, "last": None}
        dirty = True

    last = entry.get("last")
    sent_now = False

    # 下穿边沿：上一次价格在预期价上方，本次跌到预期价或以下。
    # 需要 last 非空（有上一次观测）才能判定方向，首次观测不触发。
    if last is not None and last > expect and price <= expect:
        if _send(key, name, price, expect):
            sent_now = True
            dirty = True

    # 记录本次观测价，供下次判定穿越方向
    if last is None or last != price:
        entry["last"] = price
        dirty = True

    if dirty:
        st[key] = entry
        _save_state()
    return sent_now
