"""覆盖率纯函数回归。

用的是 2026-09-18 线上实测数字：sitemap 212 URLs（101 篇 × 2 locale + 12 静态）、
Strapi total 101 → 真实覆盖率 100%。旧口径会算出 187%~198% 的假「超索引」。
"""
from __future__ import annotations

import json

from geoagent.metrics.coverage import (article_slug, compute_coverage, unique_article_slugs,
                                      unique_static_paths)


def _site_urls(articles=101, locales=2, static=12):
    urls = []
    for i in range(articles):
        slug = "article-%03d" % i
        for loc in range(locales):
            urls.append("https://www.kamooc.cn/%sblog/%s" % ("en/" if loc else "", slug))
    statics = ["/", "/home", "/products", "/solutions", "/blog", "/about"]
    for s in statics[: static // 2]:
        urls.append("https://www.kamooc.cn" + (s if s != "/" else "/"))
        urls.append("https://www.kamooc.cn/en" + (s if s != "/" else "/"))
    return urls


def test_slug_extraction_both_locales():
    assert article_slug("https://www.kamooc.cn/blog/foo-bar") == "foo-bar"
    assert article_slug("https://www.kamooc.cn/en/blog/foo-bar/") == "foo-bar"
    assert article_slug("https://www.santiy-ai.com/blog/foo-bar") == "foo-bar"
    assert article_slug("https://www.kamooc.cn/blog") is None       # 列表页是静态页
    assert article_slug("https://www.kamooc.cn/products") is None


def test_dual_locale_real_numbers():
    urls = _site_urls(articles=101, locales=2, static=12)
    assert len(urls) == 214  # 202 + 12
    m = compute_coverage(urls, 101, static_pages=12)
    assert m["unique_article_slugs"] == 101
    assert m["coverage"] == 1.0
    assert m["health"] == "OK"
    assert m["dual_locale"] is True
    assert m["over_index_artifact"] is False


def test_naive_formula_would_be_wrong():
    """旧口径（不去重、分母 +4）必须被记录成假超索引 —— 这是这个模块存在的理由。"""
    urls = _site_urls(articles=101, locales=2, static=12)
    m = compute_coverage(urls, 101, static_pages=12)
    assert m["naive_coverage_pct_legacy"] > 150


def test_single_locale_sitemap_is_still_100pct():
    urls = _site_urls(articles=101, locales=1, static=12)
    m = compute_coverage(urls, 101, static_pages=12)
    assert m["coverage"] == 1.0
    assert m["dual_locale"] is False
    assert m["health"] == "OK"


def test_collapse_when_only_static_pages():
    urls = ["https://www.santiy-ai.com/", "https://www.santiy-ai.com/products",
            "https://www.santiy-ai.com/blog", "https://www.santiy-ai.com/about"]
    m = compute_coverage(urls, 101, static_pages=4)
    assert m["unique_article_slugs"] == 0
    assert m["health"] == "COLLAPSE"
    assert m["coverage"] == 0.0


def test_missing_denominator_is_none_not_zero():
    """Strapi 500 时分母不可得 → coverage=None（绝不能记 0 拉低周均值）。"""
    m = compute_coverage(_site_urls(), None)
    assert m["coverage"] is None
    assert m["health"] == "UNKNOWN"
    assert m["coverage_pct"] is None


def test_over_index_artifact_flagged():
    urls = _site_urls(articles=101, locales=2, static=12)
    m = compute_coverage(urls, 50, static_pages=12)
    assert m["coverage"] > 1.0
    assert m["over_index_artifact"] is True
    assert m["coverage_pct"] == 100.0        # 展示按 100% 封顶
    assert m["coverage_raw"] > 1.0


def test_degrated_band():
    urls = _site_urls(articles=50, locales=2, static=12)
    m = compute_coverage(urls, 101, static_pages=12)
    assert 0 < m["coverage"] < 0.90
    assert m["health"] == "DEGRADED"


def test_static_paths_deduped_across_locales():
    urls = _site_urls(articles=2, locales=2, static=12)
    statics = unique_static_paths(urls)
    # 6 个静态页 × zh/en 两个路由 = 12 个互不相同的 path（/en 与 / 是两个 path）
    assert len(statics) == 12
    assert {"/", "/products", "/blog", "/about"} <= statics
    assert {"/en", "/en/products", "/en/blog", "/en/about"} <= statics
    assert len(unique_article_slugs(urls)) == 2
