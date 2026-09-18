"""管道 D 全图测试：用假客户端跑通写路径，同时验证几个硬规则。

覆盖：
  * 存量查重：slug 已存在 → 走 PUT（不重复 POST）
  * 三字段剥引号；category 非法直接失败而不发请求
  * 评论 ④ 必须落到 body 含该 slug 的工单（防错发到同批其他工单）
  * 回写 status: published 兼容带引号写法
  * git 只 add 本次文件（绝不 git add .）
"""
from __future__ import annotations

import shutil

from geoagent.graphs import publish as g
from geoagent.quality import quality_gate
from tests.fakes import FakeGitea, FakeRepo, FakeStrapi


def _article_text(slug, status="review", watermark="示例科技引擎", category="product-news"):
    """生成一篇能过【测试站点】门禁的文章。

    刻意不用真实站点文章的副本：测试站点的水印与品类都与真实站点不同，
    这样一旦代码里把品牌/品类写死，这里就会失败。
    """
    # 段落要足够长，正文重复多次后才能稳过 chars>=1500（按去空白字符计）
    unit = ("编译式智能体把模型能力压进确定性流程，使内网部署成为可能。"
            "%s 负责在边缘侧完成路由与调度，因此数据不需要出网即可完成推理。"
            "在实际部署中，团队通常先划定业务边界，再把高频且规则明确的任务固化为流程，"
            "最后才让模型处理需要判断的环节。这样做的好处是每一步都可观测、可回滚，"
            "也便于把失败原因定位到具体节点，而不是笼统地归因于模型。" % watermark)
    para = (unit * 2)
    rows = "".join("<tr><td>指标%d</td><td>数值%d</td></tr>" % (i, i) for i in range(1, 4))
    faq = "".join("### Q%d: 第%d 个常见问题是什么？\n\nA%d：这是回答内容。\n\n"
                  % (i, i, i) for i in range(1, 5))
    body = f"""
<p>{para}</p>

## 为什么选择内网部署

{para}

<strong>编译式智能体</strong>与<strong>{watermark}</strong>是核心关键词。{para}

## 实施路径

{para}

{para}

## 效果对比

<table><thead><tr><th>维度</th><th>方案A</th></tr></thead><tbody>{rows}</tbody></table>

## 风险与应对

{para}

{para}

<table><thead><tr><th>风险</th><th>措施</th></tr></thead><tbody>{rows}</tbody></table>

*本文数据更新至 2026-09-18。*

<section data-faq>
{faq}
</section>
"""
    return ("---\ntitle: 测试文章 %s\nslug: %s\ncategory: %s\nstatus: %s\n---\n\n"
            % (slug, slug, category, status)) + body


def _make_workspace(tmp_path, real_article=None, status="review", slug="santi-ai-agent-20260918"):
    ws = tmp_path / "workspace"
    (ws / "output" / "articles").mkdir(parents=True, exist_ok=True)
    target = ws / "output" / "articles" / ("%s.md" % slug)
    target.write_text(_article_text(slug, status=status), encoding="utf-8")
    return ws, target


def test_full_publish_flow_new_article(cfg, tmp_path, site, real_article=None):
    ws, art = _make_workspace(tmp_path)
    slug = art.stem  # santi-ai-agent-20260918
    repo = FakeRepo(ws)
    gitea = FakeGitea(issues=[{"number": 501, "title": "[article-generate] x", "state": "open",
                              "body": "输出到 output/articles/%s.md" % slug,
                              "labels": [{"name": "article-generate"}],
                              "assignees": [{"login": "YiChen"}]}])
    strapi = FakeStrapi(existing={}, site=site, cfg=cfg)          # 新文章
    graph = g.build(cfg, repo, gitea, strapi, read_only=True, site=site)

    final = graph.invoke({"pipeline": "publish"}, config={"configurable": {"thread_id": "t1"}})

    assert len(final["results"]) == 1
    r = final["results"][0]
    assert r["quality"]["pass"] is True
    assert r["action"] == "create"
    assert r["published"] is True
    assert len(strapi.created) == 1 and not strapi.updated
    # ④ 评论 + 关闭
    assert gitea.commented and "④ 发布成功" in gitea.commented[0][1]
    assert gitea.commented[0][0] == 501
    assert gitea.closed == [501]
    # status 回写
    assert "status: published" in art.read_text(encoding="utf-8")


