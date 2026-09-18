"""geo-agent CLI。

设计原则：
  * 默认就是影子（只读）。写生产需要**两个**条件同时满足：config.yml 里 shadow.read_only=false
    且命令行显式 --apply。任何一条不满足都只记录 would_*。
  * 官网发布另有一道独立硬锁：site.yml 的 publish.enabled（默认关闭，用户要求）。
  * --notify 独立于 --apply：允许在影子期真发企业微信群消息（通知不是写生产）。
  * 站点与知识库通过 --site / config.yml 指定；换站点/换知识库不改代码。

    python cli.py list
    python cli.py check                       # 只做配置自检（不跑管道）
    python cli.py publish --dry-run
    python cli.py seo-daily --dry-run
    python cli.py geo-weekly --dry-run [--with-citation]
    python cli.py strategy --dry-run [--no-llm]
    python cli.py content --dry-run
    python cli.py seo-daily --site other-site.yml     # 换一份站点档案
"""
from __future__ import annotations

import argparse
import contextlib
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from geoagent import config as cfgmod  # noqa: E402
from geoagent import knowledge as k_mod  # noqa: E402
from geoagent import obs  # noqa: E402
from geoagent import schema_loader  # noqa: E402
from geoagent import site as site_mod  # noqa: E402
from geoagent.clients import strapi as cms_mod  # noqa: E402
from geoagent.clients.gitea import Gitea  # noqa: E402
from geoagent.clients.wecom import WeCom  # noqa: E402
from geoagent.repo import Repo  # noqa: E402

PIPELINES = {
    "publish": ("管道 D · 定时发布", "geoagent.graphs.publish"),
    "seo-daily": ("管道 A · SEO 每日", "geoagent.graphs.seo_daily"),
    "geo-weekly": ("管道 B · GEO 每周", "geoagent.graphs.geo_weekly"),
    "strategy": ("管道 C · 策略自进化", "geoagent.graphs.strategy"),
    "content": ("管道 E · 内容生成", "geoagent.graphs.content"),
}


def _checkpointer(cfg, use_cp=True):
    if not use_cp:
        return None, None
    from langgraph.checkpoint.sqlite import SqliteSaver
    db = cfgmod.abspath(cfg, "paths.state_db")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    return SqliteSaver(conn), conn


def _prepare(args):
    cfg = cfgmod.load(args.config, getattr(args, "site", None))
    cfg["_allow_notify"] = bool(args.notify)
    paths = cfgmod.ensure_dirs(cfg)
    read_only = cfgmod.read_only(cfg, apply_flag=args.apply)
    run_id = obs.start_run(paths["paths.logs"], args.pipeline,
                           mode=("apply" if not read_only else "dry-run"))
    site = cfg["_site"]
    obs.log("config_loaded", config_file=cfg["_meta"]["config_file"],
            site_file=cfg["_meta"]["site_file"], site_name=site_mod.get(site, "site.name"),
            read_only=read_only, notify_allowed=bool(args.notify),
            publish_enabled=bool(cfgmod.get(cfg, "publish.enabled")))
    problems = cfgmod.check_env(cfg)
    for p in problems:
        obs.log("config_problem", level="warn", problem=p)
    return cfg, paths, read_only, run_id


