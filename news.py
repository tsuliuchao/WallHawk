# -*- coding: utf-8 -*-
"""财经与地缘政治要闻聚合模块。

聚合多个财经/地缘资讯源（实时快讯 + 深度文章），统一结构化输出。
每源独立抓取、独立容错：单源失败不影响其它源；带 TTL 内存缓存。

源类型：
  rss  —— 标准 RSS/Atom（feedparser 解析）
  json —— JSON API（华尔街见闻 / 东方财富等）
  html —— 页面内嵌 JSON（36氪快讯 initialState）

输出条目统一字段：
  {title, url, summary, ts(unix秒,可空), source, source_label, lang}

境外源（WSJ/Bloomberg/Reuters/NYT/FT/CNBC 等）在大陆直连通常被墙，
此处以 proxy_only 标注：仅当环境变量 HTTPS_PROXY/HTTP_PROXY 设置时才抓取，
否则前端只展示信息源标签（标注"需代理"）。部署到海外或挂代理即可点亮。
Bloomberg 无公开新闻 RSS，仅作信息源占位展示，不自动抓取。
"""
import copy
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import feedparser
import requests

logger = logging.getLogger("us_news")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "*/*"})
SESSION.trust_env = True  # 遵从 HTTP_PROXY/HTTPS_PROXY 环境变量

TIMEOUT = 12
PER_SOURCE = 10            # 每源取 top N
CACHE_TTL = float(os.environ.get("NEWS_CACHE_TTL", "180"))


def _ts_from_struct(t):
    """feedparser 的 struct_time → unix 秒（UTC）。"""
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def _ts_from_cn(s):
    """'YYYY-MM-DD HH:MM:SS'（东八区）→ unix 秒。"""
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=8))).timestamp()
    except Exception:
        return None


# ---------------- 各源抓取器（返回原始条目列表） ----------------
def _fetch_rss(url, referer=None, timeout=TIMEOUT):
    headers = {}
    if referer:
        headers["Referer"] = referer
    r = SESSION.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    d = feedparser.parse(r.content)
    items = []
    for e in d.entries:
        ts = _ts_from_struct(getattr(e, "published_parsed", None)
                             or getattr(e, "updated_parsed", None))
        items.append({
            "title": (getattr(e, "title", "") or "").strip(),
            "url": getattr(e, "link", "") or "",
            "summary": (getattr(e, "summary", "") or "").strip(),
            "ts": ts,
        })
    return items


def _wscn_lives():
    """华尔街见闻·实时快讯（全球频道，含地缘）。"""
    url = ("https://api-one-wscn.awtmt.com/apiv1/content/lives"
           "?channel=global-channel&limit=%d" % (PER_SOURCE * 2))
    r = SESSION.get(url, headers={"Referer": "https://wallstreetcn.com/"}, timeout=TIMEOUT)
    r.raise_for_status()
    data = (r.json().get("data") or {}).get("items") or []
    items = []
    for it in data:
        items.append({
            "title": (it.get("title") or "").strip()
                     or (it.get("content_text") or "").strip()[:80],
            "url": it.get("uri") or "",
            "summary": (it.get("content_text") or it.get("highlight_title") or "").strip(),
            "ts": it.get("display_time"),
        })
    return items


def _wscn_articles():
    """华尔街见闻·深度文章。"""
    url = ("https://api-one-wscn.awtmt.com/apiv1/content/articles"
           "?limit=%d" % (PER_SOURCE * 2))
    r = SESSION.get(url, headers={"Referer": "https://wallstreetcn.com/"}, timeout=TIMEOUT)
    r.raise_for_status()
    data = (r.json().get("data") or {}).get("items") or []
    items = []
    for it in data:
        items.append({
            "title": (it.get("title") or "").strip(),
            "url": it.get("uri") or "",
            "summary": (it.get("content_short") or "").strip(),
            "ts": it.get("display_time"),
        })
    return items


def _em_kuaixun():
    """东方财富·快讯（JSONP 变体，var ajaxResult={...}）。"""
    url = ("https://newsapi.eastmoney.com/kuaixun/v1/getlist_103_ajaxResult_"
           "%d_1_.html" % (PER_SOURCE * 2))
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    m = re.search(r"\{.*\}", r.text, re.S)
    if not m:
        return []
    ll = (json.loads(m.group(0)).get("LivesList") or [])
    items = []
    for it in ll:
        items.append({
            "title": (it.get("title") or "").strip(),
            "url": it.get("url_w") or "",
            "summary": (it.get("digest") or "").strip(),
            "ts": _ts_from_cn(it.get("newstime") or it.get("showtime")),
        })
    return items


