"""Strapi v5 客户端（发布目标之一）。

写死的坑（都是实测得来的）：
  * 单资源 CRUD 用 `documentId`，不是数字 id（用数字 id 会 404）。
  * POST 必须包在 {"data": {...}} 里。
  * title/slug/category 必须 `.strip('"')` —— category 带引号是 400 硬失败。
  * 文章总数取 `meta.pagination.total`，**不要**数字组长度（默认 pageSize=25）。
  * 带方括号的 query 用 urllib 构造 / 或预编码 %5B%5D —— curl 会当 glob 静默失败。
  * 软删除残留：DELETE 返回 204 后 slug 唯一约束仍生效 → 重发前先查，存在则 PUT。

通用性：品类清单、路由前缀、语言前缀、凭据全部来自 site.yml，本文件不含具体站点信息。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .. import obs, site as site_mod
from ..config import get


class Strapi:
    def __init__(self, cfg: dict, read_only: bool = True, site: dict | None = None):
        self.site = site if site is not None else (cfg.get("_site") or {})
        # 站点档案优先；未提供时回落到 config.yml（向后兼容旧配置）
        self.base = (site_mod.get(self.site, "publish.base_url")
                     or get(cfg, "strapi.base_url") or "").rstrip("/")
        self.token = get(cfg, "strapi.token") or ""
        self.content_type = site_mod.get(self.site, "publish.content_type", "articles") \
            or "articles"
        self.category_field = site_mod.get(self.site, "publish.category_field", "category") \
            or "category"
        self.read_only = read_only
        # 🚫 硬锁：官网发布开关。默认 false，且必须同时满足 shadow.read_only=false + --apply。
        # 用户 2026-09-18 明确要求「不要发布到官网」——代码级默认关闭，改配置才能开。
        self.publish_enabled = bool(get(cfg, "publish.enabled", False))

    # ---------------------------------------------------------------- 品类校验
    @property
    def categories(self) -> set[str]:
        s = getattr(self, "site", None) or {}
        return set(site_mod.category_ids(s))

    # ---------------------------------------------------------------- 底层
    def _call(self, method, path, body=None, write=False):
        # 只读接口通常公开可访问；token 为空时也能跑抓取/校验类读取
        if write and path.startswith("/api/"):
            if not self.publish_enabled:
                obs.log("strapi_publish_blocked", level="warn", method=method, path=path,
                        reason="publish.enabled=false（官网发布默认关闭）")
                return True, {"_skipped": "publish_disabled", "method": method, "path": path}
        if write and self.read_only:
            obs.log("strapi_write_skipped", level="warn", method=method, path=path,
                    reason="read_only shadow mode")
            return True, {"_skipped": "read_only", "method": method, "path": path}
        url = self.base + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read().decode("utf-8", "replace")
                return True, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            return False, {"http": e.code, "detail": e.read().decode("utf-8", "replace")[:400]}
        except Exception as e:  # noqa: BLE001
            return False, {"error": "%s: %s" % (type(e).__name__, e)}

    # ---------------------------------------------------------------- 读
    def articles_total(self):
        """文章总数（分母）。取 meta.pagination.total；失败返回 None → 覆盖率记 UNKNOWN。"""
        ok, data = self._call(
            "GET", "/api/%s?pagination%%5BpageSize%%5D=1" % self.content_type)
        if not ok or not isinstance(data, dict):
            return None
        total = ((data.get("meta") or {}).get("pagination") or {}).get("total")
        return int(total) if isinstance(total, int) else None

    def find_by_slug(self, slug: str):
        """返回该 slug 的现有记录列表（含 documentId）。软删除残留也会被查到。"""
        ok, data = self._call(
            "GET", "/api/%s?filters%%5Bslug%%5D%%5B%%24eq%%5D=%s"
            % (self.content_type, urllib.parse.quote(slug)))
        if not ok or not isinstance(data, dict):
            return []
        return [d for d in (data.get("data") or []) if isinstance(d, dict)]

    def get_article(self, document_id: str):
        return self._call("GET", "/api/%s/%s" % (self.content_type, document_id))

    def article_url_ok(self, path: str):
        """验证某条路由是否 200。path 形如 /blog/<slug> 或 /en/blog/<slug>。"""
        req = urllib.request.Request(self.base + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:  # noqa: BLE001
            return 0

    def verify_slug_urls(self, slug: str) -> dict:
        """按站点的 locale 路由逐个验证，返回 {path: http_code}。"""
        out = {}
        for p in site_mod.article_urls(self.site, slug):
            out[p] = self.article_url_ok(p)
        return out

    # ---------------------------------------------------------------- 写
    def clean_fields(self, payload: dict) -> dict:
        """title/slug/category 剥引号 + 品类白名单 —— 代码级强制，不靠人记。"""
        out = dict(payload)
        cfield = getattr(self, "category_field", None) or "category"
        for field in ("title", "slug", cfield):
            v = out.get(field)
            if isinstance(v, str):
                out[field] = v.strip().strip('"').strip("'")
        cat = out.get(cfield)
        allowed = self.categories
        if cat is not None and allowed and cat not in allowed:
            raise ValueError("%s=%r 不在站点允许列表 %s 内（改 site.yml 的 categories 或修正文章）"
                             % (cfield, cat, sorted(allowed)))
        return out

    def create_article(self, payload: dict):
        return self._call("POST", "/api/%s" % self.content_type,
                          {"data": self.clean_fields(payload)}, write=True)

    def update_article(self, document_id: str, payload: dict):
        return self._call("PUT", "/api/%s/%s" % (self.content_type, document_id),
                          {"data": self.clean_fields(payload)}, write=True)


class NoopCms:
    """不发布的发布器（publish.cms: none）。

    用途：只跑生成与质检、不碰任何外部 CMS。保持与 Strapi 相同接口，
    这样管道 D 不需要分支判断。
    """

    def __init__(self, cfg: dict, read_only: bool = True, site: dict | None = None):
        self.site = site if site is not None else (cfg.get("_site") or {})
        self.read_only = read_only
        self.publish_enabled = False

    def articles_total(self):
        return None

    def find_by_slug(self, slug: str):
        return []

    def get_article(self, document_id: str):
        return True, {"_skipped": "cms_none"}

    def article_url_ok(self, path: str):
        return 0

    def verify_slug_urls(self, slug: str):
        return {p: 0 for p in site_mod.article_urls(self.site, slug)}

    def clean_fields(self, payload: dict) -> dict:
        return dict(payload)

    def create_article(self, payload: dict):
        obs.log("cms_none_skip", level="warn", action="create", slug=payload.get("slug"))
        return True, {"_skipped": "cms_none"}

    def update_article(self, document_id: str, payload: dict):
        obs.log("cms_none_skip", level="warn", action="update", slug=payload.get("slug"))
        return True, {"_skipped": "cms_none"}


def build(cfg: dict, read_only: bool = True, site: dict | None = None):
    """按 site.yml 的 publish.cms 选择发布器。"""
    s = site if site is not None else (cfg.get("_site") or {})
    kind = (site_mod.get(s, "publish.cms", "strapi") or "strapi").lower()
    if kind == "strapi":
        return Strapi(cfg, read_only=read_only, site=s)
    if kind in ("none", "", "null"):
        obs.log("cms_none_selected", level="warn",
                reason="site.yml 的 publish.cms=none —— 只生成不发布")
        return NoopCms(cfg, read_only=read_only, site=s)
    raise ValueError("未知的 publish.cms=%r（支持 strapi | none）" % kind)
