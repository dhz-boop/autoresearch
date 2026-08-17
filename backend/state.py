"""全局状态定义：LangGraph StateGraph 的状态 schema（ResearchState）。

设计要点：
- 使用 TypedDict 描述图全局状态；
- search_results 使用 Annotated + operator.add，使并行 researcher /
  gap_researcher 节点的输出通过 reducer 自动合并（Send 扇出后共享同一列表）；
- sub_task / gap_query 字段仅用于并行分支（由 Send 传入），主流程中为 None；
- 反思循环（reflection loop）：reflector 质检 extracted_data，有缺口时经
  gap_researcher 补搜后回 analyst 重新提取，循环上限见 graph.py
  _MAX_REFLECTIONS；reflection_count 为全线程累计值。
"""
import operator
from typing import Annotated, Dict, List, Optional, TypedDict

from models import DataReflection, Outline, SearchResult, SubTask


class ResearchState(TypedDict):
    # ---------- 输入与规划 ----------
    topic: str
    outline: Optional[Outline]
    plan_approved: bool  # 大纲是否已通过人工确认
    sub_tasks: List[SubTask]

    # ---------- 并行搜索 ----------
    # 所有 researcher 节点的输出追加到同一列表（operator.add 自动合并）
    search_results: Annotated[list, operator.add]
    sub_task: Optional[SubTask]  # 仅 researcher 并行分支使用

    # ---------- 分析与写作 ----------
    extracted_data: Dict[str, object]
    draft_report: Optional[str]
    draft_approved: bool  # 初稿是否已通过人工审核
    final_report: Optional[str]

    # ---------- 反思与数据质检（reflection loop） ----------
    reflection: Optional[DataReflection]  # reflector 最近一次的结构化评估
    reflection_count: Optional[int]  # 已执行反思轮数（全线程累计，循环上限）
    reflection_log: Annotated[list, operator.add]  # 每轮反思摘要 dict，供追踪
    auto_feedback: Optional[str]  # reflector 生成的补数据要求（analyst 消费）
    gap_query: Optional[str]  # 仅 gap_researcher 并行分支使用（Send 传入）
    data_approved: Optional[bool]  # 数据是否通过反思质检（观察性字段）

    # ---------- 人机协同 ----------
    human_feedback: Optional[str]  # 最近一次人工反馈/修改意见

    # ---------- 健壮性 ----------
    error_log: List[str]  # 各节点捕获的异常记录
