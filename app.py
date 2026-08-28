# -*- coding: utf-8 -*-
"""美股多板块盯盘助手 - Flask 后端。

板块结构可持久化（watchlist.json），支持任意板块添加/移除股票、跨板块移动、恢复默认。

路由：
  GET  /                          单页盯盘界面
  GET  /api/sectors               当前板块结构
  GET  /api/quotes                全部标的最新行情（带服务端短缓存）
  POST /api/sectors/<key>/stocks  向板块添加股票 {symbol, name?}
  POST /api/stocks/move           跨板块移动 {symbol, from, to}
  DELETE /api/sectors/<key>/stocks/<symbol>  从板块移除股票
  POST /api/reset                 恢复默认板块与个股
  GET  /api/health                数据源/状态
"""
import copy
import json
import logging
import os
import threading
import time

from flask import Flask, jsonify, render_template, request, send_from_directory

from config import Config
from sectors import SECTORS
from datasource import get_provider
import news
import price_alert

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("us_dashboard")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

WATCHLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")


@app.before_request
def _guard_writes():
    """写接口鉴权：启用 ADMIN_TOKEN 时，POST/DELETE 必须携带匹配的 X-Admin-Token。

    未启用时放行（此时仅靠 HOST=127.0.0.1 兜底）。GET 只读接口不受影响。
    """
    if not Config.ADMIN_TOKEN:
        return None
    if request.method in ("POST", "DELETE"):
        token = request.headers.get("X-Admin-Token", "")
        if token != Config.ADMIN_TOKEN:
            return jsonify({"error": "未授权：缺少或错误的 X-Admin-Token"}), 401
    return None

# 今日关注板块 key：用户手动选择关注的标的，行首 ⭐ 一键加入/移出
WATCH_KEY = "today_watch"
# ---------------- 板块推荐关键词 ----------------
# 用于按股票名/代码猜测所属板块；中文词与长度>=4的英文词做子串匹配，
# 短英文代码(如 AI/EV/ARM)只与 symbol 精确匹配，避免误判。
SECTOR_KEYWORDS = {
    "semiconductors": ["半导体", "芯片", "晶圆", "存储芯片", "图形处理器", "英伟达", "超威", "台积电", "博通", "英特尔", "高通", "美光", "迈威尔", "德州仪器", "NVIDIA", "AMD", "Broadcom", "Qualcomm", "Micron", "Marvell", "ARM"],
    "semi_equipment": ["半导体设备", "光刻", "刻蚀", "应用材料", "阿斯麦", "科磊", "泛林", "泰瑞达", "超科林", "ASML", "Applied Materials", "KLA", "Lam Research", "Teradyne"],
    "ai": ["人工智能", "算力", "大模型", "机器学习", "Palantir", "AppLovin", "Snowflake", "Datadog", "MongoDB", "Elastic", "SoundHound", "Vistra"],
    "internet": ["互联网", "电商", "搜索", "社交", "广告", "在线", "拼多多", "阿里巴巴", "百度", "京东", "谷歌", "亚马逊", "网易", "哔哩哔哩", "腾讯音乐", "Pinduoduo", "Alibaba", "Baidu", "Amazon"],
    "ev": ["电动汽车", "新能源车", "造车", "充电", "特斯拉", "蔚来", "理想", "小鹏", "Rivian", "Lucid", "Nikola", "极氪", "Tesla", "XPeng"],
    "software": ["软件", "SaaS", "企业服务", "微软", "Salesforce", "Adobe", "甲骨文", "ServiceNow", "Intuit", "Microsoft", "Oracle"],
    "fintech": ["支付", "金融科技", "保险科技", "先买后付", "信贷", "券商", "Visa", "万事达", "PayPal", "Shopify", "Robinhood", "富途", "Affirm", "Upstart", "Lemonade", "Mastercard", "Square"],
    "big_pharma": ["制药", "医药公司", "礼来", "诺和诺德", "强生", "联合健康", "艾伯维", "默沙东", "辉瑞", "百时美", "赛默飞", "安进", "Eli Lilly", "Novo Nordisk", "Pfizer", "Merck"],
    "biotech": ["生物科技", "生物技术", "基因编辑", "Viking", "Arrowhead", "BioNTech", "Moderna", "Regeneron", "Vertex", "吉利德", "Alnylam", "Sarepta", "CRISPR", "Gilead"],
    "banks": ["银行", "投行", "储蓄", "托管", "摩根大通", "美国银行", "富国", "高盛", "摩根士丹利", "花旗", "嘉信", "纽约梅隆", "JPMorgan", "Goldman", "Morgan Stanley", "Citigroup"],
    "energy": ["石油", "能源", "油服", "炼化", "管道", "页岩", "液化天然气", "埃克森", "雪佛龙", "康菲", "西方石油", "斯伦贝谢", "马拉松", "Cheniere", "Enbridge", "Exxon", "Chevron"],
    "retail": ["零售", "商超", "百货", "折扣", "仓储", "药店", "沃尔玛", "好市多", "家得宝", "塔吉特", "劳氏", "Walmart", "Costco", "Home Depot", "Target", "CVS"],
    "tobacco": ["烟草", "电子烟", "尼古丁", "菲利普莫里斯", "奥驰亚", "英美烟草", "环球烟叶", "雾芯", "帝国品牌", "Philip Morris", "Altria", "Imperial"],
    "fnb": ["餐饮", "饮料", "可乐", "咖啡", "快餐", "啤酒", "烈酒", "零食", "可口可乐", "百事", "麦当劳", "星巴克", "百胜", "达美乐", "星座酒业", "Coca", "Pepsi", "McDonald", "Starbucks"],
    "aerospace": ["航空", "航天", "防务", "军工", "飞机", "火箭", "发动机", "导弹", "波音", "洛克希德", "雷神", "诺斯罗普", "通用动力", "GE航空", "TransDigm", "L3Harris", "HEICO", "Rocket Lab", "Boeing", "Lockheed", "Northrop"],
}


