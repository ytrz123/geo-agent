# geo-agent

**把「SEO 监控 → AI 引用检测 → 策略自进化 → 写稿质检 → 定时发布」这条 GEO/SEO 运营链路，做成 5 条可独立运行的 LangGraph 管道。**

站点、品牌、品类、人员、知识库全部走配置文件，代码里不含任何具体站点痕迹 ——
换网站、换品牌、换知识库只改 YAML，代码一行不动。

```bash
python cli.py check          # 配置自检
python cli.py seo-daily      # 影子跑（只读，不碰任何远端）
```

- 运行环境：macOS / Linux · Python 3.12+ · 实测 3.12.3
- 回归：63 项 pytest，全部跑在**虚构站点**上
- 状态：影子期（并行对拍中），默认只读

---

## 1. 设计原则

| 原则 | 落地方式 |
|---|---|
| **配置驱动** | 站点档案 `site.yml` + 知识库索引 `knowledge/index.yml`；品类/负责人校验模型按配置**动态生成** |
| **默认只读** | 没有 `--apply` 就绝不 POST/PATCH/PUT、git 不 push；写操作统一短路为 `would_*` 记入 trace |
| **官网零风险** | 发官网有三重独立锁，且 `publish.enabled` **默认为 false**（代码级拒绝） |
| **失败不静默** | 所有节点把失败写进 `state["errors"]`，不抛异常；落 JSONL trace + 终端报告 + 可通知 |
| **可断点续跑** | LangGraph + SqliteSaver checkpointer |
| **纯函数可单测** | 覆盖率、metrics 归一化是不碰 IO 的纯函数，历史 schema 4 种都有回归 |

---

## 2. 快速开始

```bash
git clone https://github.com/liujia123-oss/geo-agent.git   # 私有仓库
cd geo-agent

python3 -m venv .venv
.venv/bin/pip install --index-url https://pypi.org/simple/ -r requirements.txt

cp config.example.yml config.yml    # 运行配置（含凭据 → 已 .gitignore）
cp site.example.yml   site.yml      # 站点档案（换成你的站点照模板填）

.venv/bin/python cli.py check       # ★ 配置自检：站点 / 知识库 / 动态模型
.venv/bin/python cli.py list        # 列出 5 条管道
.venv/bin/python cli.py seo-daily   # 管道 A 影子跑（只读）
.venv/bin/python -m pytest tests -q # 63 passed
```

> **pip 源坑**：国内镜像对 `langgraph` / `langchain` 系常返回 403（表现为「找不到可用版本」）。
> 装依赖请显式带上 `--index-url https://pypi.org/simple/`。

依赖全部 pin 在 `requirements.txt`（LangChain 系迭代快，不 pin 会在某天早上无声崩掉）。

---

## 3. 五条管道

| 命令 | 管道 | LLM 节点 | 产出文件（相对路径） |
|---|---|---|---|
| `publish` | D · 定时发布 | 0 | 官网文章（三重锁放行时）+ Gitea 状态回写 |
| `seo-daily` | A · SEO 每日 | 0 | `metrics/daily-YYYY-MM-DD.json` |
| `geo-weekly` | B · GEO 每周 | 1 | `metrics/weekly-YYYY-Www.json` |
| `strategy` | C · 策略自进化 | 2 | `metrics/weekly-YYYY-Www-strategy.json` + 选题工单 |
| `content` | E · 内容生成 | 1 + 回炉环 | 草稿提交 + 工单评论 |

> **产物落在哪**：影子期（默认）一律落 `var/out/<相对路径>`，**绝不覆盖** workspace 里
> clone 下来的真实文件（否则对拍就没有基线可比）；只有 `--apply` 才写回 workspace 供 commit/push。
> 例：`var/out/metrics/daily-2026-09-18.json`。

节点链（LangGraph 图，见 `geoagent/graphs/`）：

