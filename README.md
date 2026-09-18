# geo-agent

GEO/SEO 闭环的 LangGraph 版实现（改造自 `geo-seo-pipeline` 的文本驱动流程）。

**通用化**：代码里不含任何具体站点、品牌、品类、人名。
换网站、换品牌、换知识库，只改配置（`site.yml` + `knowledge/index.yml`），代码零修改。

**这个目录是独立副本：不放进旧的 geo-seo 仓库，不接任何 git remote，不上传任何代码仓库。**
影子期规则：默认只读。没有显式 `--apply` 时，绝不 POST/PATCH/PUT，git 只本地不 push；
官网发布另有独立硬锁 `site.yml → publish.enabled`（默认 false）。

## 快速开始

```bash
cd ~/Documents/geo-agent
.venv/bin/pip install --index-url https://pypi.org/simple/ -r requirements.txt   # 已装好，需要时用
cp site.example.yml site.yml          # 站点档案（换成你的站点照模板填）
cp config.example.yml config.yml      # 运行配置（凭据可走环境变量）
.venv/bin/python cli.py check         # ★ 配置自检
.venv/bin/python cli.py list          # 列出 5 条管道
.venv/bin/python cli.py seo-daily     # 管道 A 影子跑（只读）
.venv/bin/python -m pytest tests -q   # 63 项回归（全部跑在虚构站点上）
```

## 三份配置的分工

| 文件 | 作用 | 含凭据 | 可进仓库 |
|---|---|---|---|
| `config.yml` | 运行环境：路径、开关、客户端地址 | 是（可改走环境变量） | ❌（已 .gitignore） |
| `site.yml` | ★ 站点档案：域名、品牌、水印、路由、品类、人员、查询词 | 否 | ✅ |
| `knowledge/index.yml` | ★ 知识库清单：事实来源登记 + 硬口径 | 否 | ✅ |

换站点三步：改 `site.yml` 的 site/publish/routes → 改 `categories` → 改 `knowledge/index.yml`。
代码一行不用动（见 `使用说明.md` §3）。

## 五条管道

| 命令 | 管道 | LLM 节点 | 说明 |
|---|---|---|---|
| `publish` | D 定时发布 | 0 | 扫 status:review → 质检 → md→HTML → 查重 → 发布 → 评论④ → 关闭 → 回写 → push |
| `seo-daily` | A SEO 每日 | 0 | 抓取 ×5 → 算覆盖率 → 规则判定 → 工单去重 → 写 daily → push |
| `geo-weekly` | B GEO 每周 | 1 | 爬虫统计 → 引用检测 → 建议生成 → 工单去重 → 写 weekly |
| `strategy` | C 策略自进化 | 2 | 读 4 周窗口 → 规则+封顶 → 周报叙述 → 选题 → 批次守卫 → 建工单 |
| `content` | E 内容生成 | 1 + 回炉环 | 取工单 → 撰写 → 质检 → FAIL 回炉 ≤3 轮 → 评论③ → push |

## 硬约束（写死在代码里，不是注释）

1. 所有节点返回 `errors[]`，不抛异常 —— 失败不静默。
2. 只读模式（默认）下：Strapi/Gitea/git 写操作全部短路并记 `would_*` 到 trace。
3. 官网发布三重锁：`publish.enabled` + `shadow.read_only=false` + `--apply`。
4. 覆盖率算法唯一化：按 locale 归一化去重后的 slug 数 ÷ `meta.pagination.total`；分母不可得 → `None`（不是 0）。
5. `git add` 只加本次文件，绝不 `git add .`。
6. Gitea 标签查询用**标签名**，不用数字 ID；关闭 Issue 走 basic auth。
7. Strapi 发布用 `documentId`；title/slug/category 一律 `.strip('"')`；品类过白名单。
8. 企业微信必须校验 `errcode`；HTTP 200 但 errcode≠0 视为失败。
9. 品类/负责人校验规则按 `site.yml` **动态生成**（换站点不会崩）。
10. 换品牌/换站点后水印门禁自动跟着配置走（内置回归测试验证）。

## 目录速览

```
cli.py                入口（5 管道 + list + check）
site.yml              站点档案（唯一认识具体站点的地方）
knowledge/index.yml   知识库清单
geoagent/
  site.py             站点档案加载与解释
  knowledge.py        知识库索引（含 grounding_block 注入）
  schema_loader.py    按站点动态生成校验模型
  config.py           运行配置 + 凭据解析（${ENV} / 环境变量 / 字面值）
  llm.py              LLM + 结构化输出（原生不可用自动回退文本+JSON）
  quality.py          质检门禁（官方脚本 + 站点水印覆盖）
  metrics/            覆盖率、归一化（纯函数，可单测）
  nodes/ graphs/      节点与 LangGraph 图
  clients/            gitea / strapi(+NoopCms) / wecom
  tools/              上游脚本的定制副本（阈值与品牌已参数化）
tests/                63 项；fixtures/site.test.yml 是虚构站点
var/                  运行产物（logs / out / workspaces / state）
tools_audit_no_write.py   只读取证：确认从未写入官网
tools_shadow_diff.py      影子对拍：新产物 vs 线上归档
audit_hardcode.sh         扫描非通用硬编码
```

## 与旧实现的关系

- 旧的 4 个 cron job + `geo-seo-pipeline` skill **保持原样不动**，本项目与它并行影子跑。
- 旧的 4 个脚本在本项目 `geoagent/tools/` 下是**定制副本**（参数化），原文件仍归 skill 所有。
- 对拍一致后才摘旧 cron job。本项目本身不需要进任何仓库。