def recommend_sector(name: str, symbol: str):
    """根据股名/代码关键词推荐板块 key，无匹配返回 None。"""
    text = (name + " " + symbol).lower()
    sym = symbol.lower()
    best_key, best_score = None, 0
    for key, kws in SECTOR_KEYWORDS.items():
        score = 0
        for k in kws:
            kl = k.lower()
            if any(ord(c) > 127 for c in k) or len(kl) >= 4:
                if kl in text:
                    score += 1
            elif kl == sym:        # 短代码精确匹配
                score += 2
        if score > best_score:
            best_score, best_key = score, key
    return best_key if best_score > 0 else None

# ---------------- 板块持久化 ----------------
_sectors = None
_sectors_lock = threading.RLock()


def _default_sectors():
    return copy.deepcopy(SECTORS)


def load_sectors():
    """返回内存中的板块结构；首次调用时从 watchlist.json 载入或用默认值种子化。"""
    global _sectors
    if _sectors is None:
        with _sectors_lock:
            if _sectors is None:
                if os.path.exists(WATCHLIST_PATH):
                    try:
                        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
                            _sectors = json.load(f)
                        logger.info("已载入 watchlist.json")
                    except Exception as e:
                        logger.warning("watchlist.json 读取失败，改用默认: %s", e)
                        _sectors = _default_sectors()
                        _save()
                else:
                    _sectors = _default_sectors()
                    _save()
                    logger.info("初始化 watchlist.json（默认板块）")
    return _sectors


def _save():
    """原子写入 watchlist.json。调用方需已持锁（RLock 可重入）。"""
    with _sectors_lock:
        tmp = WATCHLIST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_sectors, f, ensure_ascii=False, indent=2)
        os.replace(tmp, WATCHLIST_PATH)


def _all_symbols():
    """当前全部去重代码（快照）。"""
    with _sectors_lock:
        seen = []
        for sec in load_sectors():
            for st in sec["stocks"]:
                if st["symbol"] not in seen:
                    seen.append(st["symbol"])
    return seen


def _find_sector(key):
    return next((s for s in load_sectors() if s["key"] == key), None)


# ---------------- 行情缓存 ----------------
_cache = {"ts": 0.0, "data": {}, "lock": threading.Lock(), "err": None}


