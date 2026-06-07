# app/api.py - service2_payment_extractor
# POST /extract_payment_info：接收 paragraphs，返回付款信息提取结果

import asyncio
import json
import os
import re
import uuid
from typing import Optional, List, Dict, Any, Union
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal

from app.graphs.workflow_graph import graph as langgraph_app
from app.config.config import (
    setup_logging,
    APP_CONFIG,
    get_tiktoken_encoder,
    get_rag_models,
    get_milvus_client,
    get_bm25_data,
    get_bm25_data_installation,
)
from app.states.states import Paragraph
from app.utils.comparison_helper import ComparisonHelper, PaymentStage, ComparisonItem, EvaluationMetrics
from app.utils.payment_ratio_extractor import get_summary_extractor
from app.utils.observability import setup_observability
from app.utils.token_counter import count_tokens, warmup as token_counter_warmup
from app.config.business_dict import get_business_dict, assert_consistency_with_prompts
from app.config.env_config import assert_required_env


# =============================================================================
# 1. FastAPI App 和 Lifespan
# =============================================================================

def _safe_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(f"环境变量 {name}={raw!r} 非法，回退默认值 {default}")
        return default


# 单请求端到端硬超时（秒）。可通过环境变量覆盖。
_REQUEST_TIMEOUT_SEC = _safe_int_env("REQUEST_TIMEOUT_SEC", 300)

# 入参限额（C10）
_MAX_PARAGRAPHS = _safe_int_env("MAX_PARAGRAPHS", 200)
_MAX_CLAUSE_LEN = _safe_int_env("MAX_CLAUSE_LEN", 10000)

# 调试快照开关（I4）：必须由部署侧 env 显式启用，并配合 X-Debug-Snapshot header
_DEBUG_SNAPSHOT_ENABLED = os.getenv("SERVICE2_DEBUG_SNAPSHOT", "0").strip().lower() in ("1", "true", "yes", "on")
# S3 修复：production 环境硬关闭调试快照，无视 env 与 header（防止误配置导致原文落盘）
_IS_PRODUCTION = os.getenv("ENVIRONMENT", "development").strip().lower() == "production"
if _IS_PRODUCTION and _DEBUG_SNAPSHOT_ENABLED:
    logger.warning("生产环境检测到 SERVICE2_DEBUG_SNAPSHOT=1，已强制关闭以防原文泄漏。")
    _DEBUG_SNAPSHOT_ENABLED = False

# id 字符集白名单（C4）
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await setup_logging()
    logger.info("--- Service 2 (付款信息提取服务) 启动 ---")

    # ---- H6：必填 env 启动期 fail-closed 校验，缺失立即拒启 ----
    try:
        assert_required_env()
    except Exception as e:
        logger.critical(f"必填环境变量校验失败，进程退出: {e}")
        raise

    # ---- P0-3：启动期强制加载业务词典；失败立即拒启 ----
    try:
        get_business_dict()
    except Exception as e:
        logger.critical(f"业务词典加载失败，进程退出: {e}")
        raise

    # ---- P0-3：prompt ↔ 业务词典 一致性自检（prod 严格 / dev warning）----
    try:
        assert_consistency_with_prompts()
    except Exception as e:
        logger.critical(f"业务词典一致性自检失败: {e}")
        raise

    # ---- P0-4：预热 token encoder ----
    try:
        token_counter_warmup()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"token_counter 预热失败（已忽略）: {e}")

    app.state.langgraph_app = langgraph_app

    # 可选可观测接入（缺依赖/未启用时完全静默）
    try:
        setup_observability(app)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"setup_observability 失败（已忽略）: {e}")

    # ---- 启动预热：一次性加载重资源，避免首请求冷启动 ----
    async def _preheat():
        try:
            await asyncio.gather(
                get_tiktoken_encoder(),
                get_rag_models(),
                get_milvus_client(),
                get_bm25_data(),
                get_bm25_data_installation(),
                get_summary_extractor(),
                return_exceptions=True,
            )
            logger.success("--- 启动预热完成：tiktoken / RAG / Milvus / BM25(设备+安装) / SummaryExtractor ---")
        except Exception as e:
            logger.warning(f"启动预热出现非致命异常: {e}")

    # 预热放到后台执行，不阻塞服务就绪（即使预热未完成，首请求会退化为原 lazy-init 行为）
    asyncio.create_task(_preheat())

    yield
    logger.info("--- Service 2 关闭 ---")