```
D publish     sync_repo → scan ─┬→ process_article ×N ─┐
                                └→ noop ───────────────┴→ comment_and_close
                                                        → writeback_status
                                                        → git_commit_push → notify
              process_article 内部：质检 → md→HTML → 查重 → 发布 → 评论④ → 关闭

A seo-daily   bootstrap → [fetch_sitemap / fetch_robots / fetch_jsonld /
                           fetch_articles / fetch_crawlers  ×5 并发]
              → compute_metrics （扇入屏障）→ evaluate_rules
              → upsert_issues ┐
              → skip_notify   ┴→ write_daily → notify
              （无异常走 skip_notify，有异常才建工单）

B geo-weekly  bootstrap → fetch_crawlers → run_citation_check ─┬→ llm_analyze ─┐
                                              (cookie 失效) └→ cookie_invalid ┴
              → upsert_issues → write_weekly → notify

C strategy    bootstrap → load_window → load_rules → measure → check_rules
              → llm_week_review → llm_pick_topics → guard_open_batch
              → create_article_issues → apply_weight_changes
              → write_weekly_strategy → notify

E content     bootstrap → pick_issue ─┬→ write_one ─┐
                                      └→ noop ──────┴→ comment_and_push → notify
              write_one 内部：撰写 → 质检 → FAIL 回炉（≤ quality.max_fix_rounds）
```

公共参数：`--config <file>` `--site <file.yml>` `--dry-run`（默认）`--apply` `--notify`
`--no-checkpoint` `--no-llm` `--with-citation`（管道 B 真跑引用检测，Playwright，2–3 分钟）。

---

## 4. 三份配置的分工

| 文件 | 作用 | 含凭据 | 可进仓库 |
|---|---|---|---|
| `config.yml` | 运行环境：路径、开关、客户端地址、阈值 | 是（可改走环境变量） | ❌ 已 `.gitignore` |
| `site.yml` | ★ 站点档案：域名、品牌别名、水印、路由、品类、人员、查询词 | 否 | ✅ |
| `knowledge/index.yml` | ★ 知识库清单：事实来源登记 + 硬口径 | 否 | ✅ |

**换站点三步**：改 `site.yml` 的 `site`/`publish`/`routes` → 改 `categories` → 改 `knowledge/index.yml`。
代码一行不用动，细节见 `使用说明.md §3`。

### 环境变量

配置值支持三种写法：`${ENV_NAME}` 占位符 / 同名的环境变量优先 / 直接字面值（向后兼容）。
凭据**只写环境变量名进 YAML**，真值放环境里。

| 变量 | 用途 |
|---|---|
| `LLM_API_KEY` | LLM 网关密钥（`llm.base_url` 填在 config.yml） |
| `GITEA_USER` / `GITEA_PASSWORD` | Gitea 工单（`auth_mode: basic`；也可用 `gitea.token`） |
| `STRAPI_TOKEN` | Strapi 写入 token，仅 `publish.enabled=true` 时需要 |
| `DOUBAO_COOKIE` | 引用检测的 sessionid cookie（2–7 天过期） |
| `WECOM_WEBHOOK` | 企业微信群机器人 webhook |
| `GEOAGENT_CONFIG` | 覆盖默认的 `config.yml` 路径 |
| `GEOAGENT_SITE` | 覆盖默认的 `site.yml` 路径 |

变量名本身也可在 `site.yml` 里改（`publish.token_env` / `citation_check.cookie_env` / `wecom.webhook_env`）。

---

## 5. 写权限模型

```
                          ┌── shadow.read_only=false (config.yml, 默认 true)
写 Gitea / Strapi / git ──┼── --apply (命令行)
                          └── 两者同时满足才真写，否则只记 would_*

发官网 ── 额外第三道：publish.enabled=true (site.yml, 默认 false)
          ← 用户明确要求不发布到官网；只改这里不生效，必须三锁同时放
发企业微信 ── --notify 独立开关（通知不算写生产）
```

默认状态下跑任何管道都**不会**产生远端写入，可放心在真站点上影子跑。

---

## 6. 硬约束（写死在代码里，不是注释）

1. 所有节点返回 `errors[]`，不抛异常 —— 失败不静默。
2. 只读模式（默认）下：Strapi/Gitea/git 写操作全部短路并记 `would_*` 到 trace。
3. 官网发布三重锁：`publish.enabled` + `shadow.read_only=false` + `--apply`。
4. 覆盖率算法唯一化：按 locale 归一化去重后的 slug 数 ÷ `meta.pagination.total`；分母不可得 → `None`（不是 0）。
5. `git add` 只加本次文件，绝不 `git add .`。
6. Gitea 标签查询用**标签名**而非数字 ID；关闭 Issue 走 basic auth。
7. Strapi 发布用 `documentId`；title/slug/category 一律 `.strip('"')`；品类过白名单。
8. 企业微信必须校验 `errcode`；HTTP 200 但 errcode≠0 视为失败。
9. 品类/负责人校验规则按 `site.yml` **动态生成**（换站点不会崩）。
10. 换品牌/换站点后水印门禁自动跟着配置走（有内置回归测试验证）。

