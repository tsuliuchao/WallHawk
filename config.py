# -*- coding: utf-8 -*-
"""美股盯盘助手配置。优先读环境变量。"""
import os
import sys

_VALID_SOURCES = {"SINA", "TENCENT", "YAHOO", "FUTU"}


class Config:
    # ---- 数据源：SINA（默认，中国直连免登录，含盘前/盘后价）/ TENCENT（无独立盘前盘后）
    #      / YAHOO（部分地区可直连，含独立盘前盘后）/ FUTU（富途 OpenAPI，需 OpenD + 登录）----
    DATA_SOURCE = os.environ.get("DATA_SOURCE", "SINA").upper()
    if DATA_SOURCE not in _VALID_SOURCES:
        sys.exit(
            f"[config] 无效 DATA_SOURCE={DATA_SOURCE!r}，"
            f"可选：{', '.join(sorted(_VALID_SOURCES))}"
        )

    # ---- Web 服务 ----
    # 默认仅监听本机回环，避免局域网内他人访问写接口；确需局域网访问时显式 export HOST=0.0.0.0
    HOST = os.environ.get("HOST", "127.0.0.1")
    PORT = int(os.environ.get("PORT", "8050"))

    # ---- 写接口鉴权（可选）----
    # 设置后，所有 POST/DELETE 写接口需携带 X-Admin-Token 头，否则返回 401。
    # 留空则不启用（仅靠 HOST=127.0.0.1 兜底）。令牌只经环境变量注入，勿硬编码。
    ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

    # 服务端行情缓存TTL(秒)：多个前端/快速轮询时合并请求，避免压垮数据源
    QUOTE_CACHE_TTL = float(os.environ.get("QUOTE_CACHE_TTL", "3"))

    # 前端默认刷新间隔(秒)
    DEFAULT_REFRESH_SEC = int(os.environ.get("DEFAULT_REFRESH_SEC", "10"))

    # ---- Yahoo Finance ----
    YAHOO_BATCH_SIZE = int(os.environ.get("YAHOO_BATCH_SIZE", "50"))
    YAHOO_TIMEOUT = int(os.environ.get("YAHOO_TIMEOUT", "10"))

    # ---- 富途 OpenAPI / OpenD ----
    FUTU_HOST = os.environ.get("FUTU_HOST", "127.0.0.1")
    FUTU_PORT = int(os.environ.get("FUTU_PORT", "11111"))

    # ---- 价格触达提醒 ----
    # 通知通道：pushplus（默认）/ wecom / serverchan，与 utils/weichat_notify.py 对应
    ALERT_CHANNEL = os.environ.get("ALERT_CHANNEL", "pushplus")
    # 触达检查间隔（秒）
    ALERT_CHECK_INTERVAL = int(os.environ.get("ALERT_CHECK_INTERVAL", "10"))