app = FastAPI(
    title="Service 2 - 付款信息提取 API",
    version="1.0.0",
    description="接收 Service 1 输出的 Paragraph 列表，提取付款节点、比例、质保期等信息。",
    lifespan=lifespan
)


# =============================================================================
# 2. 请求 / 响应模型
# =============================================================================

# `ParagraphInput` 已收敛到 app.states.states.Paragraph。这里保留别名仅为向后兼容，
# 同时通过子类增加 max_length 限制（C10）。
class ParagraphInput(Paragraph):
    clause: str = Field("", max_length=_MAX_CLAUSE_LEN)
    clause_context: str = Field("", max_length=_MAX_CLAUSE_LEN * 4)


class GroundTruthItem(BaseModel):
    stage: str = Field(..., description="付款节点/阶段名称")
    ratio: Optional[Union[float, int, str]] = Field(
        None, description="金额比例，支持 0.05（小数）、'5%'（百分比字符串）、5（整数百分比）等格式"
    )
    stage_amount: Optional[Union[float, int, str]] = Field(
        None, description="金额，支持数字（5590）或字符串（'5590元'）"
    )
    category: Literal["equipment_payment", "installation_payment"] = Field(
        "equipment_payment", description="支付条款的业务分类"
    )

    @model_validator(mode='after')
    def normalize_and_validate(self) -> 'GroundTruthItem':
        if self.ratio is None and self.stage_amount is None:
            raise ValueError("'ratio' 和 'stage_amount' 必须至少提供一个。")

        # 归一化 ratio → 0-1 小数
        if self.ratio is not None:
            try:
                numeric = float(str(self.ratio).replace('%', '').strip())
                if numeric > 1.0:   # 整数或百分比字符串：5 / "5%" → 0.05
                    numeric = numeric / 100.0
                self.ratio = round(numeric, 6)
            except (ValueError, TypeError):
                raise ValueError(
                    f"无法解析 ratio 值: '{self.ratio}'，支持格式示例: 0.05、'5%'、5"
                )

        # 归一化 stage_amount → 字符串
        if self.stage_amount is not None and not isinstance(self.stage_amount, str):
            v = self.stage_amount
            # 整数值的浮点数去掉小数点，如 5590.0 → "5590"
            self.stage_amount = str(int(v)) if isinstance(v, float) and v == int(v) else str(v)

        return self


