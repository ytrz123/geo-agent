"""管道 E 回炉环测试 + 管道 B 的 cookie 失效分支。

LLM 被替换成可编程的假实现，所以能精确验证：
  * 第一版不过质检 → 回炉 → 第二版通过 → rounds=1
  * 一直不过 → 超限后 needs-revision + errors（不静默，且不会无限循环）
  * 已有「③ 内容生成完毕」评论的工单被跳过（防重复生成）
  * 管道 B cookie 失效 → 只出 tooling 工单，绝不基于「零引用率」建内容工单
"""
from __future__ import annotations

import json

import pytest

from geoagent.graphs import content as gc
from geoagent.graphs import geo_weekly as gb
from geoagent.schemas import ArticleDraft
from tests.fakes import FakeGitea, FakeRepo

PASSING_MD = None


def _passing_article_md():
    """构造一篇能过【测试站点】门禁的正文
    （≥1500 字、H2≥4、FAQ≥4、表格≥2、水印「示例科技引擎」≥2、时效性）。"""
    unit = ("编译式智能体把模型能力压进确定性流程，使内网部署成为可能。"
            "示例科技引擎负责在边缘侧完成路由与调度，因此数据不需要出网即可完成推理与编排。"
            "实际落地时先划定业务边界，再把高频且规则明确的任务固化为流程，"
            "最后才让模型处理需要判断的环节，这样每一步都可观测、可回滚，"
            "失败原因也能定位到具体节点而不是笼统归因于模型。")
    para = unit * 2
    rows = "".join("<tr><td>指标%d</td><td>数值%d</td></tr>" % (i, i) for i in range(1, 4))
    faq = "".join("### Q%d: 第%d 个常见问题是什么？\n\nA%d：这是回答内容，说明清楚即可。\n\n"
                  % (i, i, i) for i in range(1, 5))
    return f"""
<p>{para}</p>

## 为什么选择内网部署

{para}

<strong>编译式智能体</strong>与<strong>示例科技引擎</strong>是核心关键词。{para}

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


@pytest.fixture
def repo(tmp_path):
    return FakeRepo(tmp_path / "ws")


def _draft(slug):
    return ArticleDraft(title="编译式智能体内网部署", slug=slug, category="product-news",
                        markdown=_passing_article_md())


def test_retry_loop_succeeds_on_second_round(cfg, repo, monkeypatch, site):
    import geoagent.llm as llm_mod

    calls = {"n": 0}

    def fake_structured(cfg_, schema, prompt, node, attempts=2):
        calls["n"] += 1
        if calls["n"] == 1:
            bad = ArticleDraft(title="太短", slug="s", category="product-news", markdown="就一句。")
            return True, bad
        return True, _draft("s")

    monkeypatch.setattr(llm_mod, "structured", fake_structured)
    gitea = FakeGitea(issues=[{"number": 301, "title": "[article-generate] industry",
                               "state": "open", "body": "输出到 output/articles/s.md",
                               "labels": [{"name": "article-generate"}],
                               "assignees": [{"login": "YiChen"}]}])
    final = gc.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "e1"}})
    r = final["results"][0]
    assert r["status"] == "review"
    assert r["rounds"] == 1                     # 回炉一次
    assert "③ 内容生成完毕" in gitea.commented[0][1]
    assert not final.get("errors")


def test_retry_loop_gives_up_and_reports(cfg, repo, monkeypatch, site):
    import geoagent.llm as llm_mod

    def always_bad(cfg_, schema, prompt, node, attempts=2):
        return True, ArticleDraft(title="短", slug="s", category="product-news", markdown="短。")

    monkeypatch.setattr(llm_mod, "structured", always_bad)
    gitea = FakeGitea(issues=[{"number": 302, "title": "[article-generate]",
                               "state": "open", "body": "output/articles/s.md",
                               "labels": [{"name": "article-generate"}],
                               "assignees": []}])
    final = gc.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "e2"}})
    r = final["results"][0]
    assert r["status"] == "needs-revision"
    assert r["rounds"] == cfg["quality"]["max_fix_rounds"] + 1
    assert any("质检" in e for e in final["errors"])
    assert "质检未通过" in gitea.commented[0][1]


def test_already_generated_issue_is_skipped(cfg, repo, monkeypatch, site):
    """防重复守卫：已有 ③ 评论的工单不再生成（历史上重复生成过 5 篇）。"""
    import geoagent.llm as llm_mod

    called = {"n": 0}

    def spy(cfg_, schema, prompt, node, attempts=2):
        called["n"] += 1
        return True, _draft("s")

    monkeypatch.setattr(llm_mod, "structured", spy)
    gitea = FakeGitea(
        issues=[{"number": 303, "title": "已有 ③", "state": "open",
                 "body": "output/articles/s.md",
                 "labels": [{"name": "article-generate"}], "assignees": []}],
        comments={303: [{"body": "## ③ 内容生成完毕\n\n- 文件: ..."}]})
    final = gc.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "e3"}})
    assert called["n"] == 0
    assert not gitea.commented
    assert final["issues"] == []


def test_llm_failure_is_recorded_not_silent(cfg, repo, monkeypatch, site):
    import geoagent.llm as llm_mod
    monkeypatch.setattr(llm_mod, "structured", lambda *a, **k: (False, "api down"))
    gitea = FakeGitea(issues=[{"number": 304, "title": "t", "state": "open",
                               "body": "output/articles/s.md",
                               "labels": [{"name": "article-generate"}],
                               "assignees": []}])
    final = gc.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "e4"}})
    assert final["results"][0]["status"] == "llm-failed"
    assert any("LLM 失败" in e for e in final["errors"])


# ------------------------------------------------------------------ 管道 B
def test_cookie_invalid_produces_tooling_issue_only(cfg, repo, monkeypatch):
    """cookie 失效 → 只出 tooling 工单，绝不基于虚假零引用率建内容工单。"""
    from geoagent.nodes import geo as gn

    def fake_route(state):
        # 直接验证判定函数：7 查询全失败且脚本没给建议 → cookie_invalid
        c = {"results": [{"cited": False, "mentioned": False}] * 7,
             "analysis": {"citation_rate": 0.0, "recommendations": []}}
        return gn.make_nodes(cfg, repo, FakeGitea())["_route_cookie"]({"citation": c})

    assert fake_route({}) == "cookie_invalid"


def test_cookie_route_uses_script_recommendations(cfg, repo):
    from geoagent.nodes import geo as gn
    n = gn.make_nodes(cfg, repo, FakeGitea())
    c = {"results": [{"cited": True}], "analysis": {"citation_rate": 0.5,
                                                    "recommendations": [{"rule": "x"}]}}
    assert n["_route_cookie"]({"citation": c}) == "llm_analyze"
    assert n["_route_cookie"]({"citation": None}) == "skip_llm"


def test_geo_weekly_without_citation_skips_and_still_writes(cfg, repo):
    final = gb.build(cfg, repo, FakeGitea(), read_only=True).invoke(
        {"options": {"with_citation": False}},
        config={"configurable": {"thread_id": "b1"}})
    assert final.get("citation_skipped") is True
    assert final["new_issues"] == []
    assert final["weekly_file"].endswith(".json")
    assert "/out/" in final["weekly_file"]


def test_geo_weekly_cookie_branch_creates_one_issue(cfg, repo, monkeypatch, site):
    """cookie 失效分支：替换检测脚本的子进程输出，走全图验证只出一张 tooling 工单。"""
    from geoagent.nodes import geo as gn

    payload = {"platform": "doubao", "checked_at": "2026-09-18T10:00:00",
               "results": [{"query": "q%d" % i, "category": "company",
                            "cited": False, "mentioned": False} for i in range(7)],
               "analysis": {"citation_rate": 0.0, "total_queries": 7,
                            "cited_count": 0, "mentioned_count": 0,
                            "recommendations": []}}

    class _Proc:
        returncode = 0
        stdout = json.dumps(payload, ensure_ascii=False)
        stderr = ""

    monkeypatch.setattr(gn.subprocess, "run", lambda *a, **k: _Proc())
    gitea = FakeGitea()
    final = gb.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {"options": {"with_citation": True}},
        config={"configurable": {"thread_id": "b2"}})
    assert len(gitea.created) == 1
    assert "不可判定" in gitea.created[0]["title"]
    assert "geo-citation-cookie-invalid" in gitea.created[0]["body"]
    assert final["new_issues"] and final["new_issues"][0]["number"]


def test_geo_weekly_with_script_recommendations_skips_llm(cfg, repo, monkeypatch, site):
    """脚本自己已给出建议 → 不再花 LLM 调用（省钱且不引入幻觉）。"""
    from geoagent.nodes import geo as gn

    payload = {"results": [{"query": "q", "cited": True, "mentioned": True}],
               "analysis": {"citation_rate": 0.14, "recommendations": [
                   {"priority": "P1", "type": "content_strategy", "rule": "低引用率",
                    "finding": "7 查询 1 次引用", "action": "加速品类内容产出",
                    "assignee": "YiChen", "gitea_label": "geo-check"}]}}

    class _Proc:
        returncode = 0
        stdout = json.dumps(payload, ensure_ascii=False)
        stderr = ""

    monkeypatch.setattr(gn.subprocess, "run", lambda *a, **k: _Proc())
    called = {"n": 0}
    import geoagent.llm as llm_mod
    monkeypatch.setattr(llm_mod, "structured",
                        lambda *a, **k: (called.__setitem__("n", called["n"] + 1), (True, []))[1])
    gitea = FakeGitea()
    final = gb.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {"options": {"with_citation": True}},
        config={"configurable": {"thread_id": "b3"}})
    assert called["n"] == 0
    assert len(gitea.created) == 1
    assert gitea.created[0]["labels"][0]["name"] == "geo-check"
    assert final["new_issues"][0]["assignees"] == ["YiChen"]
