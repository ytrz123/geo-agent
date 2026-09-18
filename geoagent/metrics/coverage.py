"""纯函数层（一）：sitemap 覆盖率。

铁律（来自 2026-09-18 实测，历史踩坑见 geo-pipeline 记录）：
  * strapi 文章总数只能取 meta.pagination.total，不能数字组长度（默认 pageSize=25）。
  * sitemap 对每篇文章可能同时输出多语言 URL → 必须先按 slug 归一化去重。
    例：212 URLs = 101 篇文章 × 2 locale + 12 静态页 → 不去重会算出 198% 的假「超索引」。
  * 分母不可得（API 5xx / 网络失败）→ coverage = None，**不是 0**，且不计入均值。
  * coverage > 1.0 是良性伪影（分母口径过时），展示按 100% 处理但要保留 raw 值。

通用性：文章路由前缀与静态页清单**不写死**，由 site.yml 注入（见 geoagent/site.py）。
"""
from __future__ import annotations

from urllib.parse import urlsplit

# 兜底默认值：只在调用方完全没传配置时生效（保持函数可独立使用/可测试）
DEFAULT_ARTICLE_PREFIX = "/blog"
DEFAULT_STATIC_PATHS: set[str] = set()


def path_of(url: str) -> str:
    """取 URL 的 path（去掉 host / query / fragment / 末尾斜杠）。"""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    path = parts.path or ""
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]
    return path


def _prefix_segments(article_prefix: str) -> list[str]:
    pre = (article_prefix or DEFAULT_ARTICLE_PREFIX).strip("/")
    return [s for s in pre.split("/") if s] or ["blog"]


def article_slug(url: str, article_prefix: str = DEFAULT_ARTICLE_PREFIX) -> str | None:
    """从 URL 抽出文章 slug；非文章页返回 None。

    兼容多语言路由（都归一到同一个 slug）：
      https://<host>/blog/foo-bar        -> foo-bar
      https://<host>/en/blog/foo-bar/    -> foo-bar
      https://<host>/articles/foo-bar    -> foo-bar   （article_prefix=/articles 时）
    """
    path = path_of(url)
    if not path:
        return None
    segs = [s for s in path.split("/") if s]
    pre = _prefix_segments(article_prefix)
    # 在前缀可能被 locale 前缀顶开的窗口内找（如 /en/blog/<slug>）
    for start in range(0, min(len(segs), 2)):
        if segs[start:start + len(pre)] == pre:
            rest = segs[start + len(pre):]
            if not rest:
                return None  # 列表页本身是静态页
            return "/".join(rest)
    return None


def is_static(url: str, article_prefix: str = DEFAULT_ARTICLE_PREFIX) -> bool:
    return article_slug(url, article_prefix) is None


def unique_article_slugs(urls, article_prefix: str = DEFAULT_ARTICLE_PREFIX) -> set[str]:
    return {s for s in (article_slug(u, article_prefix) for u in (urls or [])) if s}


def unique_static_paths(urls, article_prefix: str = DEFAULT_ARTICLE_PREFIX) -> set[str]:
    return {path_of(u) for u in (urls or []) if is_static(u, article_prefix)}


def compute_coverage(sitemap_urls, strapi_total, static_pages: int | None = None,
                     article_prefix: str = DEFAULT_ARTICLE_PREFIX,
                     static_paths=None) -> dict:
    """算覆盖率。入参：sitemap.xml 的 <loc> 列表 + Strapi meta.pagination.total。

    static_pages / static_paths 只作为上报字段，**不参与分母**。
    """
    urls = [u for u in (sitemap_urls or []) if u and u.strip()]
    slugs = unique_article_slugs(urls, article_prefix)
    blog_url_count = sum(1 for u in urls if article_slug(u, article_prefix))
    statics = unique_static_paths(urls, article_prefix)
    declared_statics = set(static_paths or DEFAULT_STATIC_PATHS)

    total = strapi_total if isinstance(strapi_total, int) and strapi_total > 0 else None

    if total is None:
        coverage = None
        health = "UNKNOWN"
    else:
        coverage = len(slugs) / total
        if not slugs:
            health = "COLLAPSE"
        elif coverage >= 0.90:
            health = "OK"
        elif coverage > 0:
            health = "DEGRADED"
        else:
            health = "COLLAPSE"

    # 旧口径（分母 + 静态页数、不去重）留档：影子对拍时能直接看出口径差
    naive = None
    if total is not None:
        denom = total + (static_pages or len(declared_statics) or 4)
        naive = len(urls) / denom if denom else None

    return {
        "sitemap_urls": len(urls),
        "blog_urls": blog_url_count,
        "unique_article_slugs": len(slugs),
        "static_paths": len(statics),
        "static_pages": static_pages,
        "dual_locale": bool(slugs) and blog_url_count > len(slugs),
        "strapi_articles": total,
        "coverage": None if coverage is None else round(coverage, 4),
        "coverage_pct": None if coverage is None else round(min(coverage, 1.0) * 100, 2),
        "coverage_raw": None if coverage is None else round(coverage, 4),
        "over_index_artifact": bool(coverage is not None and coverage > 1.0),
        "naive_coverage_pct_legacy": None if naive is None else round(naive * 100, 2),
        "health": health,
        "article_prefix": article_prefix,
    }