class ExtractPaymentInfoRequest(BaseModel):
    """POST /extract_payment_info 请求模型"""
    id: str = Field(..., description="任务的唯一标识符（仅允许 [A-Za-z0-9_-]{1,64}）")
    paragraphs: List[ParagraphInput] = Field(
        ...,
        description="Service 1 输出的段落列表",
        max_length=_MAX_PARAGRAPHS,
    )
    gt_payment_stages: Optional[List[GroundTruthItem]] = Field(
        None, description="基准数据列表（operation_type='analyze' 时需要）"
    )
    operation_type: Literal["extract", "analyze"] = Field(
        "extract", description="操作类型: 'extract'(仅提取) | 'analyze'(提取并比对)"
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not _SAFE_ID_RE.match(v or ""):
            raise ValueError("id 必须为 1-64 位且仅含 [A-Za-z0-9_-]")
        return v

    @model_validator(mode='after')
    def validate_inputs(self) -> 'ExtractPaymentInfoRequest':
        if self.operation_type == "analyze" and not self.gt_payment_stages:
            raise ValueError("'analyze' 操作必须提供 'gt_payment_stages'。")
        return self


# --- 响应模型 ---

class PaymentItem(BaseModel):
    clause_category: Optional[str] = None
    payment_clause: Optional[str] = None
    payment_context: Optional[str] = None
    payment_type: Optional[str] = None
    payment_ratio: Optional[float] = None
    payment_amount: Optional[str] = None


class WarrantyItem(BaseModel):
    warranty: Optional[str] = None
    warranty_clause: Optional[str] = None


class ExtractionResponse(BaseModel):
    id: str
    message: str = "success"
    extraction_result: List[Union[PaymentItem, WarrantyItem]]


class AnalysisResponse(BaseModel):
    id: str
    message: str = "success"
    extraction_result: List[Union[PaymentItem, WarrantyItem]]
    correct_payments: List[Dict[str, Any]] = Field(default_factory=list)
    missed_payments: List[Dict[str, Any]] = Field(default_factory=list)
    false_payments: List[Dict[str, Any]] = Field(default_factory=list)
    evaluation_metrics: Dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# 3. 辅助函数
# =============================================================================

def _build_extraction_result(final_state: Dict[str, Any]) -> List[Union[PaymentItem, WarrantyItem]]:
    """从 final_output 中构建提取结果列表"""
    extraction_result = []

    payment_infos = final_state.get("payment_infos", [])
    paragraphs = final_state.get("paragraphs", [])

    for info in payment_infos:
        if hasattr(info, "model_dump"):
            info_dict = info.model_dump()
        else:
            info_dict = info

        ratio = info_dict.get("payment_ratio")

        extraction_result.append(PaymentItem(
            clause_category=info_dict.get("clause_category"),
            payment_clause=info_dict.get("payment_clause"),
            payment_context=info_dict.get("payment_context", ""),
            payment_type=info_dict.get("payment_type"),
            payment_ratio=round(ratio * 100, 2) if ratio is not None else None,
            payment_amount=info_dict.get("payment_amount"),
        ))

    warranty_info = final_state.get("warranty_info")
    if warranty_info:
        if hasattr(warranty_info, "model_dump"):
            w = warranty_info.model_dump()
        else:
            w = warranty_info or {}
        extraction_result.append(WarrantyItem(
            warranty=w.get("warranty"),
            warranty_clause=w.get("warranty_clause"),
        ))

    return extraction_result


# =============================================================================
# 4. API 端点
# =============================================================================

@app.post(
    "/extract_payment_info",
    tags=["付款信息提取"],
    summary="从段落中提取付款节点与质保期信息"
)
async def extract_payment_info(
    request: ExtractPaymentInfoRequest,
    x_debug_snapshot: Optional[str] = Header(None, alias="X-Debug-Snapshot"),
):
    """
    **Service 2 主接口**

    接收 Service 1 输出的 `paragraphs` 列表，提取：
    - 设备/安装付款节点（节点名称、比例、金额）
    - 质保期信息

    当 `operation_type='analyze'` 时，额外与 `gt_payment_stages` 进行比对，返回准确率指标。
    """
    logger.info(f"--- 接收到 /extract_payment_info 请求，ID: {request.id}, 模式: {request.operation_type} ---")

    # 将 ParagraphInput 转换为 Paragraph 对象
    paragraphs = [
        Paragraph(
            clause=p.clause,
            clause_context=p.clause_context,
            clause_class=p.clause_class,
            metadata=p.metadata,
        )
        for p in request.paragraphs
    ]

    # 确定文档 ID（使用请求 ID 作为标识）
    document_id = request.id

    # 构建基准数据（analyze 模式）
    ground_truth_data = None
    if request.operation_type == "analyze" and request.gt_payment_stages:
        ground_truth_data = [
            {
                "付款节点": item.stage,
                "金额比例": item.ratio,
                "金额": item.stage_amount,
                "category": item.category,
            }
            for item in request.gt_payment_stages
        ]

    # 构建初始 State 直接注入
    initial_state = {
        "document_id": document_id,
        "paragraphs": paragraphs,
        "operation_type": request.operation_type,
        "ground_truth_data": ground_truth_data,
        "payment_infos": [],
        "warranty_info": None,
        "thinking_info": None,
        "errors": [],
        "input_queue": [],
        "processed_items": [],
        # 注入运行时配置（与原 batch_controller 行为一致）
        "llm_config": APP_CONFIG.llm.model_dump(),
        "payment_ratio_llm_config": APP_CONFIG.payment_ratio_llm.model_dump() if APP_CONFIG.payment_ratio_llm else {},
        "parser_config": APP_CONFIG.text_processing.model_dump(exclude={"llm_prompts"}),
        "prompts_config": APP_CONFIG.text_processing.llm_prompts.model_dump(),
    }

    thread_id = f"thread_{request.id}_{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}

    # 调试快照模式：必须服务端 env 启用且请求显式带 header（I4）
    if _DEBUG_SNAPSHOT_ENABLED and (x_debug_snapshot or "").strip().lower() in ("1", "true", "yes", "on"):
        logger.warning("已激活调试快照模式（X-Debug-Snapshot=on）。")
        config["configurable"]["debug_mode"] = True

    try:
        final_state = await asyncio.wait_for(
            langgraph_app.ainvoke(initial_state, config=config),
            timeout=_REQUEST_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.error(f"工作流执行超时 (ID: {request.id}, timeout={_REQUEST_TIMEOUT_SEC}s)")
        raise HTTPException(status_code=504, detail={"error": "Workflow execution timeout", "timeout_sec": _REQUEST_TIMEOUT_SEC})
    except Exception as e:
        trace_id = uuid.uuid4().hex
        logger.opt(exception=True).critical(
            "工作流执行失败 trace_id={tid} req={rid}: {err}",
            tid=trace_id, rid=request.id, err=str(e),
        )
        raise HTTPException(
            status_code=500,
            detail={"error": "Internal Server Error", "trace_id": trace_id},
        )

    # 从 final_output 获取结果
    final_output = final_state.get("final_output", {})

    # 重新把 paragraphs 放回 final_state 以便构建 context map
    final_state_with_paras = dict(final_state)
    if "paragraphs" not in final_state_with_paras or not final_state_with_paras["paragraphs"]:
        final_state_with_paras["paragraphs"] = paragraphs

    # 同步 payment_infos / warranty_info（可能来自 final_output 或直接在 state 中）
    if "payment_infos" not in final_state_with_paras or not final_state_with_paras.get("payment_infos"):
        final_state_with_paras["payment_infos"] = final_output.get("payment_infos", [])
    if not final_state_with_paras.get("warranty_info"):
        final_state_with_paras["warranty_info"] = final_output.get("warranty_info")

    extraction_result = _build_extraction_result(final_state_with_paras)

    # H2 修复：抽取阶段失败率超阈值时返回 503 而非 200，便于上游重试 / 告警
    if final_state.get("extraction_partial"):
        _err_list = final_state.get("extraction_errors", []) or []
        logger.error(
            f"任务 {request.id} 抽取部分失败 "
            f"(rate={final_state.get('extraction_failure_rate')})，返回 503"
        )
        return JSONResponse(
            status_code=503,
            content={
                "code": "EXTRACTION_PARTIAL_FAILURE",
                "message": "上游 LLM/RAG 失败率过高，结果可能不完整",
                "id": request.id,
                "failure_rate": final_state.get("extraction_failure_rate"),
                "errors": _err_list[:20],   # 截断防止响应过大 / 信息泄露
                "partial_result": extraction_result,
            },
        )

    if request.operation_type == "extract":
        logger.success(f"任务 {request.id} 提取完成，结果数: {len(extraction_result)}")
        return JSONResponse(content=ExtractionResponse(
            id=request.id,
            extraction_result=extraction_result
        ).model_dump(exclude_none=True))

    # analyze 模式
    logger.success(f"任务 {request.id} 分析完成。")
    response = AnalysisResponse(
        id=request.id,
        extraction_result=extraction_result,
        correct_payments=final_output.get("correct_payments", []),
        missed_payments=final_output.get("missed_payments", []),
        false_payments=final_output.get("false_payments", []),
        evaluation_metrics=final_output.get("evaluation_metrics", {}),
    )
    return JSONResponse(content=response.model_dump(exclude_none=True))


# =============================================================================
# 5. OpenAI 兼容接口
# =============================================================================

class Message(BaseModel):
    """OpenAI 格式的消息"""
    role: str
    content: str


class OpenAIChatRequest(BaseModel):
    """OpenAI 兼容的聊天请求"""
    model: str
    messages: List[Message]
    temperature: Optional[float] = 0.1
    max_tokens: Optional[int] = 4096


class OpenAIChatChoice(BaseModel):
    """OpenAI 格式的 choice"""
    index: int
    message: Dict[str, str]
    finish_reason: str = "stop"


class OpenAIChatResponse(BaseModel):
    """OpenAI 格式的响应"""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[OpenAIChatChoice]
    usage: Dict[str, int] = Field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0
    })


