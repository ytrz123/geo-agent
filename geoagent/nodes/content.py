"""管道 E 节点：内容生成（1 LLM + 质检回炉环）。

这是现状缺失最明显的闭环：现在质检 FAIL 后靠主代理临时 patch，没有轮次上限、没有固定话术。
这里做成明确的条件边：FAIL → LLM 按失败清单整篇重写 → 再检，最多 max_fix_rounds 轮，超限升级。

通用性：
  * 水印文案、门禁阈值、品类清单从 site.yml 取
  * 事实来源从 knowledge/ 取（无登记知识库的品类会明确告知「不要编造」，而不是放任）
"""
from __future__ import annotations

import pathlib
import re

from .. import llm as llm_mod
from .. import obs, quality, site as site_mod
from ..config import get
from ..schemas import ArticleDraft

MARKER_3 = "③ 内容生成完毕"

REQUIREMENTS_TMPL = """硬约束（质检门禁会逐条验，不过会退回重写）：
1. markdown 正文 ≥{min_chars} 字（按去空白字符计），H2(## ) ≥{min_h2} 个
2. FAQ ≥{min_faq} 条，放在文末用 <section data-faq> ... </section> 包裹，
   题面写 "### Q1: 问题？"，答案以 "A1：" 开头
3. 表格 ≥{min_tables} 个；表格必须写成
   <thead><tr><th>..</th></tr></thead><tbody><tr><td>..</td></tr></tbody>，
   禁止同一 <tr> 内同时出现 <th> 和 <td>
4. 品牌水印「{watermark}」在正文出现 ≥{watermark_min} 次
5. 必须有时效性断言：「*{timeliness} YYYY-MM-DD。*」
6. 开篇必须是正常的 <p> 段落，用 <strong> 加粗 2-3 个核心关键词，禁止用 <blockquote> 开篇
7. 技术数据引用写成 <blockquote>来源：出处</blockquote>
8. markdown 字段只放正文（不要含 frontmatter、不要重复 H1）
"""


