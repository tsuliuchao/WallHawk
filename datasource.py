# -*- coding: utf-8 -*-
"""行情数据源抽象层。

默认 YahooProvider：通过 v7/finance/quote 批量取盘前/盘中/盘后价与 marketState。
可选 FutuProvider：通过 futu-api + OpenD 取快照（需登录，懒加载）。

两者返回统一的 Quote 结构，供 app.py 缓存与下发。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, time as dt_time
from typing import Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------- 数据结构 ----------------
@dataclass
class Quote:
    symbol: str
    name: str
    price: Optional[float]          # 当前活跃时段价（盘前/盘中/盘后之一）
    prev_close: Optional[float]
    change: Optional[float]         # 当前时段涨跌额
    change_pct: Optional[float]     # 当前时段涨跌幅%
    regular_price: Optional[float]
    regular_change_pct: Optional[float]
    pre_price: Optional[float]
    pre_change_pct: Optional[float]
    post_price: Optional[float]
    post_change_pct: Optional[float]
    market_state: str               # PRE / REGULAR / POST / CLOSED / PREPRE / UNKNOWN
    currency: str
    last_updated: Optional[int]     # epoch 秒
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------- 工具 ----------------
def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(price: Optional[float], prev: Optional[float]) -> Optional[float]:
    if price is None or not prev:
        return None
    return round((price - prev) / prev * 100.0, 2)


def _diff(price: Optional[float], prev: Optional[float]) -> Optional[float]:
    if price is None or prev is None:
        return None
    return round(price - prev, 4)


def _round2(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


_STATE_MAP = {
    "PRE": "PRE", "PREPRE": "PREPRE",
    "REGULAR": "REGULAR",
    "POST": "POST", "POSTPOST": "POST",
    "CLOSED": "CLOSED",
}


def _norm_state(s: str) -> str:
    return _STATE_MAP.get((s or "").upper(), "UNKNOWN")


def _us_session_state() -> str:
    """按美东时间判定美股盘前/盘中/盘后/休市（自动处理夏令时）。

    盘前 04:00-09:30 ET / 盘中 09:30-16:00 / 盘后 16:00-20:00 / 其余休市。
    用于不直接返回 marketState 的数据源（如腾讯）。
    """
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return "CLOSED"
    t = now.time()
    if dt_time(4, 0) <= t < dt_time(9, 30):
        return "PRE"
    if dt_time(9, 30) <= t < dt_time(16, 0):
        return "REGULAR"
    if dt_time(16, 0) <= t < dt_time(20, 0):
        return "POST"
    return "CLOSED"


def _resolve_active(state, reg, reg_chg, reg_pct, prev_close, pre, post):
    """按当前时段解析用于展示的 (price, change, change_pct, prev_close)。

    保证"涨跌幅"与"当前状态"一致：
    - 盘前：有盘前价 -> 盘前价相对昨收的变动；无盘前成交 -> 0%（价格=昨收）
    - 盘后：有盘后价 -> 盘后价相对昨收的变动；无盘后成交 -> 0%（价格=昨收）
    - 盘中/休市：常规行情
    """
    if state == "PRE" and pre not in (None, 0):
        return pre, _diff(pre, reg), _pct(pre, reg), reg
    if state == "POST" and post not in (None, 0):
        return post, _diff(post, reg), _pct(post, reg), reg
    if state in ("PRE", "POST"):
        return reg, 0.0, 0.0, reg
    return reg, reg_chg, reg_pct, prev_close


# ---------------- Yahoo ----------------
class YahooProvider:
    SOURCE = "YAHOO"
    QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
    CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
    COOKIE_URLS = ["https://fc.yahoo.com", "https://finance.yahoo.com"]

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self._crumb: Optional[str] = None
        self._lock = threading.Lock()

    def _init_crumb(self) -> bool:
        for url in self.COOKIE_URLS:
            try:
                self.session.get(url, timeout=Config.YAHOO_TIMEOUT)
            except Exception:
                pass  # 404 也可能已种下 cookie，忽略
        for _ in range(3):
            try:
                r = self.session.get(self.CRUMB_URL, timeout=Config.YAHOO_TIMEOUT)
                if r.status_code == 200 and r.text.strip():
                    self._crumb = r.text.strip()
                    logger.info("Yahoo crumb 已获取")
                    return True
            except Exception:
                time.sleep(0.5)
        logger.warning("Yahoo crumb 获取失败")
        return False

    def _fetch_batch(self, symbols: list[str]) -> dict:
        if not self._crumb and not self._init_crumb():
            return {}
        params = {"symbols": ",".join(symbols), "crumb": self._crumb}
        r = self.session.get(self.QUOTE_URL, params=params, timeout=Config.YAHOO_TIMEOUT)
        # crumb 失效 -> 刷新后重试一次
        if r.status_code in (401, 403) or "Invalid Crumb" in r.text:
            logger.info("Yahoo crumb 失效，刷新重试")
            self._crumb = None
            if not self._init_crumb():
                return {}
            params["crumb"] = self._crumb
            r = self.session.get(self.QUOTE_URL, params=params, timeout=Config.YAHOO_TIMEOUT)
        r.raise_for_status()
        result = r.json().get("quoteResponse", {}).get("result", []) or []
        return {item.get("symbol", ""): self._parse(item) for item in result}

    def _parse(self, item: dict) -> Quote:
        reg = _f(item.get("regularMarketPrice"))
        prev = _f(item.get("regularMarketPreviousClose") or item.get("previousClose"))
        reg_chg = _f(item.get("regularMarketChange"))
        reg_pct = _f(item.get("regularMarketChangePercent"))
        pre = _f(item.get("preMarketPrice"))
        pre_chg = _f(item.get("preMarketChange"))
        pre_pct = _f(item.get("preMarketChangePercent"))
        post = _f(item.get("postMarketPrice"))
        post_chg = _f(item.get("postMarketChange"))
        post_pct = _f(item.get("postMarketChangePercent"))
        state = _norm_state(item.get("marketState", ""))
        price, change, pct, pc = _resolve_active(state, reg, reg_chg, reg_pct, prev, pre, post)

        return Quote(
            symbol=item.get("symbol", ""),
            name=item.get("shortName") or item.get("longName") or item.get("symbol", ""),
            price=_round2(price),
            prev_close=_round2(pc),
            change=_round2(change),
            change_pct=_round2(pct),
            regular_price=_round2(reg),
            regular_change_pct=_round2(reg_pct),
            pre_price=_round2(pre),
            pre_change_pct=_round2(pre_pct),
            post_price=_round2(post),
            post_change_pct=_round2(post_pct),
            market_state=state,
            currency=item.get("currency", "USD"),
            last_updated=item.get("regularMarketTime"),
            source=self.SOURCE,
        )

    def get_quotes(self, symbols: list[str]) -> dict:
        out: dict = {}
        with self._lock:
            for i in range(0, len(symbols), Config.YAHOO_BATCH_SIZE):
                batch = symbols[i:i + Config.YAHOO_BATCH_SIZE]
                try:
                    out.update(self._fetch_batch(batch))
                except Exception as e:
                    logger.warning("Yahoo 批次 %d-%d 失败: %s", i, i + len(batch), e)
        return out


# ---------------- 富途 ----------------
class FutuProvider:
    SOURCE = "FUTU"

    def __init__(self):
        try:
            from futu import OpenQuoteContext  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "未安装 futu-api。运行 `pip install futu-api`，"
                "并启动 OpenD（富途牛牛>设置>OpenAPI）后重试。"
            ) from e
        self._OpenQuoteContext = None
        self._ctx = None
        self._lock = threading.Lock()

    def _get_ctx(self):
        from futu import OpenQuoteContext
        if self._ctx is None:
            self._ctx = OpenQuoteContext(Config.FUTU_HOST, Config.FUTU_PORT)
        return self._ctx

    @staticmethod
    def _session_state() -> str:
        return _us_session_state()

    def get_quotes(self, symbols: list[str]) -> dict:
        from futu import RET_OK
        out: dict = {}
        with self._lock:
            ctx = self._get_ctx()
            codes = [f"{s}.US" for s in symbols]
            # 快照接口单次有限制，分批 50
            for i in range(0, len(codes), 50):
                batch = codes[i:i + 50]
                ret, df = ctx.get_market_snapshot(batch)
                if ret != RET_OK:
                    logger.warning("Futu 快照失败 %s: %s", batch[:3], df)
                    continue
                state = self._session_state()
                for _, row in df.iterrows():
                    sym = str(row.get("code", "")).replace(".US", "")
                    reg = _f(row.get("last_price"))
                    prev = _f(row.get("prev_close"))
                    reg_pct = _f(row.get("change_rate"))
                    pre = _f(row.get("pre_price"))
                    post = _f(row.get("after_price"))
                    price, chg, pct, pc = _resolve_active(state, reg, _diff(reg, prev), reg_pct, prev, pre, post)
                    out[sym] = Quote(
                        symbol=sym,
                        name=str(row.get("stock_name") or sym),
                        price=_round2(price), prev_close=_round2(pc),
                        change=_round2(chg), change_pct=_round2(pct),
                        regular_price=_round2(reg), regular_change_pct=_round2(reg_pct),
                        pre_price=_round2(pre), pre_change_pct=_pct(pre, prev),
                        post_price=_round2(post), post_change_pct=_pct(post, prev),
                        market_state=state, currency="USD",
                        last_updated=int(time.time()), source=self.SOURCE,
                    )
        return out


# ---------------- 腾讯（默认，中国可直连免登录）----------------
class TencentProvider:
    """qt.gtimg.cn 美股行情：us+代码 批量、GBK、~ 分隔。

    返回最新成交价/涨跌额/涨跌幅/昨收/开盘/最高/最低，无独立盘前盘后字段。
    盘前/盘后状态按美东时间推导；盘前盘后时段若腾讯更新成交价则反映为当前价。
    需要独立盘前盘后价请改用 FUTU 或可直连的 YAHOO。
    """
    SOURCE = "TENCENT"
    URL = "https://qt.gtimg.cn/q="
    BATCH = 40

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self._lock = threading.Lock()

    def get_quotes(self, symbols: list[str]) -> dict:
        out: dict = {}
        with self._lock:
            for i in range(0, len(symbols), self.BATCH):
                batch = symbols[i:i + self.BATCH]
                q = ",".join("us" + s for s in batch)
                try:
                    r = self.session.get(self.URL + q, timeout=Config.YAHOO_TIMEOUT)
                    txt = r.content.decode("gbk", "ignore")
                    out.update(self._parse(txt))
                except Exception as e:
                    logger.warning("腾讯批次 %d-%d 失败: %s", i, i + len(batch), e)
        return out

    def _parse(self, txt: str) -> dict:
        out: dict = {}
        state = _us_session_state()
        for line in txt.split(";"):
            line = line.strip()
            if '="' not in line:
                continue
            prefix, _, val = line.partition('="')
            val = val.rstrip('";').strip()
            if not val:
                continue
            sym = prefix.replace("v_us", "").strip()
            fields = val.split("~")
            if len(fields) < 36 or not sym:
                continue
            price = _f(fields[3])
            prev = _f(fields[4])
            if price is None:
                continue
            chg = _f(fields[31])
            pct = _f(fields[32])
            # 腾讯无盘前盘后价：盘前/盘后时段统一显示 0%（价格=昨收），与状态一致
            price, chg, pct, pc = _resolve_active(state, price, chg, pct, prev, None, None)
            out[sym] = Quote(
                symbol=sym,
                name=fields[1] or sym,
                price=_round2(price), prev_close=_round2(pc),
                change=_round2(chg), change_pct=_round2(pct),
                regular_price=_round2(price), regular_change_pct=_round2(pct),
                pre_price=None, pre_change_pct=None,
                post_price=None, post_change_pct=None,
                market_state=state, currency=(fields[35] or "USD"),
                last_updated=int(time.time()), source=self.SOURCE,
            )
        return out


# ---------------- 新浪（默认，含盘前/盘后价）----------------
class SinaProvider:
    """hq.sinajs.cn 美股行情：gb_+代码 批量、GBK。

    字段（~36）：[0]名称 [1]最新常规价 [2]常规涨跌幅 [4]常规涨跌额
    [21]盘前价 [26]昨收 [34]盘后价。盘前/盘后价独立可用，弥补腾讯不更新延展时段的缺陷。
    """
    SOURCE = "SINA"
    URL = "https://hq.sinajs.cn/list="
    BATCH = 40

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"})
        self._lock = threading.Lock()

    def get_quotes(self, symbols: list[str]) -> dict:
        out: dict = {}
        with self._lock:
            for i in range(0, len(symbols), self.BATCH):
                batch = symbols[i:i + self.BATCH]
                q = ",".join("gb_" + s.lower() for s in batch)
                try:
                    r = self.session.get(self.URL + q, timeout=Config.YAHOO_TIMEOUT)
                    txt = r.content.decode("gbk", "ignore")
                    out.update(self._parse(txt))
                except Exception as e:
                    logger.warning("新浪批次 %d-%d 失败: %s", i, i + len(batch), e)
        return out

    def _parse(self, txt: str) -> dict:
        out: dict = {}
        state = _us_session_state()
        for line in txt.split("\n"):
            line = line.strip()
            if '="' not in line:
                continue
            prefix, _, val = line.partition('="')
            val = val.rstrip('";').strip()
            if not val:
                continue
            sym = prefix.replace("var hq_str_gb_", "").strip().upper()
            f = val.split(",")
            if len(f) < 27 or not sym:
                continue
            reg = _f(f[1])
            if reg is None:
                continue
            reg_pct = _f(f[2]); reg_chg = _f(f[4]); prev_close = _f(f[26])
            pre = _f(f[21])
            post = _f(f[34]) if len(f) > 34 else None
            price, chg, pct, pc = _resolve_active(state, reg, reg_chg, reg_pct, prev_close, pre, post)
            out[sym] = Quote(
                symbol=sym, name=f[0] or sym,
                price=_round2(price), prev_close=_round2(pc),
                change=_round2(chg), change_pct=_round2(pct),
                regular_price=_round2(reg), regular_change_pct=_round2(reg_pct),
                pre_price=_round2(pre) if (pre and pre != 0) else None,
                pre_change_pct=_pct(pre, reg) if (pre and pre != 0) else None,
                post_price=_round2(post) if (post and post != 0) else None,
                post_change_pct=_pct(post, reg) if (post and post != 0) else None,
                market_state=state, currency="USD",
                last_updated=int(time.time()), source=self.SOURCE,
            )
        return out


# ---------------- 工厂 ----------------
_provider = None
_provider_lock = threading.Lock()


def get_provider():
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                src = Config.DATA_SOURCE
                if src == "FUTU":
                    _provider = FutuProvider()
                    logger.info("数据源：富途 OpenAPI (%s:%s)", Config.FUTU_HOST, Config.FUTU_PORT)
                elif src == "YAHOO":
                    _provider = YahooProvider()
                    logger.info("数据源：Yahoo Finance")
                elif src == "TENCENT":
                    _provider = TencentProvider()
                    logger.info("数据源：腾讯行情（qt.gtimg.cn）")
                else:
                    _provider = SinaProvider()
                    logger.info("数据源：新浪行情（hq.sinajs.cn，含盘前盘后）")
    return _provider
