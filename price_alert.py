# -*- coding: utf-8 -*-
"""价格触达提醒。

「今日关注」板块中设置了预期价(expect_price)的标的，当最新价从预期价**上方下穿**
到预期价（<= 预期价）那一刻，通过 utils/weichat_notify.py（默认 pushplus 通道）
推送一条通知；同时支持设置 upper_price 时**向上突破**（>= 上限）的触达提醒。

触发规则：
- **边沿 + 滞回**：只在"下穿/上穿"瞬间提醒一次；提醒后进入未武装(disarmed)状态，
  必须等价格回升超过 `expect*(1+hysteresis)`（或回落到 `upper*(1-hysteresis)`）
  才重新武装，杜绝预期价附近来回震荡导致的重复轰炸。
- **启动补发**：启动时若某标的观测价已低于预期价且从未提醒过，补发一次
  "已触达"通知（catch-up），随后持久化标记不再重复。避免"服务没开着/刚添加
  目标价"漏掉历史穿越。
- 状态持久化到 alert_state.json，重启不重复提醒。
"""
import json
import logging
import os
import threading
import time

from config import Config
from utils.weichat_notify import Notifier

logger = logging.getLogger("price_alert")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "alert_state.json")

_lock = threading.Lock()
_state = None

HISTORY_LIMIT = 50  # alert_state.json 中保留的最近触发历史条数


def _load_state() -> dict:
    """载入状态。结构:
    {
      SYMBOL: {"expect": 预期价, "last": 上次观测价, "armed": bool,
               "caught_notified": bool, "upper": 上限价, "drop_notified_date": "YYYY-MM-DD"},
      "history": [{"ts":, "symbol":, "name":, "kind":, "price":, "level":, "ok":}],
    }
    """
    global _state
    if _state is None:
        with _lock:
            if _state is None:
                try:
                    with open(STATE_PATH, "r", encoding="utf-8") as f:
                        _state = json.load(f)
                except Exception:
                    _state = {}
                if not isinstance(_state, dict):
                    _state = {}
                _state.setdefault("history", [])
    return _state


def _save_state():
    with _lock:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)


def _record(state: dict, symbol: str, name: str, kind: str,
            price: float, level: float, ok: bool):
    """写入一条触发历史（保留最近 HISTORY_LIMIT 条）。"""
    hist = state.setdefault("history", [])
    hist.append({
        "ts": int(time.time()),
        "symbol": symbol,
        "name": name,
        "kind": kind,            # cross_below / cross_above / caught_below / daily_drop
        "price": price,
        "level": level,
        "ok": ok,
    })
    del hist[:-HISTORY_LIMIT]
    state["updated"] = int(time.time())


def _send(symbol: str, name: str, price: float, level: float, kind: str = "cross") -> bool:
    """通过 Notifier 发送一条价格触达通知，返回是否成功。"""
    if kind == "cross_above":
        title = f"📈 {symbol} 突破上限"
        body = f"**{name} ({symbol})** 现价 {price} 已 ≥ 上限 {level}"
    elif kind == "caught_below":
        title = f"📉 {symbol} 已触达预期价"
        body = f"**{name} ({symbol})** 现价 {price} 已 ≤ 预期价 {level}（启动补发）"
    elif kind == "daily_drop":
        title = f"⚠️ {symbol} 单日急跌"
        body = f"**{name} ({symbol})** 现价 {price}，今日跌幅 {level:.2f}%"
    else:
        title = f"📉 {symbol} 价格触达"
        body = f"**{name} ({symbol})** 现价 {price} 已 ≤ 预期价 {level}"
    try:
        notifier = Notifier()
        fn = getattr(notifier, Config.ALERT_CHANNEL, None)
        if fn is None:
            logger.warning("未知提醒通道 %s", Config.ALERT_CHANNEL)
            return False
        ok, resp = fn(title, body)
        if ok:
            logger.info("价格触达通知已发送: %s (%s %.2f @ %.2f)", symbol, kind, price, level)
        else:
            logger.warning("价格触达通知失败 %s: %s", symbol, resp)
        return bool(ok)
    except Exception as e:
        logger.warning("价格触达通知异常 %s: %s", symbol, e)
        return False