def fetch_all() -> dict:
    now = time.time()
    with _cache["lock"]:
        if _cache["data"] and now - _cache["ts"] < Config.QUOTE_CACHE_TTL:
            return _cache["data"]
    try:
        data = get_provider().get_quotes(_all_symbols())
        if data:
            with _cache["lock"]:
                _cache["data"] = data
                _cache["ts"] = time.time()
                _cache["err"] = None
        return data
    except Exception as e:
        logger.exception("fetch_all 失败: %s", e)
        with _cache["lock"]:
            _cache["err"] = str(e)
        return _cache["data"]


# ---------------- 静态资源（logo 等） ----------------
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(ASSETS_DIR, filename)


# ---------------- 路由 ----------------
@app.route("/")
def index():
    return render_template("index.html",
                           sectors=load_sectors(),
                           refresh=Config.DEFAULT_REFRESH_SEC,
                           source=Config.DATA_SOURCE,
                           admin_token=Config.ADMIN_TOKEN)


@app.route("/news")
def news_page():
    return render_template("news.html",
                           refresh=Config.DEFAULT_REFRESH_SEC,
                           source=Config.DATA_SOURCE)


@app.route("/api/news")
def api_news():
    force = request.args.get("force") == "1"
    return jsonify(news.fetch_all(force=force))


@app.route("/api/sectors")
def api_sectors():
    with _sectors_lock:
        return jsonify(copy.deepcopy(load_sectors()))


@app.route("/api/quotes")
def api_quotes():
    data = fetch_all()
    return jsonify({sym: q.to_dict() for sym, q in data.items()})


@app.route("/api/sectors/<key>/stocks", methods=["POST"])
def add_stock(key):
    data = request.get_json(silent=True) or {}
    sym = (data.get("symbol") or "").strip().upper()
    name = (data.get("name") or "").strip()
    if not sym:
        return jsonify({"error": "代码不能为空"}), 400
    # 名称留空时自动抓取（在持锁前完成，避免锁内做网络请求）
    if not name:
        try:
            q = get_provider().get_quotes([sym])
            name = (q.get(sym).name if q.get(sym) else "") or sym
        except Exception:
            name = sym
    with _sectors_lock:
        sec = _find_sector(key)
        if not sec:
            return jsonify({"error": "板块不存在"}), 404
        if any(st["symbol"] == sym for st in sec["stocks"]):
            return jsonify({"error": "该板块已存在此代码"}), 400
        sec["stocks"].append({"symbol": sym, "name": name, "note": "", "expect_price": None})
        _save()
    logger.info("添加 %s -> %s", sym, key)
    return jsonify({"ok": True, "stock": {"symbol": sym, "name": name, "note": "", "expect_price": None}})


@app.route("/api/recommend")
def api_recommend():
    sym = (request.args.get("symbol") or "").strip().upper()
    if not sym:
        return jsonify({"error": "代码不能为空"}), 400
    name = sym
    try:
        q = get_provider().get_quotes([sym])
        if q.get(sym) and q[sym].name:
            name = q[sym].name
    except Exception:
        pass
    rec_key = recommend_sector(name, sym)
    with _sectors_lock:
        secs = load_sectors()
        existing = next(({"key": s["key"], "name": s["name"]} for s in secs
                         if any(st["symbol"] == sym for st in s["stocks"])), None)
        rec = next(({"key": s["key"], "name": s["name"]} for s in secs if s["key"] == rec_key), None)
        sector_list = [{"key": s["key"], "name": s["name"]} for s in secs]
    return jsonify({
        "symbol": sym, "name": name, "recommended": existing or rec,
        "existing": existing, "sectors": sector_list,
    })


@app.route("/api/stocks/move", methods=["POST"])
def move_stock():
    data = request.get_json(silent=True) or {}
    sym = (data.get("symbol") or "").strip().upper()
    frm = data.get("from")
    to = data.get("to")
    if not sym or not frm or not to:
        return jsonify({"error": "参数缺失"}), 400
    if frm == to:
        return jsonify({"ok": True, "moved": False})
    with _sectors_lock:
        src = _find_sector(frm)
        dst = _find_sector(to)
        if not src or not dst:
            return jsonify({"error": "板块不存在"}), 404
        stock = next((st for st in src["stocks"] if st["symbol"] == sym), None)
        if not stock:
            return jsonify({"error": "源板块无此股票"}), 404
        src["stocks"] = [st for st in src["stocks"] if st["symbol"] != sym]
        if not any(st["symbol"] == sym for st in dst["stocks"]):
            dst["stocks"].append(stock)
        _save()
    logger.info("移动 %s: %s -> %s", sym, frm, to)
    return jsonify({"ok": True, "moved": True})


