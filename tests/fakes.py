"""测试替身：把生产客户端换成记录调用的假对象。

graph.build() 接受注入的 repo/gitea/strapi，所以整条图可以在不碰生产的前提下跑通写路径。
"""
from __future__ import annotations

import pathlib

from geoagent.clients.gitea import Gitea
from geoagent.clients.strapi import Strapi


class FakeRepo:
    def __init__(self, root, read_only=True):
        self.dir = pathlib.Path(root)
        self.read_only = read_only
        self.calls = []

    def bootstrap(self):
        return True

    def sync(self):
        return True

    def head(self):
        return "fake-head"

    def out_path(self, rel):
        p = self.dir / "out" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def add(self, rels):
        self.calls.append(("add", [str(r) for r in rels]))
        return True

    def commit(self, message):
        self.calls.append(("commit", message))
        return True

    def push(self):
        self.calls.append(("push",))
        return True

    def commit_files(self, rels, message):
        self.calls.append(("commit_files", [str(r) for r in rels], message))
        return True


class FakeGitea(Gitea):
    def __init__(self, issues=None, comments=None, files=None):
        # 刻意不调 super().__init__：不发任何网络请求
        self.issues = issues or []
        self._comments = comments or {}
        self.files = files or {}
        self.created = []
        self.commented = []
        self.closed = []
        self._next = 1000

    def list_issues(self, state="open", label=None, limit=50, sort="updated"):
        out = [i for i in self.issues if i.get("state", "open") == state]
        if label:
            out = [i for i in out
                   if label in [lb.get("name") for lb in (i.get("labels") or [])]]
        return True, out

    def create_issue(self, title, body, labels=None, assignees=None):
        self._next += 1
        rec = {"number": self._next, "title": title, "body": body, "state": "open",
               "labels": [{"name": lb} for lb in (labels or [])],
               "assignees": [{"login": a} for a in (assignees or [])]}
        self.issues.append(rec)
        self.created.append(rec)
        return True, rec

    def comment(self, number, body):
        self.commented.append((number, body))
        self._comments.setdefault(number, []).append({"body": body})
        return True, {"id": len(self.commented)}

    def close_issue(self, number):
        self.closed.append(number)
        for i in self.issues:
            if i.get("number") == number:
                i["state"] = "closed"
        return True, {"state": "closed"}

    def comments(self, number):
        return self._comments.get(number, [])

    def file_content(self, path, ref="main"):
        if path in self.files:
            return True, self.files[path]
        return False, {"http": 404}

    def label_id(self, name):
        return 1


class FakeStrapi(Strapi):
    """发布器替身。刻意不调 super().__init__() —— 不发任何网络请求。

    字段与 Strapi 保持一致，这样 clean_fields 的品类校验/剥引号逻辑在测试里也真实生效。
    """

    def __init__(self, existing=None, url_ok=200, site=None, cfg=None):
        self.existing = existing or {}     # slug -> [record]
        self.created, self.updated = [], []
        self.url_ok = url_ok
        self.site = site or {}
        self.content_type = "articles"
        self.category_field = "category"
        self.read_only = True
        self.publish_enabled = bool((cfg or {}).get("publish", {}).get("enabled", False))

    def verify_slug_urls(self, slug):
        import geoagent.site as site_mod
        return {p: self.url_ok for p in site_mod.article_urls(self.site, slug)}

    def find_by_slug(self, slug):
        return self.existing.get(slug, [])

    def create_article(self, payload):
        self.created.append(payload)
        return True, {"http": 201, "data": {"documentId": "doc-new"}}

    def update_article(self, document_id, payload):
        self.updated.append((document_id, payload))
        return True, {"http": 200, "data": {"documentId": document_id}}

    def article_url_ok(self, path):
        return self.url_ok
