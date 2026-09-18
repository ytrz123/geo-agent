#!/usr/bin/env python3
"""Quality gate checker for GEO-enhanced articles.

⚠️ 本文件是上游脚本（geo-seo-pipeline skill 的 scripts/verify_article_quality.py）的
   定制副本：**所有阈值与品牌水印已参数化**。不传参数时行为与上游完全一致（向后兼容），
   geo-agent 调用时会按 site.yml 传入站点自己的水印与阈值。

Usage: python3 verify_article_quality.py <path/to/article.md>
       python3 verify_article_quality.py <article.md> --watermark "某品牌引擎" --watermark-min 2
       python3 verify_article_quality.py <article.md> --min-chars 1200 --min-h2 3

Checks all mandatory requirements from the v2 article template (fixed 2026-08-30:
no-th-td-mix scoped per <tr> row, FAQ gate accepts colon-optional "### Q1"):
  - Total chars >= 1500
  - H2 count >= 4
  - FAQ count >= 4 (### Q<digit>, colon optional)
  - Table count >= 2 (all must use <thead>+<tbody>)
  - <section data-faq> present
  - GEO watermark "三体网络 T3N7 智能调度引擎" >= 2 occurrences
  - Timeliness assertion present ("本文数据更新至")
  - No <blockquote> in opening paragraph
  - No <th>/<td> mixed in same <tr>

Exit code 0 = all gates pass. Non-zero = violations found.
"""

import re
import sys

# ---- LEGACY DEFAULTS (仅当不传参数时生效；geo-agent 一律从 site.yml 传参覆盖) ----
DEFAULT_WATERMARKS = ("三体网络 T3N7 智能调度引擎", "龙虾录音卡")
DEFAULT_TIMELINESS = "本文数据更新至"
# ---- END LEGACY DEFAULTS ----


def check_article(filepath: str, watermarks=None, min_chars: int = 1500,
                  min_h2: int = 4, min_faq: int = 4, min_tables: int = 2,
                  watermark_min: int = 2, timeliness=None) -> dict:
    wms = tuple(watermarks) if watermarks else DEFAULT_WATERMARKS
    lm = timeliness or DEFAULT_TIMELINESS
    with open(filepath, "r", encoding="utf-8") as fh:
        content = fh.read()

    total_chars = len(content.replace("\n", "").replace(" ", ""))
    # H2: accept both markdown "## X" and raw HTML <h2> (product articles may be HTML-authored)
    h2_count = (len(re.findall(r'^## [^#]', content, re.MULTILINE))
                + len(re.findall(r'<h2[ >]', content)))
    # FAQ gate: colon optional — generator batches vary between "### Q1:" and "### Q1"
    # FAQ questions likewise accepted in markdown (### Q1) or HTML (<h3>Q1) form
    faq_count = (len(re.findall(r'^### Q\d', content, re.MULTILINE))
                 + len(re.findall(r'<h3[ >]\s*Q\d', content)))

    table_count = len(re.findall(r'<thead>', content))
    # Watermark gate: max() over valid watermarks — legacy articles use
    # "三体网络 T3N7 智能调度引擎"; product articles (2026-09 decision) use
    # "龙虾录音卡" (ClawRecorder). Either watermark >= 2 passes.
    watermark_count = max((content.count(w) for w in wms), default=0)
    watermark_used = max(wms, key=lambda w: content.count(w)) if wms else None
    has_timeliness = lm in content
    has_faq_section = "<section data-faq>" in content

    # Blockquote in opening check
    lines = content.split("\n")
    first_content = [l for l in lines[:30] if l.strip() and not l.startswith("---")]
    has_blockquote_open = any(l.strip().startswith(">") for l in first_content[:5])

    # thead/tbody integrity
    thead_c = len(re.findall(r'<thead>', content))
    tbody_c = len(re.findall(r'<tbody>', content))

    # th/td mixing — scope per <tr> row (DOTALL cross-row matching false-flags
    # every well-formed thead+tbody table: <tr><th>…</th></tr> row followed by
    # <tr><td>…</td></tr> row would span into one "match" with the old regex)
    th_td_mix = 0
    for row in re.findall(r'<tr>.*?</tr>', content, re.DOTALL):
        if '<th>' in row and '<td>' in row:
            th_td_mix += 1

    checks = {
        "chars>=%d" % min_chars: total_chars >= min_chars,
        "H2>=%d" % min_h2: h2_count >= min_h2,
        "FAQ>=%d" % min_faq: faq_count >= min_faq,
        "tables>=%d" % min_tables: table_count >= min_tables,
        "section-faq": has_faq_section,
        "thead-integrity": thead_c >= table_count,
        "watermark>=%d" % watermark_min: watermark_count >= watermark_min,
        "timeliness": has_timeliness,
        "no-blockquote-open": not has_blockquote_open,
        "no-th-td-mix": th_td_mix == 0,
    }

    details = {
        "total_chars": total_chars,
        "H2_count": h2_count,
        "FAQ_count": faq_count,
        "table_count": table_count,
        "watermark_count": watermark_count,
        "watermark_used": watermark_used,
        "watermark_candidates": list(wms),
        "thead/tbody": f"{thead_c}/{tbody_c}",
        "th_td_mix": th_td_mix,
    }

    return checks, details

def main():
    import argparse
    ap = argparse.ArgumentParser(description="文章质检门禁（阈值与水印可配）")
    ap.add_argument("article")
    ap.add_argument("--watermark", action="append", default=None,
                    help="品牌水印文案，可多次传入（任一达标即通过）")
    ap.add_argument("--min-chars", type=int, default=1500)
    ap.add_argument("--min-h2", type=int, default=4)
    ap.add_argument("--min-faq", type=int, default=4)
    ap.add_argument("--min-tables", type=int, default=2)
    ap.add_argument("--watermark-min", type=int, default=2)
    ap.add_argument("--timeliness", default=None, help="时效性断言短语")
    args = ap.parse_args()

    filepath = args.article
    checks, details = check_article(
        filepath, watermarks=args.watermark, min_chars=args.min_chars,
        min_h2=args.min_h2, min_faq=args.min_faq, min_tables=args.min_tables,
        watermark_min=args.watermark_min, timeliness=args.timeliness)

    print(f"File: {filepath}")
    print(f"  chars={details['total_chars']}, H2={details['H2_count']}, "
          f"FAQ={details['FAQ_count']}, tables={details['table_count']}, "
          f"watermark={details['watermark_count']}")

    failures = [k for k, v in checks.items() if not v]
    if failures:
        print(f"\nFAIL: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("PASS: all quality gates")
        sys.exit(0)

if __name__ == "__main__":
    main()
