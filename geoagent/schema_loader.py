"""站点相关 pydantic 模型的动态生成。

为什么需要这个：`Category` / `Assignee` 原本是写死的 Literal，换站点（改品类名、换负责人）
会导致结构化输出校验直接失败 —— 这是方案里点出的"会直接崩"的两处之一。
现在它们由 site.yml 在运行时生成，代码里不再出现任何具体品类名/人名。

注意：本模块**不能用** `from __future__ import annotations`，
否则注解变字符串、pydantic 无法解析运行时构造的 Literal 类型。
"""
import copy
from typing import Literal

from pydantic import BaseModel, Field, create_model

from . import site as site_mod


def build(site: dict) -> dict:
    """按站点档案构造模型集合。返回 dict：Recommendation / TopicPick / TopicPickList 等。"""
    cats = tuple(site_mod.category_ids(site)) or ("uncategorized",)
    people = tuple(site_mod.assignees(site)) or ("unassigned",)

    CategoryT = Literal[cats]
    AssigneeT = Literal[people]

    # ---- 引用检测建议（管道 B）
    Recommendation = create_model(
        "Recommendation",
        priority=(Literal["P0", "P1", "P2"], ...),
        type=(str, Field(description="content_strategy / tooling_failure / schema_fix ...")),
        rule=(str, Field(description="命中的规则名，如 零引用率")),
        finding=(str, Field(description="实测发现的客观描述，不得虚构数字")),
        action=(str, Field(description="具体行动")),
        assignee=(AssigneeT, Field(description="负责人，只能是 %s" % "、".join(people))),
        gitea_label=(str, Field(description="工单标签名")),
    )

    # ---- 周批次选题（管道 C）
    TopicPick = create_model(
        "TopicPick",
        category=(CategoryT, Field(description="品类，只能是 %s" % "、".join(cats))),
        topic=(str, ...),
        slug=(str, Field(description="小写英文+连字符+日期后缀")),
        keywords=(list[str], Field(default_factory=list)),
        is_product=(bool, Field(default=False)),
        angle=(str | None, Field(default=None, description="产品篇的角度名（防同题重复）")),
    )
    TopicPickList = create_model(
        "TopicPickList",
        # ⚠️ 必须包一层容器：with_structured_output 不接受裸泛型 list[X]
        topics=(list[TopicPick], Field(default_factory=list, description="选题列表")),
    )

    return {
        "Category": CategoryT,
        "Assignee": AssigneeT,
        "Recommendation": Recommendation,
        "TopicPick": TopicPick,
        "TopicPickList": TopicPickList,
        "categories": list(cats),
        "assignees": list(people),
    }


# ------------------------------------------------------------------ 独立可用的通用模型
# 这些不含站点耦合（字段都是 str/list），保留在模块层便于直接导入与测试。
class IssuePlan(BaseModel):
    label: str
    title: str
    body: str
    assignee: str = Field(default="unassigned")


class WeekReview(BaseModel):
    """管道 C ①：四周指标周报叙述（纯文字判断，数字由上游纯函数喂进来）。"""
    summary: str
    trends: list[str] = Field(default_factory=list, description="逐条趋势结论")
    weight_trace: str | None = Field(default=None, description="权重轨迹，如 1.0→1.2→1.5(封顶)")
    anomalies: list[str] = Field(default_factory=list)
    notes: str | None = None


class ArticleDraft(BaseModel):
    """管道 E：一篇文章草稿。

    category 刻意用 str 而不是 Literal：模型填了配置外品类时，
    我们要给出「品类 X 不在允许列表」这种可读错误，而不是 pydantic 抛校验异常。
    """
    title: str
    slug: str
    category: str
    markdown: str = Field(description="正文（含表格/FAQ section/GEO 水印/时效性断言），不含 frontmatter")


class FixInstruction(BaseModel):
    """管道 E 回炉：按质检失败清单生成的修稿指令。"""
    violations: list[str]
    instructions: str


def clone(model_cls, name: str | None = None):
    """复制一个模型类（需要给不同站点分别实例化时用）。"""
    return copy.copy(model_cls) if name is None else model_cls