def _kr_flashes():
    """36氪·快讯（页面 initialState 内嵌 JSON）。"""
    r = SESSION.get("https://36kr.com/newsflashes", timeout=TIMEOUT)
    r.raise_for_status()
    m = re.search(r"window\.initialState=(\{.*?\})\s*</script>", r.text, re.S)
    if not m:
        return []
    s = m.group(1).replace("undefined", "null")
    try:
        d = json.loads(s)
    except Exception:
        return []
    nl = (d.get("newsflashCatalogData", {}).get("data", {})
            .get("newsflashList", {}).get("data", {}).get("itemList", []) or [])
    items = []
    for it in nl:
        mat = it.get("templateMaterial") or {}
        t = mat.get("publishTime")
        ts = None
        if isinstance(t, (int, float)):
            ts = t / 1000
        elif t:
            ts = _ts_from_cn(str(t)[:19])
        iid = mat.get("itemId")
        items.append({
            "title": (mat.get("widgetTitle") or "").strip(),
            "url": ("https://www.36kr.com/newsflashes/%s" % iid) if iid else "https://www.36kr.com/newsflashes",
            "summary": (mat.get("widgetContent") or "").strip(),
            "ts": ts,
        })
    return items


# ---------------- 信息源目录 ----------------
SOURCES = [
    # —— 国内可直连 ——
    {"key": "wscn_live", "label": "华尔街见闻·快讯", "lang": "zh",
     "cat": ["finance", "geopolitics"], "fetch": _wscn_lives,
     "home": "https://wallstreetcn.com/live/global"},
    {"key": "wscn_art", "label": "华尔街见闻·深度", "lang": "zh",
     "cat": ["finance"], "fetch": _wscn_articles,
     "home": "https://wallstreetcn.com/news/global"},
    {"key": "em_kx", "label": "东方财富·快讯", "lang": "zh",
     "cat": ["finance"], "fetch": _em_kuaixun,
     "home": "https://kuaixun.eastmoney.com/"},
    {"key": "kr_flash", "label": "36氪·快讯", "lang": "zh",
     "cat": ["finance", "tech"], "fetch": _kr_flashes,
     "home": "https://36kr.com/newsflashes"},
    {"key": "kr_feed", "label": "36氪·文章", "lang": "zh",
     "cat": ["finance", "tech"], "fetch": lambda: _fetch_rss("https://36kr.com/feed"),
     "home": "https://36kr.com/"},
    {"key": "ithome", "label": "IT之家", "lang": "zh",
     "cat": ["tech"], "fetch": lambda: _fetch_rss("https://www.ithome.com/rss/"),
     "home": "https://www.ithome.com/"},
    {"key": "cointelegraph", "label": "Cointelegraph", "lang": "en",
     "cat": ["crypto"], "fetch": lambda: _fetch_rss("https://cointelegraph.com/rss"),
     "home": "https://cointelegraph.com/"},
    {"key": "seekingalpha", "label": "Seeking Alpha", "lang": "en",
     "cat": ["finance"], "fetch": lambda: _fetch_rss("https://seekingalpha.com/market_currents.xml"),
     "home": "https://seekingalpha.com/"},
    # —— 境外源（需代理 / 海外网络，列名以表明信息源）——
    {"key": "reuters", "label": "Reuters·World", "lang": "en",
     "cat": ["geopolitics", "finance"], "proxy_only": True, "note": "境外源·需代理",
     "fetch": lambda: _fetch_rss("https://feeds.feedburner.com/Reuters/worldNews"),
     "home": "https://www.reuters.com/world/"},
    {"key": "cnbc", "label": "CNBC·Top News", "lang": "en",
     "cat": ["finance"], "proxy_only": True, "note": "境外源·需代理",
     "fetch": lambda: _fetch_rss("https://www.cnbc.com/id/100003114/device.rss"),
     "home": "https://www.cnbc.com/"},
    {"key": "wsj", "label": "WSJ·Markets", "lang": "en",
     "cat": ["finance"], "proxy_only": True, "note": "境外源·需代理",
     "fetch": lambda: _fetch_rss(
         "https://feeds.content.dowjones.io/public/rss/"
         "SB10001402405067679374004585117953822879622.rss"),
     "home": "https://www.wsj.com/markets"},
    {"key": "nyt_world", "label": "NYT·World", "lang": "en",
     "cat": ["geopolitics"], "proxy_only": True, "note": "境外源·需代理",
     "fetch": lambda: _fetch_rss("https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
     "home": "https://www.nytimes.com/section/world"},
    {"key": "ft", "label": "Financial Times", "lang": "en",
     "cat": ["finance"], "proxy_only": True, "note": "境外源·需代理",
     "fetch": lambda: _fetch_rss("https://www.ft.com/rss/homepage"),
     "home": "https://www.ft.com/"},
    {"key": "bloomberg", "label": "Bloomberg", "lang": "en",
     "cat": ["finance"], "proxy_only": True, "note": "无公开新闻 RSS·仅占位",
     "fetch": None, "home": "https://www.bloomberg.com/"},
    {"key": "nikkei", "label": "Nikkei Asia", "lang": "en",
     "cat": ["finance", "geopolitics"], "proxy_only": True, "note": "境外源·需代理",
     "fetch": lambda: _fetch_rss("https://asia.nikkei.com/rss/feed/nar"),
     "home": "https://asia.nikkei/"},
]

_order = {s["key"]: i for i, s in enumerate(SOURCES)}


def _proxy_ok():
    return bool(os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
               or os.environ.get("https_proxy") or os.environ.get("http_proxy"))


def _fetch_one(src):
    fn = src.get("fetch")
    if fn is None:
        return [], "占位（无自动抓取）"
    if src.get("proxy_only") and not _proxy_ok():
        return [], "需代理"
    try:
        items = fn() or []
        items = sorted(items, key=lambda x: (x.get("ts") or 0), reverse=True)[:PER_SOURCE]
        return items, None
    except Exception as e:
        logger.warning("抓取 %s 失败: %s", src["key"], e)
        return [], str(e)[:80]


# ---------------- 缓存 ----------------
_cache = {"ts": 0.0, "data": None, "lock": threading.Lock(), "errors": {}}


def fetch_all(force=False):
    """返回 {groups:[...], updated, proxy_ok}。TTL 内返回缓存（深拷贝，防调用方污染）。"""
    now = time.time()
    with _cache["lock"]:
        if not force and _cache["data"] and now - _cache["ts"] < CACHE_TTL:
            return copy.deepcopy(_cache["data"])
    groups = []
    errors = {}
    active = SOURCES
    with ThreadPoolExecutor(max_workers=min(12, len(active))) as ex:
        fut = {ex.submit(_fetch_one, s): s for s in active}
        for f in as_completed(fut):
            s = fut[f]
            try:
                items, err = f.result()
            except Exception as e:
                items, err = [], str(e)[:80]
            groups.append({
                "key": s["key"], "label": s["label"], "lang": s.get("lang", ""),
                "cat": s.get("cat", []), "note": s.get("note", ""),
                "home": s.get("home", ""), "items": items, "error": err,
                "proxy_only": bool(s.get("proxy_only")),
            })
            if err:
                errors[s["key"]] = err
    groups.sort(key=lambda g: _order.get(g["key"], 999))
    out = {"groups": groups, "updated": time.time(), "proxy_ok": _proxy_ok()}
    with _cache["lock"]:
        _cache["data"] = out
        _cache["ts"] = time.time()
        _cache["errors"] = errors
    return copy.deepcopy(out)


def _warmup():
    try:
        d = fetch_all(force=True)
        n = sum(len(g["items"]) for g in d["groups"])
        logger.info("要闻预热完成，共 %d 条（%d 源）", n, len(d["groups"]))
    except Exception as e:
        logger.warning("要闻预热失败（不影响运行）: %s", e)


if __name__ == "__main__":
    import pprint
    d = fetch_all(force=True)
    for g in d["groups"]:
        print(f"\n== {g['label']} ({len(g['items'])}) err={g['error']} ==")
        for it in g["items"][:3]:
            print(" -", it["title"][:60])
