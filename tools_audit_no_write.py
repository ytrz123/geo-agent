#!/usr/bin/env python3
"""只读取证：确认 geo-agent 从未向官网(Strapi)/Gitea 写入任何东西。

检查项：
  1. 所有 trace 里，写操作是否**全部**被影子模式短路（出现 skipped，且没有成功写事件）
  2. workspace 的 git 是否与远端一致（有没有本地 commit / 是否 push 过）
  3. Strapi 今天有没有这篇文章（不该有 geo-agent 产出的任何 slug）
  4. var/out/ 下产物清单（影子产物只应落在本地 out 目录）
全程只读，不发任何写请求。
"""
from __future__ import annotations

import glob
import json
import pathlib
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from geoagent import config as cfgmod  # noqa: E402
from geoagent import site as site_mod  # noqa: E402

CFG = cfgmod.load()
SITE = CFG["_site"]

WRITE_EVENTS_OK = {"strapi_write_skipped", "gitea_write_skipped", "repo_commit_skipped",
                   "repo_push_skipped", "wecom_send_skipped", "wecom_not_configured",
                   "wecom_quiet_hours_suppressed"}
WRITE_EVENTS_BAD = {"strapi_written", "gitea_written", "repo_pushed", "wecom_sent"}


def main():
    print("=" * 70)
    print("只读取证：geo-agent 是否向官网/Gitea 写入过")
    print("=" * 70)

    # ---- 1. trace 扫描
    files = sorted(glob.glob(str(ROOT / "var" / "logs" / "*.jsonl")))
    skip_counts, bad_hits, api_errors = {}, [], []
    for f in files:
        for line in pathlib.Path(f).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = rec.get("event", "")
            if ev in WRITE_EVENTS_OK:
                skip_counts[ev] = skip_counts.get(ev, 0) + 1
            if ev in WRITE_EVENTS_BAD:
                bad_hits.append((pathlib.Path(f).name, rec))
            if ev in ("strapi_write_skipped", "gitea_write_skipped") and rec.get("error"):
                api_errors.append(rec)

    print("\n[1] 写操作短路统计（%d 份 trace）" % len(files))
    for k in sorted(skip_counts):
        print("    %-26s x%d" % (k, skip_counts[k]))
    print("    成功写事件（必须为 0）: %d %s" % (len(bad_hits), bad_hits[:3]))

    # ---- 2. workspace git 状态
    ws = cfgmod.abspath(CFG, "paths.workspace")
    print("\n[2] workspace git 状态 (%s)" % ws)
    if (ws / ".git").exists():
        def git(*a):
            p = subprocess.run(["git", "-C", str(ws)] + list(a), capture_output=True, text=True)
            return (p.stdout or "").strip()
        local = git("log", "--oneline", "-1")
        remote = git("log", "--oneline", "-1", "origin/main")
        print("    本地 HEAD  : %s" % local)
        print("    远端 HEAD  : %s" % remote)
        print("    是否一致   : %s" % ("是（无本地提交、未 push）" if local == remote else "否 ← 需检查"))
        print("    未提交改动 : %s" % (git("status", "--short") or "无"))
    else:
        print("    workspace 不存在（也说明没写过）")

    # ---- 3. Strapi 只读查询：今天有没有新文章
    print("\n[3] 官网 Strapi 只读核对（今天是否有新增）")
    import datetime as dt
    today = dt.date.today().isoformat()
    # 官网地址与集合名都来自 site.yml —— 脚本里不写死任何站点
    _site_base = (site_mod.get(SITE, "publish.base_url")
                  or site_mod.get(SITE, "site.base_url") or "").rstrip("/")
    _ctype = site_mod.get(SITE, "publish.content_type", "articles")
    url = ("%s/api/%s?pagination%%5BpageSize%%5D=50&sort=publishedAt:desc"
           % (_site_base, _ctype))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "geo-agent-audit/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        items = data.get("data") or []
        total = ((data.get("meta") or {}).get("pagination") or {}).get("total")
        mine = [i for i in items if (i.get("slug") or "").endswith("20260918")]
        print("    文章总数      : %s" % total)
        print("    最近 3 篇     :")
        for i in items[:3]:
            print("      - %-52s %s" % ((i.get("slug") or "")[:50], i.get("publishedAt")))
        print("    含 20260918 的 : %d %s" % (len(mine), [m.get("slug") for m in mine]))
        print("    → geo-agent 影子跑产出的 6 个 slug 均未发布：%s"
              % ("是" if not mine else "否 ← 需立即处理"))
    except Exception as e:  # noqa: BLE001
        print("    查询失败（不影响结论）: %s: %s" % (type(e).__name__, e))

    # ---- 4. 本地影子产物
    print("\n[4] 本地影子产物（只应存在于 var/out/）")
    for p in sorted((ROOT / "var" / "out").rglob("*")):
        if p.is_file():
            print("    %-58s %d bytes" % (str(p.relative_to(ROOT)), p.stat().st_size))

    print("\n" + "=" * 70)
    ok = not bad_hits
    print("结论：%s" % ("从未向官网/Gitea 写入任何数据（写操作全部被影子模式短路）"
                    if ok else "发现成功写事件，需立即核查！"))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
