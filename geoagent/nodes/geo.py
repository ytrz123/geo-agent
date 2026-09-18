"""管道 B 节点：GEO 每周引用检测（1 LLM 节点）。

LLM 只做一件事：把已经测出来的引用数据变成可执行建议（结构化输出）。
检测本身是脚本（tools/geo_citation_check.py），不经过 LLM。

通用性：
  * 查询词、等价域名、品牌别名、cookie 环境变量全部来自 site.yml
  * 检测脚本通过命令行参数接收这些配置（脚本自身保留向后兼容的默认值）
  * cookie 不再硬编码在脚本里

⚠️ cookie 失效时走「不虚构数据」分支 —— 历史踩坑：cookie 过期被误报成「7 查询全 0 引用」，
基于虚假零引用率建工单是最坏的失败模式。
"""
from __future__ import annotations

import json
import pathlib
import subprocess

from .. import llm as llm_mod
from .. import obs, schema_loader, site as site_mod
from ..config import get
from ..nodes import common

CITATION_TOOL = pathlib.Path(__file__).resolve().parent.parent / "tools" / "geo_citation_check.py"


def make_nodes(cfg, repo, gitea, conda_python=None, ssh_runner=None, site: dict | None = None,
               knowledge=None):
    site = site if site is not None else (cfg.get("_site") or {})
    conda_python = conda_python or get(cfg, "_extra.conda_python") or ""
    label_geo = site_mod.label(site, "geo_check", "geo-check")
    who = site_mod.default_assignee(site)
    models = schema_loader.build(site)
    queries = site_mod.get(site, "citation_check.queries", []) or []

    def fetch_crawlers(state):
        counts = common.crawler_log_counts(ssh_runner)
        if counts is None:
            obs.log("crawler_logs_skipped", level="warn")
        return {"crawler_stats": counts}

    def _queries_file() -> str:
        p = repo.out_path("tmp/citation_queries.json")
        p.write_text(json.dumps([{"q": q.get("q"), "category": q.get("category")}
                                 for q in queries], ensure_ascii=False), encoding="utf-8")
        return str(p)

    def run_citation_check(state):
        """跑检测脚本（Playwright + Chrome）。失败不抛，只记 errors。"""
        opts = state.get("options") or {}
        if not opts.get("with_citation"):
            obs.log("citation_check_skipped", level="warn",
                    reason="未加 --with-citation（影子期默认不跑浏览器）")
            return {"citation": None, "citation_skipped": True}
        if (site_mod.get(site, "citation_check.engine", "doubao") or "none") == "none":
            return {"citation": None, "citation_skipped": True,
                    "notes": ["site.yml 未启用引用检测引擎（engine=none）"]}
        if not queries:
            return {"citation": None, "citation_skipped": True,
                    "errors": ["site.yml 的 citation_check.queries 为空，无法检测"]}

        cmd = [conda_python or "python3", str(CITATION_TOOL),
               "--queries-file", _queries_file(),
               "--site-hosts", ",".join(site_mod.hosts(site)),
               "--brand-aliases", ",".join(site_mod.brand_aliases(site))]
        cookie_env = site_mod.resolve_env_name(site, "citation_check.cookie_env")
        if cookie_env:
            cmd += ["--cookie-env", cookie_env]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except Exception as e:  # noqa: BLE001
            return {"citation": None, "errors": ["citation check failed: %s" % e]}
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out[out.index("{"):]) if "{" in out else {}
        except json.JSONDecodeError as e:
            return {"citation": None,
                    "errors": ["citation json parse failed: %s" % e],
                    "citation_stderr_tail": (proc.stderr or "")[-300:]}
        # 后处理：脚本若按自己的默认值判定，这里用站点配置重判一次（有 html 时）
        for r in (data.get("results") or []):
            html = r.pop("_html", None) or r.pop("snippet", None)
            if html:
                r["cited"] = site_mod.is_cited(html, site)
                r["mentioned"] = site_mod.is_mentioned(html, site)
        obs.node_done("run_citation_check", keys=sorted(data.keys()))
        return {"citation": data}

    def route_cookie(state):
        c = state.get("citation")
        if c is None:
            return "skip_llm"
        analysis = c.get("analysis") or {}
        rate = analysis.get("citation_rate")
        results = c.get("results") or []
        all_failed = bool(results) and all(not r.get("cited") and not r.get("mentioned")
                                          for r in results)
        if c.get("cookie_invalid") or (rate in (0, 0.0) and all_failed
                                       and not analysis.get("recommendations")):
            return "cookie_invalid"
        return "llm_analyze"

    def cookie_invalid(state):
        """cookie 过期：报告不可判定 + P0 tooling 工单，绝不虚构零引用率。"""
        obs.log("citation_cookie_invalid", level="error")
        anomaly = {"label": label_geo, "assignee": who,
                   "title": "引用检测不可判定（sessionid cookie 失效）",
                   "key": "geo-citation-cookie-invalid",
                   "body": ("检测脚本报 cookie 失效 → 引用率不可判定。\n\n"
                            "**不要**基于「零引用率」建内容工单。\n"
                            "动作：刷新站点对应的 sessionid cookie 后重跑"
                            "（环境变量 %s）。"
                            % site_mod.resolve_env_name(site, "citation_check.cookie_env"))}
        return {"anomalies": [anomaly], "notes": ["cookie 失效，引用率不可判定"]}

    def skip_llm(state):
        return {"anomalies": [], "notes": ["引用检测未运行，跳过 LLM 建议生成"]}

    def llm_analyze(state):
        c = state.get("citation") or {}
        analysis = c.get("analysis") or {}
        recs_in = analysis.get("recommendations") or []
        if recs_in:
            # 脚本已内建建议（含 assignee/label）→ 不重复花 LLM 调用
            obs.log("recommendations_from_script", count=len(recs_in))
            return {"recommendations": recs_in}
        prompt = (
            "你是 %s 的 GEO 引用优化分析员。基于下面的实测检测结果，产出可执行的工单建议。\n"
            "硬约束：\n"
            "1. 只能基于给定数据，不得虚构任何数字；数据不足就给出 P2 的观测类建议。\n"
            "2. assignee 只能是 %s。\n"
            "3. gitea_label 从这些标签里选：%s。\n"
            "4. priority: 零引用=P0，命中率<20%%=P1，其他=P2。\n\n"
            "检测数据：\n%s"
            % (site_mod.get(site, "site.name"), "、".join(site_mod.assignees(site)),
               label_geo,
               json.dumps({"citation_rate": analysis.get("citation_rate"),
                           "total_queries": analysis.get("total_queries"),
                           "cited_count": analysis.get("cited_count"),
                           "results": c.get("results"),
                           "crawler_stats": state.get("crawler_stats")},
                          ensure_ascii=False))
        )
        ok, value = llm_mod.structured(cfg, models["Recommendation"], prompt,
                                       node="llm_analyze_citations")
        if not ok:
            return {"recommendations": [], "errors": ["LLM 建议生成失败: %s" % value]}
        return {"recommendations": [r.model_dump() for r in value]}

    def upsert_issues(state):
        # 两条来源统一成一种形状：LLM/脚本建议 + cookie 失效这类「异常」（key/body 形式）
        recs = list(state.get("recommendations") or [])
        for a in (state.get("anomalies") or []):
            recs.append({"priority": "P0", "type": "tooling_failure", "rule": a["key"],
                         "finding": a["body"], "action": a["title"],
                         "assignee": a["assignee"], "gitea_label": a["label"]})
        ok, issues = gitea.list_issues(state="open", label=label_geo, limit=50)
        issues = issues if ok else []
        created, updated, errors = [], [], []
        for r in recs:
            rule = r.get("rule") or ""
            dup = None
            for i in issues:
                if rule and (rule in (i.get("title") or "") or rule in (i.get("body") or "")):
                    dup = i
                    break
            if dup:
                ok2, _ = gitea.comment(dup["number"], "复核更新：%s" % r.get("finding"))
                if ok2:
                    updated.append(dup["number"])
                continue
            body = ("%s\n\n**规则**: %s\n\n---\n规则键: `%s`\n指派给：@%s"
                    % (r.get("finding"), rule, rule, r.get("assignee")))
            ok2, data = gitea.create_issue(
                "[%s] %s" % (r.get("gitea_label") or label_geo, (r.get("action") or "")[:60]),
                body, labels=[r.get("gitea_label") or label_geo],
                assignees=[r.get("assignee") or who])
            if ok2:
                created.append({"number": data.get("number"), "title": r.get("action"),
                                "assignees": [r.get("assignee") or who],
                                "skipped": data.get("_skipped")})
            else:
                errors.append("create failed: %s" % data)
        out = {"new_issues": created, "updated_issues": updated}
        if errors:
            out["errors"] = errors
        return out

    def write_weekly(state):
        import datetime as dt
        c = state.get("citation") or {}
        week = dt.date.today().strftime("%Y-W%V")
        record = {
            "week": week,
            "date": dt.date.today().isoformat(),
            "site": site_mod.get(site, "site.name"),
            "crawler_stats": state.get("crawler_stats"),
            "citation_rate": ((c.get("analysis") or {}) or {}).get("citation_rate"),
            "citation_results": c.get("results"),
            "recommendations": state.get("recommendations"),
            "notes": " · ".join(state.get("notes") or []),
            "producer": "geo-agent/pipeline-B",
        }
        path = repo.out_path("metrics/weekly-%s.json" % week)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        rel = "metrics/weekly-%s.json" % week
        repo.commit_files([rel], "metrics: weekly %s (geo-agent)" % week)
        return {"weekly_file": str(path), "weekly_rel": rel, "weekly_record": record}

    return {"fetch_crawlers": fetch_crawlers, "run_citation_check": run_citation_check,
            "cookie_invalid": cookie_invalid, "skip_llm": skip_llm,
            "llm_analyze": llm_analyze, "upsert_issues": upsert_issues,
            "write_weekly": write_weekly, "_route_cookie": route_cookie}