def _iter_balanced_json_blocks(text: str):
    """O(n) 扫描，依次产出顶层 [...] / {...} JSON 区段。

    H5 修复：使用 ``json.JSONDecoder.raw_decode`` 滑窗，由解码器消费完整对象/数组并返回 endpos。
    比手写括号栈更鲁棒（自然处理转义、Unicode 等异常情况），并在异常括号下不会死循环。
    """
    decoder = json.JSONDecoder()
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in "[{":
            try:
                _, end = decoder.raw_decode(text, i)
                yield text[i:end]
                i = end
                continue
            except json.JSONDecodeError:
                # 当前位置无法解析为合法 JSON，推进一字符继续找下一个候选起点
                i += 1
                continue
        i += 1


def extract_paragraphs_from_messages(messages: List[Message]) -> tuple[List[Dict[str, Any]], Optional[str], Optional[List[Dict[str, Any]]]]:
    """从 OpenAI messages 中解析 paragraphs 和操作类型。

    返回: (paragraphs, operation_type, gt_payment_stages)
    """
    paragraphs: List[Dict[str, Any]] = []
    operation_type = "extract"
    gt_payment_stages: Optional[List[Dict[str, Any]]] = None

    for msg in messages:
        content = msg.content or ""

        # 1) 整体 json.loads
        candidates: List[Any] = []
        try:
            data = json.loads(content)
            candidates.append(data)
        except (TypeError, ValueError):
            # 2) 栈式扫描枚举顶层 JSON 块
            for block in _iter_balanced_json_blocks(content):
                try:
                    candidates.append(json.loads(block))
                except (TypeError, ValueError):
                    continue

        for data in candidates:
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and ("text" in item or "para_text" in item or "content" in item or "clause" in item):
                        paragraphs.append(item)
            elif isinstance(data, dict):
                if "paragraphs" in data and isinstance(data["paragraphs"], list):
                    paragraphs = data["paragraphs"]
                elif "text" in data or "para_text" in data or "content" in data or "clause" in data:
                    paragraphs.append(data)

        # 3) 文本格式 GT
        if "## 参考标准答案" in content or "Ground Truth" in content or "gt_payment_stages" in content.lower():
            gt_pattern = r"\d+\.\s*([^:]+):\s*([^,\n]+)"
            gt_matches = re.findall(gt_pattern, content)
            if gt_matches:
                gt_payment_stages = []
                for stage, amount in gt_matches:
                    gt_item: Dict[str, Any] = {
                        "stage": stage.strip(),
                        "ratio": None,
                        "stage_amount": amount.strip(),
                        "category": "equipment_payment",
                    }
                    ratio_match = re.search(r"([\d.]+)%?", amount)
                    if ratio_match:
                        ratio_val = float(ratio_match.group(1))
                        gt_item["ratio"] = ratio_val / 100 if ratio_val > 1 else ratio_val
                    gt_payment_stages.append(gt_item)
                operation_type = "analyze"

    return paragraphs, operation_type, gt_payment_stages


