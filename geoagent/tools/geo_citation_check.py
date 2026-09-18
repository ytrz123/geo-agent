#!/usr/bin/env python3
"""GEO Citation Checker — Playwright automated Doubao citation detection.

⚠️ 本文件是上游脚本的定制副本：**cookie / 查询词 / 站点域名 / 品牌别名 / 负责人映射
   全部已参数化**。不传参数时使用上游默认值（向后兼容），
   geo-agent 调用时会从 site.yml 传入站点自己的配置。

Usage:
    python3 geo_citation_check.py                        # 用上游默认值
    python3 geo_citation_check.py --cookie-env DOUBAO_COOKIE
    python3 geo_citation_check.py --queries-file q.json \
        --site-hosts "a.com,b.com" --brand-aliases "某品牌,别名"
Outputs JSON with per-query results and optimization recommendations.

Run with conda python (Hermes venv lacks playwright):
    /usr/local/Caskroom/miniconda/base/bin/python3 geo_citation_check.py

W35 (2026-08-24) fixes:
- Doubao chat input changed from <textarea> to [contenteditable="true"] div.
  Selector now matches both; fill() HANGS on the contenteditable (Playwright
  waits for editable state forever) — use click + keyboard.type instead.
- Hardcoded sessionid cookie expires (~2-7 days). Doubao auto-logs-out invalid
  sessions: chat/completion returns 200 then passport/web/logout redirects to
  /?from_logout=1 with NO answer rendered. Script now fail-fasts with
  COOKIE_INVALID error instead of generic textarea timeout.
"""
import json, os, re, sys
from playwright.sync_api import sync_playwright
from datetime import datetime

# ---- LEGACY DEFAULTS (仅当不传参数时生效；geo-agent 一律从 site.yml 传参覆盖) ----
# ⚠️ 刷新方式: 浏览器 DevTools → Application → Cookies → doubao.com → sessionid
# 该 cookie 有效期约 2-7 天，过期后所有查询会报 COOKIE_INVALID
# 优先从环境变量读（不把凭据写进代码）；下面的字面量只是「不传参时」的兜底。
COOKIE = os.environ.get("DOUBAO_COOKIE", "") or \
    "U4AWJqPvn6QCWt1AdfeSdGZ7VRF0iVAmchWKdn1DVU5B46mUpDSqOkoOjK-mK8uqrnYVrM6RyMtBExgPNUo85q4tG7zDAGT9r4PTEaLI1s-n6OwcxOV4zHjTrE2L1CKP5a37QUfaie3d6J3VA__vFx-GWbgeamEbSCc22MKN"

# 站点判定配置（geo-agent 从 site.yml 传 --site-hosts/--brand-aliases/--assignees 覆盖）
SITE_HOSTS = ["kamooc.cn", "santiy-ai.com"]
BRAND_ALIASES = ["三体网络", "惠州三体"]
CATEGORY_ASSIGNEES = {
    "industry": "YiChen", "network-tech": "YiChen",
    "company": "Ruoxi", "forestry": "Ruoxi",
    "government-ai": "ShuHan",
}
DEFAULT_ASSIGNEE = "YiChen"

# GEO检测关键词矩阵（geo-agent 从 site.yml 的 citation_check.queries 传 --queries-file 覆盖）
QUERIES = [
    ("公安基层智能助手 AI落地", "company"),
    ("林务通智慧林业平台", "forestry"),
    ("编译式智能体内网部署方案", "industry"),
    ("RouterOS 跨境网络 SD-WAN 配置", "network-tech"),
    ("惠州三体网络科技 AI Agent", "company"),
    ("智慧林业大数据平台建设", "forestry"),
    ("数字政府 AI智能体 信创", "government-ai"),
]
# ---- END LEGACY DEFAULTS ----

def _launch(browser_type):
    """Launch chromium; fall back to system Chrome if playwright binary missing
    (download failure on restricted network, W33 2026-08-10)."""
    try:
        return browser_type.launch(headless=True)
    except Exception:
        return browser_type.launch(headless=True, channel="chrome")

def _session_dead(page):
    """Detect Doubao auto-logout: invalid sessionid cookie redirects to home."""
    url = page.url
    return 'from_logout' in url or url.rstrip('/') == 'https://www.doubao.com'

def _send_query(page, query):
    """Type query into Doubao chat input (contenteditable or textarea) and send.
    ⚠️ Must use keyboard.type — locator.fill() hangs forever on the contenteditable."""
    editor = page.locator('textarea, [contenteditable="true"]').first
    editor.click()
    page.keyboard.type(query, delay=15)
    page.wait_for_timeout(300)
    editor.press('Enter')

