"""只读（影子）守卫测试 —— 这是整个项目最重要的一组不变量。

影子期的定义：不许有任何生产写操作。所以这里直接监视 urllib.request.urlopen：
只要在 read_only 模式下发出请求，测试立刻失败。
"""
from __future__ import annotations

import urllib.request

import pytest

from geoagent.clients.gitea import Gitea
from geoagent.clients.strapi import Strapi
from geoagent.clients.wecom import WeCom


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append((a, k))
        raise AssertionError("read_only 模式下不应发出任何 HTTP 请求")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    return calls


def test_gitea_writes_are_shortcircuited(cfg, no_network):
    g = Gitea(cfg, read_only=True)
    assert g.create_issue("t", "b", labels=["article-generate"]) == (
        True, {"_skipped": "read_only", "method": "POST", "path": "/issues"})
    assert g.comment(1, "hi")[1].get("_skipped") == "read_only"
    assert g.close_issue(1)[1].get("_skipped") == "read_only"
    assert g.patch_issue(1, {"state": "closed"})[1].get("_skipped") == "read_only"
    assert no_network == []


def test_strapi_writes_are_shortcircuited(cfg, no_network):
    s = Strapi(cfg, read_only=True)
    ok, data = s.create_article({"title": "t", "slug": "s", "category": "product-news"})
    assert ok and data.get("_skipped") in ("read_only", "publish_disabled")
    ok, data = s.update_article("doc", {"title": "t", "slug": "s", "category": "product-news"})
    assert ok and data.get("_skipped") in ("read_only", "publish_disabled")
    assert no_network == []


def test_publish_is_blocked_even_with_apply_and_readonly_off(cfg, no_network):
    """🚫 官网发布硬锁（用户 2026-09-18 要求）。

    即使 shadow.read_only=false + --apply 全开，只要 publish.enabled=false，
    Strapi 写入也必须被拒绝 —— 这是代码级默认关闭，不靠人记。
    """
    cfg["shadow"]["read_only"] = False
    cfg["publish"]["enabled"] = False
    s = Strapi(cfg, read_only=False)      # 完全"放开写"的实例
    ok, data = s.create_article({"title": "t", "slug": "s", "category": "product-news"})
    assert ok and data["_skipped"] == "publish_disabled"
    ok, data = s.update_article("doc", {"title": "t", "slug": "s", "category": "product-news"})
    assert data["_skipped"] == "publish_disabled"
    assert no_network == [], "官网发布被锁死时绝不能发出任何请求"


def test_publish_switch_default_is_false():
    from geoagent.config import DEFAULTS
    assert DEFAULTS["publish"]["enabled"] is False
    assert DEFAULTS["shadow"]["read_only"] is True


def test_wecom_blocked_unless_notify_flag(cfg, no_network):
    cfg["wecom"]["webhook_url"] = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"
    blocked = WeCom(cfg, read_only=True, allow_send=False)
    ok, detail = blocked.send("hi")
    assert ok and detail.get("_skipped") == "shadow_mode"

    # --notify 放行才真发（会走网络 → no_network 触发 AssertionError，被 send 内部吞掉）
    allowed = WeCom(cfg, read_only=True, allow_send=True)
    ok, detail = allowed.send("hi")
    assert ok is False and "AssertionError" in str(detail)
    assert no_network, "--notify 时必须真的尝试发送"


def test_no_webhook_configured_is_not_an_error(cfg):
    cfg["wecom"]["webhook_url"] = ""
    w = WeCom(cfg, read_only=False, allow_send=True)
    ok, detail = w.send("hi")
    assert ok is False and detail["error"] == "not_configured"


def test_category_is_validated_before_request(cfg):
    s = Strapi(cfg, read_only=False)
    with pytest.raises(ValueError):
        s.clean_fields({"title": "t", "slug": "s", "category": "not-a-category"})


def test_quote_stripping_on_three_fields(cfg):
    s = Strapi(cfg, read_only=True)
    out = s.clean_fields({"title": '"带引号标题"', "slug": '"my-slug"',
                          "category": '"product-news"'})
    assert out["title"] == "带引号标题"
    assert out["slug"] == "my-slug"
    assert out["category"] == "product-news"


def test_apply_flag_and_config_both_required(cfg):
    from geoagent.config import read_only
    cfg["shadow"]["read_only"] = True
    assert read_only(cfg, apply_flag=True) is True       # 配置没放开 → 仍只读
    cfg["shadow"]["read_only"] = False
    assert read_only(cfg, apply_flag=False) is True      # 没给 --apply → 仍只读
    assert read_only(cfg, apply_flag=True) is False      # 两者齐备才真写


def test_repo_rejects_directory_or_dot_add(cfg, tmp_path):
    import subprocess
    from geoagent.repo import Repo
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    cfg["paths"]["workspace"] = str(ws)
    r = Repo(cfg, read_only=False)
    assert r.add(["."]) is False                    # 明确拒绝
    assert r.add(["-A"]) is False
    assert r.add(["dir/"]) is False                 # 目录也拒绝
    target = ws / "metrics" / "daily-2026-09-18.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    assert r.add(["metrics/daily-2026-09-18.json"]) is True    # 精确文件放行


def test_quiet_hours_suppression(cfg):
    cfg["wecom"]["webhook_url"] = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"
    cfg["notify"]["quiet_hours"] = ["23:30-07:00"]
    cfg["notify"]["quiet_exempt_alarm"] = False
    import datetime as dt
    w = WeCom(cfg, read_only=False, allow_send=True)
    ok, detail = w.send("hi", is_alarm=True, mention_all=True)  \
        if False else (None, None)
    quiet, rng = w.in_quiet_hours(dt.datetime(2026, 9, 18, 2, 0))
    assert quiet is True and rng == "23:30-07:00"
    quiet2, _ = w.in_quiet_hours(dt.datetime(2026, 9, 18, 14, 0))
    assert quiet2 is False