def test_existing_slug_uses_update_not_create(cfg, tmp_path, site, real_article=None):
    ws, art = _make_workspace(tmp_path)
    repo, gitea = FakeRepo(ws), FakeGitea()
    strapi = FakeStrapi(existing={art.stem: [{"documentId": "doc-existing"}]}, site=site, cfg=cfg)
    graph = g.build(cfg, repo, gitea, strapi, read_only=True, site=site)
    final = graph.invoke({"pipeline": "publish"}, config={"configurable": {"thread_id": "t2"}})

    assert final["results"][0]["action"] == "update"
    assert strapi.updated and strapi.updated[0][0] == "doc-existing"
    assert not strapi.created


def test_comment_targets_issue_whose_body_has_slug(cfg, tmp_path, site, real_article=None):
    """同批多张工单时，④ 只能发给 body 含该 slug 的那张。"""
    ws, art = _make_workspace(tmp_path)
    repo = FakeRepo(ws)
    gitea = FakeGitea(issues=[
        {"number": 601, "title": "同批-其他篇", "state": "open",
         "body": "输出到 output/articles/other-slug.md",
         "labels": [{"name": "article-generate"}], "assignees": []},
        {"number": 602, "title": "同批-本篇", "state": "open",
         "body": "输出到 output/articles/%s.md" % art.stem,
         "labels": [{"name": "article-generate"}], "assignees": [{"login": "Ruoxi"}]},
    ])
    strapi = FakeStrapi(existing={}, site=site, cfg=cfg)
    final = g.build(cfg, repo, gitea, strapi, read_only=True, site=site).invoke(
        {"pipeline": "publish"}, config={"configurable": {"thread_id": "t3"}})
    assert [c[0] for c in gitea.commented] == [602]
    assert gitea.closed == [602]
    assert final["results"][0]["issue"] == 602


def test_quoted_status_is_scanned_and_rewritten(cfg, tmp_path, site, real_article=None):
    ws, art = _make_workspace(tmp_path, status='"review"')
    assert 'status: "review"' in art.read_text(encoding="utf-8")
    repo, gitea = FakeRepo(ws), FakeGitea()
    strapi = FakeStrapi(existing={}, site=site, cfg=cfg)
    final = g.build(cfg, repo, gitea, strapi, read_only=True, site=site).invoke(
        {"pipeline": "publish"}, config={"configurable": {"thread_id": "t4"}})
    assert len(final["results"]) == 1                     # 带引号也能被扫到
    assert "status: published" in art.read_text(encoding="utf-8")
    assert '"review"' not in art.read_text(encoding="utf-8")


def test_bad_article_goes_needs_revision_and_no_publish(cfg, tmp_path, site):
    ws = tmp_path / "workspace"
    (ws / "output" / "articles").mkdir(parents=True)
    (ws / "output" / "articles" / "bad-slug.md").write_text(
        "---\ntitle: 太短\nslug: bad-slug\ncategory: company\nstatus: review\n---\n\n就一句话。",
        encoding="utf-8")
    repo = FakeRepo(ws)
    gitea = FakeGitea(issues=[{"number": 701, "title": "bad", "state": "open",
                               "body": "output/articles/bad-slug.md",
                               "labels": [{"name": "article-generate"}], "assignees": []}])
    strapi = FakeStrapi(site=site)
    final = g.build(cfg, repo, gitea, strapi, read_only=True, site=site).invoke(
        {"pipeline": "publish"}, config={"configurable": {"thread_id": "t5"}})

    r = final["results"][0]
    assert r["published"] is False and r["action"] == "needs-revision"
    assert "chars>=1500" in r["quality"]["violations"]
    assert not strapi.created
    assert "质量检查未通过" in gitea.commented[0][1]
    assert gitea.closed == []
    assert "status: published" not in (ws / "output" / "articles" / "bad-slug.md").read_text()


def test_git_add_only_this_run_files(cfg, tmp_path, site, real_article=None):
    """回归：绝不允许 git add . —— 历史上它把共享 clone 的跟踪文件标记成 D。"""
    ws, art = _make_workspace(tmp_path)
    repo = FakeRepo(ws)
    gitea = FakeGitea(issues=[])
    strapi = FakeStrapi(existing={}, site=site, cfg=cfg)
    g.build(cfg, repo, gitea, strapi, read_only=True, site=site).invoke(
        {"pipeline": "publish"}, config={"configurable": {"thread_id": "t6"}})
    adds = [c for c in repo.calls if c[0] in ("add", "commit_files")]
    assert adds, "应该有提交动作"
    for c in adds:
        for p in c[1]:
            assert p != "." and not p.endswith("/") and "*" not in p


def test_real_article_passes_official_gate(real_article):
    q = quality_gate(real_article)
    assert q["pass"] is True, q["violations"]
    assert q["details"]["total_chars"] >= 1500
