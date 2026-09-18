#!/usr/bin/env python3
"""影子对拍：把 geo-agent 的产物与线上归档逐字段比。

规则（写死在这里，不靠人记）：
  * 允许差异：时间戳、措辞、markdown 空格、notes 文案。
  * 不允许差异：所有数字（覆盖率/篇数/条数/字数）、health、dual_locale、strapi 分母。
  * 影子期新系统只读 —— 它写的是 var/out/，线上文件来自 workspace clone。

用法：
    .venv/bin/python tools_shadow_diff.py                # 对当天 daily
    .venv/bin/python tools_shadow_diff.py --date 2026-09-18
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from geoagent.metrics import normalize as nz  # noqa: E402

NUMERIC_KEYS = ["sitemap_urls", "unique_article_slugs", "strapi_articles", "static_pages"]
BOOL_KEYS = ["dual_locale", "over_index_artifact"]
TEXT_KEYS = ["health"]


def _load(p: pathlib.Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--workspace", default=str(ROOT / "var" / "workspaces" / "geo-seo"))
    args = ap.parse_args()

    ours_p = ROOT / "var" / "out" / "metrics" / ("daily-%s.json" % args.date)
    theirs_p = pathlib.Path(args.workspace) / "metrics" / ("daily-%s.json" % args.date)

    ours_raw, theirs_raw = _load(ours_p), _load(theirs_p)
    print("=" * 68)
    print("影子对拍 · daily-%s" % args.date)
    print("=" * 68)
    print("geo-agent 产物 : %s  %s" % (ours_p, "存在" if ours_raw else "缺失"))
    print("线上归档       : %s  %s" % (theirs_p, "存在" if theirs_raw else "缺失"))
    if not ours_raw or not theirs_raw:
        print("\n无法对拍（缺一侧）。提示：先跑 `cli.py seo-daily`，并确保 workspace 已 clone。")
        return 1

    ours = nz.normalize_daily(ours_raw, args.date)
    theirs = nz.normalize_daily(theirs_raw, args.date)

    rows, extra_rows, hard_fail = [], [], 0

    def _present(v):
        return v is not None

    for k in NUMERIC_KEYS + TEXT_KEYS + BOOL_KEYS + ["coverage"]:
        a, b = ours.get(k), theirs.get(k)
        if not _present(b) and not _present(a):
            continue
        if not _present(b):
            extra_rows.append((k, a, "—（线上无此字段）", "新增"))
            continue
        if not _present(a):
            hard_fail += 1
            rows.append((k, a, b, "DIFF"))
            continue
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            ok = abs(float(a) - float(b)) <= 1e-9
        else:
            ok = (str(a) == str(b))
        if not ok:
            hard_fail += 1
        rows.append((k, a, b, "OK" if ok else "DIFF"))

    # 覆盖率不可判定性也要一致（None vs 0 是语义差异，不是格式差异）
    cov_ours, cov_theirs = ours.get("coverage"), theirs.get("coverage")
    if (cov_ours is None) != (cov_theirs is None) and "coverage" not in [r[0] for r in rows]:
        hard_fail += 1
        rows.append(("coverage(可判定性)", cov_ours, cov_theirs, "DIFF"))

    print("\n%-24s %-14s %-14s %s" % ("字段", "geo-agent", "线上", "结论"))
    print("-" * 68)
    for k, a, b, verdict in rows:
        print("%-24s %-14s %-14s %s" % (k, a, b, verdict))
    if extra_rows:
        print("-" * 68)
        for k, a, b, verdict in extra_rows:
            print("%-24s %-14s %-14s %s" % (k, a, b, verdict))

    print("-" * 68)
    if hard_fail == 0:
        print("结论：%d 项硬指标全部一致 → 本日对拍通过" % len(rows))
    else:
        print("结论：%d 项硬指标不一致 → 以工单为准，人工裁决，不得自动覆盖线上结论" % hard_fail)

    print("\n允许差异项（不参与判定）：时间戳 / 措辞 / notes 文案 / 字段书写顺序")
    print("geo-agent 额外留档：unique_article_slugs=%s · over_index_artifact=%s · "
          "naive_coverage_pct_legacy=%s"
          % (ours.get("unique_article_slugs"), ours.get("over_index_artifact"),
             (ours_raw or {}).get("over_index_artifact")
             if "naive_coverage_pct_legacy" not in (ours_raw or {})
             else ours_raw.get("naive_coverage_pct_legacy")))
    return 0 if hard_fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