def build_payment_extraction_prompt(paragraphs: List[Dict[str, Any]], operation_type: str = "extract") -> str:
    """构建付款信息提取的提示词"""
    prompt_parts = []

    prompt_parts.append("你是一个专业的付款条款信息提取助手。请从给定的合同条款段落中提取付款信息。\n")
    prompt_parts.append("请提取以下类型的付款条款：\n")
    prompt_parts.append("1. 设备付款条款\n")
    prompt_parts.append("2. 安装付款条款\n")
    prompt_parts.append("3. 质保期条款\n\n")
    prompt_parts.append("对于每条付款条款，请提取以下字段：\n")
    prompt_parts.append("- payment_type: 付款类型（如：预付款、进度款、验收款、质保金等）\n")
    prompt_parts.append("- payment_ratio: 付款比例（数值，如：0.3 表示 30%）\n")
    prompt_parts.append("- payment_amount: 付款金额（如：10万元，如有具体金额）\n")
    prompt_parts.append("- clause_category: 条款分类（设备付款条款/安装付款条款/质保期条款）\n")
    prompt_parts.append("- payment_clause: 原文条款\n")
    prompt_parts.append("- payment_context: 条款上下文\n\n")
    prompt_parts.append("待分析的合同条款段落：\n")

    for i, para in enumerate(paragraphs):
        para_text = para.get('para_text', para.get('text', para.get('content', '')))
        clause_class = para.get('clause_class', [])
        prompt_parts.append(f"【段落{i+1}】分类: {', '.join(clause_class) if clause_class else '未分类'}\n内容: {para_text}\n")

    prompt_parts.append("\n请以JSON数组格式返回提取结果，数组中的每个元素代表一条付款条款。")
    prompt_parts.append("如果没有找到任何付款条款，返回空数组 []。")

    return "".join(prompt_parts)


