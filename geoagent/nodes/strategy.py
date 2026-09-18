"""管道 C 节点：策略分析与自进化（2 LLM 节点）。

分工：数字全部由纯函数算（窗口聚合/规则触发/封顶判断），LLM 只做两件事 ——
  ① 把四周数字讲成人能读的周报结论（WeekReview）
  ② 按引用查询词反推一周选题（TopicPick，品类与产品篇由 site.yml + knowledge 决定）

通用性：品类清单、负责人、权重上限、知识库、水印、标签、批次构成全部来自配置。
规则文件通过 Gitea API 读，不依赖本地 clone。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re

from .. import llm as llm_mod
from .. import obs, schema_loader, site as site_mod
from ..config import get
from ..metrics import normalize as nz
from ..schemas import WeekReview


def make_nodes(cfg, repo, gitea, site: dict | None = None, knowledge=None):
    site = site if site is not None else (cfg.get("_site") or {})
    window_weeks = 4
    models = schema_loader.build(site)
    cats = site_mod.category_ids(site)
    people = site_mod.assignees(site)
    label_article = site_mod.label(site, "article_generate", "article-generate")
    label_review = site_mod.label(site, "strategy_review", "strategy-review")
    batch = site_mod.get(site, "batch", {}) or {}

    # ---------------------------------------------------------------- 窗口
    def load_window(state):
        mdir = pathlib.Path(repo.dir) / "metrics"
        today = dt.date.today()
        expected = nz.date_range(today, window_weeks * 7)
        records, present = [], []
        for d in expected:
            p = mdir / ("daily-%s.json" % d.isoformat())
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            records.append(nz.normalize_daily(data, d.isoformat()))
            present.append(d.isoformat())
        # 历史文件里的 coverage>1.0 是良性伪影（旧分母写进去的）：按 100% 处理，保留 raw
        for rec in records:
            c = rec.get("coverage")
            if c is not None and c > 1.0:
                rec["coverage_raw_historical"] = c
                rec["over_index_artifact"] = True
                rec["coverage"] = 1.0
        agg = nz.mean_coverage(records)
        missing = nz.missing_dates(present, [d.isoformat() for d in expected])

        # 爬虫/引用只存在 weekly 文件里（daily 里没有 —— 别去 daily 找）
        weekly = []
        for p in sorted(mdir.glob("weekly-*.json")):
            try:
                weekly.append(nz.normalize_weekly(json.loads(p.read_text(encoding="utf-8")),
                                                  p.stem))
            except json.JSONDecodeError:
                continue
        weekly = weekly[-window_weeks:]
        cites = [w.get("citation_rate") for w in weekly if w.get("citation_rate") is not None]
        crawler_totals = {}
        for w in weekly:
            for bot, n in ((w.get("crawler_stats") or {}) or {}).items():
                crawler_totals[bot] = crawler_totals.get(bot, 0) + int(n or 0)

        obs.node_done("load_window", days=len(records), missing=len(missing),
                      mean_coverage=agg["mean_coverage"])
        return {"window": {
            "days_present": len(records), "missing_days": missing,
            "coverage": agg, "records": records, "weekly": weekly,
            "citation_rate_mean": round(sum(cites) / len(cites), 4) if cites else None,
            "crawler_totals": crawler_totals or None,
            "data_points": len(records),
        }}

    # ---------------------------------------------------------------- 规则
    def load_rules(state):
        ok, text = gitea.file_content("strategy/evolution-rules.yaml")
        if not ok:
            local = pathlib.Path(__file__).resolve().parents[2] / "tests" / "fixtures" / \
                "evolution-rules.yaml"
            if local.exists():
                text = local.read_text(encoding="utf-8")
                obs.log("rules_from_local_fixture", level="warn", reason="API 读取失败，用本地样本")
            else:
                return {"rules": [], "errors": ["rules 读取失败: %s" % text]}
        try:
            import yaml
            data = yaml.safe_load(text) or {}
        except Exception as e:  # noqa: BLE001
            return {"rules": [], "errors": ["rules yaml 解析失败: %s" % e]}
        rules = data.get("rules") or []
        return {"rules": rules, "rules_meta": {"schedule": data.get("schedule"),
                                              "count": len(rules)}}

    def measure(state):
        """把 metrics 映射到规则里的 metric 名。取不到的一律记 unavailable，不虚构。"""
        w = state.get("window") or {}
        vals = {
            "sitemap_coverage": (w.get("coverage") or {}).get("mean_coverage"),
            "ai_citation_rate": w.get("citation_rate_mean"),
            "doubaobot_crawl_count": (w.get("crawler_totals") or {}).get("DoubaoBot"),
        }
        unavailable = []
        for name in ("gsc_impressions_no_click", "deepseek_citations_monthly",
                     "new_article_crawl_delay_hours", "lcp_seconds"):
            vals[name] = None
            unavailable.append(name)
        return {"rule_metrics": vals, "rule_metrics_unavailable": unavailable}

    def _read_weights() -> dict:
        """读 keywords.yaml 的 category_weights（不写死品类名）。"""
        ok, text = gitea.file_content("strategy/keywords.yaml")
        if not ok:
            return {}
        try:
            import yaml
            data = yaml.safe_load(text) or {}
        except Exception:  # noqa: BLE001
            m = re.search(r"category_weights:\s*\n((?:\s+\S+:.*\n)+)", text)
            return {}
        return dict((data.get("category_weights") or {}))

    def _target_category(rule, site_cfg):
        """从规则里解析要调权重的品类：优先 action.category，否则从 change 文本里找站点品类名。"""
        act = rule.get("action") or {}
        if act.get("category"):
            return act["category"]
        change = str(act.get("change") or "")
        for cid in site_mod.category_ids(site_cfg):
            if cid in change:
                return cid
        return None

    def check_rules(state):
        rules = state.get("rules") or []
        vals = state.get("rule_metrics") or {}
        weights = _read_weights()
        triggered, skipped = [], []
        for r in rules:
            trig = r.get("trigger") or {}
            metric = trig.get("metric")
            value = vals.get(metric)
            if value is None:
                skipped.append({"id": r.get("id"), "why": "metric 不可得（不虚构，不触发）"})
                continue
            op, thr = trig.get("operator"), trig.get("threshold")
            hit = {"<": value < thr, ">": value > thr, "==": value == thr,
                   ">=": value >= thr, "<=": value <= thr}.get(op, False)
            if not hit:
                skipped.append({"id": r.get("id"),
                                "why": "未达阈值 %s %s (%s)" % (op, thr, value)})
                continue
            entry = {"id": r.get("id"), "name": r.get("name"), "action": r.get("action"),
                     "priority": r.get("priority"), "value": value, "trigger": trig}
            if (r.get("action") or {}).get("type") == "adjust_category_weight":
                tgt = _target_category(r, site)
                entry["cap"] = (r.get("action") or {}).get("max")
                entry["target_category"] = tgt
                cur = weights.get(tgt) if tgt else None
                try:
                    entry["current_weight"] = float(cur) if cur is not None else None
                except (TypeError, ValueError):
                    entry["current_weight"] = None
                entry["capped"] = (entry["cap"] is not None
                                   and entry["current_weight"] is not None
                                   and entry["current_weight"] >= float(entry["cap"]))
            triggered.append(entry)
        obs.node_done("check_rules", triggered=len(triggered), skipped=len(skipped))
        return {"triggered_rules": triggered, "skipped_rules": skipped}

    # ---------------------------------------------------------------- LLM ① 周报
    def llm_week_review(state):
        w = state.get("window") or {}
        prompt = (
            "你是 %s 的 GEO/SEO 运营周报分析员。基于下面的实测数字写一份周报结论。\n"
            "硬约束：\n"
            "1. 所有数字必须来自给定数据，不得虚构；数据缺失就明确写「不可判定」。\n"
            "2. coverage 为 null 的日期是从均值里剔除的（分母不可得），要说明而不是当 0。\n"
            "3. 缺档日（调度空档）不算异常，只在 notes 里提一句。\n"
            "4. weight_trace 形如 1.0→1.2→1.5(封顶)，没有就留空。\n\n"
            "数据：\n%s"
            % (site_mod.get(site, "site.name"),
               json.dumps({
                   "coverage_agg": w.get("coverage"),
                   "days_present": w.get("days_present"),
                   "missing_days": w.get("missing_days"),
                   "citation_rate_mean": w.get("citation_rate_mean"),
                   "crawler_totals": w.get("crawler_totals"),
                   "triggered_rules": state.get("triggered_rules"),
                   "unavailable_metrics": state.get("rule_metrics_unavailable"),
               }, ensure_ascii=False))
        )
        ok, value = llm_mod.structured(cfg, WeekReview, prompt, node="llm_week_review")
        if not ok:
            return {"week_review": None, "errors": ["周报 LLM 失败: %s" % value]}
        return {"week_review": value.model_dump()}

    # ---------------------------------------------------------------- LLM ② 选题
    def _batch_plan() -> tuple[list[str], dict | None]:
        per = int(batch.get("categories_per_week", 1) or 1)
        plan = []
        for cid in cats:
            plan.extend([cid] * per)
        prod = None
        if batch.get("product_slot") and knowledge is not None:
            prod = knowledge.product_slot_entry()
        return plan, prod

    def llm_pick_topics(state):
        today = dt.date.today().strftime("%Y%m%d")
        plan, prod_entry = _batch_plan()
        n_total = len(plan) + (1 if prod_entry else 0)
        cat_desc = "\n".join("  - %s（%s，负责：%s，风格：%s）"
                             % (c.get("id"), c.get("name") or "", c.get("assignee") or "",
                                c.get("style") or "")
                             for c in site_mod.categories(site))
        prod_hint = ""
        if prod_entry:
            angles = knowledge.angles(prod_entry)
            prod_hint = ("\n另需 1 条产品篇（is_product=true，category 用 %s），"
                         "以知识库条目「%s」为依据，角度从这些里轮换（防同题重复）：%s。"
                         % (batch.get("product_category") or (cats[0] if cats else ""),
                            prod_entry.get("title") or prod_entry.get("id"),
                            "、".join(angles) if angles else "自定"))
        prompt = (
            "为「%s」（%s）规划本周内容批次，产出 %d 条选题：%s\n"
            "品类清单：\n%s\n%s\n"
            "硬约束：\n"
            "1. slug 必须小写英文+连字符并以 %s 结尾（如 my-topic-%s）。\n"
            "2. 选择题材要贴近用户会在 AI 助手里问的问题（引用查询词），以提升 GEO 命中。\n"
            "3. category 只能从上面的品类清单里选（产品篇除外）。\n"
            "4. 不写具体价格、不做竞品贬损。\n\n上期周报结论：%s"
            % (site_mod.get(site, "site.name"), site_mod.get(site, "site.base_url"),
               n_total,
               "、".join(plan) if plan else "（无品类配置）",
               cat_desc, prod_hint, today, today,
               json.dumps(state.get("week_review") or {}, ensure_ascii=False))
        )
        ok, value = llm_mod.structured(cfg, models["TopicPickList"], prompt,
                                       node="llm_pick_topics")
        if not ok:
            return {"topics": [], "errors": ["选题 LLM 失败: %s" % value]}
        topics = [t.model_dump() for t in (value.topics or [])]
        for t in topics:
            if not t["slug"].endswith(today):
                t["slug"] = "%s-%s" % (t["slug"].rstrip("-"), today)
        if len(topics) != n_total:
            return {"topics": topics,
                    "errors": ["选题数量异常：期望 %d 条，实得 %d" % (n_total, len(topics))]}
        return {"topics": topics}

    # ---------------------------------------------------------------- 批次守卫 + 建单
    def guard_open_batch(state):
        ok, issues = gitea.list_issues(state="open", label=label_article, limit=50)
        issues = issues if ok else []
        return {"open_batch": [{"number": i.get("number"), "title": i.get("title")}
                               for i in issues], "batch_blocked": bool(issues)}

    def _issue_body(t: dict, prod_entry) -> str:
        """工单正文：施工要求从站点配置与知识库生成，不含任何硬编码品牌。"""
        wm = site_mod.watermark_for_entry(site, prod_entry if t.get("is_product") else None)
        lines = [
            "## 施工要求",
            "",
            "1. 输出到 `output/articles/%s.md`，frontmatter 含 title/slug/category，"
            "`status: review`" % t["slug"],
            "2. 字数≥%s，H2≥%s，FAQ≥%s 条（`<section data-faq>` 包裹），表格≥%s 个"
            % (get(cfg, "quality.min_chars"), get(cfg, "quality.min_h2"),
               get(cfg, "quality.min_faq"), get(cfg, "quality.min_tables")),
            "3. 表格用 `<thead><tr><th>` + `<tbody><tr><td>`，禁止 th/td 同行混用",
            "4. 水印「%s」正文出现≥%s 次；时效性「%s %s。」"
            % (wm, site_mod.watermark_min(site), get(cfg, "quality.timeliness_phrase"),
               dt.date.today().isoformat()),
            "5. 禁止用 `<blockquote>` 开篇",
        ]
        if t.get("is_product") and prod_entry:
            files = prod_entry.get("files") or {}
            lines.append("6. 产品篇硬约束：参数/规格/名单必须来自 `%s`"
                         "（FAQ 取自 `%s`），不得编造" % (files.get("facts"), files.get("faqs")))
            for r in (knowledge.rules if knowledge else []):
                lines.append("   - %s" % r)
        elif knowledge is not None and not knowledge.for_category(t["category"]):
            lines.append("6. 本品类暂无登记知识库 → 涉及具体参数/数字时只能用行业通用表述，"
                         "不得编造")
        body = "\n".join(lines)
        body += ("\n\n---\n规则键: `strategy-batch-%s`\n指派给：@%s"
                 % (t["slug"], t.get("assignee") or site_mod.default_assignee(site)))
        return body

    def create_article_issues(state):
        topics = state.get("topics") or []
        created, updated, errors = [], [], []
        if state.get("batch_blocked"):
            # 上一批仍 open → 只评论更新，不新建批次（防重复建单）
            for i in (state.get("open_batch") or []):
                ok2, _ = gitea.comment(i["number"],
                                       "策略复核：本批次仍未完成，本期不新建批次，继续沿用。")
                if ok2:
                    updated.append(i["number"])
            obs.log("batch_blocked_comment_only", updated=len(updated))
            return {"new_issues": created, "updated_issues": updated}

        _, prod_entry = _batch_plan()
        for t in topics:
            who = t.get("assignee") or site_mod.assignee_for_category(site, t["category"])
            cname = (site_mod.category(site, t["category"]) or {}).get("name") or t["category"]
            title = ("[%s] %s品类 %s" % (label_article, cname, t["topic"])
                     if not t.get("is_product")
                     else "[%s] 产品篇 %s" % (label_article, t["topic"]))
            ok2, data = gitea.create_issue(title, _issue_body(t, prod_entry),
                                           labels=[label_article], assignees=[who])
            if ok2:
                created.append({"number": data.get("number"), "title": title,
                                "assignees": [who], "skipped": data.get("_skipped")})
            else:
                errors.append("create failed: %s" % data)
        out = {"new_issues": created, "updated_issues": updated}
        if errors:
            out["errors"] = errors
        return out

    def apply_weight_changes(state):
        """权重调整：带封顶判断。达到 max 只记录「已封顶」，不再 +20%。"""
        changes, notes = [], []
        for e in (state.get("triggered_rules") or []):
            if (e.get("action") or {}).get("type") != "adjust_category_weight":
                continue
            tgt = e.get("target_category")
            if not tgt:
                notes.append("规则 %s 无法解析目标品类（在 action.category 里写明）" % e.get("id"))
                continue
            cur, cap = e.get("current_weight"), e.get("cap")
            if e.get("capped"):
                notes.append("权重 %s=%s 已达上限 %s → 只记录已封顶，不调整" % (tgt, cur, cap))
                continue
            newv = round((cur or 1.0) * 1.2, 4)
            if cap is not None and newv > float(cap):
                newv = float(cap)
            changes.append({"category": tgt, "from": cur, "to": newv, "cap": cap})
        if changes:
            notes.append("待应用权重调整: %s（影子期只记录，不写 keywords.yaml）" % changes)
        return {"weight_changes": changes, "notes": notes}

    def write_weekly_strategy(state):
        week = dt.date.today().strftime("%Y-W%V")
        record = {
            "week": week,
            "date": dt.date.today().isoformat(),
            "site": site_mod.get(site, "site.name"),
            "window": (state.get("window") or {}).get("coverage"),
            "days_present": (state.get("window") or {}).get("days_present"),
            "missing_days": (state.get("window") or {}).get("missing_days"),
            "citation_rate_mean": (state.get("window") or {}).get("citation_rate_mean"),
            "triggered": [t.get("id") for t in (state.get("triggered_rules") or [])],
            "skipped": (state.get("skipped_rules") or []),
            "week_review": state.get("week_review"),
            "topics": state.get("topics"),
            "weight_changes": state.get("weight_changes"),
            "notes": state.get("notes"),
            "knowledge": knowledge.summary() if knowledge is not None else None,
            "producer": "geo-agent/pipeline-C",
        }
        path = repo.out_path("metrics/weekly-%s-strategy.json" % week)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        rel = "metrics/weekly-%s-strategy.json" % week
        repo.commit_files([rel], "metrics: weekly %s strategy (geo-agent)" % week)
        return {"weekly_file": str(path), "weekly_rel": rel}

    return {"load_window": load_window, "load_rules": load_rules, "measure": measure,
            "check_rules": check_rules, "llm_week_review": llm_week_review,
            "llm_pick_topics": llm_pick_topics, "guard_open_batch": guard_open_batch,
            "create_article_issues": create_article_issues,
            "apply_weight_changes": apply_weight_changes,
            "write_weekly_strategy": write_weekly_strategy}