def check_doubao():
    results = []
    with sync_playwright() as p:
        browser = _launch(p.chromium)
        context = browser.new_context()
        context.add_cookies([{'name': 'sessionid', 'value': COOKIE, 'domain': '.doubao.com', 'path': '/'}])
        page = context.new_page()

        for i, (query, category) in enumerate(QUERIES):
            try:
                page.goto('https://www.doubao.com/chat/new', timeout=10000)
                page.wait_for_timeout(2500)

                if _session_dead(page):
                    for q2, c2 in QUERIES[i:]:
                        results.append({'query': q2, 'category': c2, 'cited': False, 'mentioned': False,
                                        'error': 'COOKIE_INVALID: Doubao 会话已登出（sessionid cookie 过期），需刷新 cookie 后重跑'})
                    break

                _send_query(page, query)
                page.wait_for_timeout(12000)

                if _session_dead(page):
                    results.append({'query': query, 'category': category, 'cited': False, 'mentioned': False,
                                    'error': 'COOKIE_INVALID: 发送后 Doubao 自动登出（sessionid cookie 过期）'})
                    for q2, c2 in QUERIES[i+1:]:
                        results.append({'query': q2, 'category': c2, 'cited': False, 'mentioned': False,
                                        'error': 'COOKIE_INVALID: Doubao 会话已登出（sessionid cookie 过期），需刷新 cookie 后重跑'})
                    break

                html = page.content()
                cited = any(h in html for h in SITE_HOSTS)
                mentioned = any(b in html for b in BRAND_ALIASES)

                results.append({
                    'query': query,
                    'category': category,
                    'cited': cited,
                    'mentioned': mentioned,
                })
            except Exception as e:
                results.append({
                    'query': query,
                    'category': category,
                    'cited': False,
                    'mentioned': False,
                    'error': str(e)[:100]
                })

        browser.close()
    return results

def generate_recommendations(results):
    """Analyze results and generate optimization recommendations."""
    total = len(results)
    errored = sum(1 for r in results if r.get('error'))
    cited = sum(1 for r in results if r.get('cited'))
    mentioned = sum(1 for r in results if r.get('mentioned'))
    cite_rate = cited / total if total > 0 else 0

    recs = []

    # If ALL queries errored (e.g. COOKIE_INVALID), do not fabricate findings
    if errored == total and total > 0:
        err_kinds = sorted(set(r['error'][:60] for r in results if r.get('error')))
        recs.append({
            'priority': 'P0',
            'type': 'tooling_failure',
            'rule': '检测工具失效',
            'finding': f'全部 {total} 个查询失败（{err_kinds[0] if err_kinds else "未知"}），本轮引用率不可判定',
            'action': '刷新 Doubao sessionid cookie（浏览器 DevTools → Cookies → doubao.com）后重跑 geo_citation_check.py；若为页面结构变更，检查选择器',
            'assignee': DEFAULT_ASSIGNEE,
            'gitea_label': 'geo-check'
        })
        return {
            'citation_rate': None,
            'total_queries': total,
            'cited_count': cited,
            'mentioned_count': mentioned,
            'errored_count': errored,
            'recommendations': recs
        }

    # By category analysis
    from collections import defaultdict
    cat_stats = defaultdict(lambda: {'total': 0, 'cited': 0, 'mentioned': 0})
    for r in results:
        cat = r['category']
        cat_stats[cat]['total'] += 1
        if r.get('cited'): cat_stats[cat]['cited'] += 1
        if r.get('mentioned'): cat_stats[cat]['mentioned'] += 1

    # Rule 1: Citation rate
    if cite_rate == 0:
        recs.append({
            'priority': 'P0',
            'type': 'content_strategy',
            'rule': '零引用率',
            'finding': f'豆包对 {total} 个关键词查询均无本站（{SITE_HOSTS[0]}）引用',
            'action': '全品类内容加速产出。DoubaoBot 抓取量为0，需先确保内容被收录：1) 提交sitemap到搜索资源平台 2) 增加文章发布频率 3) 检查robots.txt是否对ByteSpider/DoubaoBot友好',
            'assignee': DEFAULT_ASSIGNEE,
            'gitea_label': 'geo-check'
        })
    elif cite_rate < 0.2:
        recs.append({
            'priority': 'P1',
            'type': 'content_strategy',
            'rule': '低引用率',
            'finding': f'引用率 {cite_rate:.0%} < 20%',
            'action': '增加 FAQ 区块文章占比，优先补强零曝光品类（豆包偏好场景化+FAQ块+表格数据）',
            'assignee': DEFAULT_ASSIGNEE,
            'gitea_label': 'geo-check'
        })

    # Rule 2: Mentioned but not cited
    mentioned_not_cited = [r for r in results if r.get('mentioned') and not r.get('cited')]
    if mentioned_not_cited:
        queries_str = '、'.join(r['query'] for r in mentioned_not_cited)
        recs.append({
            'priority': 'P1',
            'type': 'seo_optimization',
            'rule': '有提及无引用',
            'finding': f'豆包提及品牌（{BRAND_ALIASES[0]}）但未链接本站（{SITE_HOSTS[0]}）。查询: {queries_str}',
            'action': '优化官网结构化数据(Organization schema)和首页meta描述，确保品牌名与域名强关联。增加官网外链（知乎/CSDN/公众号）提升域名权威度',
            'assignee': DEFAULT_ASSIGNEE,
            'gitea_label': 'geo-check'
        })

    # Rule 3: Category performance — merge into ONE action, not per-category
    zero_cats = []
    for cat, stats in cat_stats.items():
        if stats['total'] > 0 and stats['cited'] == 0 and stats['mentioned'] == 0:
            zero_cats.append(cat)
    
    if zero_cats:
        cat_assignees = CATEGORY_ASSIGNEES
        # One consolidated geo-check for diagnosis
        recs.append({
            'priority': 'P1',
            'type': 'geo_diagnosis',
            'rule': '多品类零曝光',
            'finding': f'{", ".join(zero_cats)} 品类共 {sum(cat_stats[c]["total"] for c in zero_cats)} 个关键词均无引用/提及',
            'action': f'检查DoubaoBot是否已收录本站（{SITE_HOSTS[0]}）；确认sitemap已提交搜索资源平台；增加全品类内容产出频率',
            'assignee': DEFAULT_ASSIGNEE,  # 主要负责人排查
            'gitea_label': 'geo-check'
        })
        
        # Directly create article-generate recommendations per category
        for cat in zero_cats:
            recs.append({
                'priority': 'P2',
                'type': 'article_generate',
                'rule': f'{cat}品类需产出',
                'finding': f'{cat}品类关键词零曝光，需加速内容产出',
                'action': f'生成 1 篇 {cat} 品类文章，优化关键词覆盖（豆包偏好场景化+FAQ+表格，或论证型+引用来源）',
                'assignee': cat_assignees.get(cat, DEFAULT_ASSIGNEE),
                'gitea_label': 'article-generate',
                'category': cat
            })

    return {
        'citation_rate': cite_rate,
        'total_queries': total,
        'cited_count': cited,
        'mentioned_count': mentioned,
        'errored_count': errored,
        'recommendations': recs
    }

