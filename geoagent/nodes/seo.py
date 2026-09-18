"""管道 A 节点：SEO 每日健康检查（0 LLM）。

「覆盖率算成 198%」这个历史坑的根因是让 LLM 做算术 —— 这里全部落到纯函数，
并把每个阈值判定写成显式规则，可单测。

通用性：站点地址、路由前缀、静态页清单、品类/负责人、工作流标签、阈值全部来自 site.yml。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

from .. import obs, site as site_mod
from ..config import get
from ..metrics import coverage as cov_mod
from ..nodes import common

BOTS = ["DoubaoBot", "Bytespider", "DeepSeekBot", "GPTBot", "Googlebot"]


def make_nodes(cfg, repo, gitea, ssh_runner=None, site: dict | None = None):
    site = site if site is not None else (cfg.get("_site") or {})
    base_url = site_mod.get(site, "site.base_url", "")
    thr = get(cfg, "thresholds", {}) or {}
    label_seo = site_mod.label(site, "seo_monitor", "seo-monitor")
    who = site_mod.default_assignee(site)

    # ---------------------------------------------------------------- 并发抓取（5 个节点）
    def fetch_sitemap(state):
        ok, urls = common.sitemap_urls(base_url)
        if not ok:
            return {"sitemap_urls": [], "errors": ["sitemap fetch failed: %s" % urls]}
        return {"sitemap_urls": urls}

    def fetch_robots(state):
        ok, body = common.robots_txt(base_url)
        if not ok:
            return {"robots": None, "errors": ["robots fetch failed: %s" % body]}
        return {"robots": {b: (b in body) for b in BOTS}, "robots_raw_len": len(body)}

    def fetch_jsonld(state):
        ok, info = common.jsonld_check(base_url)
        if not ok:
            return {"jsonld": None, "errors": ["jsonld fetch failed: %s" % info]}
        return {"jsonld": info}

    def fetch_articles(state):
        ok, total = common.strapi_articles(base_url)
        if not ok:
            # 分母不可得 → 覆盖率记 UNKNOWN，不是 0
            return {"strapi_articles": None, "errors": ["articles-total-failed: %s" % total]}
        return {"strapi_articles": total}

    def fetch_crawlers(state):
        counts = common.crawler_log_counts(ssh_runner)
        if counts is None:
            obs.log("crawler_logs_skipped", level="warn", reason="no ssh_runner / 采集不可用")
        return {"crawler_stats": counts}

    # ---------------------------------------------------------------- 计算（纯函数）
    def compute_metrics(state):
        m = cov_mod.compute_coverage(
            state.get("sitemap_urls") or [],
            state.get("strapi_articles"),
            site_mod.get(site, "thresholds.static_pages_expected") or None,
            article_prefix=site_mod.article_prefix(site),
            static_paths=site_mod.static_paths(site),
        )
        obs.node_done("compute_metrics", coverage=m["coverage"], health=m["health"],
                      unique_slugs=m["unique_article_slugs"],
                      strapi=m["strapi_articles"], dual_locale=m["dual_locale"])
        return {"metrics": m}

    # ---------------------------------------------------------------- 规则判定（纯函数）
    def evaluate_rules(state):
        m = state.get("metrics") or {}
        anomalies, notes = [], []

        c = m.get("coverage")
        cov_min = float(thr.get("sitemap_coverage_min", 0.90))
        if c is None:
            notes.append("覆盖率不可判定（分母不可得）→ 不建工单，不记 0%")
        elif m.get("over_index_artifact"):
            notes.append("coverage_raw=%.4f > 1.0 属良性伪影，按 100%% 处理" % c)
        elif c < cov_min:
            anomalies.append({
                "label": label_seo, "assignee": who,
                "title": "sitemap 覆盖率 %.1f%% < %.0f%%" % (c * 100, cov_min * 100),
                "key": "sitemap-coverage-drop",
                "body": ("sitemap %s URLs / 唯一 slug %s / 文章总数 %s\n\n"
                         "health=%s · dual_locale=%s · 旧口径(会算出假超索引)=%s%% · 路由前缀=%s"
                         % (m.get("sitemap_urls"), m.get("unique_article_slugs"),
                            m.get("strapi_articles"), m.get("health"), m.get("dual_locale"),
                            m.get("naive_coverage_pct_legacy"), m.get("article_prefix")))})

        robots = state.get("robots") or {}
        for b in BOTS:
            if robots and not robots.get(b):
                anomalies.append({"label": label_seo, "assignee": who,
                                  "title": "robots.txt 缺少 %s 放行规则" % b,
                                  "key": "robots-missing-%s" % b,
                                  "body": "robots.txt 未匹配到 %s" % b})

        j = state.get("jsonld") or {}
        for schema in ("Organization", "WebSite"):
            if j and not j.get("has_" + schema.lower()):
                anomalies.append({"label": label_seo, "assignee": who,
                                  "title": "首页缺少 %s JSON-LD" % schema,
                                  "key": "jsonld-missing-%s" % schema,
                                  "body": "首页 JSON-LD 未含 %s（注意 RSC 内嵌写法）" % schema})

        lcp = (state.get("vitals") or {}).get("lcp")
        lcp_max = float(thr.get("lcp_max_seconds", 4.0))
        if lcp is not None and lcp > lcp_max:
            anomalies.append({"label": label_seo, "assignee": who,
                              "title": "首页 LCP=%.1fs 超过 %.1fs 阈值" % (lcp, lcp_max),
                              "key": "lcp-high", "body": "LCP %.1fs" % lcp})

        obs.node_done("evaluate_rules", anomalies=len(anomalies), notes=len(notes))
        return {"anomalies": anomalies, "notes": notes}

    # ---------------------------------------------------------------- 工单去重后建/更新
    def upsert_issues(state):
        anomalies = state.get("anomalies") or []
        ok, issues = gitea.list_issues(state="open", label=label_seo, limit=50)
        issues = issues if ok else []
        created, updated, errors = [], [], []
        for a in anomalies:
            dup = None
            for i in issues:
                if a["key"] in (i.get("body") or "") or a["title"] in (i.get("title") or ""):
                    dup = i
                    break
            if dup:
                ok2, _ = gitea.comment(dup["number"],
                                       "复发/更新：%s\n\n规则键: %s" % (a["title"], a["key"]))
                if ok2:
                    updated.append(dup["number"])
                else:
                    errors.append("comment failed #%s" % dup["number"])
            else:
                body = a["body"] + "\n\n---\n规则键: `%s`\n指派给：@%s" % (a["key"], a["assignee"])
                ok2, data = gitea.create_issue("[%s] %s" % (a["label"], a["title"]),
                                               body, labels=[a["label"]],
                                               assignees=[a["assignee"]])
                if ok2:
                    created.append({"number": data.get("number"), "title": a["title"],
                                    "assignees": [a["assignee"]],
                                    "skipped": data.get("_skipped")})
                else:
                    errors.append("create failed: %s" % data)
        obs.node_done("upsert_issues", created=len(created), updated=len(updated))
        out = {"new_issues": created, "updated_issues": updated}
        if errors:
            out["errors"] = errors
        return out

    def skip_notify(state):
        obs.log("no_anomaly_silent", level="info")
        return {}

    # ---------------------------------------------------------------- 落盘 + 提交
    def write_daily(state):
        m = state.get("metrics") or {}
        today = dt.date.today().isoformat()
        record = {
            "date": today,
            "site": site_mod.get(site, "site.name"),
            "base_url": base_url,
            # static_pages 显式写实际值（配置文件里的期望值），别沿用旧 +4 分母
            "static_pages": site_mod.get(site, "thresholds.static_pages_expected"),
            "sitemap_urls": m.get("sitemap_urls"),
            "unique_article_slugs": m.get("unique_article_slugs"),
            "blog_urls": m.get("blog_urls"),
            "dual_locale": m.get("dual_locale"),
            "strapi_articles": m.get("strapi_articles"),
            "coverage": m.get("coverage"),
            "coverage_raw": m.get("coverage_raw"),
            "over_index_artifact": m.get("over_index_artifact"),
            "health": m.get("health"),
            "article_prefix": m.get("article_prefix"),
            "robots": state.get("robots"),
            "jsonld": state.get("jsonld"),
            "crawler_stats": state.get("crawler_stats"),
            "notes": " · ".join(state.get("notes") or []),
            "producer": "geo-agent/pipeline-A",
        }
        path = repo.out_path("metrics/daily-%s.json" % today)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        rel = "metrics/daily-%s.json" % today
        repo.commit_files([rel], "metrics: daily %s (geo-agent)" % today)
        obs.node_done("write_daily", file=rel, coverage=m.get("coverage"))
        return {"daily_file": str(path), "daily_rel": rel, "daily_record": record}

    return {
        "fetch_sitemap": fetch_sitemap, "fetch_robots": fetch_robots,
        "fetch_jsonld": fetch_jsonld, "fetch_articles": fetch_articles,
        "fetch_crawlers": fetch_crawlers, "compute_metrics": compute_metrics,
        "evaluate_rules": evaluate_rules, "upsert_issues": upsert_issues,
        "skip_notify": skip_notify, "write_daily": write_daily,
    }
