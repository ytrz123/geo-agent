"""知识库索引读取（knowledge/index.yml）。

代码不假设知识库的目录结构，只按 index.yml 找文件；换知识库 = 换目录 + 改 index.yml。

两种来源（site.yml 里配 knowledge.source）：
  repo  （默认）→ 读 git 工作副本里的 knowledge/（与现状一致）
  local        → 读本项目 knowledge/ 目录
两种都先找 index.yml：本地项目内优先，其次远端仓库。
"""
from __future__ import annotations

import copy
import pathlib

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from . import obs
from .config import ROOT, get as cfg_get

DEFAULTS = {
    "defaults": {"policy": "", "rules": []},
    "entries": [],
}


def _deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Knowledge:
    """知识库访问层。

    root       文件解析根目录（本地路径）
    files_via  取文件内容的回调 (relpath) -> (ok, text)；用仓库源时走 Gitea API
    """

    def __init__(self, index: dict, root: pathlib.Path, files_via=None, source="local"):
        self.index = _deep_merge(DEFAULTS, index or {})
        self.root = pathlib.Path(root)
        self.files_via = files_via
        self.source = source

    # ---------------------------------------------------------------- 元数据
    @property
    def entries(self) -> list[dict]:
        return list(self.index.get("entries") or [])

    @property
    def policy(self) -> str:
        return (self.index.get("defaults") or {}).get("policy", "")

    @property
    def rules(self) -> list[str]:
        return list((self.index.get("defaults") or {}).get("rules") or [])

    def entry(self, entry_id: str) -> dict | None:
        for e in self.entries:
            if e.get("id") == entry_id:
                return e
        return None

    def for_category(self, category: str) -> list[dict]:
        """该品类适用的知识库条目。"""
        out = []
        for e in self.entries:
            cats = ((e.get("applies_to") or {}).get("categories") or [])
            if category in cats:
                out.append(e)
        return out

    def product_slot_entry(self) -> dict | None:
        """占用「产品篇」名额的条目。"""
        for e in self.entries:
            if (e.get("applies_to") or {}).get("is_product_slot"):
                return e
        return None

    def angles(self, entry: dict | None) -> list[str]:
        return list((entry or {}).get("angles") or [])

    # ---------------------------------------------------------------- 文件内容
    def _read(self, rel: str) -> tuple[bool, str]:
        if self.files_via is not None:
            return self.files_via(rel)
        p = self.root / rel
        try:
            return True, p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return False, "%s: %s" % (type(e).__name__, e)

    def facts(self, entry: dict | None) -> tuple[bool, str]:
        rel = ((entry or {}).get("files") or {}).get("facts")
        if not rel:
            return False, "entry 未登记 facts 文件"
        return self._read(rel)

    def faqs(self, entry: dict | None) -> tuple[bool, str]:
        rel = ((entry or {}).get("files") or {}).get("faqs")
        if not rel:
            return False, "entry 未登记 faqs 文件"
        return self._read(rel)

    # ---------------------------------------------------------------- Prompt 片段
    def grounding_block(self, category: str, max_chars: int = 6000) -> str:
        """给撰写 prompt 用的「事实来源」段落。

        没有登记知识库的品类会明确写「无登记事实来源」—— 让模型知道自己在裸奔，
        而不是假装有依据（这比静默更安全）。
        """
        ents = self.for_category(category)
        lines = []
        if self.policy:
            lines.append("知识库政策：%s" % self.policy)
        if self.rules:
            lines.append("硬口径（必须遵守）：")
            lines.extend("  - %s" % r for r in self.rules)

        if not ents:
            lines.append("")
            lines.append("⚠️ 本品类【无登记事实来源】：涉及具体参数/规格/名单/案例时，"
                         "只能用行业通用表述，不得编造具体数字或客户名。")
            return "\n".join(lines)

        for e in ents:
            ok, text = self.facts(e)
            lines.append("")
            lines.append("【%s】（来源：%s，版本 %s）"
                         % (e.get("title") or e.get("id"), e.get("source") or "未登记",
                            e.get("version") or "-"))
            if ok:
                body = text.strip()
                if len(body) > max_chars:
                    body = body[:max_chars] + "\n…（知识卡已截断）"
                lines.append(body)
            else:
                lines.append("（知识卡读取失败：%s —— 不得凭记忆编造参数）" % text)
            if self.angles(e):
                lines.append("可选切入角度（轮换，避免同题重复）：%s" % "、".join(self.angles(e)))
        return "\n".join(lines)

    def summary(self) -> dict:
        return {
            "source": self.source,
            "root": str(self.root),
            "entries": len(self.entries),
            "ids": [e.get("id") for e in self.entries],
            "policy_set": bool(self.policy),
            "rules": len(self.rules),
        }


# ------------------------------------------------------------------ 工厂
def _index_from_local() -> tuple[dict, pathlib.Path] | tuple[None, None]:
    p = ROOT / "knowledge" / "index.yml"
    if not p.exists() or yaml is None:
        return None, None
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}, ROOT / "knowledge"
    except Exception as e:  # noqa: BLE001
        obs.log("knowledge_index_parse_failed", level="error", path=str(p), err=str(e))
        return None, None


def _repo_knowledge_dir(repo):
    """仓库里的 knowledge/ 目录（存在才返回）。"""
    if repo is None or not hasattr(repo, "dir"):
        return None
    d = pathlib.Path(repo.dir) / "knowledge"
    return d if d.exists() else None


def load(cfg: dict, repo=None, site: dict | None = None) -> Knowledge:
    """构造 Knowledge。

    ★ index.yml 与文件根目录是**分开找**的：
      - index.yml：本地项目 knowledge/index.yml 优先（它是 geo-agent 的元数据），
        其次仓库 knowledge/index.yml
      - 文件根目录：由 knowledge.source 决定
          repo  → 仓库 workspace 的 knowledge/（默认，与现状一致）
          local → 本项目 knowledge/
      这样可以「索引在项目内、事实文件在仓库里」，也支持全部本地化。
    """
    source = (cfg_get(cfg, "knowledge.source") or "").strip()
    local_index, local_root = _index_from_local()

    remote_dir = _repo_knowledge_dir(repo)
    remote_index = None
    if remote_dir is not None:
        idx = remote_dir / "index.yml"
        if idx.exists() and yaml is not None:
            try:
                remote_index = yaml.safe_load(idx.read_text(encoding="utf-8")) or {}
            except Exception as e:  # noqa: BLE001
                obs.log("knowledge_index_parse_failed", level="warn", path=str(idx), err=str(e))

    if not source:
        source = "repo" if remote_dir is not None else "local"

    index = local_index or remote_index or {}
    if source == "repo" and remote_dir is not None:
        root = remote_dir
    else:
        root = local_root or (ROOT / "knowledge")

    k = Knowledge(index, root, source=source)
    obs.log("knowledge_loaded", **k.summary())
    if not k.entries:
        obs.log("knowledge_empty", level="warn",
                reason="没有登记任何知识库条目 —— 内容生成将完全没有事实依据")
    return k
