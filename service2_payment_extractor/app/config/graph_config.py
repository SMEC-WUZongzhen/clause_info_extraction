# app/config/graph_config.py

"""
这个文件存放与工作流图结构相关的共享配置，
例如节点的进度范围，以避免循环导入。
"""

# 定义全局工作流进度范围
# key: 节点名称 (与 add_node 中的名称一致)
# value: (起始百分比, 结束百分比)
WORKFLOW_PROGRESS_RANGES = {
    "initializer": (0, 5),
    "doc_parser": (5, 70),
    "payment_info_extractor": (70, 95),
    "comparison_node": (95, 98), # 比对占一小部分
    "output_node": (98, 100),   # 输出占最后一部分
}

# M3 修复：payment_info_extractor 节点内部各阶段的进度百分比（0-100，在节点 progress_range 内归一化）。
# 集中维护，避免散落在节点函数内的 magic number；调整阶段时只改此处。
EXTRACTOR_STAGE_PROGRESS = {
    "validate":           5,    # 条款有效性验证
    "resolve_mixed":      8,    # 混签归属判定
    "concurrent_extract": 20,   # RAG + LLM 并发抽取
    "summary_review":     60,   # 批量复核
    "result_verify":      70,   # 校验阶段
}
