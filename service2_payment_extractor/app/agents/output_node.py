# app/agents/output_node.py - service2_payment_extractor
# 将单次任务的付款信息提取结果汇入 processed_items

from typing import Dict, Any, Optional
from loguru import logger
from langchain_core.runnables import RunnableConfig

from app.states.states import State
from app.utils.node_decorator import node_with_progress
from app.config.graph_config import WORKFLOW_PROGRESS_RANGES


@node_with_progress(
    node_name="output_node",
    display_name="结果输出",
    track_state_keys=["document_id"],
    progress_range=WORKFLOW_PROGRESS_RANGES.get("output_node", (98, 100))
)
async def output_node(state: State, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    """
    Service 2 输出节点：收集付款信息提取结果并打包。
    """
    document_id = state.get("document_id", "N/A")
    comparison_result = state.get("current_comparison_result", {})

    logger.info(f"--- [Service 2 Output Node] 文档 {document_id} ---")

    payment_infos = state.get("payment_infos", [])
    warranty_info = state.get("warranty_info")
    thinking_info = state.get("thinking_info")

    task_result = {
        "document_id": document_id,
        "payment_infos": [
            p.model_dump() if hasattr(p, "model_dump") else p for p in payment_infos
        ],
        "warranty_info": warranty_info.model_dump() if warranty_info and hasattr(warranty_info, "model_dump") else warranty_info,
        "thinking_info": thinking_info.model_dump() if thinking_info and hasattr(thinking_info, "model_dump") else thinking_info,
    }

    # 合并比对结果（如果存在），仅保留顶层比对字段
    if comparison_result:
        task_result.update({
            "correct_payments": comparison_result.get("correct_payments", []),
            "missed_payments": comparison_result.get("missed_payments", []),
            "false_payments": comparison_result.get("false_payments", []),
            "evaluation_metrics": comparison_result.get("evaluation_metrics", {}),
        })

    return {
        "processed_items": [task_result],
        "current_step": "payment_output_done",
        "input_queue": state.get("input_queue", []),
        "is_initialized": state.get("is_initialized"),
    }
