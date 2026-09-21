"""站点档案加载（site.yml）。

这是 geo-agent 里**唯一**认识「具体站点」的地方。代码其它部分一律通过本模块取值，
不得出现任何具体域名/品牌/品类/人名的字面量。

设计要点：
  * site.yml 一次只加载一份 → 一次只服务一个站点。要切站点用 --site 指定另一份档案。
  * 凭据不进 site.yml，只写环境变量名（token_env / cookie_env / webhook_env），
    由 resolve_secret() 在运行时解析。
  * 提供 DEFAULTS，让没有任何 site.yml 时也能跑（全部空值/无品类），而不是崩。
"""
from __future__ import annotations

import copy
import os
import pathlib
import re

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .config import HOME_DIR, ROOT, _first_existing

# 最小可用默认：一个没有任何品类、没有水印的"空站点"。
# 目的不是能用，而是「缺配置时给出清晰错误」而不是 AttributeError。
DEFAULTS = {
    "site": {"name": "unnamed-site", "base_url": "", "extra_hosts": [], "brand_aliases": []},
    "publish": {"cms": "none", "base_url": "", "token_env": "STRAPI_TOKEN",
                "content_type": "articles", "category_field": "category"},
    "routes": {"article_prefix": "/blog", "locale_prefixes": [""], "static_paths": []},
    "categories": [],
    "batch": {"categories_per_week": 1, "product_slot": False, "product_category": None},
    "watermark": {"default": "", "min_occurrences": 2, "overrides": []},
    "quality": {"min_chars": 1500, "min_h2": 4, "min_faq": 4, "min_tables": 2,
                "require_section_faq": True, "timeliness_phrase": "本文数据更新至",
                "max_fix_rounds": 3},
    "citation_check": {"engine": "none", "cookie_env": "DOUBAO_COOKIE",
                       "timeout_seconds": 900, "queries": []},
    "workflow": {"labels": {}},
    "people": {"default_assignee": "unassigned", "assignees": []},
    "thresholds": {"sitemap_coverage_min": 0.90, "lcp_max_seconds": 4.0,
                   "doubaobot_zero_streak_days": 3, "static_pages_expected": 0},
}

_ENV_PAT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def site_path(cfg: dict | None = None, explicit: str | os.PathLike | None = None) -> pathlib.Path:
    """三级查找 site.yml：

      ① 命令行 --site <file.yml>
      ② 环境变量 GEOAGENT_SITE
      ③ 默认位置：项目内 ./site.yml，其次 ~/.config/geoagent/site.yml

    ⚠️ site.yml 是站点身份（域名/品牌/品类/人名），属于私有档案，不进版本控制。
    """
    if explicit:
        return pathlib.Path(explicit).expanduser()
    env = os.environ.get("GEOAGENT_SITE")
    if env:
        return pathlib.Path(env).expanduser()
    if cfg and cfg.get("_meta", {}).get("site_file"):
        return pathlib.Path(cfg["_meta"]["site_file"]).expanduser()
    return _first_existing([ROOT / "site.yml", HOME_DIR / "site.yml"])


def load_site(cfg: dict | None = None, explicit: str | os.PathLike | None = None) -> dict:
    """读 site.yml 并与默认值深合并。返回的 dict 带 _meta 记录来源。"""
    p = site_path(cfg, explicit)
    data = {}
    if p.exists():
        if yaml is None:
            raise RuntimeError("读取 site.yml 需要 pyyaml")
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    s = _deep_merge(DEFAULTS, data)
    s["_meta"] = {"site_file": str(p), "exists": p.exists()}
    return s


def get(site: dict, dotted: str, default=None):
    cur = site
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


# ------------------------------------------------------------------ 站点身份
def hosts(site: dict) -> list[str]:
    """等价域名清单（含 base_url 的 host）。引用检测判「是否引用本站」用。"""
    out = []
    base = get(site, "site.base_url", "") or ""
    if base:
        out.append(re.sub(r"^https?://", "", base).rstrip("/"))
    for h in (get(site, "site.extra_hosts", []) or []):
        h = str(h).strip()
        if h and h not in out:
            out.append(h)
    return out


def brand_aliases(site: dict) -> list[str]:
    """品牌别名。检测「AI 回答是否提到本站品牌」用。"""
    out = []
    for a in (get(site, "site.brand_aliases", []) or []):
        a = str(a).strip()
        if a and a not in out:
            out.append(a)
    name = get(site, "site.name", "")
    if name and name not in out:
        out.append(name)
    return out


def is_cited(text: str, site: dict) -> bool:
    """文本里是否出现本站任意等价域名。"""
    return any(h in (text or "") for h in hosts(site))


def is_mentioned(text: str, site: dict) -> bool:
    """文本里是否出现本站任意品牌别名。"""
    return any(b in (text or "") for b in brand_aliases(site))


# ------------------------------------------------------------------ 品类与人员
def categories(site: dict) -> list[dict]:
    return list(get(site, "categories", []) or [])


def category_ids(site: dict) -> list[str]:
    return [c.get("id") for c in categories(site) if c.get("id")]


