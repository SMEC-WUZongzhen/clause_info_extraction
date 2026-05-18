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
