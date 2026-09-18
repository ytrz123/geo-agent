"""纯函数层（二）：metrics 归一化。

daily-*.json 在历史上并存四个变体（W32/W34/W36/W38 实测），聚合前必须归一化，
否则全部读成 None 或者 TypeError。变体：
  ① 顶层字段            {sitemap_urls, strapi_articles, coverage: 1.0}
  ② 嵌套 sitemap 子键    {sitemap: {url_count: N}}
  ③ 更旧字段名          {strapi_articles_total, coverage_pct: 100}
  ④ checks 包装层        {checks: {sitemap_coverage: {...}, robots: {...}}}
另：coverage: null ≠ 0%。Strapi 500 使分母不可得时该日必须从均值里剔除。
"""
from __future__ import annotations

import datetime as _dt

# 字段别名表：值的取法按「顶层 → 子键 → 嵌套 dict → checks 包装」顺序尝试
ALIASES = {
    "sitemap_urls": [
        ("sitemap_urls",), ("sitemap", "url_count"), ("sitemap", "urls"),
        ("checks", "sitemap_coverage", "sitemap_urls"), ("checks", "sitemap", "url_count"),
        ("sitemap_coverage", "sitemap_urls"),
    ],
    "strapi_articles": [
        ("strapi_articles",), ("strapi_articles_total",),
        ("checks", "sitemap_coverage", "strapi_articles"), ("sitemap_coverage", "strapi_articles"),
    ],
    "coverage": [
        ("coverage",), ("coverage_pct",),
        ("checks", "sitemap_coverage", "coverage"), ("checks", "sitemap_coverage", "coverage_pct"),
        ("sitemap_coverage", "coverage"), ("sitemap_coverage",),
    ],
    "static_pages": [
        ("static_pages",), ("checks", "sitemap_coverage", "static_pages"),
        ("sitemap_coverage", "static_pages"),
    ],
    "robots": [
        ("robots",), ("robots_txt",), ("checks", "robots"), ("checks", "robots_txt"),
    ],
    "jsonld": [
        ("jsonld",), ("json_ld",), ("ld_json",),
        ("checks", "jsonld"), ("checks", "json_ld"), ("checks", "ld_json"),
    ],
    "lcp": [
        ("lcp",), ("core_web_vitals", "lcp"), ("checks", "core_web_vitals", "lcp"),
    ],
}

# weekly 专有（daily 里没有爬虫统计和引用率，别去 daily 找）
WEEKLY_ALIASES = {
    "crawler_stats": [("crawler_stats",), ("crawler_logs",), ("checks", "crawler_stats")],
    "citation_results": [("citation_results",), ("geo_citation",), ("checks", "citation_results")],
    "citation_rate": [
        ("citation_rate",), ("analysis", "citation_rate"),
        ("geo_citation", "citation_rate"), ("checks", "citation_rate"),
    ],
}


def dig(data, path):
    cur = data
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def unwrap_checks(data: dict) -> dict:
    """有些文件把全部检查项包在 checks 下 —— 解包一层，同时保留原键。"""
    if isinstance(data, dict) and isinstance(data.get("checks"), dict):
        merged = dict(data["checks"])
        for k, v in data.items():
            if k != "checks":
                merged.setdefault(k, v)
        return merged
    return data or {}


def coerce_number(v):
    """把 '100%' / '1.0' / 100 / None 归一化成 float 或 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().rstrip("%")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def pick(data: dict, aliases) -> tuple:
    """按别名表取值，返回 (值, 命中的路径字符串)。"""
    for path in aliases:
        v = dig(data, path)
        if v is not None:
            return v, ".".join(path)
    return None, None


def normalize_coverage_value(v, source_key: str | None):
    """coverage 可能是数字、可能是嵌套 dict、也可能是 0-100 的百分数。"""
    if isinstance(v, dict):
        v = v.get("coverage", v.get("coverage_pct"))
    num = coerce_number(v)
    if num is None:
        return None
    # coverage_pct / 明显是百分数的值 → 除以 100
    if source_key and ("pct" in source_key or num > 1.5):
        num = num / 100.0
    return round(num, 4)


def normalize_daily(data: dict, date: str | None = None) -> dict:
    """归一化一份 daily metrics。"""
    raw = data or {}
    d = unwrap_checks(raw)
    out = {"date": date or raw.get("date") or raw.get("_date"), "raw_keys": sorted(raw.keys())}

    v, k = pick(d, ALIASES["sitemap_urls"])
    out["sitemap_urls"] = int(v) if isinstance(v, (int, float)) else None
    out["sitemap_urls_from"] = k

    v, k = pick(d, ALIASES["strapi_articles"])
    out["strapi_articles"] = int(v) if isinstance(v, (int, float)) else None
    out["strapi_articles_from"] = k

    v, k = pick(d, ALIASES["coverage"])
    out["coverage"] = normalize_coverage_value(v, k)
    out["coverage_from"] = k
    out["coverage_unavailable"] = out["coverage"] is None

    v, _ = pick(d, ALIASES["static_pages"])
    out["static_pages"] = int(v) if isinstance(v, (int, float)) else None

    v, _ = pick(d, ALIASES["robots"])
    out["robots"] = v
    v, _ = pick(d, ALIASES["jsonld"])
    out["jsonld"] = v
    v, _ = pick(d, ALIASES["lcp"])
    out["lcp"] = coerce_number(v)

    out["notes"] = raw.get("notes")
    return out


def normalize_weekly(data: dict, week: str | None = None) -> dict:
    raw = data or {}
    d = unwrap_checks(raw)
    out = normalize_daily(raw, week)
    for name, aliases in WEEKLY_ALIASES.items():
        v, k = pick(d, aliases)
        out[name] = coerce_number(v) if name.endswith("rate") else v
    return out


def mean_coverage(records) -> dict:
    """算覆盖率均值。null 明确剔除并单独计数 —— 绝不能当 0 拉低均值。"""
    vals, unavailable, zero = [], 0, 0
    for r in records:
        c = r.get("coverage")
        if c is None:
            unavailable += 1
            continue
        if c == 0:
            zero += 1
        vals.append(c)
    return {
        "mean_coverage": round(sum(vals) / len(vals), 4) if vals else None,
        "days_with_coverage": len(vals),
        "days_unavailable": unavailable,
        "days_zero": zero,
        "min_coverage": round(min(vals), 4) if vals else None,
        "max_coverage": round(max(vals), 4) if vals else None,
    }


def date_range(end: _dt.date, days: int):
    return [end - _dt.timedelta(days=i) for i in range(days - 1, -1, -1)]


def missing_dates(present_dates, expected_dates) -> list[str]:
    """缺档日（cron 空档/未 push）。缺档日既不算 0 也不算异常，只在报告里标注。"""
    have = {str(d) for d in present_dates}
    return [str(d) for d in expected_dates if str(d) not in have]
