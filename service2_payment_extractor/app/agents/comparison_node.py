# app/agents/comparison_node.py

from typing import Dict, Any, Optional, List
from loguru import logger
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import AIMessage

from app.states.states import State, PaymentInfo, WarrantyInfo 
from app.utils.comparison_helper import ComparisonHelper, ComparisonResult, EvaluationMetrics, PaymentStage
from app.utils.node_decorator import node_with_progress
from app.config.graph_config import WORKFLOW_PROGRESS_RANGES

def _format_ratio(ratio: Optional[float]) -> str:
    if ratio is None: return "N/A"
    try: return f"{ratio:.1%}"
    except (TypeError, ValueError): return str(ratio)

def _generate_markdown_report(result: ComparisonResult) -> str:
    summary = result.summary
    details = result.comparison_details

    # 按提取顺序输出（不再按 para_seq 排序）
    logger.trace("详细比对报告按提取顺序输出。")

    report_parts = [f"# 支付条款提取与基准比对报告\n\n"]
    report_parts.append("## 📊 比对摘要\n\n")
    report_parts.append("| 指标 | 结果 |\n")
    report_parts.append("|:---|:---|\n")
    report_parts.append(f"| **文档ID** | `{summary.document_id}` |\n")
    report_parts.append(f"| **模型提取数** | {summary.total_extracted} |\n")
    report_parts.append(f"| **基准数据数** | {summary.total_ground_truth} |\n")
    report_parts.append(f"| ✅ **匹配成功** | {summary.matched_count} |\n")
    report_parts.append(f"| ➕ **模型多提** | {summary.extra_count} |\n")
    report_parts.append(f"| ➖ **模型漏提** | {summary.missing_count} |\n")
    report_parts.append(f"| 🎯 **综合准确率 (F1-Score)** | {_format_ratio(summary.overall_accuracy)} |\n")
    report_parts.append(f"| 💰 **比例一致率** | {_format_ratio(summary.ratio_match_rate)} |\n\n")
    report_parts.append(f"| 💵 **金额一致率** | {_format_ratio(summary.amount_match_rate)} |\n\n")
    report_parts.append("## 🔍 详细比对\n\n")

    matched_items = [d for d in details if d.status == 'matched']
    extra_items = [d for d in details if d.status == 'extra']
    missing_items = [d for d in details if d.status == 'missing']

    if matched_items:
        report_parts.append("### ✅ 匹配成功的条款\n\n")
        for i, item in enumerate(matched_items, 1):
            overall_status = "✅ 完全匹配" if item.is_fully_matched else "⚠️ 部分匹配 (冲突)"
            ratio_status = "✅" if item.is_ratio_match else "❌" if item.is_ratio_match is not None else "N/A"
            amount_status = "✅" if item.is_amount_match else "❌" if item.is_amount_match is not None else "N/A"
            report_parts.append(f"**{i}. {item.extracted_type}** - {overall_status} (相似度: {item.match_score:.0f})\n")
            report_parts.append(f"- **模型提取**: 比例 `{_format_ratio(item.extracted_ratio)}` {ratio_status} | 金额 `{item.extracted_amount or 'N/A'}` {amount_status}\n")
            report_parts.append(f"- **基准数据**: 比例 `{_format_ratio(item.ground_truth_ratio)}` | 金额 `{item.ground_truth_amount or 'N/A'}` (节点: {item.ground_truth_node})\n")
            report_parts.append(f"> {item.extracted_clause}\n\n")

    if extra_items:
        report_parts.append("### ➕ 模型多提的条款\n\n")
        for i, item in enumerate(extra_items, 1):
            report_parts.append(f"**{i}. {item.extracted_type}**\n")
            report_parts.append(f"- **提取信息**: 比例 `{_format_ratio(item.extracted_ratio)}` | 金额 `{item.extracted_amount or 'N/A'}`\n")
            report_parts.append(f"> {item.extracted_clause}\n\n")

    if missing_items:
        report_parts.append("### ➖ 模型漏提的条款\n\n")
        for i, item in enumerate(missing_items, 1):
            report_parts.append(f"**{i}. {item.ground_truth_node}**\n")
            report_parts.append(f"- **基准信息**: 比例 `{_format_ratio(item.ground_truth_ratio)}` | 金额 `{item.ground_truth_amount or 'N/A'}`\n\n")
            
    return "".join(report_parts)

@node_with_progress(
    node_name="comparison_node", 
    display_name="结果比对", 
    track_state_keys=["payment_infos"],
    progress_range=WORKFLOW_PROGRESS_RANGES.get("comparison_node")
)
async def comparison_node(state: State, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    document_id = state.get("document_id", "unknown_doc")
    logger.info(f"--- 开始为文档 {document_id} 进行结果比对 ---")
    
    # 添加明显的调试信息
    logger.info("=" * 50)
    logger.info("COMPARISON_NODE 开始执行...")
    logger.info(f"State 中包含的键: {list(state.keys())}")
    logger.info("=" * 50)
    
    predicted_results: List[PaymentInfo] = state.get("payment_infos", [])
    warranty_info: Optional[WarrantyInfo] = state.get("warranty_info", None)
    
    logger.info(f"从 state 中获取到的 payment_infos 数量: {len(predicted_results)}")
    logger.info(f"从 state 中获取到的 warranty_info: {warranty_info}")
    
    # 添加调试信息
    if warranty_info:
        logger.success(f"成功获取质保期信息: {warranty_info.warranty}")
        logger.info(f"✅ 成功获取质保期信息: {warranty_info.warranty}")
    else:
        logger.warning("未能从 state 中获取到质保期信息")
        logger.info("❌ 未能从 state 中获取到质保期信息")
    
    # 调试：检查 state 中的质保期信息
    logger.info(f"State 中的 warranty_info: {state.get('warranty_info')}")
    logger.info(f"warranty_info 类型: {type(state.get('warranty_info'))}")
    helper = ComparisonHelper()

    # 从 state 中提取由 API 注入的 ground_truth_data
    ground_truth_data_for_run = state.get("ground_truth_data", [])
    if ground_truth_data_for_run is None:
        ground_truth_data_for_run = []

    # +++ 现在 payment_infos 列表中的每个 PaymentInfo 对象都包含 clause_category 字段 +++
    comparison_result_obj = await helper.compare(
        document_id=document_id or "unknown_doc",
        extracted_items=[p.model_dump() for p in predicted_results],
        ground_truth_items=ground_truth_data_for_run # 直接传递
    )
    correct, missed, false, metrics = ComparisonHelper.calculate_metrics_from_details(
        comparison_result_obj.comparison_details
    )
    
    # 构建最终的、平铺的、API友好的输出字典
    single_task_output = {
        "document_id": document_id,
        
        # 顶层比对字段，与 compare 模式完全一致
        "correct_payments": [p.model_dump() for p in correct],
        "missed_payments": [p.model_dump() for p in missed],
        "false_payments": [p.model_dump() for p in false],
        "evaluation_metrics": metrics.model_dump(),
        
        # 保留 summary 和 details 用于生成报告或深度分析
        "summary": comparison_result_obj.summary.model_dump(),
        "comparison_details": [d.model_dump() for d in comparison_result_obj.comparison_details],
        
        # 人类可读的报告
        # "report_markdown": _generate_markdown_report(comparison_result_obj)
    }

    return {
        "current_comparison_result": single_task_output, 
        "current_step": "comparison_done",
        # --- 手动传递核心控制状态 ---
        "is_initialized": state.get("is_initialized"),
        "input_queue": state.get("input_queue", []),
        "processed_items": state.get("processed_items", [])
    }