# -*- coding: utf-8 -*-
"""价格环比推送（「今日关注」板块，默认 10 分钟维度）。

每隔 SURGE_CHECK_INTERVAL 秒，把「今日关注」板块全部标的的当前现价与上一轮
观测现价做对比，涨/跌幅 >= SURGE_DELTA_PCT 视为「价格环比异动」（上涨、
下跌都触发），本轮所有触发标的**合并成一条**微信通知：股票代码、现价、
当前涨幅、近 N 分钟价格变动。

为什么比现价而不是涨幅：涨幅的分母在时段切换时会换挡（盘中相对昨收、
盘后相对今收），收盘切换那一轮会凭空出现"价格没动、涨幅 ±10pp"的假异动；
现价口径全程一致，环比才是真实成交变动。现价在收盘切换时是连续的（常规
收盘 → 盘后成交从收盘价附近起步），不会出现口径跳变。

- 基线（上轮现价 + 上轮时段）持久化到 surge_state.json，重启后沿用，不漏报。
- 基线按美东交易日分组，跨交易日自动清空（新交易日昨收换挡，隔夜基线无意义）。
- 每轮快照追加写入 data/watch_track/YYYY-MM-DD.jsonl（数据埋点，每天一个
  文件），pushed 在通知实际送达后才置 True，可如实回放每轮的推送/漏推。
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


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "surge_state.json")
DATA_DIR = os.path.join(BASE_DIR, "data", "watch_track")

_lock = threading.Lock()
_state = None

# 活跃交易时段（与 app._in_active_session 保持一致）
_ACTIVE_STATES = ("PRE", "REGULAR", "POST")


def _today() -> str:
    """当前美东交易日（YYYY-MM-DD）。基线跨交易日清空：新交易日开盘后
    昨收换挡，隔夜基线无意义。用美东而非北京日界，避免盘中被清基线。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def _load_state() -> dict:
    """载入状态。结构:
    {
      "date": "YYYY-MM-DD",   # 基线所属美东交易日，跨交易日清空
      "pcts": {SYMBOL: {"price": 上轮现价, "ts_min": epoch分钟, "state": 时段}},
    }
    旧版基线存的是涨幅数值（float），口径已换成现价，直接丢弃重建。
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
                if not isinstance(_state["pcts"], dict):
                    _state["pcts"] = {}
                _state["pcts"] = {
                    s: v for s, v in _state["pcts"].items()
                    if isinstance(v, dict) and _f(v.get("price")) is not None
                }
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
    title = f"🔥 关注股价格异动（{len(triggered)}只）"
    lines = []
    for t in triggered:
        arrow = "↑" if t["delta"] > 0 else "↓"
        lines.append(
            f"**{t['name']} ({t['symbol']})** 现价 {t['price']}，"
            f"当前涨幅 {t['change_pct']:+.2f}%，{t['minutes']:.0f}分钟{arrow}{abs(t['delta']):.2f}%"
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
            logger.info("价格环比通知已发送: %s", ", ".join(t["symbol"] for t in triggered))
        else:
            logger.warning("价格环比通知失败: %s", resp)
        return bool(ok)
    except Exception as e:
        logger.warning("价格环比通知异常: %s", e)
        return False


def check(watch, quotes) -> list:
    """对「今日关注」标的做一轮价格环比检查。

    watch: [(symbol, name), ...]；quotes: {symbol: Quote}。
    返回本轮触发的 [{"symbol","name","price","change_pct","delta","minutes"}, ...]。
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
        if not q or q.price is None or q.price <= 0:
            continue
        if getattr(Config, "SURGE_SESSION_ONLY", 1) and q.market_state not in _ACTIVE_STATES:
            continue  # 休市期价格横跳无意义，跳过
        now_min = time.time() / 60.0
        base = pcts.get(key)
        if base is None:
            # 首轮无基线只建立基线
            delta, minutes = None, None
        else:
            delta = round((q.price - base["price"]) / base["price"] * 100.0, 2)
            minutes = round(now_min - base["ts_min"], 1)
        snapshots.append({
            "ts": int(time.time()),
            "symbol": key, "name": name,
            "price": q.price, "change_pct": q.change_pct,
            "delta": delta, "minutes": minutes, "pushed": False,
        })
        if delta is not None and abs(delta) >= threshold:
            triggered.append({
                "symbol": key, "name": name,
                "price": q.price, "change_pct": q.change_pct,
                "delta": delta, "minutes": minutes,
            })
        pcts[key] = {"price": q.price, "ts_min": now_min, "state": q.market_state}

    sent_ok = True
    if triggered:
        sent_ok = _send(triggered)
        if sent_ok:
            pushed_syms = {t["symbol"] for t in triggered}
            for snap in snapshots:
                if snap["symbol"] in pushed_syms:
                    snap["pushed"] = True
        else:
            # 发送失败：触发标的恢复旧基线，下一轮以累计环比重试
            for t in triggered:
                key = t["symbol"]
                if key in old_pcts:
                    pcts[key] = old_pcts[key]
                else:
                    pcts.pop(key, None)

    # 通知发送（含失败）之后再落快照，pushed 字段如实反映是否送达
    _append_daily(snapshots, today)

    st["pcts"] = pcts
    _save_state()
    if triggered and not sent_ok:
        return []  # 本轮未送达；下一轮会基于旧基线重试
    return triggered
