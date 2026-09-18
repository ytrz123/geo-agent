"""通用性回归 —— 这是本次改造的验收红线。

核心断言：**代码里不许出现具体站点的域名/品牌/品类/人名**。
所以换站点只需改 site.yml，不需要动代码。

唯一的白名单是 `geoagent/tools/` 下两个上游脚本里的「LEGACY 默认值」块：
它们是「不传参数时的兜底」，geo-agent 调用时一律从 site.yml 传参覆盖。
白名单有明确的注释标记，且被本测试逐文件核实（不是"整个目录跳过"）。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "geoagent"

# 具体站点的痕迹（真实站点/品牌/品类/人名）——出现在代码里就是硬编码
FORBIDDEN = [
    "kamooc", "santiy-ai",
    "三体网络", "惠州三体", "T3N7", "龙虾录音卡", "ClawRecorder", "clawrecorder",
    "government-ai", "network-tech", "forestry",
    "YiChen", "Ruoxi", "ShuHan",
]

# 允许出现的位置：上游脚本的 LEGACY 默认值块 + 注释/文档字符串
TOOLS = PKG / "tools"


def _py_files(root=PKG):
    return [p for p in root.rglob("*.py") if p.is_file()]


def _strip_comments_and_docstrings(src: str) -> str:
    """去掉注释与字符串文档，只留"活代码"。

    这样「注释里提到历史站点」不算违规，但代码里的字面量算。
    """
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    src = re.sub(r"^\s*#.*$", "", src, flags=re.MULTILINE)
    # 行尾注释
    src = re.sub(r"\s+#\s.*$", "", src, flags=re.MULTILINE)
    return src


def test_no_site_hardcode_in_active_code():
    """活代码（不含注释/文档）里不得出现具体站点痕迹。

    tools/ 下的上游脚本例外 —— 但例外范围被精确约束到 LEGACY 块：
    其它位置出现同样会失败。
    """
    offenders = []
    for p in _py_files():
        raw = p.read_text(encoding="utf-8", errors="replace")
        if p.parent == TOOLS:
            # 只允许 LEGACY 默认值块内出现
            body = raw
            body = re.sub(r"# -+ LEGACY DEFAULTS[\s\S]*?# -+ END LEGACY DEFAULTS", "", body)
            active = _strip_comments_and_docstrings(body)
            # 去掉工具脚本里被判定的"站点痕迹"字符串字面量（它们只在 LEGACY 块）
            for bad in FORBIDDEN:
                if bad.lower() in active.lower():
                    offenders.append((str(p.relative_to(ROOT)), bad, "tools 非 LEGACY 区"))
            continue
        active = _strip_comments_and_docstrings(raw)
        for bad in FORBIDDEN:
            if bad.lower() in active.lower():
                offenders.append((str(p.relative_to(ROOT)), bad, "活代码"))
    assert not offenders, "代码里仍有站点硬编码：\n%s" % "\n".join(
        "  %s ← %r（%s）" % o for o in offenders)


def test_defaults_have_no_site_literals():
    """config.py / site.py 的 DEFAULTS 不得含具体站点值。"""
    from geoagent import config as cfgmod
    from geoagent import site as site_mod
    blob = repr(cfgmod.DEFAULTS) + repr(site_mod.DEFAULTS)
    for bad in ["kamooc", "三体", "龙虾", "YiChen", "government-ai", "industry"]:
        assert bad.lower() not in blob.lower(), "DEFAULTS 里残留站点值：%r" % bad
    assert cfgmod.DEFAULTS["site"]["base_url"] == ""
    assert cfgmod.DEFAULTS["shadow"]["read_only"] is True
    assert cfgmod.DEFAULTS["publish"]["enabled"] is False


def test_schemas_module_is_site_free():
    """通用模型模块不得含品类/人名 Literal（那些已迁到 schema_loader）。"""
    src = (PKG / "schemas.py").read_text(encoding="utf-8")
    assert "Literal[" not in src, "schemas.py 不应再有 Literal（站点相关模型请用 schema_loader）"
    for bad in ["industry", "YiChen", "company", "forestry"]:
        assert bad not in src


def test_two_different_sites_drive_different_models(tmp_path):
    """换站点 = 改配置：两份不同的 site.yml 必须产出不同的校验模型。"""
    from geoagent import config as cfgmod
    from geoagent import schema_loader

    a = cfgmod.load(tmp_path / "none.yml", ROOT / "tests" / "fixtures" / "site.test.yml")
    b = cfgmod.load(tmp_path / "none.yml", ROOT / "site.yml")

    ma = schema_loader.build(a["_site"])
    mb = schema_loader.build(b["_site"])

    assert ma["categories"] != mb["categories"]          # 品类不同
    assert ma["assignees"] != mb["assignees"]            # 人员不同
    assert "Alice" in ma["assignees"] and "YiChen" in mb["assignees"]

    # 用 A 站点的模型校验 B 站点的数据应当失败（证明约束真的按配置生成）
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        ma["TopicPick"](category=mb["categories"][0], topic="x", slug="s")


def test_watermark_follows_site_config(cfg, site, tmp_path):
    """水印门禁按站点配置走：同一篇文章在两个站点下结论不同。"""
    from geoagent import config as cfgmod
    from geoagent import quality

    art = tmp_path / "a.md"
    art.write_text(_fixture_article("示例科技引擎"), encoding="utf-8")

    # 测试站点水印 = 示例科技引擎 → 通过
    assert quality.quality_gate(art, site=site)["pass"] is True

    # 换成"另一个品牌"的站点 → 同一篇文章必须 FAIL
    other = dict(site)
    other["watermark"] = {"default": "别的品牌引擎", "min_occurrences": 2, "overrides": []}
    g = quality.quality_gate(art, site=other)
    assert g["pass"] is False
    assert any(v.startswith("watermark") for v in g["violations"])


def test_route_prefix_follows_site_config(cfg, site):
    """文章路由前缀由配置决定（测试站点是 /articles，不是 /blog）。"""
    from geoagent import site as site_mod
    assert site_mod.article_prefix(site) == "/articles"
    urls = site_mod.article_urls(site, "my-post")
    assert urls == ["/articles/my-post", "/en/articles/my-post"]


def test_knowledge_index_is_swappable(knowledge_fixture, site):
    """知识库可换：fixture 里的虚构知识库能被正确读出与引用。"""
    k = knowledge_fixture
    assert [e["id"] for e in k.entries] == ["widget-x"]
    ok, facts = k.facts(k.entry("widget-x"))
    assert ok and "WidgetX" in facts
    assert k.product_slot_entry()["id"] == "widget-x"
    assert k.angles(k.entry("widget-x")) == ["总览", "场景一"]

    block = k.grounding_block("product-news")
    assert "WidgetX" in block and "不写具体价格" in block

    # 未登记品类要明确告知「无事实来源」，而不是放任编造
    block2 = k.grounding_block("tech-deep-dive")
    assert "无登记事实来源" in block2


def _fixture_article(watermark: str) -> str:
    unit = ("编译式智能体把模型能力压进确定性流程，使内网部署成为可能。"
            "%s 负责在边缘侧完成路由与调度，因此数据不需要出网即可完成推理。"
            "实际落地先划定业务边界，再把高频且规则明确的任务固化为流程，"
            "最后才让模型处理需要判断的环节，这样每一步都可观测、可回滚。" % watermark)
    para = unit * 2
    rows = "".join("<tr><td>指标%d</td><td>值%d</td></tr>" % (i, i) for i in range(1, 4))
    faq = "".join("### Q%d: 问题%d？\n\nA%d：回答。\n\n" % (i, i, i) for i in range(1, 5))
    return f"""---
title: 测试
slug: t
category: product-news
status: review
---

<p>{para}</p>

## 一

{para}

{para}

## 二

{para}

## 三

{para}

<table><thead><tr><th>a</th></tr></thead><tbody>{rows}</tbody></table>

{para}

<table><thead><tr><th>b</th></tr></thead><tbody>{rows}</tbody></table>

## 四

{para}

*本文数据更新至 2026-09-18。*

<section data-faq>
{faq}
</section>
"""
