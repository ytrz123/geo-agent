"""管道 C 图：策略分析与自进化（2 LLM 节点）。

    START → load_window → load_rules → measure → check_rules
          → llm_week_review → llm_pick_topics → guard_open_batch
          → create_article_issues → apply_weight_changes → write_weekly_strategy → notify → END

规则触发、封顶判断、批次守卫都是纯逻辑；LLM 只出「周报结论」与「选题」两类文本产物。
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from ..clients.wecom import WeCom
from ..nodes.notify import make_node as make_notify
from ..nodes.strategy import make_nodes


class StrategyState(TypedDict, total=False):
    window: dict
    rules: list
    rules_meta: dict
    rule_metrics: dict
    rule_metrics_unavailable: list
    triggered_rules: list
    skipped_rules: list
    week_review: dict | None
    topics: list
    open_batch: list
    batch_blocked: bool
    weight_changes: list
    notes: Annotated[list, operator.add]
    new_issues: Annotated[list, operator.add]
    updated_issues: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]
    weekly_file: str
    weekly_rel: str
    pipeline: str


def build(cfg, repo, gitea, read_only=True, checkpointer=None, site=None, knowledge=None):
    site = site if site is not None else (cfg.get("_site") or {})
    n = make_nodes(cfg, repo, gitea, site=site, knowledge=knowledge)
    notify = make_notify(cfg, WeCom(cfg, read_only=read_only,
                                    allow_send=cfg.get("_allow_notify", False)), site=site)

    b = StateGraph(StrategyState)
    b.add_node("bootstrap", lambda s: {"bootstrapped": repo.bootstrap()})
    for name in ("load_window", "load_rules", "measure", "check_rules", "llm_week_review",
                 "llm_pick_topics", "guard_open_batch", "create_article_issues",
                 "apply_weight_changes", "write_weekly_strategy"):
        b.add_node(name, n[name])
    b.add_node("notify", notify)

    b.add_edge(START, "bootstrap")
    b.add_edge("bootstrap", "load_window")
    b.add_edge("load_window", "load_rules")
    b.add_edge("load_rules", "measure")
    b.add_edge("measure", "check_rules")
    b.add_edge("check_rules", "llm_week_review")
    b.add_edge("llm_week_review", "llm_pick_topics")
    b.add_edge("llm_pick_topics", "guard_open_batch")
    b.add_edge("guard_open_batch", "create_article_issues")
    b.add_edge("create_article_issues", "apply_weight_changes")
    b.add_edge("apply_weight_changes", "write_weekly_strategy")
    b.add_edge("write_weekly_strategy", "notify")
    b.add_edge("notify", END)
    return b.compile(checkpointer=checkpointer)
