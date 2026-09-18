"""管道 B 图：GEO 每周引用检测（1 LLM 节点）。

    START → fetch_crawlers → run_citation_check ─┬ cookie_invalid → upsert_issues
                                                 ├ llm_analyze    → upsert_issues
                                                 └ skip_llm       → upsert_issues
                                                 → write_weekly → notify → END

cookie 失效走独立分支：只产出 P0 tooling 工单，绝不基于虚假「零引用率」建内容工单。
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from ..clients.wecom import WeCom
from ..nodes.geo import make_nodes
from ..nodes.notify import make_node as make_notify


class GeoWeeklyState(TypedDict, total=False):
    crawler_stats: dict | None
    citation: dict | None
    citation_skipped: bool
    citation_stderr_tail: str
    recommendations: list
    anomalies: list
    notes: Annotated[list, operator.add]
    new_issues: Annotated[list, operator.add]
    updated_issues: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]
    weekly_file: str
    weekly_rel: str
    options: dict
    pipeline: str


def build(cfg, repo, gitea, ssh_runner=None, conda_python=None, read_only=True,
          checkpointer=None, site=None, knowledge=None):
    site = site if site is not None else (cfg.get("_site") or {})
    n = make_nodes(cfg, repo, gitea, conda_python=conda_python, ssh_runner=ssh_runner,
                   site=site, knowledge=knowledge)
    notify = make_notify(cfg, WeCom(cfg, read_only=read_only,
                                    allow_send=cfg.get("_allow_notify", False)), site=site)

    b = StateGraph(GeoWeeklyState)
    b.add_node("bootstrap", lambda s: {"bootstrapped": repo.bootstrap()})
    b.add_node("fetch_crawlers", n["fetch_crawlers"])
    b.add_node("run_citation_check", n["run_citation_check"])
    b.add_node("cookie_invalid", n["cookie_invalid"])
    b.add_node("llm_analyze", n["llm_analyze"])
    b.add_node("skip_llm", n["skip_llm"])
    b.add_node("upsert_issues", n["upsert_issues"])
    b.add_node("write_weekly", n["write_weekly"])
    b.add_node("notify", notify)

    b.add_edge(START, "bootstrap")
    b.add_edge("bootstrap", "fetch_crawlers")
    b.add_edge("fetch_crawlers", "run_citation_check")
    b.add_conditional_edges("run_citation_check", n["_route_cookie"],
                            {"cookie_invalid": "cookie_invalid",
                             "llm_analyze": "llm_analyze",
                             "skip_llm": "skip_llm"})
    for src in ("cookie_invalid", "llm_analyze", "skip_llm"):
        b.add_edge(src, "upsert_issues")
    b.add_edge("upsert_issues", "write_weekly")
    b.add_edge("write_weekly", "notify")
    b.add_edge("notify", END)
    return b.compile(checkpointer=checkpointer)
