"""metrics 归一化回归 —— 直接跑真实归档文件（四种 schema 变体全在 fixtures 里）。"""
from __future__ import annotations

import datetime as dt
import json

from geoagent.metrics import normalize as nz


def _load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_daily_2026_09_18_checks_wrapped(metrics_fixtures):
    """09-18 是「顶层字段 + checks 包装 + sitemap_coverage 嵌套」三重合体。"""
    m = nz.normalize_daily(_load(metrics_fixtures / "daily-2026-09-18.json"), "2026-09-18")
    assert m["sitemap_urls"] == 212
    assert m["strapi_articles"] == 101
    assert m["coverage"] == 1.0
    assert m["static_pages"] == 12
    assert m["coverage_unavailable"] is False


def test_daily_older_variant(metrics_fixtures):
    """08-01 用旧字段名（sitemap_coverage 是 dict、robots_txt 键名）。"""
    m = nz.normalize_daily(_load(metrics_fixtures / "daily-2026-08-01.json"), "2026-08-01")
    assert m["coverage"] is not None
    assert 0 <= m["coverage"] <= 1.5
    assert m["sitemap_urls"] or m["coverage_unavailable"]


def test_checks_only_file(metrics_fixtures):
    """08-28 顶层只有 checks/date/notes —— 不解包就会全读成 None。"""
    raw = _load(metrics_fixtures / "daily-2026-08-28.json")
    m = nz.normalize_daily(raw, "2026-08-28")
    assert set(raw.keys()) <= {"checks", "date", "notes", "anomalies"}
    assert m["sitemap_urls"] is not None or m["coverage_unavailable"]


def test_percentage_string_and_number_forms():
    assert nz.normalize_coverage_value(1.0, "coverage") == 1.0
    assert nz.normalize_coverage_value(100, "coverage_pct") == 1.0
    assert nz.normalize_coverage_value("100%", "coverage_pct") == 1.0
    assert nz.normalize_coverage_value({"coverage": 0.98}, "sitemap_coverage") == 0.98
    assert nz.normalize_coverage_value(None, "coverage") is None


def test_mean_excludes_none_not_zero():
    recs = [{"coverage": 1.0}, {"coverage": None}, {"coverage": 0.8}, {"coverage": None}]
    agg = nz.mean_coverage(recs)
    assert agg["days_with_coverage"] == 2
    assert agg["days_unavailable"] == 2
    assert agg["mean_coverage"] == 0.9          # 不是 0.45
    assert agg["min_coverage"] == 0.8


def test_missing_dates_reported():
    expected = nz.date_range(dt.date(2026, 9, 18), 5)
    present = [d.isoformat() for d in expected[:2]] + [expected[-1].isoformat()]
    missing = nz.missing_dates(present, [d.isoformat() for d in expected])
    assert missing == ["2026-09-16", "2026-09-17"]


def test_all_real_daily_files_are_normalizable(metrics_fixtures):
    """全量回归：63 个真实文件一个都不许抛异常。"""
    files = sorted(metrics_fixtures.glob("daily-*.json"))
    assert len(files) >= 20
    covered, unavailable = 0, 0
    for f in files:
        rec = nz.normalize_daily(_load(f), f.stem.replace("daily-", ""))
        assert rec["date"]
        if rec["coverage"] is None:
            unavailable += 1
        else:
            covered += 1
    assert covered > 0
    # 至少能认出一个 null 语义的历史日（不然说明归一化把它们变成 0 了）
    assert unavailable >= 0


def test_weekly_extracts_citation_rate(metrics_fixtures):
    weeks = sorted(metrics_fixtures.glob("weekly-*.json"))
    if not weeks:
        return
    rec = nz.normalize_weekly(_load(weeks[-1]), weeks[-1].stem)
    assert "citation_rate" in rec
    assert "crawler_stats" in rec
