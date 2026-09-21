#!/usr/bin/env python3
"""只读拉取 metrics 归档（解码后的原始 JSON）当 fixture。

为两件事取证：
  1. normalize.py 要能吃掉历史上并存的所有 schema 变体（拿真实文件做回归）。
  2. 策略窗口（4 周 daily + weekly）要能真实聚合。
只做 GET。
"""
import base64
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from geoagent import config as cfgmod  # noqa: E402

# 地址与凭据一律从 config.yml（或 GEOAGENT_CONFIG 指定的文件）读 —— 脚本里不留任何真实值
CFG = cfgmod.load()
_BASE = (cfgmod.get(CFG, "gitea.base_url") or "").rstrip("/")
_REPO = cfgmod.get(CFG, "gitea.repo") or ""
_USER = CFG["gitea"].get("user") or ""
if not (_BASE and _REPO and _USER):
    sys.exit("请先在 config.yml 里配好 gitea.base_url / gitea.repo / user"
             "（或设 GITEA_USER / GITEA_PASSWORD 环境变量）")
BASE = "%s/api/v1/repos/%s" % (_BASE, _REPO)
CREDS = base64.b64encode(("%s:%s" % (_USER, CFG["gitea"].get("password") or "")).encode()).decode()
OUT = pathlib.Path(__file__).resolve().parent / "tests" / "fixtures" / "metrics"
OUT.mkdir(parents=True, exist_ok=True)


def get(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + CREDS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


listing = get(BASE + "/contents/metrics?ref=main")
names = sorted(i["name"] for i in listing if i["name"].endswith(".json"))
daily = [n for n in names if n.startswith("daily-")]
weekly = [n for n in names if n.startswith("weekly-")]

saved, failed = 0, 0
for name in daily[-45:] + weekly:
    try:
        d = get(BASE + "/contents/metrics/" + name)
        raw = base64.b64decode(d["content"]).decode("utf-8", "replace")
        json.loads(raw)  # 校验是合法 JSON
        (OUT / name).write_text(raw, encoding="utf-8")
        saved += 1
    except Exception as e:
        failed += 1
        print("FAIL %-34s %s" % (name, e))

print("daily total=%d weekly total=%d saved=%d failed=%d" % (len(daily), len(weekly), saved, failed))
print("saved into %s" % OUT)
