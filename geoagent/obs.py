"""结构化日志 + 运行 trace。

存在两个目的：
1. 替代「翻 session 文件判断 cron 是否在跑」——每次运行落一份 JSONL trace，一眼看清走到哪一步。
2. 影子期对拍 —— trace 里逐节点记录产物数字，直接和旧实现比。
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys
import threading

_LOCK = threading.Lock()
_CTX = {"run_id": "", "log_dir": None, "trace_path": None}


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def start_run(log_dir, pipeline: str, mode: str = "dry-run") -> str:
    """开一次运行：建 var/logs/<pipeline>-<ts>.jsonl 与 latest 软链。"""
    log_dir = pathlib.Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = "%s-%s" % (pipeline, _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    path = log_dir / (run_id + ".jsonl")
    _CTX.update({"run_id": run_id, "log_dir": log_dir, "trace_path": path})
    log("run_start", pipeline=pipeline, mode=mode, run_id=run_id)
    latest = log_dir / ("latest-%s.jsonl" % pipeline)
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(path.name)
    except OSError:
        pass
    return run_id


def run_id() -> str:
    return _CTX["run_id"]


def trace_path():
    return _CTX["trace_path"]


def log(event: str, level: str = "info", **fields):
    """一行 JSON。始终同时打到 stderr，便于交互运行时直接看。"""
    rec = {"ts": now(), "run_id": _CTX["run_id"] or "-", "level": level, "event": event}
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with _LOCK:
        path = _CTX["trace_path"]
        if path is not None:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
        print(line, file=sys.stderr)


def node_start(node: str, **fields):
    log("node_start", node=node, **fields)


def node_done(node: str, **fields):
    log("node_done", node=node, **fields)


def read_trace(path) -> list[dict]:
    """读回一份 trace（对拍脚本用）。"""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return out