@app.route("/api/sectors/<key>/stocks/<symbol>", methods=["DELETE"])
def remove_stock(key, symbol):
    sym = symbol.strip().upper()
    with _sectors_lock:
        sec = _find_sector(key)
        if not sec:
            return jsonify({"error": "板块不存在"}), 404
        before = len(sec["stocks"])
        sec["stocks"] = [st for st in sec["stocks"] if st["symbol"] != sym]
        if len(sec["stocks"]) == before:
            return jsonify({"error": "该板块无此股票"}), 404
        _save()
    logger.info("移除 %s <- %s", sym, key)
    return jsonify({"ok": True})


@app.route("/api/watch/<symbol>", methods=["POST", "DELETE"])
def toggle_watch(symbol):
    """加入/移出「今日关注」板块。POST 加入，DELETE 移出。"""
    sym = symbol.strip().upper()
    if request.method == "POST":
        # 名称留空时自动抓取（在持锁前完成，避免锁内做网络请求）
        name = sym
        try:
            q = get_provider().get_quotes([sym])
            if q.get(sym) and q[sym].name:
                name = q[sym].name
        except Exception:
            pass
    with _sectors_lock:
        sec = _find_sector(WATCH_KEY)
        if not sec:
            return jsonify({"error": "今日关注板块不存在"}), 404
        if request.method == "POST":
            if any(st["symbol"] == sym for st in sec["stocks"]):
                return jsonify({"ok": True, "watched": True})
            sec["stocks"].insert(0, {"symbol": sym, "name": name, "note": "", "expect_price": None})
            _save()
            return jsonify({"ok": True, "watched": True, "stock": {"symbol": sym, "name": name, "note": "", "expect_price": None}})
        else:  # DELETE
            before = len(sec["stocks"])
            sec["stocks"] = [st for st in sec["stocks"] if st["symbol"] != sym]
            if len(sec["stocks"]) == before:
                return jsonify({"ok": True, "watched": False})
            _save()
            return jsonify({"ok": True, "watched": False})


@app.route("/api/watch/<symbol>/expect", methods=["POST"])
def set_expect_price(symbol):
    """保存「今日关注」板块中某只股票的下限价/上限价。body: {expect?: number|null, upper?: number|null}"""
    sym = symbol.strip().upper()
    data = request.get_json(silent=True) or {}

    def _num(v, field):
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ValueError(field)

    # 兼容旧前端：body.price 作为下限价
    expect = data.get("expect", data.get("price"))
    upper = data.get("upper")
    try:
        expect = _num(expect, "下限价")
        upper = _num(upper, "上限价")
    except ValueError as e:
        return jsonify({"error": f"{e}格式错误"}), 400
    with _sectors_lock:
        sec = _find_sector(WATCH_KEY)
        if not sec:
            return jsonify({"error": "今日关注板块不存在"}), 404
        stock = next((st for st in sec["stocks"] if st["symbol"] == sym), None)
        if not stock:
            return jsonify({"error": "该股不在今日关注板块"}), 404
        stock["expect_price"] = expect
        stock["upper_price"] = upper
        _save()
    return jsonify({"ok": True, "symbol": sym, "expect_price": expect, "upper_price": upper})


@app.route("/api/reset", methods=["POST"])
def reset():
    with _sectors_lock:
        global _sectors
        _sectors = _default_sectors()
        _save()
    logger.info("已恢复默认板块")
    return jsonify({"ok": True})


@app.route("/api/alerts/history")
def api_alerts_history():
    return jsonify({"history": price_alert.history()})


