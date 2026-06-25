# app/states/states.py - service2_payment_extractor
# 精简版 State：仅保留付款信息提取所需的字段
# 入口直接从 paragraphs 注入，不依赖文档解析流水线

from __future__ import annotations

import json
import os
from loguru import logger
from datetime import datetime
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, TypedDict, Union, Literal
from typing_extensions import Annotated
import operator


# ========== 基础数据模型 ==========

class PerformanceLog(BaseModel):
    step: str
    action: str
    duration_ms: float
    input_chars: Optional[int] = None
    output_chars: Optional[int] = None
    item_count: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Paragraph(BaseModel):
    """段落模型类。
    每个实例代表一个原子子条款，使用 clause + clause_context。
    """
    clause: str = ""            # 子条款原文
    clause_context: str = ""    # 子条款所在段落的完整上下文
    clause_class: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PaymentInfo(BaseModel):
    """支付信息模型类"""
    clause_category: Literal["equipment_payment", "installation_payment"] = Field(
        "equipment_payment",
        description="条款的业务分类"
    )
    payment_clause: str
    payment_context: Optional[str] = None
    payment_type: Optional[str] = None
    payment_ratio: Optional[float] = None
    payment_amount: Optional[str] = None
    image_url: Optional[str] = None
    first_page_image_url: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # === Stage 7（付款时效 LLM 提取 + 特殊条款汇总）新增字段 ===
    payment_days: Optional[int] = None
    latest_payment_stage: Optional[str] = None
    latest_payment_date: Optional[int] = None
    special_clause_content: Optional[str] = None


class WarrantyInfo(BaseModel):
    """质保期信息模型类"""
    warranty: str
    effective_conditions: str
    closed_end_conditions: str
    warranty_clause: Optional[str] = None


class ThinkingInfo(BaseModel):
    """llm思考内容输出"""
    thinking_output: str


# ========== LangGraph 状态定义 ==========

class State(TypedDict, total=False):
    """
    Service 2 状态定义 - 仅包含付款信息提取所需字段
    移除了 documents / full_text / regions_of_interest 等上游文档解析字段
    入口直接从 paragraphs 注入
    """

    # --- 任务标识 ---
    document_id: Optional[str]

    # --- 核心输入：由 API 层直接注入 ---
    paragraphs: List[Paragraph]

    # --- 运行模式控制 ---
    operation_type: str  # "extract" | "analyze"

    # --- 支付信息和下游字段 ---
    payment_infos: List[PaymentInfo]
    warranty_info: Optional[WarrantyInfo]
    thinking_info: Optional[ThinkingInfo]

    # --- 比对数据 ---
    ground_truth_data: Optional[List[Dict[str, Any]]]
    current_comparison_result: Optional[Dict[str, Any]]

    # --- 错误与流程控制 ---
    errors: List[Dict[str, Any]]
    last_error: Optional[str]
    current_step: str
    final_output: Optional[Any]

    # --- 运行时配置 ---
    llm_config: Dict[str, Any]
    payment_ratio_llm_config: Dict[str, Any]
    parser_config: Dict[str, Any]
    prompts_config: Dict[str, Any]

    # --- 兼容性字段（部分 agent 代码可能访问） ---
    is_initialized: Optional[bool]
    input_queue: List[Any]
    processed_items: Annotated[List[Dict[str, Any]], operator.add]


# ========== 状态辅助类 ==========

class StateHelper:

    @staticmethod
    def create_error_log(step: str, message: str, **kwargs) -> Dict[str, Any]:
        log = {
            "step": step,
            "message": message,
            "timestamp": datetime.now().isoformat()
        }
        log.update(kwargs)
        return log

    @staticmethod
    def _json_default_serializer(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, 'model_dump'):
            return obj.model_dump()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# ========== LLM 结构化输出 Schema 模型（json_schema response_format 专用）==========

class _PaymentRatioNode(BaseModel):
    """Chain 1/2（equipment_chain / install_chain）单条款初步提取的单个节点"""
    payment_type: Optional[str] = None
    ratio: Optional[str] = None
    amount: Optional[str] = None


class PaymentRatioResult(BaseModel):
    """Chain 1/2 输出包装：equipment_chain / install_chain"""
    nodes: List[_PaymentRatioNode] = []


class _PaymentSummaryNode(BaseModel):
    """Chain 3/4 批量汇总复审的单个节点"""
    id: str
    payment_clause: str
    payment_type: Optional[str] = None
    final_ratio: Optional[str] = None
    final_amount: Optional[str] = None


class PaymentSummaryResult(BaseModel):
    """Chain 3/4 输出包装：llm_chain / install_llm_chain（设备/安装批量汇总复审）"""
    items: List[_PaymentSummaryNode] = []
    thinking_output: Optional[str] = None


class _WarrantyNode(BaseModel):
    """Chain 5 质保期提取的单个条目"""
    warranty: str
    effective_conditions: str
    closed_end_conditions: str


class WarrantySummaryResult(BaseModel):
    """Chain 5 输出包装：warranty_llm_chain"""
    items: List[_WarrantyNode] = []
    thinking_output: Optional[str] = None


class _VerificationSelectItem(BaseModel):
    """Chain 6 双组去重校验的单个保留节点"""
    select_clause_id: str


class VerificationResult(BaseModel):
    """Chain 6 输出包装：result_verification_llm_chain"""
    items: List[_VerificationSelectItem] = []
    thinking_output: Optional[str] = None


class ClauseValidationResult(BaseModel):
    """Chain 7 输出：clause_validation_llm_chain 单条款有效性验证"""
    id: str
    is_valid: bool
    reason: str


class ClauseCategoryResult(BaseModel):
    """Chain 8 输出：clause_category_llm_chain 混合类型条款分类"""
    id: str
    category: Literal["equipment_payment", "installation_payment", "both"]
    reason: str


class _TypeCorrection(BaseModel):
    """单条类型纠正：将指定 id 的条款 payment_type 修正为 corrected_payment_type"""
    id: str
    corrected_payment_type: str


class SingleGroupVerificationResult(BaseModel):
    """Chain 9 输出：result_verification_single_group_llm_chain 单组去重 / 类型纠正

    action:
      - "select_one"   → 真正重复，从候选中保留一条（select_clause_id 有效，corrections 为空）
      - "correct_type" → 类型误判，对指定条款修正 payment_type，全部保留（corrections 有效，select_clause_id 为 null）
    """
    action: Literal["select_one", "correct_type"]
    select_clause_id: Optional[str] = None
    corrections: List[_TypeCorrection] = []
    reason: str


class PaymentTimingResult(BaseModel):
    """Chain 10 输出：payment_timing_llm_chain 付款时序提取"""
    payment_days: Optional[int] = None
    latest_payment_stage: Optional[str] = None
    latest_payment_date: Optional[int] = None
