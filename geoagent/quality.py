"""质检门禁 + md→HTML 转换的封装。

刻意不重写官方脚本：直接调用 geoagent/tools/ 下与上游字节一致的脚本。
  * verify_article_quality.check_article(path) -> (checks, details)
  * md_to_strapi.py 走子进程（它只有 main()，读 argv、输出 JSON 到 stdout）

通用性做法：**官方脚本一个字节不改**（保持可独立运行、可升级），
站点相关的水印门禁在本层**覆盖**官方结果 —— 换品牌/换站点时只改 site.yml 的水印配置。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys

from . import obs, site as site_mod

TOOLS = pathlib.Path(__file__).resolve().parent / "tools"
VERIFY = TOOLS / "verify_article_quality.py"
MD2STRAPI = TOOLS / "md_to_strapi.py"

_verify_mod = None


def _load_verify():
    global _verify_mod
    if _verify_mod is None:
        spec = importlib.util.spec_from_file_location("geo_verify_article_quality", VERIFY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _verify_mod = mod
    return _verify_mod


def _watermark_check(text: str, site: dict, knowledge=None) -> dict:
    """按站点配置算水印。语义与官方 max() 一致：候选里任意一个达标即 PASS。"""
    entries = knowledge.entries if knowledge is not None else None
    cands = site_mod.watermark_candidates(site, entries)
    if not cands:
        return {"ok": True, "count": 0, "used": None, "candidates": {},
                "skipped": "站点未配置水印 → 该项不判定"}
    counts = {c: text.count(c) for c in cands}
    used = max(counts, key=lambda k: counts[k]) if counts else None
    best = counts.get(used, 0)
    return {"ok": best >= site_mod.watermark_min(site), "count": best, "used": used,
            "candidates": counts}


def quality_gate(article_path, site: dict | None = None, knowledge=None) -> dict:
    """跑官方质检脚本 + 站点水印覆盖。返回 {pass, violations[], checks{}, details{}}。

    不传 site 时行为与官方脚本完全一致（保持向后兼容与可单独测试）。
    """
    path = pathlib.Path(article_path)
    if not path.exists():
        return {"pass": False, "violations": ["file-missing"], "checks": {},
                "details": {"path": str(path)}}
    checks, details = _load_verify().check_article(str(path))

    if site:
        text = path.read_text(encoding="utf-8", errors="replace")
        w = _watermark_check(text, site, knowledge)
        key_candidates = [k for k in checks if k.startswith("watermark")]
        key = key_candidates[0] if key_candidates else "watermark>=2"
        if w.get("skipped"):
            details["watermark_note"] = w["skipped"]
        else:
            checks[key] = w["ok"]
            details["watermark_count"] = w["count"]
            details["watermark_used"] = w["used"]
            details["watermark_candidates"] = w["candidates"]

    violations = [k for k, v in checks.items() if not v]
    return {"pass": not violations, "violations": violations, "checks": checks,
            "details": details}


def md_to_payload(article_path, python_exe: str | None = None) -> dict:
    """md → 发布 payload（title/slug/excerpt/content）。子进程跑官方脚本，副本零改动。"""
    exe = python_exe or sys.executable
    proc = subprocess.run([exe, str(MD2STRAPI), str(article_path)],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        return {"error": "md_to_strapi failed rc=%s" % proc.returncode,
                "stderr": (proc.stderr or "")[-400:]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return {"error": "bad json: %s" % e, "stdout_head": (proc.stdout or "")[:200]}


def frontmatter(path) -> dict:
    """极简 YAML frontmatter 解析。status 兼容带引号写法。"""
    text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    fm = {}
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    block = m.group(1) if m else ""
    for line in block.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def category_ok(article_path, site: dict) -> tuple[bool, str]:
    """文章 frontmatter 的品类是否在站点允许列表内。"""
    fm = frontmatter(article_path)
    cat = fm.get("category") or ""
    allowed = site_mod.category_ids(site)
    if not allowed:
        return True, cat
    return (cat in allowed), cat


def scan_review_articles(repo_dir, status: str = "review") -> list[dict]:
    """扫出 status: review 的文章。

    兼容 `status: review` 与 `status: "review"`（朴素子串匹配曾漏掉带引号的全部文件）。
    """
    root = pathlib.Path(repo_dir) / "output" / "articles"
    out = []
    if not root.exists():
        return out
    for f in sorted(root.glob("*.md")):
        head = f.read_text(encoding="utf-8", errors="replace")[:1200]
        m = re.search(r'status:\s*"?([a-zA-Z\-]+)"?', head)
        s = (m.group(1) if m else "").lower()
        if s == status:
            out.append({"path": str(f), "slug": f.stem, "status": s})
    return out


def writeback_published(path) -> bool:
    """status: review → published（一次匹配两种引号写法）。"""
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    new, n = re.subn(r'status:\s*"?review"?', "status: published", text, count=1)
    if n:
        p.write_text(new, encoding="utf-8")
        return True
    return False
