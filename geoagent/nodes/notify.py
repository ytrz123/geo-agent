"""通知节点（每条管道的统一终点）。

只推值得推的 —— 这条决定了通知会不会被嫌吵：
  推：新建工单 / 评论更新（可关）/ 发布成败 / errors（当告警，@所有人）
  不推：无异常的静默日、老工单的日常评论更新（默认关）
"""
from __future__ import annotations

from .. import obs, site as site_mod
from ..clients.wecom import build_run_summary
from ..config import get


def _issues_url(cfg) -> str:
    """工单系统入口地址（从配置拼，不写死）。"""
    base = (get(cfg, "gitea.base_url") or "").rstrip("/")
    repo = get(cfg, "gitea.repo") or ""
    if base and repo:
        return "%s/%s/issues" % (base, repo)
    return ""


def make_node(cfg, wecom, site: dict | None = None):
    site = site if site is not None else (cfg.get("_site") or {})
    site_name = site_mod.get(site, "site.name", "")
    issues_url = _issues_url(cfg)

    def notify(state):
        pipeline = state.get("pipeline", "geo-agent")
        new = state.get("new_issues") or []
        updated = state.get("updated_issues") or []
        published = state.get("published") or []
        errors = state.get("errors") or []

        push_new = bool(get(cfg, "notify.push_new_issues", True))
        push_upd = bool(get(cfg, "notify.push_issue_updates", False))
        push_pub = bool(get(cfg, "notify.push_publish", True))

        lines, facts = [], {}
        if push_new and new:
            for i in new:
                num = i.get("number")
                tag = "#%s" % num if num else "(shadow)"
                skipped = " · 影子期未真建" if i.get("skipped") else ""
                lines.append("- 🆕 [%s] %s → @%s%s"
                             % (tag, i.get("title"),
                                ",".join(i.get("assignees") or []) or "-", skipped))
            facts["新建"] = len(new)
        if push_upd and updated:
            lines.append("- ♻️ 评论更新 %d 张既有工单：%s"
                         % (len(updated), ", ".join("#%s" % u for u in updated)))
        if push_pub and published:
            for s in published:
                lines.append("- ✅ 已发布 `%s`" % s)
            facts["发布"] = len(published)

        m = state.get("metrics") or {}
        if m:
            facts.setdefault("覆盖率", "%s%%" % m.get("coverage_pct")
                             if m.get("coverage_pct") is not None else "UNKNOWN")

        if not lines and not errors:
            obs.log("notify_skipped_silent", reason="无新建工单/无发布/无异常")
            return {}

        stage = "alarm" if errors else "ok"
        if errors:
            for e in errors[:6]:
                lines.append("- 🔴 %s" % e)
            facts["错误"] = len(errors)

        msg = build_run_summary(pipeline, stage, lines, facts,
                                site_name=site_name, issues_url=issues_url)
        ok, detail = wecom.send(msg, mention_all=bool(errors), is_alarm=bool(errors))
        obs.node_done("notify", ok=ok, detail=str(detail)[:120])
        return {"notified": ok, "notify_detail": str(detail), "message": msg}

    return notify
