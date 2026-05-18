# app/agents/aggregator_node.py - service2_payment_extractor
# 聚合付款信息提取结果，写入 final_output

from typing import Dict, Any, List
from loguru import logger

from app.states.states import State
from app.utils.node_decorator import node_with_progress


@node_with_progress(
    node_name="aggregator",
    display_name="结果聚合",
    progress_range=(100, 100)
)
async def aggregator_node(state: State, config=None) -> Dict[str, Any]:
    """Service 2 聚合节点：收集单次任务的提取结果，返回 final_output。"""
    logger.info("--- [Service 2 Aggregator] 开始聚合 ---")
    processed_items: List[Dict[str, Any]] = state.get("processed_items", [])

    if not processed_items:
        return {"final_output": {"message": "没有处理任何项目。"}}

    # 单任务模式：直接返回第一个结果
    return {"final_output": processed_items[0]}
