"""LLM 工厂 + 结构化输出（含失败降级与「原生结构化不可用」的自动回退）。

模型走统一的 litellm 出口（地址在 config.yml 的 `llm.base_url`，不入库），**必须** chat_completions 协议：
litellm 的 anthropic 协议(/v1/messages)转发 glm 会 404。

实测（2026-09-18）：这个 glm 端点经 litellm 不认 function calling / json_schema ——
`with_structured_output()` 拿回的是 markdown 正文，pydantic 报 Invalid JSON。
所以这里做两段式：
  ① 原生结构化（对支持的模型最省事、约束最强）
  ② 纯文本 + 显式 JSON 指令 + 自己抽取解析（对 OpenAI 兼容代理最稳）
一旦①失败就把「原生不可用」记下来，后续不再白花一次调用。

LLM 节点失败不抛异常：返回 (False, err)，由调用方塞进 state["errors"] —— 失败不静默。
"""
from __future__ import annotations

import json
import re

from . import obs
from .config import get

_llm_cache = {}
_NATIVE_OK = None      # None=未验证 / False=已确认不可用（跳过） / True=可用

JSON_INSTRUCTION = (
    "\n\n【输出格式要求】只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释文字。"
    "字段必须严格符合以下 JSON Schema：\n%s"
)


def available() -> bool:
    try:
        import langchain_openai  # noqa: F401
        return True
    except ImportError:
        return False


def get_llm(cfg: dict, temperature: float | None = None):
    key = (get(cfg, "llm.base_url"), get(cfg, "llm.model"), temperature)
    if key in _llm_cache:
        return _llm_cache[key]
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(
        base_url=get(cfg, "llm.base_url"),
        api_key=get(cfg, "llm.api_key") or "sk-not-set",
        model=get(cfg, "llm.model"),
        temperature=get(cfg, "llm.temperature", 0) if temperature is None else temperature,
        timeout=get(cfg, "llm.timeout", 120),
        max_retries=1,
    )
    _llm_cache[key] = llm
    return llm


def extract_json(text: str):
    """从模型回复里抠出 JSON（兼容 ```json 代码块、前后带解释文字的情况）。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    end = max(t.rfind("}"), t.rfind("]"))
    if end <= start:
        return None
    return t[start:end + 1]


def _schema_hint(schema) -> str:
    try:
        return json.dumps(schema.model_json_schema(), ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return "{}"


def structured(cfg: dict, schema, prompt: str, node: str, attempts: int = 2):
    """返回 (ok, value_or_error)。

    ok=True 时 value 是 schema 实例；ok=False 时 value 是错误字符串。
    """
    global _NATIVE_OK
    from langchain_core.messages import HumanMessage

    if not available():
        return False, "langchain_openai 未安装（pip install -r requirements.txt）"

    mode = get(cfg, "_extra.llm_structured_mode") or "auto"
    last = "unknown"
    for i in range(max(1, attempts)):
        # ---- ① 原生结构化（只在未确认不可用时尝试）
        if mode in ("auto", "native") and _NATIVE_OK is not False:
            try:
                llm = get_llm(cfg).with_structured_output(schema)
                value = llm.invoke([HumanMessage(content=prompt)])
                _NATIVE_OK = True
                obs.log("llm_ok", node=node, attempt=i + 1, path="native")
                return True, value
            except Exception as e:  # noqa: BLE001
                last = "%s: %s" % (type(e).__name__, e)
                if mode == "auto":
                    _NATIVE_OK = False
                    obs.log("llm_native_structured_unavailable", level="warn", node=node,
                            err=str(e)[:200],
                            action="自动回退为「文本 + 显式 JSON 指令 + 自己解析」")

        # ---- ② 文本模式 + 自己解析
        try:
            text_prompt = prompt + (JSON_INSTRUCTION % _schema_hint(schema))
            resp = get_llm(cfg).invoke([HumanMessage(content=text_prompt)])
            content = resp.content if hasattr(resp, "content") else str(resp)
            raw = extract_json(content if isinstance(content, str) else json.dumps(content))
            if raw is None:
                last = "模型未返回可解析的 JSON（首 120 字: %s）" % (content or "")[:120]
            else:
                value = schema.model_validate_json(raw)
                obs.log("llm_ok", node=node, attempt=i + 1, path="json_text")
                return True, value
        except Exception as e:  # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, e)

        obs.log("llm_failed", level="warn", node=node, attempt=i + 1, err=str(last)[:220])
    return False, last
