"""管道 A 全图测试：抓取全部打桩，验证规则判定与工单去重。

关键断言：
  * 分母不可得（Strapi 500）时**不建任何工单**，也不写 0%
  * 覆盖率塌缩时建 seo-monitor 工单
  * 同一异常重复出现时评论更新已有工单，不重复建单
  * 抓取全失败时不静默：errors 进 state
"""
from __future__ import annotations

import json

import pytest

from geoagent.graphs import seo_daily as g
from geoagent.nodes import common
from tests.fakes import FakeGitea, FakeRepo


@pytest.fixture
def repo(tmp_path):
    return FakeRepo(tmp_path / "workspace")


def _stub(monkeypatch, sitemap=("ok", None), robots=("ok", None), jsonld=("ok", None),
          articles=("ok", None)):
    def fake_sitemap(base):
        if sitemap[0] != "ok":
            return False, {"error": "boom"}
        return True, sitemap[1]

    def fake_robots(base):
        if robots[0] != "ok":
            return False, {"error": "boom"}
        return True, robots[1] or "DoubaoBot Bytespider DeepSeekBot GPTBot Googlebot"

    def fake_jsonld(base):
        if jsonld[0] != "ok":
            return False, {"error": "boom"}
        return True, jsonld[1] or {"has_organization": True, "has_website": True,
                                   "occurrences": 2, "script_blocks": 0}

    def fake_articles(base):
        if articles[0] != "ok":
            return False, {"http": 500}
        return True, articles[1]

    monkeypatch.setattr(common, "sitemap_urls", fake_sitemap)
    monkeypatch.setattr(common, "robots_txt", fake_robots)
    monkeypatch.setattr(common, "jsonld_check", fake_jsonld)
    monkeypatch.setattr(common, "strapi_articles", fake_articles)


# ⚠️ 刻意用【测试站点】的路由 /articles（不是 /blog）——
# 一旦代码里把文章前缀写死，这些测试会立刻失败。
PREFIX = "/articles"


def _urls(articles=101, locales=2):
    urls = []
    for i in range(articles):
        for loc in ("", "en/"):
            if locales == 1 and loc:
                continue
            urls.append("https://test.example.com/%s%s/a-%03d" % (loc, PREFIX.strip("/"), i))
    for s in ("/", "/goods", PREFIX, "/about"):
        urls += ["https://test.example.com" + s, "https://test.example.com/en" + s]
    return urls


def test_healthy_day_creates_no_issue(cfg, monkeypatch, repo, site):
    _stub(monkeypatch, sitemap=("ok", _urls(101, 2)), articles=("ok", 101))
    gitea = FakeGitea()
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "s1"}})
    assert final["metrics"]["coverage"] == 1.0
    assert final["metrics"]["health"] == "OK"
    assert not gitea.created and not gitea.commented
    assert not final.get("errors")


def test_strapi_500_is_unknown_and_creates_nothing(cfg, monkeypatch, repo, site):
    """分母不可得 → UNKNOWN，不建工单，daily 里 coverage 必须是 None 而不是 0。"""
    _stub(monkeypatch, sitemap=("ok", _urls(101, 2)), articles=("fail", None))
    gitea = FakeGitea()
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "s2"}})
    assert final["metrics"]["health"] == "UNKNOWN"
    assert final["metrics"]["coverage"] is None
    assert not gitea.created
    assert final["daily_record"]["coverage"] is None
    assert any("articles-total-failed" in e for e in final["errors"])


def test_collapse_creates_seo_monitor_issue(cfg, monkeypatch, repo, site):
    static_only = ["https://legacy.example.com/", "https://legacy.example.com/products",
                   "https://legacy.example.com/blog", "https://legacy.example.com/about"]
    _stub(monkeypatch, sitemap=("ok", static_only), articles=("ok", 101))
    gitea = FakeGitea()
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "s3"}})
    assert final["metrics"]["health"] == "COLLAPSE"
    assert len(gitea.created) == 1
    issue = gitea.created[0]
    assert issue["title"].startswith("[seo-monitor]")
    assert "sitemap-coverage-drop" in issue["body"]
    assert issue["assignees"][0]["login"] == "Alice"   # 测试站点的默认负责人


def test_repeat_anomaly_comments_existing_issue_not_new(cfg, monkeypatch, repo, site):
    static_only = ["https://legacy.example.com/", "https://legacy.example.com/articles"]
    _stub(monkeypatch, sitemap=("ok", static_only), articles=("ok", 101))
    existing = [{"number": 85, "title": "[seo-monitor] sitemap 塌缩",
                 "state": "open", "body": "规则键: `sitemap-coverage-drop`",
                 "labels": [{"name": "seo-monitor"}], "assignees": []}]
    gitea = FakeGitea(issues=existing)
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "s4"}})
    assert not gitea.created
    assert [c[0] for c in gitea.commented] == [85]
    assert final["updated_issues"] == [85]


def test_missing_bots_and_jsonld_are_flagged(cfg, monkeypatch, repo, site):
    _stub(monkeypatch, sitemap=("ok", _urls(101, 2)), articles=("ok", 101),
          robots=("ok", "DoubaoBot only"), jsonld=("ok", {"has_organization": False,
                                                          "has_website": True}))
    gitea = FakeGitea()
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "s5"}})
    titles = " ".join(i["title"] for i in gitea.created)
    assert "robots.txt 缺少 Bytespider" in titles
    assert "缺少 Organization JSON-LD" in titles


def test_fetch_failures_surface_as_errors(cfg, monkeypatch, repo, site):
    _stub(monkeypatch, sitemap=("fail", None), articles=("fail", None))
    gitea = FakeGitea()
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "s6"}})
    joined = " ".join(final["errors"])
    assert "sitemap fetch failed" in joined
    assert "articles-total-failed" in joined


def test_daily_record_shape(cfg, monkeypatch, repo):
    _stub(monkeypatch, sitemap=("ok", _urls(101, 2)), articles=("ok", 101))
    final = g.build(cfg, repo, FakeGitea(), read_only=True).invoke(
        {}, config={"configurable": {"thread_id": "s7"}})
    rec = final["daily_record"]
    assert rec["static_pages"] == 8           # 来自测试站点配置（不是写死的 12）
    assert rec["coverage"] == 1.0
    assert rec["unique_article_slugs"] == 101
    assert rec["producer"].endswith("pipeline-A")
    # 影子期写 var/out 而不是 workspace（否则对拍没基线）
    assert "/out/" in final["daily_file"]
