"""管道 E 图：内容生成（1 LLM + 质检回炉环）。

    START → pick_issue ─Send(每张工单)→ write_one  ┐
                        └Send(空)→ noop            ├→ comment_and_push → notify → END

write_one 内部是回炉环：撰写 → 质检 → FAIL → 整篇重写（≤max_fix_rounds）→ 再检。
超限不静默：评论 needs-revision + errors[] 进通知（当告警 @所有人）。
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..clients.wecom import WeCom
from ..nodes.content import make_nodes
from ..nodes.notify import make_node as make_notify


class ContentState(TypedDict, total=False):
    issues: list
    issue: dict            # Send 入参
    results: Annotated[list, operator.add]
    generated: Annotated[list, operator.add]
    new_issues: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]
    pipeline: str


def build(cfg, repo, gitea, read_only=True, checkpointer=None, site=None, knowledge=None):
    site = site if site is not None else (cfg.get("_site") or {})
    n = make_nodes(cfg, repo, gitea, site=site, knowledge=knowledge)
    notify = make_notify(cfg, WeCom(cfg, read_only=read_only,
                                    allow_send=cfg.get("_allow_notify", False)), site=site)

    b = StateGraph(ContentState)
    b.add_node("bootstrap", lambda s: {"bootstrapped": repo.bootstrap()})
    b.add_node("pick_issue", n["pick_issue"])
    b.add_node("write_one", n["write_one"])
    b.add_node("noop", n["noop"])
    b.add_node("comment_and_push", n["comment_and_push"])
    b.add_node("notify", notify)

    b.add_edge(START, "bootstrap")
    b.add_edge("bootstrap", "pick_issue")
    b.add_conditional_edges("pick_issue", n["route_issues"], ["write_one", "noop"])
    b.add_edge("write_one", "comment_and_push")
    b.add_edge("noop", "comment_and_push")
    b.add_edge("comment_and_push", "notify")
    b.add_edge("notify", END)
    return b.compile(checkpointer=checkpointer)