def make_nodes(cfg, repo, gitea, site: dict | None = None, knowledge=None):
    site = site if site is not None else (cfg.get("_site") or {})
    max_rounds = int(get(cfg, "quality.max_fix_rounds", 3))
    allowed_cats = site_mod.category_ids(site)
    label_article = site_mod.label(site, "article_generate", "article-generate")

    def requirements(category: str, is_product: bool = False) -> str:
        entry = None
        if is_product and knowledge is not None:
            entry = knowledge.product_slot_entry()
        wm = site_mod.watermark_for_entry(site, entry)
        return REQUIREMENTS_TMPL.format(
            min_chars=get(cfg, "quality.min_chars", 1500),
            min_h2=get(cfg, "quality.min_h2", 4),
            min_faq=get(cfg, "quality.min_faq", 4),
            min_tables=get(cfg, "quality.min_tables", 2),
            watermark=wm or "（站点未配置水印，此项不判定）",
            watermark_min=site_mod.watermark_min(site),
            timeliness=get(cfg, "quality.timeliness_phrase", "本文数据更新至"),
        )

    # ---------------------------------------------------------------- 选工单
    def _slug_from_body(body: str):
        m = re.search(r"output/articles/([A-Za-z0-9\-_]+)\.md", body or "")
        return m.group(1) if m else None

    def _category_from_body(body: str):
        if allowed_cats:
            for cid in allowed_cats:
                if cid in (body or ""):
                    return cid
        return allowed_cats[0] if allowed_cats else ""

    def pick_issue(state):
        ok, issues = gitea.list_issues(state="open", label=label_article, limit=50)
        issues = issues if ok else []
        picked = []
        for i in issues:
            if gitea.has_comment_marker(i["number"], MARKER_3):
                obs.log("skip_already_generated", issue=i["number"])
                continue
            body = i.get("body") or ""
            picked.append({"number": i["number"], "title": i.get("title"), "body": body,
                           "slug": _slug_from_body(body),
                           "category": _category_from_body(body),
                           "is_product": "产品篇" in (i.get("title") or "")})
        obs.node_done("pick_issue", open=len(issues), todo=len(picked))
        return {"issues": picked}

    def route_issues(state):
        from langgraph.types import Send
        todo = state.get("issues") or []
        if not todo:
            return [Send("noop", {})]
        return [Send("write_one", {"issue": i}) for i in todo]

    def noop(state):
        obs.log("no_article_issue_todo", level="info")
        return {}

    # ---------------------------------------------------------------- 单篇生成 + 回炉
    def _grounding(issue) -> str:
        if knowledge is None:
            return ""
        return "\n【事实来源】\n%s\n" % knowledge.grounding_block(issue.get("category") or "")

    def _write_prompt(issue):
        return ("你是 %s 官网的内容作者。按工单写一篇中文长文，输出结构化字段"
                "（title/slug/category/markdown）。\n\n"
                "品类：%s（category 字段必须填 %s）\n\n%s\n%s工单要求：\n%s\n\n工单标题：%s\n"
                % (site_mod.get(site, "site.name"), issue.get("category"),
                   issue.get("category"),
                   requirements(issue.get("category"), issue.get("is_product")),
                   _grounding(issue),
                   issue.get("body") or "(无正文要求)", issue.get("title")))

    def _fix_prompt(draft, violations, details, issue):
        return ("下面这篇文章没有通过质检门禁。请**整篇重写**并修掉全部失败项，"
                "然后输出完整文章（title/slug/category/markdown）。\n\n"
                "失败项：%s\n（详情：%s）\n\n%s\n%s\n待修文章：\n%s"
                % (", ".join(violations), details,
                   requirements(issue.get("category"), issue.get("is_product")),
                   _grounding(issue), draft.get("markdown", "")))

    def _assemble(draft, slug, issue):
        cat = draft.get("category") or issue.get("category") or \
            (allowed_cats[0] if allowed_cats else "uncategorized")
        fm = ("---\ntitle: %s\nslug: %s\ncategory: %s\nstatus: review\n---\n\n"
              % (draft.get("title"), draft.get("slug") or slug, cat))
        return fm + draft.get("markdown", "")

    def write_one(state):
        issue = state.get("issue") or {}
        num = issue.get("number")
        slug = issue.get("slug") or "untitled"
        draft, gate, rounds = None, None, 0
        path = pathlib.Path(repo.dir) / "output" / "articles" / ("%s.md" % slug)

        while rounds <= max_rounds:
            if rounds == 0:
                ok, value = llm_mod.structured(cfg, ArticleDraft, _write_prompt(issue),
                                               node="llm_write_article")
            else:
                ok, value = llm_mod.structured(
                    cfg, ArticleDraft,
                    _fix_prompt(draft, gate["violations"], gate["details"], issue),
                    node="llm_fix_article")
            if not ok:
                res = {"issue": num, "slug": slug, "status": "llm-failed", "rounds": rounds,
                       "quality": gate, "error": str(value)}
                return {"results": [res], "errors": ["#%s LLM 失败: %s" % (num, value)]}

            draft = value.model_dump()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_assemble(draft, slug, issue), encoding="utf-8")

            # 品类白名单校验（ArticleDraft.category 是 str，这里做可读校验）
            cat_ok, cat = quality.category_ok(path, site)
            gate = quality.quality_gate(path, site=site, knowledge=knowledge)
            if not cat_ok:
                gate = dict(gate)
                gate["pass"] = False
                gate["violations"] = list(gate["violations"]) + [
                    "category-allowlist(%s not in %s)" % (cat, allowed_cats)]
            obs.log("quality_gate", level="info", slug=slug, round=rounds,
                    passed=gate["pass"], violations=gate["violations"])
            if gate["pass"]:
                break
            rounds += 1

        res = {"issue": num, "slug": slug, "path": str(path), "quality": gate,
               "rounds": rounds, "status": "review" if gate["pass"] else "needs-revision",
               "char_count": (gate.get("details") or {}).get("total_chars")}
        out = {"results": [res]}
        if not gate["pass"]:
            out["errors"] = ["%s 质检 %d 轮未通过: %s"
                             % (slug, rounds, ", ".join(gate["violations"]))]
        return out

    # ---------------------------------------------------------------- 扇入收尾
    def comment_and_push(state):
        results = state.get("results") or []
        ok, issues = gitea.list_issues(state="open", label=label_article, limit=50)
        by_num = {i["number"]: i for i in (issues if ok else [])}
        created, errors, files = [], [], []
        for r in results:
            num = r.get("issue")
            if r["status"] == "review":
                body = ("## %s\n\n- 文件: `output/articles/%s.md`\n- 字数: %s\n"
                        "- 回炉轮次: %s\n- 质检: PASS\n- 下一步: 等待发布管道"
                        % (MARKER_3, r["slug"], r.get("char_count"), r.get("rounds")))
                gitea.comment(num, body)
                files.append("output/articles/%s.md" % r["slug"])
                src = by_num.get(num) or {}
                created.append({"number": num, "title": src.get("title"),
                                "assignees": [a.get("login") for a in (src.get("assignees") or [])],
                                "skipped": True})
            elif r["status"] == "needs-revision":
                gitea.comment(num, "## 质检未通过（%d 轮回炉后仍失败）\n\n- violations: %s\n\n"
                                   "status → needs-revision，需人工介入"
                              % (r.get("rounds", 0),
                                 ", ".join(((r.get("quality") or {}).get("violations") or []))))
                errors.append("#%s 质检 %s 轮未通过" % (num, r.get("rounds")))
        if files:
            repo.commit_files(files, "content: %d article(s) status=review (geo-agent)"
                              % len(files))
        out = {"new_issues": created,
               "generated": [r["slug"] for r in results if r["status"] == "review"]}
        if errors:
            out["errors"] = errors
        return out

    return {"pick_issue": pick_issue, "route_issues": route_issues, "noop": noop,
            "write_one": write_one, "comment_and_push": comment_and_push}
