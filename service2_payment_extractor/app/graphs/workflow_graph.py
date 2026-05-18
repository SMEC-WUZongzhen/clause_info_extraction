# app/graphs/workflow_graph.py - service2_payment_extractor
# 精简版工作流：payment_info_extractor → (comparison_node 可选) → output_node → aggregator → END

from loguru import logger
from typing import Any, Literal
from langgraph.graph import StateGraph, END
from app.states.states import State

from app.agents.payment_info_extractor_node import payment_info_extractor_node
from app.agents.comparison_node import comparison_node
from app.agents.output_node import output_node
from app.agents.aggregator_node import aggregator_node


def route_after_extraction(state: State) -> Literal["comparison_node", "output_node"]:
    """
    根据 operation_type 决定是否执行比对步骤。
    - "analyze": 执行比对 → comparison_node
    - "extract": 跳过比对 → output_node
    """
    op = state.get("operation_type", "extract")
    if op == "analyze" and state.get("ground_truth_data"):
        logger.info("operation_type=analyze，路由至 comparison_node。")
        return "comparison_node"
    logger.info("operation_type=extract 或无基准数据，跳过比对，路由至 output_node。")
    return "output_node"


def create_workflow_graph() -> Any:
    """
    Service 2 工作流图：付款信息提取流水线。
    入口直接从 paragraphs 注入 State，不依赖 HumanMessage 路由。
    """
    workflow = StateGraph(State)

    logger.info("定义 Service 2 工作流节点...")
    workflow.add_node("payment_info_extractor", payment_info_extractor_node)
    workflow.add_node("comparison_node", comparison_node)
    workflow.add_node("output_node", output_node)
    workflow.add_node("aggregator", aggregator_node)

    # 入口：直接从 payment_info_extractor 开始
    workflow.set_entry_point("payment_info_extractor")

    logger.info("连接 Service 2 工作流边...")
    workflow.add_conditional_edges(
        "payment_info_extractor",
        route_after_extraction,
        {"comparison_node": "comparison_node", "output_node": "output_node"}
    )
    workflow.add_edge("comparison_node", "output_node")
    workflow.add_edge("output_node", "aggregator")
    workflow.add_edge("aggregator", END)

    logger.info("Service 2 工作流图定义完成，准备编译...")
    return workflow.compile()


logger.info("创建并编译 Service 2 工作流图实例...")
graph = create_workflow_graph()
logger.success("Service 2 全局图实例已创建并编译成功。")
