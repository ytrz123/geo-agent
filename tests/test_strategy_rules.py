"""管道 C 规则引擎测试：封顶、不可得指标不虚构、批次守卫、工单去重。"""
from __future__ import annotations

import datetime as dt
import json

from geoagent.graphs import strategy as g
from tests.fakes import FakeGitea, FakeRepo

RULES = """
rules:
  - id: "deepseek-preference-tech"
    trigger: {metric: "deepseek_citations_monthly", operator: "<", threshold: 2}
    action: {type: "adjust_category_weight", target: "strategy/keywords.yaml",
             change: "industry +20%", max: 1.50}
    priority: P1
  - id: "sitemap-coverage-drop"
    trigger: {metric: "sitemap_coverage", operator: "<", threshold: 0.90}
    action: {type: "create_issue", label: "seo-monitor", assignee: "YiChen",
             template: "覆盖率 {value}%"}
    priority: P0
"""

KEYWORDS = """
category_weights:
  industry: 1.50   # 已封顶1.50
  company: 1.0
"""


def _write_daily(repo, day, coverage, strapi=101, sitemap=212):
    p = repo.dir / "metrics" / ("daily-%s.json" % day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": day, "sitemap_urls": sitemap,
                             "strapi_articles": strapi, "coverage": coverage,
                             "static_pages": 12}), encoding="utf-8")


def _window(tmp_path, coverage=0.95, days=28):
    repo = FakeRepo(tmp_path / "ws")
    today = dt.date.today()
    for i in range(days):
        d = (today - dt.timedelta(days=i)).isoformat()
        _write_daily(repo, d, coverage)
    return repo


def test_weight_rule_is_capped_not_inflated(cfg, tmp_path, site):
    repo = _window(tmp_path, coverage=0.95)
    gitea = FakeGitea(files={"strategy/evolution-rules.yaml": RULES,
                             "strategy/keywords.yaml": KEYWORDS})
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "c1"}})
    # deepseek 指标不可得 → 该规则不触发（不虚构）
    ids = [t["id"] for t in final["triggered_rules"]]
    assert "deepseek-preference-tech" not in ids
    assert any(s["id"] == "deepseek-preference-tech" for s in final["skipped_rules"])
    assert "deepseek_citations_monthly" in final["rule_metrics_unavailable"]


def test_high_citation_rule_not_triggered_when_metric_missing(cfg, tmp_path, site):
    repo = _window(tmp_path, coverage=0.30)     # 覆盖率低 → 该规则应触发
    gitea = FakeGitea(files={"strategy/evolution-rules.yaml": RULES,
                            "strategy/keywords.yaml": KEYWORDS})
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "c2"}})
    ids = [t["id"] for t in final["triggered_rules"]]
    assert "sitemap-coverage-drop" in ids


def test_batch_guard_blocks_creating_new_issues(cfg, tmp_path, site):
    repo = _window(tmp_path)
    gitea = FakeGitea(
        files={"strategy/evolution-rules.yaml": RULES, "strategy/keywords.yaml": KEYWORDS},
        issues=[{"number": 107, "title": "[article-generate] industry", "state": "open",
                 "body": "still open", "labels": [{"name": "article-generate"}],
                 "assignees": [{"login": "YiChen"}]}])
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "c3"}})
    assert final["batch_blocked"] is True
    assert not gitea.created, "上一批仍 open 时不许新建批次"
    assert [c[0] for c in gitea.commented] == [107]


def test_no_llm_run_still_produces_metrics_and_notes(cfg, tmp_path, monkeypatch, site):
    """没有 LLM（无 key / --no-llm）时，图仍要跑完并落盘，errors 里说明原因。"""
    import geoagent.llm as llm_mod
    monkeypatch.setattr(llm_mod, "available", lambda: False)
    repo = _window(tmp_path)
    gitea = FakeGitea(files={"strategy/evolution-rules.yaml": RULES,
                            "strategy/keywords.yaml": KEYWORDS})
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "c4"}})
    assert final.get("week_review") is None
    assert any("周报 LLM 失败" in e for e in final["errors"])
    assert final["weekly_file"].endswith("-strategy.json")
    assert "/out/" in final["weekly_file"]


def test_missing_days_reported_not_treated_as_zero(cfg, tmp_path, site):
    repo = FakeRepo(tmp_path / "ws")
    today = dt.date.today()
    # 只写 3 天，其中一天 coverage=None
    _write_daily(repo, today.isoformat(), 1.0)
    _write_daily(repo, (today - dt.timedelta(days=1)).isoformat(), 1.0)
    p = repo.dir / "metrics" / ("daily-%s.json" % (today - dt.timedelta(days=2)).isoformat())
    p.write_text(json.dumps({"date": "x", "coverage": None, "sitemap_urls": 4,
                             "strapi_articles": None}), encoding="utf-8")
    gitea = FakeGitea(files={"strategy/evolution-rules.yaml": RULES,
                            "strategy/keywords.yaml": KEYWORDS})
    final = g.build(cfg, repo, gitea, read_only=True, site=site).invoke(
        {}, config={"configurable": {"thread_id": "c5"}})
    w = final["window"]
    assert w["days_present"] == 3
    assert len(w["missing_days"]) == 25
    assert w["coverage"]["days_unavailable"] == 1
    assert w["coverage"]["mean_coverage"] == 1.0     # None 被剔除，不是 0