@app.route("/api/alerts/test", methods=["POST"])
def api_alerts_test():
    """发送一条测试通知，验证当前通道与令牌配置。"""
    body = request.get_json(silent=True) or {}
    title = body.get("title") or "WallHawk 测试通知"
    text = body.get("body") or "这是一条测试消息，通知通道配置正常。"
    notifier = price_alert.Notifier()
    fn = getattr(notifier, Config.ALERT_CHANNEL, None)
    if fn is None:
        return jsonify({"error": f"未知提醒通道 {Config.ALERT_CHANNEL}"}), 400
    try:
        ok, resp = fn(title, text)
    except Exception as e:
        return jsonify({"error": f"测试通知发送异常: {e}"}), 500
    if not ok:
        return jsonify({"error": resp or "测试通知发送失败"}), 500
    return jsonify({"ok": True, "channel": Config.ALERT_CHANNEL})


@app.route("/api/health")
def api_health():
    with _cache["lock"]:
        ts, err, ok = _cache["ts"], _cache["err"], bool(_cache["data"])
    return jsonify({
        "source": Config.DATA_SOURCE,
        "symbols_total": len(_all_symbols()),
        "symbols_returned": len(_cache["data"]),
        "last_update": ts,
        "ok": ok,
        "error": err,
        "alert_channel": Config.ALERT_CHANNEL,
        # 只暴露是否已配置，绝不泄露令牌值本身
        "alert_configured": bool(os.environ.get("PUSHPLUS_TOKEN") or os.environ.get("WECOM_WEBHOOK") or os.environ.get("SERVERCHAN_SENDKEY")),
    })


def _warmup():
    try:
        fetch_all()
        logger.info("预热完成，已缓存 %d 只", len(_cache["data"]))
    except Exception as e:
        logger.warning("预热失败（不影响运行，前端轮询会重试）: %s", e)


# ---------------- 价格触达提醒 ----------------
def _in_active_session(q) -> bool:
    """是否处于可提醒的活跃交易时段（盘前/盘中/盘后）。"""
    return bool(q and q.market_state in ("PRE", "REGULAR", "POST"))


def _collect_expected() -> list:
    """收集「今日关注」板块中设置了目标价（下限价/上限价）的标的快照。

    返回 [(symbol, name, expect_price, upper_price), ...]，expect/upper 可能为 None。
    """
    sec = _find_sector(WATCH_KEY)
    if not sec:
        return []
    out = []
    for st in sec["stocks"]:
        exp = st.get("expect_price")
        up = st.get("upper_price")
        if exp is not None or up is not None:
            out.append((st.get("symbol", "").upper(), st.get("name", ""), exp, up))
    return out


def _alert_loop():
    """后台轮询：对设置了目标价的标的做边沿+滞回检查，并做单日急跌提醒。"""
    while True:
        try:
            expected = _collect_expected()
            if expected:
                quotes = fetch_all()
                for sym, name, exp, up in expected:
                    q = quotes.get(sym)
                    if not q:
                        continue
                    if Config.ALERT_SESSION_ONLY and not _in_active_session(q):
                        continue  # 休市期不打扰，避免收盘价横跳误报
                    price = q.price if q else None
                    # 启动后第一次轮询：用首次观测判断"启动时已低于下限价"的历史穿越，补发一次
                    if not price_alert._state or (price_alert._state.get(sym, {}) or {}).get("last") is None:
                        price_alert.notify_caught_below(sym, name, price, exp)
                    price_alert.check_and_notify(sym, name, price, exp, up)
                    price_alert.check_daily_drop(sym, name, price, q.change_pct)
        except Exception as e:
            logger.warning("价格提醒轮询异常: %s", e)
        time.sleep(Config.ALERT_CHECK_INTERVAL)


if __name__ == "__main__":
    load_sectors()  # 触发种子化
    threading.Thread(target=_warmup, daemon=True).start()
    threading.Thread(target=news._warmup, daemon=True).start()
    threading.Thread(target=_alert_loop, daemon=True).start()
    logger.info("启动: http://localhost:%s  数据源=%s  标的=%d", Config.PORT, Config.DATA_SOURCE, len(_all_symbols()))
    app.run(host=Config.HOST, port=Config.PORT, debug=False, threaded=True)
