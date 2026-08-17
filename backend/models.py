"""Pydantic 模型定义：用于 LLM 结构化输出约束与节点间数据传递。

全部基于 Pydantic v2。结构化输出走 function_calling 优先
（ChatOpenAI.with_structured_output(method="function_calling")），
失败回退 json_object 容错管线（见 nodes.py::_structured_invoke）。
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class Outline(BaseModel):
    """调研大纲：由 supervisor_planner 生成，需人工确认。"""

    sections: List[str] = Field(description="报告章节列表")
    key_questions: List[str] = Field(description="本次调研需要解答的关键问题")


class SubTask(BaseModel):
    """单个并行搜索子任务：由 task_decomposer 基于确认后的大纲拆解。"""

    id: str = Field(description="子任务唯一标识")
    description: str = Field(description="子任务要调研的具体问题")
    keywords: List[str] = Field(description="用于搜索引擎的关键词列表")


class SearchResult(BaseModel):
    """单条搜索结果：由 search_web（多源）产出，供 analyst 聚合分析。"""

    title: str = Field(description="标题")
    url: str = Field(description="来源链接")
    content: str = Field(description="摘要正文")
    score: Optional[float] = Field(default=None, description="相关度分数（Tavily 返回）")
    query: str = Field(description="产生该结果所使用的搜索关键词")
    source: Optional[str] = Field(default=None, description="结果来源：tavily / bocha / mock")


class SubTaskList(BaseModel):
    """task_decomposer 的结构化输出包装：LLM 一次输出多个 SubTask。

    LangChain 的 with_structured_output 需要接收单个 BaseModel，
    因此用一层 wrapper 包裹 List[SubTask]。
    """

    tasks: List[SubTask] = Field(description="拆解出的并行搜索子任务列表")


class DataReflection(BaseModel):
    """reflector 节点的结构化输出：对已提取数据的完整性评估与缺口补搜建议。

    字段刻意扁平（bool / str / List[str]）：function_calling 主路线下最稳，
    兜底管线的字段级清洗（_coerce_str_list）也能覆盖 List[str]。
    """

    has_gaps: bool = Field(description="数据是否不足以支撑大纲各章节与关键问题")
    summary: str = Field(description="对数据质量与覆盖度的简要评估（中文 1-2 句）")
    missing_questions: List[str] = Field(
        description="未被数据覆盖或数据不充分的关键问题，无缺口时为空数组"
    )
    gap_queries: List[str] = Field(
        description="针对缺口的补充搜索查询（面向搜索引擎的中文关键词 1-3 个），无缺口时为空数组"
    )
