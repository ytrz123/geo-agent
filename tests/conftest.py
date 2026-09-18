from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from geoagent import config as cfgmod  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def cfg(tmp_path):
    """默认配置 + 隔离到 tmp 的路径。

    刻意从「文件不存在」加载 → 只拿 DEFAULTS，这样测试不依赖本机 config.yml 内容。
    站点用 tests/fixtures/site.test.yml（一个**虚构**站点），
    以此保证测试不依赖任何真实站点配置 —— 这本身就是通用性的回归。
    """
    c = cfgmod.load(tmp_path / "no-such-config.yml", FIXTURES / "site.test.yml")
    c["paths"]["workspace"] = str(tmp_path / "workspace")
    c["paths"]["logs"] = str(tmp_path / "logs")
    c["paths"]["state_db"] = str(tmp_path / "state.db")
    c["wecom"]["webhook_url"] = ""
    return c


@pytest.fixture
def site(cfg):
    return cfg["_site"]


@pytest.fixture
def knowledge_fixture(cfg, tmp_path):
    """用本地 fixture 知识库（虚构产品），不依赖仓库里的真实知识库。"""
    from geoagent import knowledge as k_mod
    kdir = tmp_path / "kernel"
    (kdir / "products").mkdir(parents=True)
    (kdir / "index.yml").write_text(
        "defaults:\n"
        "  policy: 事实必须来自登记文件\n"
        "  rules:\n"
        "    - 不写具体价格\n"
        "entries:\n"
        "  - id: widget-x\n"
        "    title: 示例小部件 WidgetX\n"
        "    files:\n"
        "      facts: products/widget-x.md\n"
        "      faqs: products/widget-x-faqs.md\n"
        "    applies_to:\n"
        "      categories: [product-news]\n"
        "      is_product_slot: true\n"
        "    watermark: 示例小部件 WidgetX\n"
        "    angles: [总览, 场景一]\n",
        encoding="utf-8")
    (kdir / "products" / "widget-x.md").write_text(
        "WidgetX 是一个示例产品。规格：10cm，电池 12 小时。", encoding="utf-8")
    (kdir / "products" / "widget-x-faqs.md").write_text("Q: 能用多久？A: 12 小时。",
                                                        encoding="utf-8")
    return k_mod.Knowledge(
        __import__("yaml").safe_load((kdir / "index.yml").read_text(encoding="utf-8")),
        kdir, source="local")


@pytest.fixture
def metrics_fixtures():
    d = FIXTURES / "metrics"
    if not d.exists():
        pytest.skip("metrics fixtures 未采集（跑 tools_fetch_metrics.py）")
    return d


@pytest.fixture
def real_article():
    """一篇真实已发布文章（质检门禁本来就能过）—— 管道测试用真数据而不是手搓。"""
    ws = ROOT / "var" / "workspaces" / "geo-seo" / "output" / "articles"
    if not ws.exists():
        pytest.skip("workspace 未 clone（先跑一次 cli.py seo-daily）")
    cands = sorted(ws.glob("*20260913*.md"))
    if not cands:
        cands = sorted(ws.glob("*.md"))
    if not cands:
        pytest.skip("workspace 里没有文章")
    return cands[0]
