"""Gitea 工单/仓库 API 客户端。

写死的坑（都来自历史事故）：
  * 标签查询必须用**标签名**，不能用数字 ID —— ?labels=71 在这版 Gitea 里是模糊文本搜索，
    曾一次性把所有 open issue 都返回并导致误关。
  * admin token 也可能 403 → 一律降级 basic auth。
  * 关闭 Issue 需要 admin 权限，三人 token 会 403 → 关闭固定走 basic auth。
  * 写操作在只读（影子）模式下全部短路，只记录 would_* 到 trace。
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from .. import obs
from ..config import get

TIMEOUT = 20


class GiteaError(RuntimeError):
    pass


class Gitea:
    def __init__(self, cfg: dict, read_only: bool = True):
        self.base = str(get(cfg, "gitea.base_url")).rstrip("/")
        self.repo = get(cfg, "gitea.repo")
        self.auth_mode = (get(cfg, "gitea.auth_mode") or "basic").lower()
        self.user = get(cfg, "gitea.user")
        self.password = get(cfg, "gitea.password")
        self.token = get(cfg, "gitea.token")
        self.read_only = read_only
        self._labels_cache = None

    # ---------------------------------------------------------------- 底层
    def _auth_header(self) -> str:
        if self.auth_mode == "token" and self.token:
            return "token " + self.token
        creds = base64.b64encode(("%s:%s" % (self.user, self.password)).encode()).decode()
        return "Basic " + creds

    def _call(self, method: str, path: str, body=None, params=None, write: bool = False):
        """返回 (ok, data_or_error)。不抛异常 —— 调用方永远拿到结构化结果。"""
        if write and self.read_only:
            obs.log("gitea_write_skipped", level="warn", method=method, path=path,
                    reason="read_only shadow mode")
            return True, {"_skipped": "read_only", "method": method, "path": path}
        url = "%s/api/v1/repos/%s%s" % (self.base, self.repo, path)
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth_header())
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8", "replace")
                return True, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            # 权限不足 → 用 basic auth 重试一次（token 403 的历史解法）
            if e.code == 403 and self.auth_mode == "token" and not write:
                obs.log("gitea_token_403_fallback_basic", level="warn", path=path)
                return self._call_basic(method, url, data, write)
            return False, {"http": e.code, "detail": detail}
        except Exception as e:  # noqa: BLE001
            return False, {"error": "%s: %s" % (type(e).__name__, e)}

    def _call_basic(self, method, url, data, write):
        creds = base64.b64encode(("%s:%s" % (self.user, self.password)).encode()).decode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Basic " + creds)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8", "replace")
                return True, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            return False, {"http": e.code, "detail": e.read().decode("utf-8", "replace")[:300]}
        except Exception as e:  # noqa: BLE001
            return False, {"error": "%s: %s" % (type(e).__name__, e)}

    # ---------------------------------------------------------------- 读
    def labels(self):
        if self._labels_cache is None:
            ok, data = self._call("GET", "/labels")
            self._labels_cache = data if ok else []
        return self._labels_cache

    def label_id(self, name: str):
        for lb in self.labels():
            if lb.get("name") == name:
                return lb.get("id")
        return None

    def list_issues(self, state="open", label: str | None = None, limit=50, sort="updated"):
        """按状态/标签名列工单。label 传**标签名**。

        返回 (ok, issues)。并对每条 issue 校验 labels 数组真的含目标标签名 —— 防模糊匹配。
        """
        params = {"state": state, "limit": limit, "sort": sort}
        if label:
            params["labels"] = label
        ok, data = self._call("GET", "/issues", params=params)
        if not ok:
            return False, data
        issues = [i for i in (data or []) if isinstance(i, dict)]
        if label:
            exact = [i for i in issues
                     if label in [lb.get("name") for lb in (i.get("labels") or [])]]
            dropped = len(issues) - len(exact)
            if dropped:
                obs.log("gitea_label_fuzzy_dropped", level="warn", label=label, dropped=dropped)
            issues = exact
        return True, issues

    def get_issue(self, number: int):
        return self._call("GET", "/issues/%d" % number)

    def comments(self, number: int):
        ok, data = self._call("GET", "/issues/%d/comments" % number)
        return (data if ok else []) if isinstance(data, list) else []

    def has_comment_marker(self, number: int, marker: str) -> bool:
        """防重复守卫：工单里已含某标记（如「③ 内容生成完毕」）就跳过重复施工。"""
        return any(marker in (c.get("body") or "") for c in self.comments(number))

    def file_content(self, path: str, ref: str = "main"):
        """读仓库文件（策略 YAML 等）—— 不依赖本地 clone，绕开共享副本损坏那类坑。"""
        ok, data = self._call("GET", "/contents/" + path.lstrip("/"), params={"ref": ref})
        if not ok or not isinstance(data, dict) or "content" not in data:
            return False, data
        try:
            return True, base64.b64decode(data["content"]).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return False, {"error": str(e)}

    def dir_listing(self, path: str = "", ref: str = "main"):
        ok, data = self._call("GET", "/contents/" + path.lstrip("/"), params={"ref": ref})
        if not ok or not isinstance(data, list):
            return False, data
        return True, [{"name": i.get("name"), "type": i.get("type"), "size": i.get("size")}
                      for i in data]

    # ---------------------------------------------------------------- 写（只读模式短路）
    def create_issue(self, title: str, body: str, labels=None, assignees=None):
        payload = {"title": title, "body": body}
        # 只读模式先短路：连「查标签 ID」这种读请求都不该发（否则影子跑会白打生产）
        if self.read_only:
            obs.log("gitea_write_skipped", level="warn", method="POST", path="/issues",
                    reason="read_only shadow mode")
            return True, {"_skipped": "read_only", "method": "POST", "path": "/issues"}
        ids = []
        for name in (labels or []):
            lid = self.label_id(name)
            if lid is None:
                obs.log("gitea_label_missing", level="warn", label=name)
                continue
            ids.append(lid)
        if ids:
            payload["labels"] = ids
        if assignees:
            payload["assignees"] = assignees
        return self._call("POST", "/issues", payload, write=True)

    def comment(self, number: int, body: str):
        return self._call("POST", "/issues/%d/comments" % number, {"body": body}, write=True)

    def close_issue(self, number: int):
        # 关闭需 admin：固定 basic auth（三人 token 会 403）
        if self.read_only:
            obs.log("gitea_write_skipped", level="warn", path="/issues/%d" % number,
                    reason="read_only shadow mode")
            return True, {"_skipped": "read_only"}
        return self._call_basic("PATCH", "%s/api/v1/repos/%s/issues/%d"
                                % (self.base, self.repo, number),
                                json.dumps({"state": "closed"}).encode(), write=True)

    def patch_issue(self, number: int, payload: dict):
        return self._call("PATCH", "/issues/%d" % number, payload, write=True)


# ------------------------------------------------------------------ 工单去重
def find_similar_open_issue(issues, label: str, title_keywords=None, body_contains=None):
    """在 open 工单里找同题工单。

    规则：同标签 + （标题含任一关键词 或 body 含指定子串）。
    —— 命中就评论更新，不新建（历史每周重复建单的修复）。
    """
    for i in issues or []:
        labels = [lb.get("name") for lb in (i.get("labels") or [])]
        if label and label not in labels:
            continue
        title = i.get("title") or ""
        body = i.get("body") or ""
        if title_keywords and any(k in title for k in title_keywords):
            return i
        if body_contains and body_contains in body:
            return i
    return None
