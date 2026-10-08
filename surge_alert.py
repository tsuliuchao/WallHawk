# -*- coding: utf-8 -*-
"""涨幅环比推送（「今日关注」板块，默认 10 分钟维度）。

每隔 SURGE_CHECK_INTERVAL 秒，把「今日关注」板块全部标的的当前涨幅与上一轮
观测涨幅做对比，差值绝对值 >= SURGE_DELTA_PCT 个百分点视为「涨幅环比异动」
（加速上涨 / 加速下跌都触发），本轮所有触发标的**合并成一条**微信通知：
股票代码、现价、当前涨幅、环比变动。

- 基线持久化到 surge_state.json，重启后沿用重启前的涨幅基线，不漏报不误报。
- 日期切换时清空基线（当日常规涨幅归零，隔夜基线无意义）。
- 每轮快照追加写入 data/watch_track/YYYY-MM-DD.jsonl（数据埋点，每天一个文件），
  含 pushed 标记，可事后回放每轮的推送/漏推情况。
- 推送失败时保留旧基线，下一轮以累计环比重试。
"""
import json
import logging
import os
import threading
import time

from config import Config
from utils.weichat_notify import Notifier

logger = logging.getLogger("surge_alert")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "surge_state.json")
DATA_DIR = os.path.join(BASE_DIR, "data", "watch_track")

_lock = threading.Lock()
_state = None

# 活跃交易时段（与 app._in_active_session 保持一致）
_ACTIVE_STATES = ("PRE", "REGULAR", "POST")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load_state() -> dict:
    """载入状态。结构:
    {
      "date": "YYYY-MM-DD",          # 基线所属交易日，跨日清空
      "pcts": {SYMBOL: 上轮涨幅%},    # 环比基线
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
                _state.setdefault("date", None)
                _state.setdefault("pcts", {})
    return _state


def _save_state():
    with _lock:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)


def _append_daily(snapshots: list, today: str):
    """把本轮快照追加到当日埋点文件 data/watch_track/YYYY-MM-DD.jsonl。"""
    if not snapshots:
        return
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"{today}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for snap in snapshots:
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("埋点写入失败: %s", e)


def _send(triggered: list) -> bool:
    """把本轮全部触发标的合并成一条通知发送，返回是否成功。"""
    title = f"🔥 关注股涨幅异动（{len(triggered)}只）"
    lines = []
    for t in triggered:
        arrow = "↑" if t["delta"] > 0 else "↓"
        lines.append(
            f"**{t['name']} ({t['symbol']})** 现价 {t['price']}，"
            f"当前涨幅 {t['change_pct']:+.2f}%，10分钟环比 {arrow}{abs(t['delta']):.2f}pp"
        )
    body = "\n\n".join(lines)
    try:
        notifier = Notifier()
        fn = getattr(notifier, Config.ALERT_CHANNEL, None)
        if fn is None:
            logger.warning("未知提醒通道 %s", Config.ALERT_CHANNEL)
            return False
        ok, resp = fn(title, body)
        if ok:
            logger.info("涨幅环比通知已发送: %s", ", ".join(t["symbol"] for t in triggered))
        else:
            logger.warning("涨幅环比通知失败: %s", resp)
        return bool(ok)
    except Exception as e:
        logger.warning("涨幅环比通知异常: %s", e)
        return False


def check(watch, quotes) -> list:
    """对「今日关注」标的做一轮涨幅环比检查。

    watch: [(symbol, name), ...]；quotes: {symbol: Quote}。
    返回本轮触发的 [{"symbol","name","price","change_pct","delta"}, ...]。
    首轮（无基线）只建立基线不推送。
    """
    threshold = float(getattr(Config, "SURGE_DELTA_PCT", 1.0))
    if threshold <= 0:
        return []
    today = _today()
    st = _load_state()
    if st.get("date") != today:
        st["date"] = today
        st["pcts"] = {}

    pcts = st["pcts"]
    watch_syms = {sym.upper() for sym, _ in watch if sym}
    # 清理已移出「今日关注」的陈旧基线
    pcts = {s: p for s, p in pcts.items() if s in watch_syms}
    old_pcts = dict(pcts)  # 发送失败时用于恢复旧基线

    snapshots = []
    triggered = []
    for sym, name in watch:
        key = (sym or "").upper()
        q = quotes.get(key)
        if not q or q.price is None or q.change_pct is None:
            continue
        if getattr(Config, "SURGE_SESSION_ONLY", 1) and q.market_state not in _ACTIVE_STATES:
            continue  # 休市期涨幅横跳无意义，跳过
        prev = pcts.get(key)
        delta = None if prev is None else round(q.change_pct - prev, 2)
        if prev is not None and abs(delta) >= threshold:
            triggered.append({
                "symbol": key, "name": name,
                "price": q.price, "change_pct": q.change_pct, "delta": delta,
            })
        snapshots.append({
            "ts": int(time.time()),
            "symbol": key, "name": name,
            "price": q.price, "change_pct": q.change_pct,
            "delta": delta, "pushed": key in {t["symbol"] for t in triggered},
        })
        pcts[key] = q.change_pct

    _append_daily(snapshots, today)

    sent_ok = True
    if triggered:
        sent_ok = _send(triggered)
        if not sent_ok:
            # 发送失败：触发标的恢复旧基线，下一轮以累计环比重试
            for t in triggered:
                key = t["symbol"]
                if key in old_pcts:
                    pcts[key] = old_pcts[key]
                else:
                    pcts.pop(key, None)

    st["pcts"] = pcts
    _save_state()
    if triggered and not sent_ok:
        return []  # 本轮未送达；下一轮会基于旧基线重试
    return triggered
