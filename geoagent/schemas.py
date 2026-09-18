"""LLM 节点的结构化输出 schema（站点无关的通用模型）。

站点相关的模型（品类/负责人是 Literal 的那些）由 `schema_loader.build(site)` 运行时生成 ——
见该模块说明。这里只保留不含任何具体站点信息的通用模型。

设计原则：字段沿用现有脚本/工单已经在用的名字（不新发明），
这样对拍时新旧产物能直接逐字段比。
"""
import copy
from typing import Optional

from pydantic import BaseModel, Field


class IssuePlan(BaseModel):
    """要创建的工单计划。"""
    label: str
    title: str
    body: str
    assignee: str = "unassigned"


class WeekReview(BaseModel):
    """管道 C ①：四周指标周报叙述（纯文字判断，数字由上游纯函数喂进来）。"""
    summary: str
    trends: list[str] = Field(default_factory=list, description="逐条趋势结论")
    weight_trace: Optional[str] = Field(default=None, description="权重轨迹，如 1.0→1.2→1.5(封顶)")
    anomalies: list[str] = Field(default_factory=list)
    notes: Optional[str] = None


class ArticleDraft(BaseModel):
    """管道 E：一篇文章草稿。

    category 刻意用 str 而非 Literal：模型填了配置外的品类时，要给出
    「品类 X 不在允许列表」这种可读错误，而不是 pydantic 抛校验异常。
    """
    title: str
    slug: str
    category: str
    markdown: str = Field(description="正文（含表格/FAQ section/水印/时效性断言），不含 frontmatter")


class FixInstruction(BaseModel):
    """管道 E 回炉：按质检失败清单生成的修稿指令。"""
    violations: list[str]
    instructions: str


# 兼容旧引用：这些名字现在由 schema_loader.build(site) 动态生成，
# 保留别名会让「忘了传 site」静默变成通用 str —— 所以刻意**不**提供别名。
# 需要 Recommendation / TopicPick / TopicPickList 请用 schema_loader.build(site)。


def clone(model_cls):
    return copy.copy(model_cls)
