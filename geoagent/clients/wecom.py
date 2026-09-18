"""企业微信机器人群通知。

铁律：
  * HTTP 200 但 errcode≠0 也是失败（93000 key 无效 / 45009 超频）→ 必须校验 errcode。
  * 群机器人限频约 20 条/分钟 → 一批工单必须合并成 1 条。
  * markdown 上限约 4096 字节，中文 3 字节 → 截断到 max_chars(默认 1500)。
  * key 必须人工在企业微信里创建，程序无法生成；未配置时只记录不发送（不报错、不静默）。
"""
from __future__ import annotations

import datetime as _dt
import json
import time
import urllib.error
import urllib.request

from .. import obs
from ..config import get


class WeCom:
    def __init__(self, cfg: dict, read_only: bool = True, allow_send: bool = False):
        self.url = get(cfg, "wecom.webhook_url") or ""
        self.max_chars = int(get(cfg, "wecom.max_chars", 1500))
        self.retry = int(get(cfg, "wecom.retry", 3))
        self.timeout = int(get(cfg, "wecom.connect_timeout", 8))
        self.mention_all_on_alarm = bool(get(cfg, "wecom.mention_all_on_alarm", False))
        self.quiet_hours = get(cfg, "notify.quiet_hours", []) or []
        self.quiet_exempt_alarm = bool(get(cfg, "notify.quiet_exempt_alarm", False))
        self.read_only = read_only
        # 通知不是「写生产」：影子期也允许真发，但必须显式 --notify 放行。
        # 这样 --apply（写 Gitea/Strapi）与 --notify（发群）互相独立，不会为了发消息而开写权限。
        self.block_send = bool(read_only) and not bool(allow_send)

    # ---------------------------------------------------------------- 静默期
    def in_quiet_hours(self, when: _dt.datetime | None = None):
        now = when or _dt.datetime.now()
        cur = now.hour * 60 + now.minute
        for rng in self.quiet_hours:
            try:
                a, b = [x.strip() for x in str(rng).split("-")]
                ah, am = [int(x) for x in a.split(":")]
                bh, bm = [int(x) for x in b.split(":")]
            except ValueError:
                continue
            start, end = ah * 60 + am, bh * 60 + bm
            if start <= end:
                if start <= cur < end:
                    return True, str(rng)
            else:  # 跨零点
                if cur >= start or cur < end:
                    return True, str(rng)
        return False, None

    # ---------------------------------------------------------------- 发送
    def send(self, text: str, mention_all: bool = False, is_alarm: bool = False):
        """返回 (ok, detail)。任何一步失败都返回结构化结果，不抛异常。"""
        body_text = text
        if len(body_text) > self.max_chars:
            body_text = body_text[: self.max_chars - 20] + "\n…（已截断）"
        if mention_all and self.mention_all_on_alarm:
            body_text += "\n<@all>"

        if not self.url:
            obs.log("wecom_not_configured", level="warn",
                    reason="config.yml 里 wecom.webhook_url 为空 —— 群机器人 key 需人工创建")
            return False, {"error": "not_configured"}

        quiet, rng = self.in_quiet_hours()
        if quiet and not (is_alarm and self.quiet_exempt_alarm):
            obs.log("wecom_quiet_hours_suppressed", rng=rng)
            return True, {"suppressed": "quiet_hours", "range": rng}

        if self.block_send:
            obs.log("wecom_send_skipped", level="warn", reason="shadow mode（加 --notify 可真发）",
                    preview=body_text[:200])
            return True, {"_skipped": "shadow_mode"}

        payload = {"msgtype": "markdown", "markdown": {"content": body_text}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last = "unknown"
        for i in range(max(1, self.retry)):
            try:
                req = urllib.request.Request(self.url, data=data,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    resp = json.loads(r.read().decode("utf-8", "replace") or "{}")
                if resp.get("errcode") == 0:
                    obs.log("wecom_sent", chars=len(body_text), mention_all=mention_all)
                    return True, {"ok": True}
                last = "errcode=%s errmsg=%s" % (resp.get("errcode"), resp.get("errmsg"))
            except urllib.error.HTTPError as e:
                last = "HTTP %s" % e.code
            except Exception as e:  # noqa: BLE001
                last = "%s: %s" % (type(e).__name__, e)
            if i < self.retry - 1:
                time.sleep(2 ** i)
        obs.log("wecom_send_failed", level="error", detail=last)
        return False, {"error": last}


# ------------------------------------------------------------------ 文案构造（纯函数）
def build_run_summary(pipeline: str, stage: str, lines, facts=None, max_items=12,
                      site_name: str = "", issues_url: str = "") -> str:
    icon = "🔴" if stage in ("alarm", "failed") else ("✅" if stage in ("ok", "published") else "ℹ️")
    head = "**%s %s" % (icon, pipeline)
    if site_name:
        head += " · %s" % site_name
    head += " · %s**" % stage
    out = [head]
    if facts:
        out.append(" · ".join("%s=%s" % (k, v) for k, v in facts.items()))
    shown = list(lines or [])[:max_items]
    rest = len(lines or []) - len(shown)
    if shown:
        out.append("")
        out.extend(shown)
    if rest > 0:
        out.append("… 另有 %d 条" % rest)
    if issues_url:
        out.append("")
        out.append("工单：%s" % issues_url)
    return "\n".join(out)


def build_new_issues_message(pipeline: str, created, updated=None, **kw) -> str:
    lines = ["- [#%s] %s → @%s" % (i.get("number"), i.get("title"),
                                   ",".join(i.get("assignees") or []) or "-")
             for i in created or []]
    facts = {"新建": len(created or [])}
    if updated:
        facts["评论更新"] = len(updated)
    return build_run_summary(pipeline, "新工单", lines, facts, **kw)
