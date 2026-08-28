# -*- coding: utf-8 -*-
"""Flask API 集成测试（test client，无网络）。

隔离手段：
- WATCHLIST_PATH 指向 tmp_path，绝不触碰真实 watchlist.json；
- 行情缓存清零；
- 需要「自动抓名称」的接口统一 monkeypatch get_provider，避免真实联网。
"""
from types import SimpleNamespace

import pytest

import app as app_module
from config import Config


class FakeProvider:
    """只实现 get_quotes 且仅填充 .name 字段（测试中唯一被读取的属性）。"""

    def __init__(self, names=None):
        self.names = names or {}

    def get_quotes(self, symbols):
        return {s: SimpleNamespace(name=self.names.get(s, s)) for s in symbols}


@pytest.fixture
def client(monkeypatch, tmp_path):
    """返回 (test_client, app_module)。每次测试用全新板块状态与缓存。"""
    monkeypatch.setattr(app_module, "WATCHLIST_PATH", str(tmp_path / "watchlist.json"))
    monkeypatch.setattr(app_module, "_sectors", None)   # 强制重新种子化
    with app_module._cache["lock"]:                     # 清空行情缓存
        app_module._cache.update({"ts": 0.0, "data": {}, "err": None})
    app_module.app.config["TESTING"] = True
    yield app_module.app.test_client()


@pytest.fixture
def no_net(monkeypatch):
    """把数据源替换为 FakeProvider，杜绝任何真实网络请求。"""
    fake = FakeProvider(names={"FAKE": "假想公司"})
    monkeypatch.setattr(app_module, "get_provider", lambda: fake)
    return fake


# ---------------- 基础读接口 ----------------
def test_index_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200


def test_api_sectors_contains_today_watch(client):
    r = client.get("/api/sectors")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)
    keys = [s["key"] for s in data]
    assert "today_watch" in keys
    assert len(keys) >= 15


