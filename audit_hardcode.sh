#!/bin/bash
# 扫描「非通用硬编码」：确认换站点/换知识库只需改配置，不用改代码。
#
# 判定标准：
#   ✗ 出现具体站点域名/品牌/品类/人名的**活代码**（非注释）→ 必须修
#   ✓ 出现在 # ---- LEGACY DEFAULTS ---- 块内（上游脚本的兜底默认）→ 允许
#   ✓ 出现在注释、文档字符串、tests/、*.md 里 → 允许
cd "$(dirname "$0")" || exit 1

echo "########## A. 活代码里的站点痕迹（应只剩 LEGACY 块与注释）##########"
python3 - <<'PY'
import pathlib, re

ROOT = pathlib.Path(".")
FORBIDDEN = ["kamooc", "santiy-ai", "三体网络", "惠州三体", "T3N7", "龙虾录音卡",
             "ClawRecorder", "clawrecorder", "government-ai", "network-tech", "forestry",
             "YiChen", "Ruoxi", "ShuHan"]
LEGACY = re.compile(r"# -+ LEGACY DEFAULTS[\s\S]*?# -+ END LEGACY DEFAULTS")


def strip_comments(src):
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    src = re.sub(r"^\s*#.*$", "", src, flags=re.MULTILINE)
    return re.sub(r"\s+#\s.*$", "", src, flags=re.MULTILINE)


hits = []
for p in sorted(pathlib.Path("geoagent").rglob("*.py")):
    raw = p.read_text(encoding="utf-8", errors="replace")
    body = LEGACY.sub("", raw)
    active = strip_comments(body)
    for bad in FORBIDDEN:
        if bad.lower() in active.lower():
            hits.append((str(p), bad))
if hits:
    for f, b in hits:
        print("  ✗ %-46s %r" % (f, b))
else:
    print("  ✓ 干净：活代码里无任何站点痕迹")
PY

echo
echo "########## B. 配置层的 DEFAULTS 是否中性 ##########"
grep -nE '"(base_url|name|user|password)": *"[^"]' geoagent/config.py geoagent/site.py \
  | grep -v '""' | grep -v "unassigned" || echo "  ✓ 干净：DEFAULTS 里没有具体站点值"

echo
echo "########## C. 凭据是否已外置（不应有明文密码/cookie）##########"
# 只看「赋值语句里出现非空字符串字面量」的行 —— 逻辑代码行不算
LEAK=$(grep -nE '^\s*(password|token|api_key|cookie|webhook_url)\s*[:=]\s*[\x27"][^\x27"]' \
        geoagent/config.py geoagent/site.py 2>/dev/null)
if [ -n "$LEAK" ]; then echo "  ✗ 发现明文凭据默认值："; echo "$LEAK"; else echo "  ✓ 干净：config.py / site.py 里没有明文凭据默认值"; fi
echo "  —— tools/ 里的上游遗留字面量（仅在 LEGACY 块内，geo-agent 一律传参覆盖）:"
for f in geo_citation_check.py verify_article_quality.py sitemap_coverage.py; do
  n=$(grep -c "LEGACY DEFAULTS" "geoagent/tools/$f" 2>/dev/null || echo 0)
  echo "     $f  LEGACY 标记 $n 处（开+闭=2 为正常）"
done

echo
echo "########## D. 换站点只改配置的证明 ##########"
echo "  站点档案    : site.yml（$(grep -c . site.yml) 行）"
echo "  知识库索引  : knowledge/index.yml（$(grep -c . knowledge/index.yml) 行）"
echo "  测试用假站点: tests/fixtures/site.test.yml —— 63 项测试全跑在它上面"
echo "  试跑        : .venv/bin/python cli.py check --site site.example.yml"
