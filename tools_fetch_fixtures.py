#!/usr/bin/env python3
"""只读取样：把策略文件与现有工单取回本地当 fixture（为规则引擎取证）。

只做 GET，不写任何远端。落盘到 tests/fixtures/。
"""
import base64
import json
import pathlib
import sys
import urllib.request

BASE = "http://192.168.31.162:3000/api/v1/repos/stkj/geo-seo"
CREDS = base64.b64encode(b"liujia:admin123").decode()
OUT = pathlib.Path(__file__).resolve().parent / "tests" / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)


def get(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + CREDS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def save(name, obj):
    p = OUT / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved %-34s %d bytes" % (name, p.stat().st_size))


def file_text(path):
    d = get(BASE + "/contents/" + path)
    return base64.b64decode(d["content"]).decode("utf-8", "replace")


print("=== strategy files ===")
for path, name in [("strategy/evolution-rules.yaml", "evolution-rules.yaml"),
                   ("strategy/keywords.yaml", "keywords.yaml")]:
    try:
        txt = file_text(path)
        (OUT / name).write_text(txt, encoding="utf-8")
        print("saved %-34s %d bytes" % (name, len(txt)))
    except Exception as e:
        print("FAIL %s: %s" % (path, e))

print("=== open issues ===")
try:
    issues = get(BASE + "/issues?state=open&limit=50")
    slim = [{"number": i.get("number"), "title": i.get("title"),
             "labels": [lb.get("name") for lb in (i.get("labels") or [])],
             "assignees": [a.get("login") for a in (i.get("assignees") or [])],
             "state": i.get("state"), "updated_at": i.get("updated_at"),
             "body_head": (i.get("body") or "")[:400]} for i in issues]
    save("open_issues.json", slim)
except Exception as e:
    print("FAIL issues: %s" % e)

print("=== metrics listing ===")
try:
    items = get(BASE + "/contents/metrics?ref=main")
    names = sorted(i["name"] for i in items if i["name"].endswith(".json"))
    save("metrics_listing.json", names)
    dailies = [n for n in names if n.startswith("daily-")]
    print("daily files: %d, latest 5: %s" % (len(dailies), dailies[-5:]))
except Exception as e:
    print("FAIL metrics: %s" % e)

print("=== sample daily/weekly payloads (for normalize tests) ===")
try:
    for name in ["daily-2026-09-17.json", "daily-2026-09-18.json",
                 "daily-2026-08-28.json", "daily-2026-09-12.json"]:
        try:
            save(name, get(BASE + "/contents/metrics/" + name))
        except Exception as e:
            print("skip %s: %s" % (name, e))
except Exception as e:
    print("FAIL samples: %s" % e)