def cmd_check(args):
    """只做配置自检：验证 site.yml + knowledge/index.yml 是否自洽，不跑任何管道。"""
    cfg = cfgmod.load(args.config, getattr(args, "site", None))
    site = cfg["_site"]
    print("config.yml : %s" % cfg["_meta"]["config_file"])
    print("site.yml   : %s%s" % (cfg["_meta"]["site_file"],
                                 "" if cfg["_meta"]["site_exists"] else "  ← 不存在！"))
    print("\n--- 站点 ---")
    print("名称      : %s" % site_mod.get(site, "site.name"))
    print("目标站    : %s" % site_mod.get(site, "site.base_url"))
    print("域名清单  : %s" % ", ".join(site_mod.hosts(site)) or "-")
    print("品牌别名  : %s" % ", ".join(site_mod.brand_aliases(site)) or "-")
    print("发布器    : %s（publish.enabled=%s）"
          % (site_mod.get(site, "publish.cms"), cfgmod.get(cfg, "publish.enabled")))
    print("路由前缀  : %s · locale=%s"
          % (site_mod.article_prefix(site), site_mod.locale_prefixes(site)))
    print("水印候选  : %s（≥%d 次）"
          % (site_mod.watermark_candidates(site) or "（未配置）",
             site_mod.watermark_min(site)))
    print("\n--- 品类 / 负责人 ---")
    for c in site_mod.categories(site):
        print("  %-16s %-10s %-8s weight=%s" % (c.get("id"), c.get("name"),
                                                c.get("assignee"), c.get("weight")))
    print("  默认负责人: %s" % site_mod.default_assignee(site))
    print("\n--- 知识库 ---")
    repo = Repo(cfg, read_only=True)
    repo.bootstrap()
    k = k_mod.load(cfg, repo, site)
    s = k.summary()
    print("来源      : %s（%s）" % (s["source"], s["root"]))
    print("条目      : %d %s" % (s["entries"], s["ids"]))
    print("硬口径    : %d 条" % s["rules"])
    for e in k.entries:
        ok, txt = k.facts(e)
        print("  - %-16s facts %s" % (e.get("id"), "OK %d 字符" % len(txt) if ok else "读取失败"))
    import json as _json
    try:
        m = schema_loader.build(site)
        print("\n--- 动态模型 ---")
        print("  Category Literal : %s" % list(m["categories"]))
        print("  Assignee Literal : %s" % list(m["assignees"]))
    except Exception as e:  # noqa: BLE001
        print("\n!! 动态模型构造失败: %s" % e)

    problems = cfgmod.check_env(cfg)
    print("\n--- 自检 ---")
    if problems:
        for p in problems:
            print("  ⚠️  %s" % p)
    else:
        print("  ✅ 配置自洽")
    return 0 if not problems else 1


def run_pipeline(args):
    cfg, paths, read_only, run_id = _prepare(args)
    site = cfg["_site"]
    repo = Repo(cfg, read_only=read_only)
    gitea = Gitea(cfg, read_only=read_only)
    cms = cms_mod.build(cfg, read_only=read_only, site=site)
    knowledge = k_mod.load(cfg, repo, site)
    cp, conn = _checkpointer(cfg, use_cp=not args.no_checkpoint)

    kw = {"read_only": read_only, "checkpointer": cp, "site": site}
    if args.pipeline == "publish":
        from geoagent.graphs import publish as g
        graph = g.build(cfg, repo, gitea, cms, knowledge=knowledge, **kw)
        init = {"pipeline": args.pipeline}
    elif args.pipeline == "seo-daily":
        from geoagent.graphs import seo_daily as g
        graph = g.build(cfg, repo, gitea, **kw)
        init = {"pipeline": args.pipeline}
    elif args.pipeline == "geo-weekly":
        from geoagent.graphs import geo_weekly as g
        graph = g.build(cfg, repo, gitea, knowledge=knowledge, **kw)
        init = {"pipeline": args.pipeline,
                "options": {"with_citation": bool(args.with_citation)}}
    elif args.pipeline == "strategy":
        from geoagent.graphs import strategy as g
        graph = g.build(cfg, repo, gitea, knowledge=knowledge, **kw)
        init = {"pipeline": args.pipeline}
    elif args.pipeline == "content":
        from geoagent.graphs import content as g
        graph = g.build(cfg, repo, gitea, knowledge=knowledge, **kw)
        init = {"pipeline": args.pipeline}
    else:
        raise SystemExit("unknown pipeline: %s" % args.pipeline)

    if args.no_llm:
        import geoagent.llm as llm_mod
        llm_mod.available = lambda: False
        llm_mod.get_llm = lambda *a, **k: None
        obs.log("llm_stubbed", level="warn", reason="--no-llm")

    obs.log("run_graph_start", pipeline=args.pipeline, read_only=read_only)
    final = graph.invoke(init, config={"configurable": {"thread_id": run_id}})
    _report(final, read_only, run_id, site)
    if conn is not None:
        conn.close()
    return final