def _apply_cli_args(argv=None):
    """按命令行参数覆盖模块级配置（cookie / 查询词 / 站点域名 / 品牌别名 / 负责人）。

    不传参数时一切保持上游默认值 —— 旧调度方照旧可用。
    """
    global COOKIE, QUERIES, SITE_HOSTS, BRAND_ALIASES, CATEGORY_ASSIGNEES, DEFAULT_ASSIGNEE
    import argparse
    ap = argparse.ArgumentParser(description="豆包 GEO 引用检测（配置可外部注入）")
    ap.add_argument("--cookie", default=None, help="直接给 sessionid 值")
    ap.add_argument("--cookie-env", default=None, help="从该环境变量读 sessionid")
    ap.add_argument("--queries-file", default=None,
                    help="JSON 文件：[{q, category}, ...]")
    ap.add_argument("--site-hosts", default=None, help="逗号分隔：判「引用了本站」的域名")
    ap.add_argument("--brand-aliases", default=None, help="逗号分隔：判「提到了品牌」的词")
    ap.add_argument("--assignees", default=None,
                    help="逗号分隔的负责人，按品类顺序循环分配")
    ap.add_argument("--default-assignee", default=None)
    args = ap.parse_args(argv)

    if args.cookie:
        COOKIE = args.cookie
    elif args.cookie_env:
        COOKIE = os.environ.get(args.cookie_env, "")

    if args.queries_file:
        with open(args.queries_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        QUERIES = [(d.get("q") or d.get("query"), d.get("category")) for d in data]

    if args.site_hosts:
        SITE_HOSTS = [h.strip() for h in args.site_hosts.split(",") if h.strip()]
    if args.brand_aliases:
        BRAND_ALIASES = [b.strip() for b in args.brand_aliases.split(",") if b.strip()]
    if args.assignees:
        pool = [a.strip() for a in args.assignees.split(",") if a.strip()]
        if pool:
            cats = [c for _, c in QUERIES if c]
            ordered = sorted(set(cats), key=cats.index)
            CATEGORY_ASSIGNEES = {c: pool[i % len(pool)] for i, c in enumerate(ordered)}
            DEFAULT_ASSIGNEE = pool[0]
    if args.default_assignee:
        DEFAULT_ASSIGNEE = args.default_assignee

    if not COOKIE:
        print(json.dumps({"error": "COOKIE_INVALID",
                          "detail": "未提供 cookie（用 --cookie / --cookie-env 或环境变量 "
                                    "DOUBAO_COOKIE）"}, ensure_ascii=False))
        sys.exit(3)


if __name__ == '__main__':
    _apply_cli_args()
    results = check_doubao()
    analysis = generate_recommendations(results)

    output = {
        'platform': 'doubao',
        'checked_at': datetime.now().isoformat(),
        'results': results,
        'analysis': analysis
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))