---

## 7. 目录结构

```
cli.py                    入口（5 管道 + list + check）
site.yml                  站点档案（唯一认识具体站点的地方）
knowledge/index.yml       知识库清单

geoagent/
  config.py               运行配置 + 凭据解析（${ENV} / 环境变量 / 字面值）
  site.py                 站点档案加载与解释（hosts/brand/watermark/routes/validate）
  knowledge.py            知识库索引（entries / facts / grounding_block 注入）
  schema_loader.py        按站点动态生成校验模型（品类、负责人 Literal）
  llm.py                  LLM 工厂 + 结构化输出（原生不可用自动回退文本+JSON）
  quality.py              质检门禁（官方脚本 + 站点水印覆盖）+ md→HTML
  obs.py                  JSONL 结构化日志 + trace
  repo.py                 git（只 add 精确文件 / 修损坏 clone / 影子输出目录）
  metrics/                覆盖率、归一化（纯函数，可单测）
  nodes/ graphs/          节点与 LangGraph 图（publish/seo/geo/strategy/content/notify）
  clients/                gitea / strapi(+NoopCms) / wecom
  tools/                  上游脚本的定制副本（阈值与品牌已参数化）

tests/                    63 项；fixtures/site.test.yml 是虚构站点
var/                      运行产物（logs / out / workspaces / state，均不入库）
tools_audit_no_write.py   只读取证：确认从未写入官网/Gitea
tools_shadow_diff.py      影子对拍：新产物 vs 线上归档逐字段比
audit_hardcode.sh         扫描非通用硬编码
```

---

## 8. 日常运维与排障

```bash
.venv/bin/python cli.py check                       # 改完 site.yml / knowledge 先跑这个
cat var/logs/latest-seo-daily.jsonl                 # 看最近一次运行发生了什么
ls -t var/logs/ | head                              # 历史 run 列表

.venv/bin/python tools_audit_no_write.py            # 有没有偷偷写到生产
.venv/bin/python tools_shadow_diff.py --date 2026-09-18   # 影子对拍
bash audit_hardcode.sh                              # 扫非通用硬编码
.venv/bin/python -m pytest tests -q                 # 回归
```

| 现象 | 处理 |
|---|---|
| `check` 报 `gitea.base_url/repo 未配置` | 工单功能不可用，补齐 config.yml 的 `gitea` 段 |
| 引用检测走到 `cookie_invalid` | `DOUBAO_COOKIE` 过期（2–7 天），刷新后重跑；不会虚构数据 |
| 节点报错但进程没退出 | 正常设计：错误在 `state["errors"]` + trace 末尾，用 `_report` 输出看 |
| 断点续跑状态异常 | 删 `var/state/geoagent.db`，或用 `--no-checkpoint` 跑无状态 |

**未接调度**：目前全部手工触发，稳定后可加 cron 指向 `cli.py <管道>`。

---

## 9. 与旧实现的关系

旧的 4 个 Hermes cron job + `geo-seo-pipeline` skill（文本驱动版）**保持原样不动**，与本项目并行影子跑：

```
旧：skill + 4 个 cron job          新：本项目
   cron 表达式触发                    cli.py <管道>；配置描述站点
   共享 clone /tmp/...                独立 var/workspaces/geo-seo
```

- 旧 job 仍在执行，本项目在影子期只读，不与之争抢写入。
- 旧的 4 个脚本在 `geoagent/tools/` 下是**定制副本**（阈值/品牌参数化），原文件仍归 skill 所有。
- **对拍一致后才摘旧 cron job。**

已知限制与待办（5 个品类暂无事实知识库、企业微信通知待实测、未接调度、CMS 只实现 strapi）见 `使用说明.md §10`。

---

## 10. 更多文档

| 文档 | 内容 |
|---|---|
| `使用说明.md` | 逐节详解：换站点/换知识库怎么做、字段与文件地图、写权限放行条件、已知限制 |
| `通用化改造方案.md` | 把 9 类 38 处硬编码收拢成配置的改造记录与验证方法 |