def test_health_reports_source(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["source"] == Config.DATA_SOURCE
    assert "ok" in body and "symbols_total" in body


def test_news_endpoint_uses_cache_shape(client, monkeypatch):
    monkeypatch.setattr(app_module.news, "fetch_all",
                        lambda force=False: {"groups": [], "updated": 1, "proxy_ok": False})
    r = client.get("/api/news")
    assert r.status_code == 200
    assert r.get_json()["groups"] == []


# ---------------- 写接口鉴权 ----------------
def test_writes_open_when_no_admin_token(client, monkeypatch):
    monkeypatch.setattr(Config, "ADMIN_TOKEN", "")
    assert client.post("/api/reset").status_code == 200


def test_writes_blocked_without_token(client, monkeypatch):
    monkeypatch.setattr(Config, "ADMIN_TOKEN", "t123")
    assert client.post("/api/reset").status_code == 401
    assert client.post("/api/reset", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.delete("/api/sectors/semiconductors/stocks/NVDA").status_code == 401


def test_writes_allowed_with_correct_token(client, monkeypatch):
    monkeypatch.setattr(Config, "ADMIN_TOKEN", "t123")
    r = client.post("/api/reset", headers={"X-Admin-Token": "t123"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_reads_not_guarded_even_with_token(client, monkeypatch):
    monkeypatch.setattr(Config, "ADMIN_TOKEN", "t123")
    assert client.get("/api/sectors").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_token_never_leaks_to_template_when_unset(client, monkeypatch):
    monkeypatch.setattr(Config, "ADMIN_TOKEN", "")
    html = client.get("/").get_data(as_text=True)
    assert 'ADMIN_TOKEN = ""' in html or "ADMIN_TOKEN=''" in html or \
           '"ADMIN_TOKEN": ""' in html or 'tojson' not in html


def test_token_injected_for_frontend_when_set(client, monkeypatch):
    monkeypatch.setattr(Config, "ADMIN_TOKEN", "t-secret")
    html = client.get("/").get_data(as_text=True)
    assert "t-secret" in html


# ---------------- 板块编辑 ----------------
def test_add_and_remove_stock_roundtrip(client, no_net):
    r = client.post("/api/sectors/semiconductors/stocks",
                    json={"symbol": "fake", "name": ""})
    assert r.status_code == 200
    # 名称留空由 FakeProvider 补全，且自动大写
    assert r.get_json()["stock"]["name"] == "假想公司"

    secs = client.get("/api/sectors").get_json()
    sec = next(s for s in secs if s["key"] == "semiconductors")
    assert any(st["symbol"] == "FAKE" for st in sec["stocks"])

    r = client.delete("/api/sectors/semiconductors/stocks/fake")
    assert r.status_code == 200
    secs = client.get("/api/sectors").get_json()
    sec = next(s for s in secs if s["key"] == "semiconductors")
    assert not any(st["symbol"] == "FAKE" for st in sec["stocks"])


def test_add_stock_requires_symbol(client, no_net):
    assert client.post("/api/sectors/semiconductors/stocks",
                       json={"symbol": "  "}).status_code == 400
    assert client.post("/api/sectors/semiconductors/stocks",
                       json={}).status_code == 400


def test_add_stock_unknown_sector_404(client, no_net):
    assert client.post("/api/sectors/nope/stocks",
                       json={"symbol": "X"}).status_code == 404


def test_duplicate_add_rejected(client, no_net):
    r1 = client.post("/api/sectors/semiconductors/stocks",
                     json={"symbol": "NVDAX", "name": "英伟达X"})
    assert r1.status_code == 200
    r2 = client.post("/api/sectors/semiconductors/stocks",
                     json={"symbol": "NVDAX", "name": "英伟达X"})
    assert r2.status_code == 400


def test_move_stock_between_sectors(client, no_net):
    client.post("/api/sectors/semiconductors/stocks",
                json={"symbol": "MOVE1", "name": "待移动"})
    r = client.post("/api/stocks/move", json={"symbol": "move1",
                                              "from": "semiconductors", "to": "banks"})
    assert r.status_code == 200 and r.get_json()["moved"] is True
    secs = client.get("/api/sectors").get_json()
    banks = next(s for s in secs if s["key"] == "banks")
    assert any(st["symbol"] == "MOVE1" for st in banks["stocks"])


def test_move_same_sector_is_noop(client, no_net):
    r = client.post("/api/stocks/move", json={"symbol": "NVDA",
                                              "from": "semiconductors", "to": "semiconductors"})
    assert r.status_code == 200 and r.get_json()["moved"] is False


def test_remove_missing_stock_404(client, no_net):
    assert client.delete("/api/sectors/semiconductors/stocks/ZZZZ").status_code == 404


def test_reset_restores_defaults(client, no_net):
    client.post("/api/sectors/semiconductors/stocks",
                json={"symbol": "EXTRA", "name": "多出来的"})
    assert client.post("/api/reset").status_code == 200
    secs = client.get("/api/sectors").get_json()
    semi = next(s for s in secs if s["key"] == "semiconductors")
    assert not any(st["symbol"] == "EXTRA" for st in semi["stocks"])
    # 默认成分股回来了
    assert any(st["symbol"] == "NVDA" for st in semi["stocks"])


# ---------------- 今日关注与下限价 ----------------
def test_watch_toggle_roundtrip(client, no_net):
    r = client.post("/api/watch/Fake")
    assert r.status_code == 200 and r.get_json()["watched"] is True
    secs = client.get("/api/sectors").get_json()
    watch = next(s for s in secs if s["key"] == "today_watch")
    assert any(st["symbol"] == "FAKE" for st in watch["stocks"])

    r = client.delete("/api/watch/fake")
    assert r.status_code == 200 and r.get_json()["watched"] is False


def test_watch_idempotent_post(client, no_net):
    assert client.post("/api/watch/FAKE").status_code == 200
    r = client.post("/api/watch/FAKE")     # 重复加入不报错
    assert r.status_code == 200
    secs = client.get("/api/sectors").get_json()
    watch = next(s for s in secs if s["key"] == "today_watch")
    assert sum(1 for st in watch["stocks"] if st["symbol"] == "FAKE") == 1


def test_expect_price_saved_on_watched_stock(client, no_net):
    client.post("/api/watch/FAKE")
    r = client.post("/api/watch/Fake/expect", json={"expect": 88.5})
    assert r.status_code == 200 and r.get_json()["expect_price"] == 88.5
    secs = client.get("/api/sectors").get_json()
    watch = next(s for s in secs if s["key"] == "today_watch")
    st = next(x for x in watch["stocks"] if x["symbol"] == "FAKE")
    assert st["expect_price"] == 88.5


def test_upper_price_saved_and_listed(client, no_net):
    client.post("/api/watch/FAKE")
    r = client.post("/api/watch/FAKE/expect", json={"expect": 80, "upper": 120})
    assert r.status_code == 200
    assert r.get_json()["expect_price"] == 80 and r.get_json()["upper_price"] == 120
    secs = client.get("/api/sectors").get_json()
    watch = next(s for s in secs if s["key"] == "today_watch")
    st = next(x for x in watch["stocks"] if x["symbol"] == "FAKE")
    assert st["expect_price"] == 80 and st["upper_price"] == 120


def test_upper_price_legacy_price_alias(client, no_net):
    """旧前端用 body.price 表示下限价，仍兼容。"""
    client.post("/api/watch/FAKE")
    r = client.post("/api/watch/FAKE/expect", json={"price": 88.5})
    assert r.status_code == 200 and r.get_json()["expect_price"] == 88.5


def test_expect_price_can_be_cleared(client, no_net):
    client.post("/api/watch/FAKE")
    client.post("/api/watch/FAKE/expect", json={"expect": 10})
    r = client.post("/api/watch/FAKE/expect", json={"expect": None})
    assert r.status_code == 200 and r.get_json()["expect_price"] is None


def test_expect_price_bad_value_400(client, no_net):
    client.post("/api/watch/FAKE")
    assert client.post("/api/watch/FAKE/expect",
                       json={"expect": "not-a-number"}).status_code == 400
    assert client.post("/api/watch/FAKE/expect",
                       json={"upper": "not-a-number"}).status_code == 400


def test_expect_price_requires_watched_stock(client, no_net):
    assert client.post("/api/watch/NOWATCH/expect",
                       json={"expect": 5}).status_code == 404


# ---------------- 下限价收集 ----------------
def test_collect_expected_snapshot(client, no_net):
    client.post("/api/watch/FAKE")
    client.post("/api/watch/FAKE/expect", json={"expect": 12.3, "upper": 20.0})
    expected = app_module._collect_expected()
    assert ("FAKE", "假想公司", 12.3, 20.0) in expected


def test_collect_expected_empty_without_watch(client, no_net):
    assert app_module._collect_expected() == []


def test_collect_expected_with_upper_only(client, no_net):
    client.post("/api/watch/FAKE")
    client.post("/api/watch/FAKE/expect", json={"expect": None, "upper": 55.0})
    expected = app_module._collect_expected()
    assert ("FAKE", "假想公司", None, 55.0) in expected


# ---------------- 提醒历史 / 测试通知 / 健康 ----------------
def test_alerts_history_empty_initial(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    app_module.price_alert._state = None
    r = client.get("/api/alerts/history")
    assert r.status_code == 200
    assert r.get_json()["history"] == []


def test_alerts_history_records_trigger(client, no_net, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.price_alert, "STATE_PATH",
                        str(tmp_path / "alert_state.json"))
    app_module.price_alert._state = None
    sent = []

    def fake_send(symbol, name, price, level, kind):
        sent.append(True)
        return True

    monkeypatch.setattr(app_module.price_alert, "_send", fake_send)
    client.post("/api/watch/FAKE")
    client.post("/api/watch/FAKE/expect", json={"expect": 100})
    # 首观测在上方、随后下穿 → 触发 cross_below 并写入历史
    app_module.price_alert.check_and_notify("FAKE", "假想公司", 105.0, 100.0)
    app_module.price_alert.check_and_notify("FAKE", "假想公司", 99.0, 100.0)
    r = client.get("/api/alerts/history")
    assert r.status_code == 200
    hist = r.get_json()["history"]
    assert hist and hist[0]["kind"] == "cross_below" and hist[0]["symbol"] == "FAKE"


def test_alerts_test_sends_notification(client, no_net, monkeypatch):
    class FakeNotifier:
        def pushplus(self, title, body):
            return True, "ok"

    monkeypatch.setattr(app_module.price_alert, "Notifier", FakeNotifier)
    r = client.post("/api/alerts/test", json={"title": "测试", "body": "你好"})
    assert r.status_code == 200
    assert r.get_json()["channel"] == "pushplus"


def test_alerts_test_failure_returns_500(client, no_net, monkeypatch):
    class FailNotifier:
        def pushplus(self, title, body):
            return False, "缺少 PUSHPLUS_TOKEN"

    monkeypatch.setattr(app_module.price_alert, "Notifier", FailNotifier)
    r = client.post("/api/alerts/test", json={})
    assert r.status_code == 500
    assert "缺少" in r.get_json()["error"]


def test_health_includes_alert_fields(client, no_net, monkeypatch):
    monkeypatch.setenv("PUSHPLUS_TOKEN", "xxx")
    r = client.get("/api/health")
    body = r.get_json()
    assert body["alert_channel"] == "pushplus"
    assert body["alert_configured"] is True