def check_and_notify(symbol: str, name: str, price, expect, upper=None) -> bool:
    """边沿+滞回触发：下穿/上穿时各发一次通知。

    - expect: 预期价（向下目标，<= 触发）
    - upper: 上限价（向上目标，>= 触发），可空
    返回是否在本次调用中发出通知。
    """
    if price is None:
        return False
    try:
        price = float(price)
        expect = float(expect) if expect is not None else None
        upper = float(upper) if upper is not None else None
    except (TypeError, ValueError):
        return False
    if expect is None and upper is None:
        return False

    key = symbol.upper()
    st = _load_state()
    entry = dict(st.get(key, {}) or {})
    entry.setdefault("history", [])  # 旧版本状态无该字段时兜底

    dirty = False
    sent_now = False

    # ---- 目标价变更 → 重置该方向穿越状态 ----
    if entry.get("expect") != expect:
        entry["expect"] = expect
        entry["armed"] = True      # 新目标默认已武装
        entry["last"] = None
        entry["caught_notified"] = False
        dirty = True
    if entry.get("upper") != upper:
        entry["upper"] = upper
        entry["armed_upper"] = True
        dirty = True

    last = entry.get("last")
    hb = max(0.0, float(getattr(Config, "ALERT_HYSTERESIS_PCT", 0.0)) / 100.0)

    # ---- 向下：下穿触发（需已武装 + 有上一次观测判定方向）----
    if expect is not None and last is not None and entry.get("armed", True):
        if last > expect and price <= expect:
            if _send(key, name, price, expect, "cross_below"):
                sent_now = True
                entry["armed"] = False
                _record(st, key, name, "cross_below", price, expect, True)
                dirty = True
    # 回升超过滞回带 → 重新武装（允许再次下穿时提醒）
    if expect is not None and price > expect * (1 + hb):
        if not entry.get("armed", True):
            entry["armed"] = True
            dirty = True

    # ---- 向上：上穿触发（对称语义）----
    if upper is not None and last is not None and entry.get("armed_upper", True):
        if last < upper and price >= upper:
            if _send(key, name, price, upper, "cross_above"):
                sent_now = True
                entry["armed_upper"] = False
                _record(st, key, name, "cross_above", price, upper, True)
                dirty = True
    if upper is not None and price < upper * (1 - hb):
        if not entry.get("armed_upper", True):
            entry["armed_upper"] = True
            dirty = True

    # ---- 记录本次观测价，供下次判定穿越方向 ----
    if last is None or last != price:
        entry["last"] = price
        dirty = True

    if dirty:
        st[key] = entry
        _save_state()
    return sent_now


def notify_caught_below(symbol: str, name: str, price, expect) -> bool:
    """启动补发：观测价已 ≤ 预期价且从未提醒过时，补发一次。

    仅在进程启动时调用（首次观测），用"启动时已低于"判定历史穿越，避免漏报。
    已标记过 caught_notified 的标的永不重复补发。
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
    if entry.get("caught_notified"):
        return False
    if price <= expect:
        if _send(key, name, price, expect, "caught_below"):
            entry["caught_notified"] = True
            entry["armed"] = False       # 已低于，等待回升武装后再计穿越
            entry["last"] = price
            _record(st, key, name, "caught_below", price, expect, True)
            st[key] = entry
            _save_state()
            return True
    return False


def check_daily_drop(symbol: str, name: str, price, change_pct) -> bool:
    """单日急跌提醒：今日跌幅超过 ALERT_DAILY_DROP_PCT 时提醒，每标每个自然日一次。

    change_pct 为当日常规涨跌幅（-5.0 表示跌 5%）。发送成功后记录 drop_notified_date。
    """
    threshold = float(getattr(Config, "ALERT_DAILY_DROP_PCT", 0.0))
    if threshold <= 0 or price is None or change_pct is None:
        return False
    try:
        price = float(price)
        change_pct = float(change_pct)
    except (TypeError, ValueError):
        return False

    key = symbol.upper()
    st = _load_state()
    entry = dict(st.get(key, {}) or {})
    today = time.strftime("%Y-%m-%d")
    if entry.get("drop_notified_date") == today:
        return False
    if change_pct <= -threshold:
        if _send(key, name, price, abs(change_pct), "daily_drop"):
            entry["drop_notified_date"] = today
            _record(st, key, name, "daily_drop", price, abs(change_pct), True)
            st[key] = entry
            _save_state()
            return True
    return False


def history() -> list:
    """最近触发历史（按时间倒序）。"""
    st = _load_state()
    return list(reversed(st.get("history", [])))
