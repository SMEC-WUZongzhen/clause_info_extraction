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