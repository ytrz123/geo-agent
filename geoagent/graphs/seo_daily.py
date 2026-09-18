"""管道 A 图：SEO 每日健康检查（0 LLM）。

    START → fetch_sitemap ┐
            fetch_robots  │
            fetch_jsonld  ├→ compute_metrics → evaluate_rules → upsert_issues ┐
            fetch_articles│                                     skip_notify   ├→ write_daily
            fetch_crawlers┘                                                   → notify → END

五个抓取节点并发（从 START 各连一条边），compute_metrics 是扇入屏障。
阈值判定与覆盖率全部是纯函数，所以「覆盖率算成 198%」这类错在这版结构上不可能发生。
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from ..clients.wecom import WeCom
from ..nodes.notify import make_node as make_notify
from ..nodes.seo import make_nodes


class SeoDailyState(TypedDict, total=False):
    sitemap_urls: list
    robots: dict
    jsonld: dict
    strapi_articles: int | None
    crawler_stats: dict | None
    vitals: dict
    metrics: dict
    anomalies: list
    notes: Annotated[list, operator.add]
    new_issues: Annotated[list, operator.add]
    updated_issues: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]
    daily_file: str
    daily_rel: str
    daily_record: dict
    pipeline: str


FETCHERS = ["fetch_sitemap", "fetch_robots", "fetch_jsonld", "fetch_articles", "fetch_crawlers"]


def build(cfg, repo, gitea, ssh_runner=None, read_only=True, checkpointer=None, site=None):
    site = site if site is not None else (cfg.get("_site") or {})
    n = make_nodes(cfg, repo, gitea, ssh_runner=ssh_runner, site=site)
    notify = make_notify(cfg, WeCom(cfg, read_only=read_only,
                                    allow_send=cfg.get("_allow_notify", False)), site=site)

    def route_anomalies(state):
        return "upsert_issues" if (state.get("anomalies") or []) else "skip_notify"

    b = StateGraph(SeoDailyState)
    b.add_node("bootstrap", lambda s: {"bootstrapped": repo.bootstrap()})
    for name in FETCHERS:
        b.add_node(name, n[name])
    b.add_node("compute_metrics", n["compute_metrics"])
    b.add_node("evaluate_rules", n["evaluate_rules"])
    b.add_node("upsert_issues", n["upsert_issues"])
    b.add_node("skip_notify", n["skip_notify"])
    b.add_node("write_daily", n["write_daily"])
    b.add_node("notify", notify)

    b.add_edge(START, "bootstrap")
    for name in FETCHERS:
        b.add_edge("bootstrap", name)
        b.add_edge(name, "compute_metrics")
    b.add_edge("compute_metrics", "evaluate_rules")
    b.add_conditional_edges("evaluate_rules", route_anomalies,
                            {"upsert_issues": "upsert_issues", "skip_notify": "skip_notify"})
    b.add_edge("upsert_issues", "write_daily")
    b.add_edge("skip_notify", "write_daily")
    b.add_edge("write_daily", "notify")
    b.add_edge("notify", END)
    return b.compile(checkpointer=checkpointer)
