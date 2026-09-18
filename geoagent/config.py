"""配置加载。

两个文件分工明确：
  config.yml  —— 运行环境：路径、开关、凭据、客户端地址（机器相关，不进仓库）
  site.yml    —— 站点档案：域名、品牌、品类、水印、路由、知识库索引（站点相关，可进仓库）

本模块把两者合并成一个 cfg 视图，代码继续用 cfg[...] 取值。
站点强相关的取值请用 geoagent.site 模块（它知道怎么解释这些字段）。

⚠️ 本文件不含任何具体域名/品牌/人名的兜底默认值 —— 那些都在 site.yml 里。
"""
import copy
import os
import pathlib

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent

DEFAULTS = {
    "shadow": {"read_only": True},
    # 官网发布默认关闭（用户 2026-09-18 要求）。改 false 也不能单独生效，仍需 read_only=false + --apply。
    "publish": {"enabled": False},
    "paths": {
        "workspace": "var/workspaces/site",
        "logs": "var/logs",
        "state_db": "var/state/geoagent.db",
    },
    # 站点值的兜底（正常应由 site.yml 提供；留空以便缺配置时给出清晰报错）
    "site": {"base_url": "", "static_pages": 0},
    "strapi": {"base_url": "", "token": ""},
    # 知识库来源：repo（读 git 副本里的 knowledge/）| local（读本项目 knowledge/）
    "knowledge": {"source": ""},
    "gitea": {
        "base_url": "",
        "repo": "",
        "auth_mode": "basic",
        "user": "",
        "password": "",
        "token": "",
    },
    "llm": {
        "base_url": "",
        "api_key": "",
        "model": "",
        "temperature": 0,
        "timeout": 120,
    },
    "wecom": {
        "webhook_url": "",
        "mention_all_on_alarm": False,
        "max_chars": 1500,
        "retry": 3,
        "connect_timeout": 8,
    },
    "notify": {
        "push_new_issues": True,
        "push_issue_updates": False,
        "push_publish": True,
        "quiet_hours": ["23:30-07:00"],
        "quiet_exempt_alarm": False,
    },
    # quality / thresholds 的真实值在 site.yml；这里给最小的安全兜底
    "quality": {"min_chars": 1500, "min_h2": 4, "min_faq": 4, "min_tables": 2,
                "max_fix_rounds": 3},
    "thresholds": {"sitemap_coverage_min": 0.90, "lcp_max_seconds": 4.0,
                   "doubaobot_zero_streak_days": 3},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path(path=None) -> pathlib.Path:
    if path:
        return pathlib.Path(path).expanduser()
    env = os.environ.get("GEOAGENT_CONFIG")
    if env:
        return pathlib.Path(env).expanduser()
    return ROOT / "config.yml"


def load(path=None, site_file=None) -> dict:
    """读 config.yml + site.yml 并合并。

    site.yml 的 quality / thresholds 会覆盖 config.yml 的同名段 —— 让站点档案成为
    这两类的单一真相源，同时保持代码里 `get(cfg,'quality.x')` 的取值方式不变。
    """
    p = config_path(path)
    data = {}
    if p.exists():
        if yaml is None:
            raise RuntimeError("读取 config.yml 需要 pyyaml：pip install -r requirements.txt")
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    cfg = _deep_merge(DEFAULTS, data)
    cfg["_meta"] = {"config_file": str(p), "exists": p.exists()}

    # 延迟导入避免与 site 模块循环依赖
    from . import site as site_mod
    site = site_mod.load_site(cfg, site_file)

    # 向后兼容：config.yml 里的 site.base_url / site.static_pages 作为兜底（旧配置仍生效）
    legacy_base = get(cfg, "site.base_url")
    if legacy_base and not site_mod.get(site, "site.base_url"):
        site["site"]["base_url"] = legacy_base
    legacy_static = get(cfg, "site.static_pages")
    if legacy_static and not site_mod.get(site, "thresholds.static_pages_expected"):
        site["thresholds"]["static_pages_expected"] = legacy_static

    cfg["_site"] = site
    cfg["_meta"]["site_file"] = site["_meta"]["site_file"]
    cfg["_meta"]["site_exists"] = site["_meta"]["exists"]

    # 站点档案里的 quality / thresholds 覆盖 config.yml
    cfg["quality"] = _deep_merge(cfg.get("quality", {}), site_mod.get(site, "quality", {}) or {})
    cfg["thresholds"] = _deep_merge(cfg.get("thresholds", {}),
                                    site_mod.get(site, "thresholds", {}) or {})

    # 凭据解析：${ENV} 占位符 / 环境变量优先
    cfg["strapi"]["token"] = site_mod.resolve_secret(
        cfg["strapi"].get("token"), site_mod.resolve_env_name(site, "publish.token_env"))
    cfg["wecom"]["webhook_url"] = site_mod.resolve_secret(
        cfg["wecom"].get("webhook_url"),
        site_mod.resolve_env_name(site, "wecom.webhook_env") or "WECOM_WEBHOOK")
    cfg["llm"]["api_key"] = site_mod.resolve_secret(cfg["llm"].get("api_key"), "LLM_API_KEY")
    cfg["gitea"]["user"] = site_mod.resolve_secret(cfg["gitea"].get("user"), "GITEA_USER")
    cfg["gitea"]["password"] = site_mod.resolve_secret(cfg["gitea"].get("password"),
                                                      "GITEA_PASSWORD")
    return cfg


def get(cfg: dict, dotted: str, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def abspath(cfg: dict, dotted: str) -> pathlib.Path:
    """把配置里的相对路径解析到项目根下。"""
    raw = get(cfg, dotted)
    if raw is None:
        raise KeyError(dotted)
    p = pathlib.Path(raw).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return p


def ensure_dirs(cfg: dict) -> dict:
    """建好 var/ 下的目录；返回解析后的绝对路径。"""
    out = {}
    for key in ("paths.workspace", "paths.logs"):
        p = abspath(cfg, key)
        p.mkdir(parents=True, exist_ok=True)
        out[key] = p
    db = abspath(cfg, "paths.state_db")
    db.parent.mkdir(parents=True, exist_ok=True)
    out["paths.state_db"] = db
    return out


def read_only(cfg: dict, apply_flag: bool = False) -> bool:
    """是否处于只读（影子）模式。

    两道闸：配置的 shadow.read_only 与命令行的 --apply，必须同时放行才写。
    """
    shadow = bool(get(cfg, "shadow.read_only", True))
    return shadow or not apply_flag


def check_env(cfg: dict) -> list[str]:
    """启动自检：缺什么凭据要早报，而不是跑到一半失败。返回问题清单。"""
    from . import site as site_mod
    site = cfg.get("_site") or {}
    problems = list(site_mod.validate(site))
    cms = site_mod.get(site, "publish.cms", "none")
    publish_on = bool(get(cfg, "publish.enabled"))
    if cms == "strapi" and publish_on and not cfg["strapi"].get("token"):
        # 只在「真要发布」时才算问题；影子期只读接口通常公开，不需要 token
        problems.append("publish.cms=strapi 且 publish.enabled=true，但 Strapi token 为空"
                        "（设 %s 环境变量或 config.yml strapi.token）"
                        % site_mod.resolve_env_name(site, "publish.token_env"))
    if not cfg["gitea"].get("base_url") or not cfg["gitea"].get("repo"):
        problems.append("gitea.base_url / gitea.repo 未配置（工单功能不可用）")
    return problems
