#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信通知模块（价格触达提醒用）。

支持三种通道：企业微信群机器人 / PushPlus / Server酱。用法：

    export WECOM_WEBHOOK="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
    export PUSHPLUS_TOKEN="xxx"
    export SERVERCHAN_SENDKEY="xxx"
    python utils/weichat_notify.py --channel pushplus --title "标题" --body "正文"

由 price_alert.py 直接 import Notifier 使用；也可单独作为 CLI 测试。
"""
import argparse
import json
import os
import time

import requests


class Notifier:
    """统一通知入口，带超时与重试（指数退避）。"""

    def __init__(self, timeout=10, max_retries=3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()

    def _post(self, url, payload):
        last_err = None
        for attempt in range(self.max_retries):
            try:
                r = self.session.post(url, json=payload, timeout=self.timeout)
                result = r.json()
                if result.get("code") == 200 or result.get("errcode") == 0:
                    return True, result
                last_err = result
            except Exception as e:
                last_err = str(e)
            time.sleep(2 ** attempt)  # 指数退避: 1s, 2s, 4s
        return False, last_err

    def wecom(self, title, body, webhook=None):
        """企业微信群机器人：markdown 消息"""
        webhook = webhook or os.environ.get("WECOM_WEBHOOK")
        if not webhook:
            return False, "缺少 WECOM_WEBHOOK 环境变量"
        content = f"## {title}\n{body}"
        return self._post(webhook, {
            "msgtype": "markdown",
            "markdown": {"content": content},
        })

    def pushplus(self, title, body, token=None):
        """PushPlus：直达个人微信，实名用户免费 200 条/天"""
        token = token or os.environ.get("PUSHPLUS_TOKEN")
        if not token:
            return False, "缺少 PUSHPLUS_TOKEN 环境变量"
        ok, result = self._post("https://www.pushplus.plus/send", {
            "token": token,
            "title": title[:100],  # 标题上限100字（会员200）
            "content": body,
            "template": "markdown",
        })
        # code=200 仅代表"收到请求"，需用流水号异步查询最终结果
        # 900=当日额度用尽/账号受限（停止重试） 905=未实名 903=token错误
        if ok and isinstance(result, dict):
            code = result.get("code")
            if code in (900, 905, 903):
                return False, f"pushplus 拒绝: code={code} msg={result.get('msg')}（900=额度耗尽勿重试, 905=需实名认证, 903=token错误）"
        return ok, result

    def serverchan(self, title, body, sendkey=None):
        """Server酱 Turbo：直达个人微信，免费 5 条/天"""
        sendkey = sendkey or os.environ.get("SERVERCHAN_SENDKEY")
        if not sendkey:
            return False, "缺少 SERVERCHAN_SENDKEY 环境变量"
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        try:
            r = self.session.post(url, data={"title": title, "desp": body},
                                  timeout=self.timeout)
            result = r.json()
            return result.get("code") == 0, result
        except Exception as e:
            return False, str(e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="微信通知测试")
    parser.add_argument("--channel", choices=["wecom", "pushplus", "serverchan"], required=True)
    parser.add_argument("--title", default="测试通知")
    parser.add_argument("--body", default="这是一条测试消息")
    args = parser.parse_args()

    n = Notifier()
    fn = getattr(n, args.channel)
    ok, resp = fn(args.title, args.body)
    print("发送成功" if ok else f"发送失败: {resp}")
    print(json.dumps(resp, ensure_ascii=False, indent=2))
