"""管道 D 节点：定时发布（0 LLM）。

每篇的处理顺序与旧管道一致：质检门禁 → md→HTML → slug 存量查重 → 发布 → URL 验证。

通用性：
  * 文章 URL 由 site.yml 的 routes 生成（不再写死域名与 /blog）
  * 品类白名单校验来自 site.yml
  * 发布器由 publish.cms 决定（strapi | none）
"""
from __future__ import annotations

from .. import obs, quality, site as site_mod
from ..config import get


def make_nodes(cfg, repo, gitea, cms, read_only=True, site=None, knowledge=None):
    site = site if site is not None else (cfg.get("_site") or {})
    label_article = site_mod.label(site, "article_generate", "article-generate")
    base_url = (site_mod.get(site, "site.base_url", "") or "").rstrip("/")
    allowed_cats = site_mod.category_ids(site)

    # ---------------------------------------------------------------- 同步 + 扫描
    def sync_repo(state):
        obs.node_start("sync_repo")
        ok = repo.sync()
        obs.node_done("sync_repo", ok=ok, head=repo.head())
        if not ok:
            return {"errors": ["repo sync failed"], "aborted": True}
        return {"repo_head": repo.head(), "workspace": str(repo.dir)}

    def scan(state):
        articles = quality.scan_review_articles(repo.dir)
        obs.node_done("scan", count=len(articles))
        return {"articles": articles}

    # ---------------------------------------------------------------- 单篇处理
    def process_article(state):
        art = state.get("article") or {}
        path, slug = art.get("path"), art.get("slug")
        obs.node_start("process_article", slug=slug)
        res = {"slug": slug, "path": path, "quality": None, "action": None,
               "http": None, "published": False}

        q = quality.quality_gate(path, site=site, knowledge=knowledge)
        cat_ok, cat = quality.category_ok(path, site)
        if not cat_ok:
            q = dict(q)
            q["pass"] = False
            q["violations"] = list(q["violations"]) + ["category-allowlist(%s)" % cat]
        res["quality"] = {"pass": q["pass"], "violations": q["violations"],
                          "details": q["details"]}
        if not q["pass"]:
            res["action"] = "needs-revision"
            obs.node_done("process_article", slug=slug, action="needs-revision",
                          violations=q["violations"])
            return {"results": [res]}

        payload = quality.md_to_payload(path)
        if "error" in payload:
            res["action"] = "convert-failed"
            obs.node_done("process_article", slug=slug, action="convert-failed")
            return {"results": [res], "errors": ["%s: md_to_html failed %s"
                                                % (slug, payload.get("error"))]}

        # 三字段剥引号 + 品类白名单（category 带引号/不合法都是硬失败）
        try:
            clean = cms.clean_fields({"title": payload.get("title"),
                                      "slug": payload.get("slug"),
                                      "category": payload.get("category", cat)})
        except ValueError as e:
            res["action"] = "invalid-category"
            return {"results": [res], "errors": ["%s: %s" % (slug, e)]}
        payload.update(clean)
        res["char_count"] = payload.get("char_count")
        res["title"] = payload.get("title")

        # 存量查重（软删除残留也会被查到 → 走 PUT，不重复 POST）
        existing = cms.find_by_slug(payload["slug"])
        if existing:
            doc = existing[0]
            res["existing_document_id"] = doc.get("documentId")
            res["action"] = "update"
            ok, data = cms.update_article(doc.get("documentId"), payload)
        else:
            res["action"] = "create"
            ok, data = cms.create_article(payload)
        res["http"] = data.get("http") if isinstance(data, dict) else None
        res["api_ok"] = bool(ok)
        if not ok:
            return {"results": [res], "errors": ["%s: publish failed %s" % (slug, data)]}

        res["skipped_reason"] = (data or {}).get("_skipped") if isinstance(data, dict) else None
        res["published"] = not res["skipped_reason"]
        # 逐条 locale 路由验证（路由由 site.yml 决定，不再假设 /blog 与 /en/blog）
        urls = cms.verify_slug_urls(payload["slug"])
        res["urls"] = urls
        res["url_zh"] = urls.get("/%s/%s" % (site_mod.article_prefix(site).strip("/"),
                                            payload["slug"]))
        obs.node_done("process_article", slug=slug, action=res["action"],
                      skipped=res["skipped_reason"], urls=urls)
        return {"results": [res]}

    def noop(state):
        obs.log("no_review_articles", level="info")
        return {}

    # ---------------------------------------------------------------- 收尾（扇入后各跑一次）
    def comment_and_close(state):
        results = state.get("results") or []
        ok, issues = gitea.list_issues(state="open", label=label_article, limit=50)
        issues = issues if ok else []
        new_issues, errors = [], []
        for r in results:
            slug = r.get("slug")
            if not r.get("published"):
                if r.get("action") == "needs-revision":
                    body = ("## 质量检查未通过\n\n- slug: `%s`\n- violations: %s\n\n"
                            "status → needs-revision"
                            % (slug, ", ".join((r.get("quality") or {}).get("violations") or [])))
                    for i in issues:
                        if slug and slug in (i.get("body") or ""):
                            gitea.comment(i["number"], body)
                            break
                    continue
                if r.get("skipped_reason"):
                    continue  # 影子期/发布器关闭：不评论，避免误导
            # 工单标题常不含 slug → 必须按 body 精确匹配，防把 ④ 评论错发到同批其他工单
            target = None
            for i in issues:
                if slug and slug in (i.get("body") or ""):
                    target = i
                    break
            if target is None:
                r["issue"] = None
                continue
            r["issue"] = target.get("number")
            urls_txt = " · ".join("%s: HTTP %s" % (u, c) for u, c in (r.get("urls") or {}).items())
            body = ("## ④ 发布成功\n\n"
                    "- 文章网址: %s/%s/%s\n"
                    "- 路由验证: %s\n"
                    "- 字数: %s | HTTP: %s | 动作: %s"
                    % (base_url, site_mod.article_prefix(site).strip("/"), slug,
                       urls_txt or "（无路由配置）",
                       r.get("char_count"), r.get("http"), r.get("action")))
            ok2, _ = gitea.comment(target["number"], body)
            if ok2:
                gitea.close_issue(target["number"])
                new_issues.append({"number": target.get("number"),
                                   "title": target.get("title"),
                                   "assignees": [a.get("login")
                                                 for a in (target.get("assignees") or [])]})
            else:
                errors.append("comment failed on #%s" % target.get("number"))
        obs.node_done("comment_and_close", commented=len(new_issues), errors=len(errors))
        out = {"new_issues": new_issues,
               "published": [r["slug"] for r in results if r.get("published")]}
        if errors:
            out["errors"] = errors
        return out

    def writeback_status(state):
        changed = []
        for r in state.get("results") or []:
            if r.get("published") and r.get("path"):
                if quality.writeback_published(r["path"]):
                    changed.append(r["path"])
        obs.node_done("writeback_status", changed=len(changed))
        return {"status_changed": changed}

    def git_commit_push(state):
        changed = state.get("status_changed") or []
        if not changed:
            return {}
        rel = []
        for p in changed:
            s = str(p)
            if "/output/" in s:
                rel.append("output/" + s.split("/output/", 1)[1])
            else:
                rel.append(s)
        repo.commit_files(rel, "publish: %d article(s) status→published" % len(rel))
        return {"git": {"added": rel, "head": repo.head()}}

    return {
        "sync_repo": sync_repo, "scan": scan, "process_article": process_article,
        "noop": noop, "comment_and_close": comment_and_close,
        "writeback_status": writeback_status, "git_commit_push": git_commit_push,
    }
