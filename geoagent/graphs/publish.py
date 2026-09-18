"""管道 D 图：定时发布（0 LLM）。

    START → sync_repo → scan ─Send(每篇)→ process_article ┐
                                └Send(空)→ noop           ├→ comment_and_close
                                                          → writeback_status → git_commit_push
                                                          → notify → END

LangGraph 的扇入屏障：process_article 的所有实例跑完后 comment_and_close 才跑一次，
所以按 slug 找工单、评论 ④、关闭、回写都只做一遍。
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..clients.wecom import WeCom
from ..nodes.notify import make_node as make_notify
from ..nodes.publish import make_nodes


class PublishState(TypedDict, total=False):
    repo_head: str
    workspace: str
    articles: list
    article: dict          # Send 的入参
    results: Annotated[list, operator.add]
    new_issues: Annotated[list, operator.add]
    published: Annotated[list, operator.add]
    status_changed: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]
    git: dict
    aborted: bool
    pipeline: str


def build(cfg, repo, gitea, cms, read_only=True, checkpointer=None, site=None, knowledge=None):
    site = site if site is not None else (cfg.get("_site") or {})
    n = make_nodes(cfg, repo, gitea, cms, read_only=read_only, site=site, knowledge=knowledge)
    notify = make_notify(cfg, WeCom(cfg, read_only=read_only,
                                    allow_send=cfg.get("_allow_notify", False)), site=site)

    def route_sync(state):
        return "abort" if state.get("aborted") else "scan"

    def fan_out(state):
        arts = state.get("articles") or []
        if not arts:
            return [Send("noop", {})]
        return [Send("process_article", {"article": a}) for a in arts]

    b = StateGraph(PublishState)
    b.add_node("sync_repo", n["sync_repo"])
    b.add_node("scan", n["scan"])
    b.add_node("process_article", n["process_article"])
    b.add_node("noop", n["noop"])
    b.add_node("comment_and_close", n["comment_and_close"])
    b.add_node("writeback_status", n["writeback_status"])
    b.add_node("git_commit_push", n["git_commit_push"])
    b.add_node("notify", notify)
    b.add_node("abort", lambda s: {"errors": ["aborted before scan"]})

    b.add_edge(START, "sync_repo")
    b.add_conditional_edges("sync_repo", route_sync, {"scan": "scan", "abort": "abort"})
    b.add_conditional_edges("scan", fan_out, ["process_article", "noop"])
    b.add_edge("process_article", "comment_and_close")
    b.add_edge("noop", "comment_and_close")
    b.add_edge("comment_and_close", "writeback_status")
    b.add_edge("writeback_status", "git_commit_push")
    b.add_edge("git_commit_push", "notify")
    b.add_edge("abort", END)
    b.add_edge("notify", END)
    return b.compile(checkpointer=checkpointer)