def _report(final: dict, read_only: bool, run_id: str, site: dict):
    errors = final.get("errors") or []
    print("\n===== 影子跑结果 =====" if read_only else "\n===== 执行结果 =====")
    print("run_id      : %s" % run_id)
    print("site        : %s (%s)" % (site_mod.get(site, "site.name"),
                                     site_mod.get(site, "site.base_url")))
    print("mode        : %s" % ("dry-run（只读，无任何远端写入）" if read_only else "APPLY"))
    if "metrics" in final and final["metrics"]:
        m = final["metrics"]
        print("覆盖率      : %s  (health=%s)" % (m.get("coverage"), m.get("health")))
        print("  唯一 slug : %s / 文章总数 %s · sitemap %s URLs · dual_locale=%s"
              % (m.get("unique_article_slugs"), m.get("strapi_articles"),
                 m.get("sitemap_urls"), m.get("dual_locale")))
        print("  旧口径    : %s%%（会算出假超索引，仅留档对比）" % m.get("naive_coverage_pct_legacy"))
    for key in ("articles", "results", "recommendations", "topics", "triggered_rules",
                "anomalies", "new_issues", "updated_issues", "generated", "published", "notes"):
        if final.get(key):
            v = final[key]
            print("%-11s : %s" % (key, len(v) if isinstance(v, list) else v))
    if final.get("window"):
        w = final["window"]
        print("窗口        : days=%s missing=%s mean_coverage=%s"
              % (w.get("days_present"), w.get("missing_days"),
                 (w.get("coverage") or {}).get("mean_coverage")))
    if errors:
        print("\n!! errors (%d)：" % len(errors))
        for e in errors[:10]:
            print("   - %s" % e)
    print("trace       : %s" % obs.trace_path())


def cmd_list(args):
    print("geo-agent 管道：\n")
    for key, (desc, mod) in PIPELINES.items():
        print("  %-11s %-22s %s" % (key, desc, mod))
    print("\n默认 dry-run（只读）。写生产需 config.yml shadow.read_only=false + --apply；"
          "\n官网发布另需 site.yml 的 publish.enabled=true（默认关闭）。"
          "\n发企业微信群消息用 --notify（独立于写权限）。"
          "\n换站点用 --site <file.yml>，换知识库改 knowledge/index.yml。")


def main(argv=None):
    p = argparse.ArgumentParser(prog="geo-agent", description="GEO/SEO LangGraph 化管道")
    sub = p.add_subparsers(dest="cmd")
    for key, (desc, _) in PIPELINES.items():
        sp = sub.add_parser(key, help=desc)
        sp.add_argument("--config", default=None)
        sp.add_argument("--site", default=None, help="站点档案（默认 site.yml）")
        sp.add_argument("--dry-run", action="store_true", default=True,
                        help="默认行为：只读影子跑")
        sp.add_argument("--apply", action="store_true",
                        help="真写生产（还需 shadow.read_only=false）")
        sp.add_argument("--notify", action="store_true", help="允许真发企业微信群消息")
        sp.add_argument("--no-checkpoint", action="store_true", help="不用 SqliteSaver")
        sp.add_argument("--no-llm", action="store_true", help="把 LLM 节点置为不可用")
        sp.add_argument("--with-citation", action="store_true",
                        help="管道 B：真跑引用检测（Playwright，2-3 分钟）")
        sp.set_defaults(pipeline=key, func=run_pipeline)
    lp = sub.add_parser("list", help="列出管道")
    lp.set_defaults(func=cmd_list)
    cp = sub.add_parser("check", help="只做配置自检（站点/知识库/模型）")
    cp.add_argument("--config", default=None)
    cp.add_argument("--site", default=None)
    cp.set_defaults(func=cmd_check)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    return args.func(args) or 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        raise SystemExit(main())