def category(site: dict, cid: str) -> dict | None:
    for c in categories(site):
        if c.get("id") == cid:
            return c
    return None


def assignee_for_category(site: dict, cid: str) -> str:
    c = category(site, cid) or {}
    return c.get("assignee") or get(site, "people.default_assignee", "unassigned")


def assignees(site: dict) -> list[str]:
    out = list(get(site, "people.assignees", []) or [])
    if not out:
        out = sorted({c.get("assignee") for c in categories(site) if c.get("assignee")})
    return out or [get(site, "people.default_assignee", "unassigned")]


def default_assignee(site: dict) -> str:
    return get(site, "people.default_assignee", "unassigned")


def label(site: dict, key: str, fallback: str = "") -> str:
    """工作流标签名（Gitea label）。key 如 seo_monitor / geo_check / article_generate。"""
    return get(site, "workflow.labels." + key, fallback) or fallback


def category_weight(site: dict, cid: str, fallback: float = 1.0) -> float:
    c = category(site, cid) or {}
    try:
        return float(c.get("weight", fallback))
    except (TypeError, ValueError):
        return fallback


# ------------------------------------------------------------------ 水印
def watermark_candidates(site: dict, knowledge_entries=None) -> list[str]:
    """水印候选集合。

    语义与官方质检脚本的 max() 一致：候选里任意一个达到次数即 PASS。
    候选 = default + 所有 override 文案（含 knowledge 条目的 watermark）。
    """
    out = []
    d = get(site, "watermark.default", "")
    if d:
        out.append(d)
    for o in (get(site, "watermark.overrides", []) or []):
        t = (o or {}).get("text")
        if t and t not in out:
            out.append(t)
    for e in (knowledge_entries or []):
        w = (e or {}).get("watermark")
        if w and w not in out:
            out.append(w)
    return out


def watermark_min(site: dict) -> int:
    try:
        return int(get(site, "watermark.min_occurrences", 2))
    except (TypeError, ValueError):
        return 2


def watermark_for_entry(site: dict, entry: dict | None) -> str:
    """某个知识库条目该用哪条水印。"""
    if entry and entry.get("watermark"):
        return entry["watermark"]
    kid = (entry or {}).get("id")
    for o in (get(site, "watermark.overrides", []) or []):
        if kid and (o or {}).get("when_knowledge") == kid:
            return o.get("text") or get(site, "watermark.default", "")
    return get(site, "watermark.default", "")


# ------------------------------------------------------------------ 路由
def article_prefix(site: dict) -> str:
    p = get(site, "routes.article_prefix", "/blog") or "/blog"
    return "/" + p.strip("/")


def locale_prefixes(site: dict) -> list[str]:
    out = []
    for p in (get(site, "routes.locale_prefixes", [""]) or [""]):
        p = str(p).strip()
        out.append("" if not p or p == "/" else "/" + p.strip("/"))
    return out or [""]


def article_urls(site: dict, slug: str) -> list[str]:
    """一篇 slug 在本站的全部 locale 路由（用于发布后验证）。"""
    pre = article_prefix(site)
    out = []
    for lp in locale_prefixes(site):
        out.append("%s%s/%s" % (lp, pre, slug))
    return out


def static_paths(site: dict) -> set[str]:
    return {str(p) for p in (get(site, "routes.static_paths", []) or [])}


# ------------------------------------------------------------------ 凭据解析
def resolve_secret(value: str | None, literal_key: str | None = None) -> str:
    """把配置里的值解析成真实凭据。

    支持三种写法：
      "${ENV_NAME}"    → 从环境变量读（推荐）
      ""               → 空
      直接的字面值      → 原样返回（向后兼容：老 config.yml 里写死值仍能用）
    """
    if not value:
        return ""
    m = _ENV_PAT.search(str(value))
    if m:
        return os.environ.get(m.group(1), "")
    if literal_key and literal_key in os.environ:
        return os.environ[literal_key]
    return str(value)


def resolve_env_name(site: dict, dotted: str) -> str:
    """取配置里声明的环境变量名（如 publish.token_env → STRAPI_TOKEN）。"""
    return str(get(site, dotted, "") or "")


# ------------------------------------------------------------------ 自检
def validate(site: dict) -> list[str]:
    """返回配置问题清单（空 = 通过）。启动时调用，缺东西要早报而不是跑到一半崩。"""
    problems = []
    if not get(site, "site.base_url"):
        problems.append("site.base_url 未配置（抓取与验证的目标站）")
    if not get(site, "site.name"):
        problems.append("site.name 未配置（日志/工单里会显示 unnamed-site）")
    cats = categories(site)
    if not cats:
        problems.append("categories 为空 —— 管道 C/E 无法生成任何内容")
    for c in cats:
        if not c.get("id"):
            problems.append("存在没有 id 的品类条目")
        if not c.get("assignee"):
            problems.append("品类 %s 没有 assignee" % c.get("id"))
    cms = get(site, "publish.cms", "none")
    if cms == "strapi" and not get(site, "publish.base_url"):
        problems.append("publish.cms=strapi 但 publish.base_url 为空")
    return problems
