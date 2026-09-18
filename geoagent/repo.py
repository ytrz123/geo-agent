"""git 工作副本。

硬规则：
  * 只 `git add <本次文件>` —— 绝不 `git add .`（历史事故：把共享 clone 的非 metrics 跟踪文件
    标记为 D 从磁盘消失，破坏策略 Cron 的本地读取）。
  * push 只在非只读模式执行；影子期只 fetch/reset 读最新，不写远端。
  * 本项目的 workspace 是自己的副本（var/workspaces/geo-seo），不碰 /tmp/geo-seo-metrics，
    也不碰 ~/Documents/liujia/geo-seo（那是人工用的 clone）。
"""
from __future__ import annotations

import pathlib
import subprocess

from . import obs
from .config import get


class Repo:
    def __init__(self, cfg: dict, read_only: bool = True):
        self.read_only = read_only
        g = get(cfg, "gitea")
        user, pw = g.get("user"), g.get("password")
        host = str(g.get("base_url")).replace("http://", "").replace("https://", "").rstrip("/")
        self.url = "http://%s:%s@%s/%s.git" % (user, pw, host, g.get("repo"))
        from .config import abspath
        self.dir = abspath(cfg, "paths.workspace")
        self._dirty = []

    # ---------------------------------------------------------------- git 底层
    def _git(self, *args, timeout=120):
        proc = subprocess.run(["git", "-C", str(self.dir)] + list(args),
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()

    def exists(self) -> bool:
        return (self.dir / ".git" / "HEAD").exists() and (self.dir / ".git" / "config").exists()

    # ---------------------------------------------------------------- 生命周期
    def ensure_clone(self):
        """目录不存在 → clone；目录在但 .git 残缺（HEAD/config 缺失）→ nuke 重 clone。

        这两种是实测过的两类损坏：/tmp 被系统清理、以及 .git 骨架整体缺失导致
        `fatal: not a git repository`（reset/checkout 全部无效）。
        """
        if self.dir.exists() and not self.exists():
            obs.log("repo_corrupt_reclone", level="warn", dir=str(self.dir))
            import shutil
            shutil.rmtree(self.dir, ignore_errors=True)
        if not self.exists():
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(["git", "clone", "-q", self.url, str(self.dir)],
                                  capture_output=True, text=True, timeout=300)
            obs.log("repo_cloned", ok=proc.returncode == 0, rc=proc.returncode)
            return proc.returncode == 0
        return True

    def sync(self):
        """fetch + reset --hard origin/main（只动本项目 workspace）。"""
        if not self.ensure_clone():
            return False
        rc, _, err = self._git("fetch", "-q", "origin", "main")
        if rc != 0:
            obs.log("repo_fetch_failed", level="error", err=err[-200:])
            return False
        rc, _, err = self._git("reset", "--hard", "origin/main")
        if rc != 0:
            # 对象损坏的唯一出路：重 clone
            obs.log("repo_reset_failed_reclone", level="warn", err=err[-200:])
            import shutil
            shutil.rmtree(self.dir, ignore_errors=True)
            return self.ensure_clone()
        return True

    def status_short(self):
        rc, out, _ = self._git("status", "--short")
        return [l for l in out.split("\n") if l.strip()] if rc == 0 else []

    def head(self):
        rc, out, _ = self._git("log", "--oneline", "-1")
        return out if rc == 0 else ""

    # ---------------------------------------------------------------- 写
    def add(self, paths):
        """只 add 指定文件。传目录/'.' 会被拒绝 —— 这是防呆，不是建议。"""
        clean = []
        for p in paths:
            s = str(p)
            if s in (".", "-A", "-a", "*") or s.endswith("/"):
                obs.log("repo_add_rejected", level="error", path=s,
                        reason="只允许精确文件路径，禁止 git add . / 目录")
                return False
            clean.append(s)
        self._dirty = clean
        rc, _, err = self._git("add", *clean)
        if rc != 0:
            obs.log("repo_add_failed", level="error", err=err[-200:])
            return False
        return True

    def commit(self, message: str):
        if self.read_only:
            obs.log("repo_commit_skipped", level="warn", reason="read_only shadow mode",
                    would_add=self._dirty)
            return True
        rc, out, err = self._git("commit", "-m", message)
        if rc != 0:
            obs.log("repo_commit_failed", level="error", err=(err or out)[-200:])
            return False
        return True

    def push(self):
        if self.read_only:
            obs.log("repo_push_skipped", level="warn", reason="read_only shadow mode")
            return True
        rc, out, err = self._git("push", "-q", "origin", "main", timeout=180)
        if rc != 0:
            obs.log("repo_push_failed", level="error", err=err[-200:])
            return False
        return True

    # ---------------------------------------------------------------- 读
    def list_metrics(self, kind="daily"):
        d = self.dir / "metrics"
        if not d.exists():
            return []
        return sorted(p.name for p in d.glob("%s-*.json" % kind))

    # ---------------------------------------------------------------- 影子输出
    def out_path(self, rel: str) -> pathlib.Path:
        """写产物到哪里。

        影子期写 var/out/<rel>（绝不覆盖 workspace 里 clone 下来的真实文件 —— 否则对拍
        就没有基线可比了）；apply 模式才写回 workspace 供 commit/push。
        """
        from .config import ROOT
        base = (ROOT / "var" / "out") if self.read_only else self.dir
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def commit_files(self, rels, message: str):
        """只提交指定文件。影子期整体短路（只记录 would_add）。"""
        rels = [str(r) for r in rels]
        if self.read_only:
            obs.log("repo_commit_skipped", reason="read_only shadow mode", would_add=rels)
            return True
        if not self.add(rels):
            return False
        if not self.commit(message):
            return False
        return self.push()

    def bootstrap(self):
        """确保 workspace 可用（clone / 修损坏）。不拉取，只保证目录就位。"""
        return self.ensure_clone()
