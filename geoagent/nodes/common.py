"""节点层（一）：公共抓取节点 —— 纯 Python，不调 LLM。

抓取失败一律记进 state["errors"] 并把字段留 None，绝不抛异常：
历史事故「POST 失败只 print、任务仍报 success」就是抛/吞异常的后果。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .. import obs
from ..config import get

UA = {"User-Agent": "geo-agent/1.0 (+shadow-mode)"}


def fetch(url: str, timeout: int = 30):
    """返回 (ok, text_or_error)。"""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, {"http": e.code, "url": url}
    except Exception as e:  # noqa: BLE001
        return False, {"error": "%s: %s" % (type(e).__name__, e), "url": url}


def sitemap_urls(base_url: str):
    ok, body = fetch(base_url.rstrip("/") + "/sitemap.xml")
    if not ok:
        return False, body
    return True, re.findall(r"<loc>\s*(.*?)\s*</loc>", body)


def robots_txt(base_url: str):
    return fetch(base_url.rstrip("/") + "/robots.txt")


def jsonld_check(base_url: str):
    """首页 JSON-LD 检查。

    坑（实测）：某些 Next.js 站点的 JSON-LD 不在标准 <script type=application/ld+json> 里，
    而是转义后嵌在 self.__next_f.push 的 RSC payload 中。
    所以不能只匹配 script 标签 —— 改为统计 `application/ld+json` 出现次数 + 子串确认类型。
    """
    ok, body = fetch(base_url.rstrip("/"))
    if not ok:
        return False, body
    occurrences = body.count("application/ld+json")
    types = [t for t in ("Organization", "WebSite") if t in body]
    script_blocks = len(re.findall(r'<script[^>]+application/ld\+json', body))
    return True, {"occurrences": occurrences, "script_blocks": script_blocks,
                  "types": types, "has_organization": "Organization" in body,
                  "has_website": "WebSite" in body,
                  "rsc_embedded": script_blocks == 0 and occurrences > 0}


def strapi_articles(base_url: str):
    """文章总数 = meta.pagination.total（不是 data 长度）。"""
    ok, body = fetch(base_url.rstrip("/") + "/api/articles?pagination%5BpageSize%5D=1")
    if not ok:
        return False, body
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return False, {"error": "bad json: %s" % e}
    total = ((data.get("meta") or {}).get("pagination") or {}).get("total")
    return True, (int(total) if isinstance(total, int) else None)


def crawler_log_counts(ssh_runner, patterns=None):
    """AI 爬虫抓取计数。

    ssh_runner 由调用方注入（默认 None = 不采集，影子跑不强依赖 SSH）。
    必须用 `docker logs ent-nginx`，**不能** docker exec cat access.log
    （容器内 access.log 是 /dev/stdout 的符号链接，会永久挂起）。
    """
    patterns = patterns or ["DoubaoBot", "Bytespider", "DeepSeekBot", "GPTBot", "Googlebot"]
    if ssh_runner is None:
        return None
    ok, out = ssh_runner("docker logs ent-nginx 2>&1 | "
                         "grep -oE '%s' | sort | uniq -c" % "|".join(patterns))
    if not ok:
        return None
    counts = {p: 0 for p in patterns}
    for line in (out or "").split("\n"):
        parts = line.strip().split()
        if len(parts) == 2 and parts[1] in counts:
            counts[parts[1]] = int(parts[0])
    return counts


def append_error(state: dict, msg: str):
    state.setdefault("errors", []).append(msg)
    obs.log("error_recorded", level="error", msg=msg)
