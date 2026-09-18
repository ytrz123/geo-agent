#!/usr/bin/env python3
"""Locale-aware sitemap coverage for kamooc.cn / santiy-ai.com.

Why this script exists (2026-09-18):
  The sitemap emits BOTH zh and /en variants for every blog article
  (212 URLs = 101 articles x 2 locales + 12 static pages).  Naively doing
  `sitemap_urls - static_pages` / strapi_articles gives 200/101 = 198% and
  looks like a catastrophic over-index.  Deduplicating by slug fixes it.

Usage:
    python3 scripts/sitemap_coverage.py            # live fetch
    python3 scripts/sitemap_coverage.py --offline  # reads /tmp/sitemap_daily.xml
                                                   # + /tmp/articles_daily.json

Prints one JSON object to stdout.  Exit 0 always (report, don't crash cron).

Notes / pitfalls baked in:
  * curl and `[...]` globbing: query params like pagination[pageSize] make curl
    silently fail (exit 1, no -o file).  We use urllib here, so it's moot --
    but if you ever shell out, add -g or URL-encode as %5B/%5D.
  * Strapi total MUST come from meta.pagination.total, never len(data):
    default pageSize=25 makes len(data) <= 25 while the corpus is 100+.
  * Both single-locale (sitemap ~= articles + 12) and dual-locale
    (sitemap ~= 2*articles + 12) sitemaps are healthy -- coverage is 100% either
    way.  Only a sitemap containing just the static pages is the collapse signal.
"""
import argparse
import json
import re
import sys
import urllib.request

# ---- LEGACY DEFAULTS (仅当不传参数时生效；geo-agent 一律从 site.yml 传参覆盖) ----
SITE = "https://www.kamooc.cn"
LOCALES = ("https://www.kamooc.cn", "https://www.santiy-ai.com")

# Static (non-article) pages, zh + en variants
STATIC_SLUGS = {
    "", "home", "products", "solutions", "blog", "about",
    "en", "en/home", "en/products", "en/solutions", "en/blog", "en/about",
}
# ---- END LEGACY DEFAULTS ----


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "geo-seo-cron/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def sitemap_urls(offline):
    if offline:
        xml = open("/tmp/sitemap_daily.xml", encoding="utf-8", errors="replace").read()
    else:
        xml = fetch(f"{SITE}/sitemap.xml")
    return re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)


def strapi_total(offline):
    if offline:
        payload = json.load(open("/tmp/articles_daily.json"))
    else:
        # No pagination params on purpose: fewer moving parts, and the total
        # lives in meta.pagination regardless of page size.
        payload = json.loads(fetch(f"{SITE}/api/articles"))
    total = payload.get("meta", {}).get("pagination", {}).get("total")
    if total is None:
        raise SystemExit("no meta.pagination.total in /api/articles response")
    return int(total)


def article_slugs(urls):
    """Normalise every blog URL down to a bare slug, across all locales."""
    slugs = set()
    for u in urls:
        path = u
        for base in LOCALES:
            if path.startswith(base):
                path = path[len(base):]
                break
        path = path.strip("/")
        if path.startswith("blog/"):
            slug = path[len("blog/"):].strip("/")
            if slug:
                slugs.add(slug)
    return slugs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="read /tmp/sitemap_daily.xml + /tmp/articles_daily.json")
    args = ap.parse_args()

    urls = sitemap_urls(args.offline)
    total = strapi_total(args.offline)
    slugs = article_slugs(urls)

    # Count non-article URLs (both locales' home page included). Every sitemap
    # entry that isn't an article URL is by construction a static page.
    static_present = 0
    for u in urls:
        path = u
        for base in LOCALES:
            if path.startswith(base):
                path = path[len(base):]
                break
        path = path.strip("/")
        if not path.startswith("blog/"):
            static_present += 1

    sitemap_total = len(urls)
    coverage = round(len(slugs) / total, 4) if total else None

    # Health classification
    if len(slugs) == 0 and sitemap_total <= 16:
        health = "COLLAPSE"          # static-only sitemap -> the rebrand-era bug
    elif coverage is not None and coverage >= 0.90:
        health = "OK"
    else:
        health = "DEGRADED"

    locales = "dual" if sitemap_total > total * 1.5 else "single"

    out = {
        "sitemap_urls": sitemap_total,
        "static_urls_present": static_present,
        "static_pages": len(STATIC_SLUGS),
        "unique_article_slugs": len(slugs),
        "strapi_articles": total,
        "coverage": coverage,
        "coverage_pct": None if coverage is None else f"{coverage:.1%}",
        "locales": locales,
        "missing_from_sitemap": total - len(slugs),
        "health": health,
        "naive_coverage_TRAP": round(sitemap_total / (total + 4), 4),
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