@app.post("/v1/chat/completions", tags=["OpenAI兼容接口"], response_model=OpenAIChatResponse)
async def chat_completions(request: OpenAIChatRequest):
    """
    OpenAI 兼容接口

    支持通过标准 OpenAI 格式调用付款信息提取服务。
    请求格式遵循 OpenAI Chat Completions API 规范。
    """
    import time

    logger.info(f"--- 接收到 /v1/chat/completions 请求，模型: {request.model} ---")

    try:
        # 合并所有 user 消息的内容
        all_content = []
        for msg in request.messages:
            if msg.role == "user":
                all_content.append(msg.content)
            elif msg.role == "system":
                all_content.insert(0, msg.content)

        combined_content = "\n\n".join(all_content)

        # 尝试解析 paragraphs
        paragraphs, operation_type, gt_payment_stages = extract_paragraphs_from_messages(request.messages)

        # 如果没有解析到 paragraphs，再用栈式扫描尝试一次顶层 JSON 块
        if not paragraphs:
            for block in _iter_balanced_json_blocks(combined_content):
                try:
                    data = json.loads(block)
                except (TypeError, ValueError):
                    continue
                if isinstance(data, list):
                    paragraphs = data
                    break

        if not paragraphs:
            # 如果仍然没有 paragraphs，使用默认的空段落（让 LLM 直接从内容中提取）
            logger.warning("未检测到标准 paragraphs 格式，尝试从用户消息中提取条款信息")

        # 构建系统提示词
        system_prompt = build_payment_extraction_prompt(paragraphs, operation_type)

        # 调用 LangGraph 工作流
        if paragraphs:
            # 将 paragraphs 转换为 Paragraph 对象
            paragraph_objects = []
            for i, p in enumerate(paragraphs):
                if isinstance(p, dict):
                    para_obj = Paragraph(
                        clause=p.get('clause', p.get('text', p.get('para_text', p.get('content', '')))),
                        clause_context=p.get('clause_context', ''),
                        clause_class=p.get('clause_class', []),
                        metadata=p.get('metadata', {})
                    )
                    paragraph_objects.append(para_obj)
                else:
                    paragraph_objects.append(p)

            document_id = f"openai_{uuid.uuid4().hex}"
        else:
            # 没有 paragraphs，创建一个包含全部内容的段落
            combined_text = combined_content
            paragraph_objects = [
                Paragraph(
                    clause=combined_text,
                    clause_context=combined_text,
                    clause_class=["设备付款条款", "安装付款条款", "质保期条款"],
                    metadata={}
                )
            ]
            document_id = f"openai_{uuid.uuid4().hex}"

        # 构建 ground_truth_data
        ground_truth_data = None
        if operation_type == "analyze" and gt_payment_stages:
            ground_truth_data = [
                {
                    "付款节点": item.get("stage", ""),
                    "金额比例": item.get("ratio"),
                    "金额": item.get("stage_amount", ""),
                    "category": item.get("category", "equipment_payment"),
                }
                for item in gt_payment_stages
            ]

        # 构建初始状态
        initial_state = {
            "document_id": document_id,
            "paragraphs": paragraph_objects,
            "operation_type": operation_type,
            "ground_truth_data": ground_truth_data,
            "payment_infos": [],
            "warranty_info": None,
            "thinking_info": None,
            "errors": [],
            "input_queue": [],
            "processed_items": [],
            "llm_config": APP_CONFIG.llm.model_dump(),
            "payment_ratio_llm_config": APP_CONFIG.payment_ratio_llm.model_dump() if APP_CONFIG.payment_ratio_llm else {},
            "parser_config": APP_CONFIG.text_processing.model_dump(exclude={"llm_prompts"}),
            "prompts_config": APP_CONFIG.text_processing.llm_prompts.model_dump(),
        }

        thread_id = f"thread_openai_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}

        # 执行工作流（带请求级超时）
        try:
            final_state = await asyncio.wait_for(
                langgraph_app.ainvoke(initial_state, config=config),
                timeout=_REQUEST_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error(f"OpenAI 兼容工作流执行超时，timeout={_REQUEST_TIMEOUT_SEC}s")
            raise HTTPException(
                status_code=504,
                detail={"error": "Workflow execution timeout", "timeout_sec": _REQUEST_TIMEOUT_SEC},
            )
        final_output = final_state.get("final_output", {})

        # 构建结果
        extraction_result = []
        payment_infos = final_state.get("payment_infos", []) or final_output.get("payment_infos", [])

        for info in payment_infos:
            if hasattr(info, "model_dump"):
                info_dict = info.model_dump()
            else:
                info_dict = info

            # 内部 state 中 payment_ratio 始终为 [0,1] 或 None；统一转百分比后输出
            ratio = info_dict.get("payment_ratio")
            ratio_pct = round(ratio * 100, 2) if ratio is not None else None

            extraction_result.append({
                "clause_category": info_dict.get("clause_category"),
                "payment_clause": info_dict.get("payment_clause"),
                "payment_context": info_dict.get("payment_context"),
                "payment_type": info_dict.get("payment_type"),
                "payment_ratio": ratio_pct,
                "payment_amount": info_dict.get("payment_amount"),
            })

        warranty_info = final_state.get("warranty_info") or final_output.get("warranty_info")
        if warranty_info:
            if hasattr(warranty_info, "model_dump"):
                w = warranty_info.model_dump()
            else:
                w = warranty_info or {}
            extraction_result.append({
                "warranty": w.get("warranty"),
                "warranty_clause": w.get("warranty_clause"),
            })

        # 构建 OpenAI 格式的响应
        result_content = json.dumps(extraction_result, ensure_ascii=False, indent=2)

        # P0-4: 真实 token 计数（tiktoken cl100k_base，失败 fallback 字符比）
        # prompt 部分包含 system_prompt 与全部 user/system 消息内容
        prompt_text_for_count = system_prompt + "\n" + combined_content
        prompt_tokens = count_tokens(prompt_text_for_count)
        completion_tokens = count_tokens(result_content)

        response = OpenAIChatResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
            object="chat.completion",
            created=int(time.time()),
            model=request.model,
            choices=[
                OpenAIChatChoice(
                    index=0,
                    message={"role": "assistant", "content": result_content},
                    finish_reason="stop"
                )
            ],
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
        )

        logger.success(f"OpenAI 兼容接口请求完成，提取结果数: {len(extraction_result)}")
        return response

    except HTTPException:
        raise
    except Exception as e:
        trace_id = uuid.uuid4().hex
        logger.opt(exception=True).critical(
            "OpenAI 兼容接口处理失败 trace_id={tid}: {err}", tid=trace_id, err=str(e),
        )
        raise HTTPException(
            status_code=500,
            detail={"error": "Internal Server Error", "trace_id": trace_id},
        )


# =============================================================================
# 6. 健康检查
# =============================================================================

@app.get("/health", tags=["运维"])
async def health_check():
    return {"status": "ok", "service": "service2_payment_extractor"}
