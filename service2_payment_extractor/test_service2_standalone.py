"""
Service 2 付款信息提取服务 - 独立测试脚本
============================================
直接使用 Service 1 的输出（paragraphs）来调用 Service 2，
无需启动 Service 1，专注于测试和调试 Service 2 的提取逻辑。

用法：
    # 本地开发模式（需先启动: python main.py）
    python test_service2_standalone.py

    # 百舸平台远程模式
    python test_service2_standalone.py --remote

    # 批量处理单个文件夹
    python test_service2_standalone.py -d E:\\DEMO_CODE\\付款条款节点提取服务\\output\\2007197

    # 批量处理所有子文件夹（自动遍历 2007197/、2009700/ 等，跳过非文件夹）
    python test_service2_standalone.py -d E:\\DEMO_CODE\\付款条款节点提取服务\\output

    # 指定输入文件（从 output 目录自动选择最新，或手动指定）
    python test_service2_standalone.py --input ../../../output/filter_result_20260413_112557.json

    # 查看所有选项
    python test_service2_standalone.py -h
"""

import json
import time
import requests
from typing import List, Dict, Any, Optional
import pdb
import os
import argparse
import ast


# ===== Service 2 配置 =====

# 本地开发模式
SERVICE2_LOCAL_CONFIG = {
    "base_url": "http://localhost:8001",
    "endpoint": "/extract_payment_info",
    "timeout": 600,
}

# 远程生产模式（百舸平台）
SERVICE2_REMOTE_CONFIG = {
    "base_url": "http://106.13.172.186/s-r644699c4b7c/8000",
    "endpoint": "/extract_payment_info",
    "api_key": "7d9b2e17-2290d95b9773-2e862b5cee2c",  # 不包含 Bearer 前缀
    "timeout": 600,
}

# 当前使用的配置（可通过命令行参数切换）
SERVICE2_CONFIG = SERVICE2_LOCAL_CONFIG.copy()
SERVICE2_URL = f"{SERVICE2_CONFIG['base_url']}{SERVICE2_CONFIG['endpoint']}"
TIMEOUT = SERVICE2_CONFIG["timeout"]


OUT_BASE = r"E:\DEMO_CODE\付款条款节点提取服务_smec\test_input_output\服务2结果\payment_service2_line300_context700_v9_local_618"

# ===== 测试数据：Service 1 的输出结果 =====


def load_filter_result(input_file: str = None) -> List[Dict[str, Any]]:
    """从 filter_result JSON 文件加载段落数据"""
    if input_file is None:
        input_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "..", "output"
        )
        import glob
        files = sorted(glob.glob(os.path.join(input_file, "filter_result_*.json")))
        if not files:
            raise FileNotFoundError("未找到 filter_result_*.json 文件")
        input_file = files[-1]
        print(f"自动使用最新文件: {input_file}")

    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    paragraphs = []
    # 从 grouped_result 中提取
    grouped = data.get("grouped_result", {})
    for cls, items in grouped.items():
        for item in items:
            text = item.get("text", "").strip()
            if not text:
                continue
            context_data = item.get("context", {})
            full_context = context_data.get("full_context", "")
            clause = text
            paragraphs.append({
                "clause_class": [item.get("mapped_class", cls)],
                "clause_context": full_context,
                "clause": clause,
            })
    # 从 all_filtered_lines 中补充
    for line in data.get("all_filtered_lines", []):
        text = line.get("text", "").strip()
        if not text:
            continue
        if any(p.get("clause") == text for p in paragraphs):
            continue
        context_data = line.get("context", {})
        paragraphs.append({
            "clause_class": line.get("mapped_class", ""),
            "clause_context": context_data.get("full_context", ""),
            "clause": text,
        })

    print(f"共加载 {len(paragraphs)} 个有效段落")
    return paragraphs


def load_python_input(input_file: str = None) -> Dict[str, Any]:
    """
    从 Python 字典格式的文件加载数据（支持三引号多行字符串）。
    
    使用方式：
    1. 创建一个 .py 文件（不是 .json）
    2. 文件内容为 Python 字典，如：
       {
           "task_id": "demo-001",
           "full_text": '''# 标题
       
       多行文本
       '''
       }
    """
    if input_file is None:
        input_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "..", "input"
        )
        import glob
        # 优先查找 .py 文件
        files = sorted(glob.glob(os.path.join(input_file, "*.py")))
        if not files:
            raise FileNotFoundError("未找到 input 目录下的 .py 文件")
        input_file = files[-1]
        print(f"自动使用最新文件: {input_file}")
    
    with open(input_file, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 使用 ast.literal_eval 安全解析 Python 字典
    data = ast.literal_eval(content.strip())
    print(f"已加载 Python 输入文件: {input_file}")
    print(f"  task_id: {data.get('task_id', 'N/A')}")
    full_text = data.get('full_text', '')
    if isinstance(full_text, str):
        print(f"  full_text 长度: {len(full_text)} 字符")
        lines = full_text.split('\n')
        print(f"  full_text 行数: {len(lines)}")
        if lines:
            print(f"  首行: {lines[0][:60]}{'...' if len(lines[0]) > 60 else ''}")
    
    return data


SERVICE1_OUTPUT_PARAGRAPHS: List[Dict[str, Any]] = []

# ===== Ground Truth 数据（可选，用于 analyze 模式） =====
# 与 correct_payments 对应，可验证提取准确性
GT_PAYMENT_STAGES: List[Dict[str, Any]] = [
    {
        "stage": "预付款",
        "ratio": 0.2,             # 整数百分比，5 → 0.05
        "stage_amount": 193600,   # 纯数字，无需带"元"
        "category": "equipment_payment"
    },
    {
        "stage": "进场前（首付）",
        "ratio": "50%",         # 百分比字符串，"95%" → 0.95
        "stage_amount": 11350,
        "category": "installation_payment"
    }
]


# ===== 调用函数 =====
def call_service2(
    record_id: str,
    paragraphs: List[Dict[str, Any]],
    operation_type: str = "extract",
    sis_payment_stages: Optional[List[Dict[str, Any]]] = None,
    debug: bool = True
) -> Dict[str, Any]:
    """
    调用 Service 2 的 /extract_payment_info 接口。

    :param record_id: 请求的唯一标识
    :param paragraphs: Service 1 输出的段落列表
    :param operation_type: 'extract'（仅提取）或 'analyze'（提取并对比）
    :param sis_payment_stages: Ground Truth 数据（仅 analyze 模式需要）
    :param debug: 是否打印调试信息
    :return: Service 2 的响应
    """
    # 构建请求体
    payload: Dict[str, Any] = {
        "id": f"{record_id}_debug" if debug else record_id,
        "paragraphs": paragraphs,
        "operation_type": operation_type,
    }

    if operation_type == "analyze":
        if not sis_payment_stages:
            raise ValueError("'analyze' 操作需要提供 sis_payment_stages")
        payload["sis_payment_stages"] = sis_payment_stages

    # 打印请求摘要
    if debug:
        print(f"\n{'=' * 80}")
        print(f"【输入】Service 2 请求")
        print(f"{'=' * 80}")
        print(f"URL: {SERVICE2_URL}")
        print(f"ID: {payload['id']}")
        print(f"操作类型: {operation_type}")
        print(f"\n>>> 输入段落数: {len(paragraphs)}")
        for i, p in enumerate(paragraphs):
            cls = ", ".join(p.get("clause_class", []))
            text_preview = (p.get("clause") or p.get("text") or "")[:80].replace("\n", " ")
            print(f"  [{i + 1}] [{cls}]")
            print(f"      {text_preview}{'...' if len(p.get('clause') or p.get('text') or '') > 80 else ''}")
        if operation_type == "analyze":
            print(f"\n>>> GT节点数: {len(sis_payment_stages)}")
            for gt in sis_payment_stages:
                print(f"  - {gt['stage']}: {gt['ratio']} / {gt['stage_amount']}")
        print(f"{'=' * 80}")

    # 构建请求头
    headers = {"Content-Type": "application/json"}
    if "api_key" in SERVICE2_CONFIG:
        headers["Authorization"] = f"Bearer {SERVICE2_CONFIG['api_key']}"

    # 发送请求
    start_time = time.time()
    try:
        response = requests.post(
            SERVICE2_URL,
            json=payload,
            headers=headers,
            timeout=TIMEOUT
        )
        print("\n" + "=" * 80)
        print("【服务端日志输出】")
        print("=" * 80)
        print(response.json())
        print("=" * 80 + "\n")
        # pdb.set_trace()

    except requests.exceptions.Timeout:
        elapsed = time.time() - start_time
        print(f"[错误] 请求超时（{TIMEOUT}秒，耗时 {elapsed:.1f}s）")
        return {"id": record_id, "message": f"error: 请求超时（{TIMEOUT}秒）"}
    except requests.exceptions.ConnectionError:
        elapsed = time.time() - start_time
        print(f"[错误] 无法连接到 Service 2 ({SERVICE2_CONFIG['base_url']})，耗时 {elapsed:.1f}s")
        print(f"  请确认 Service 2 已启动，或使用 --remote 切换到百舸平台模式")
        return {"id": record_id, "message": f"error: 连接失败 - {SERVICE2_URL} 不可达"}
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"[错误] 请求失败: {str(e)}，耗时 {elapsed:.1f}s")
        return {"id": record_id, "message": f"error: {str(e)}"}

    elapsed = time.time() - start_time

    if response.status_code == 200:
        result = response.json()
        if debug:
            print(f"[成功] Service 2 调用完成，耗时 {elapsed:.1f}s")
        return result
    else:
        print(f"[错误] HTTP {response.status_code}: {response.text[:500]}")
        print(f"  耗时 {elapsed:.1f}s")
        return {"id": record_id, "message": f"error: HTTP {response.status_code}"}




def pretty_print_result(result: Dict[str, Any], paragraphs: List[Dict[str, Any]] = None):
    """美化打印 Service 2 的响应结果。paragraphs 用于匹配输入中的 clause_context。"""
    print(f"\n{'=' * 80}")
    print(f"【输出】Service 2 响应结果")
    print(f"{'=' * 80}")

    msg = result.get("message", "success")
    print(f"状态: {msg}")

    extraction = result.get("extraction_result", [])
    print(f"\n>>> 提取结果数: {len(extraction)}")

    payment_items = [r for r in extraction if r.get("payment_ratio") is not None or r.get("payment_clause")]
    warranty_items = [r for r in extraction if r.get("warranty") is not None]

    if payment_items:
        print(f"\n【付款条款】({len(payment_items)} 条)")
        print("-" * 60)
        for i, item in enumerate(payment_items, 1):
            clause = item.get("payment_clause", "")
            print(f"\n  #{i} [{item.get('clause_category', 'N/A')}]")
            print(f"     类型: {item.get('payment_type', 'N/A')}")
            print(f"     比例: {item.get('payment_ratio', 'N/A')}")
            print(f"     金额: {item.get('payment_amount', 'N/A')}")
            print(f"     原文: {clause[:100]}{'...' if len(clause) > 100 else ''}")
            # 服务端 payment_context 当前等于 payment_clause（已知问题），
            # 因此始终从输入 paragraphs 中匹配真实的 clause_context
            context = ""
            if paragraphs:
                for p in paragraphs:
                    p_clause = p.get("clause") or p.get("text") or ""
                    if p_clause and (p_clause in clause or clause in p_clause):
                        context = p.get("clause_context", "")
                        break
            if not context:
                context = item.get("payment_context", "")
            if context and context != clause:
                print(f"     上下文: {context[:200]}{'...' if len(context) > 200 else ''}")

    if warranty_items:
        print(f"\n【质保期】({len(warranty_items)} 条)")
        print("-" * 60)
        for i, item in enumerate(warranty_items, 1):
            print(f"\n  #{i}")
            print(f"     质保期: {item.get('warranty', 'N/A')}")
            clause = item.get("warranty_clause", "")
            print(f"     原文: {clause[:100]}{'...' if len(clause) > 100 else ''}")

    # analyze 模式额外输出
    if "correct_payments" in result:
        print(f"\n【评估 - 正确匹配】({len(result['correct_payments'])} 条)")
        print("-" * 40)
        for cp in result["correct_payments"]:
            print(f"  ✓ {cp}")

    if "missed_payments" in result:
        print(f"\n【评估 - 漏检】({len(result['missed_payments'])} 条)")
        print("-" * 40)
        for mp in result["missed_payments"]:
            print(f"  ✗ {mp}")

    if "false_payments" in result:
        print(f"\n【评估 - 误检】({len(result['false_payments'])} 条)")
        print("-" * 40)
        for fp in result["false_payments"]:
            print(f"  ? {fp}")

    if "evaluation_metrics" in result:
        metrics = result["evaluation_metrics"]
        print(f"\n【评估指标】")
        print("-" * 40)
        print(f"  Accuracy:  {metrics.get('accuracy', 'N/A')}")
        print(f"  Precision: {metrics.get('precision', 'N/A')}")
        print(f"  Recall:    {metrics.get('recall', 'N/A')}")
        print(f"  F1 Score: {metrics.get('f1_score', 'N/A')}")

    # 打印原始 JSON（可选）
    # print(f"\n【原始 JSON 响应】")
    # print(json.dumps(result, ensure_ascii=False, indent=2))

    print(f"\n{'=' * 80}\n")


def run_extract_mode():
    """测试 extract 模式（仅提取，无 GT 对比）"""
    print("\n" + "=" * 60)
    print("  测试模式: extract（仅提取）")
    print("=" * 60)

    result = call_service2(
        record_id="test_service2_extract",
        paragraphs=SERVICE1_OUTPUT_PARAGRAPHS,
        operation_type="extract",
        debug=True
    )

    if not result.get("message", "success").startswith("error"):
        pretty_print_result(result, paragraphs=SERVICE1_OUTPUT_PARAGRAPHS)
    else:
        print(f"\n[失败] {result.get('message')}")

    return result


def run_analyze_mode():
    """测试 analyze 模式（提取 + GT 对比）"""
    print("\n" + "=" * 60)
    print("  测试模式: analyze（提取并对比）")
    print("=" * 60)

    result = call_service2(
        record_id="test_service2_analyze",
        paragraphs=SERVICE1_OUTPUT_PARAGRAPHS,
        operation_type="analyze",
        sis_payment_stages=GT_PAYMENT_STAGES,
        debug=True
    )

    if not result.get("message", "success").startswith("error"):
        pretty_print_result(result, paragraphs=SERVICE1_OUTPUT_PARAGRAPHS)
    else:
        print(f"\n[失败] {result.get('message')}")

    return result


def run_batch_dir(batch_dir: str, record_id: str, operation_type: str = "extract",
                   sis_payment_stages: Optional[List[Dict[str, Any]]] = None,
                   raw: bool = False):
    """
    批量处理文件夹内所有 JSON 文件，汇总所有段落后一次性调用 Service 2，
    以便服务端执行去重等全局逻辑。结果输出到单个 JSON 文件。

    Args:
        batch_dir: 包含多个 JSON 文件的文件夹路径
        record_id: 请求 ID 前缀
        operation_type: extract 或 analyze
        sis_payment_stages: GT 数据（仅 analyze 模式）
        raw: 是否打印原始 JSON
    """
    import glob

    batch_dir = os.path.abspath(batch_dir)
    if not os.path.isdir(batch_dir):
        print(f"文件夹不存在: {batch_dir}")
        return

    json_files = sorted(glob.glob(os.path.join(batch_dir, "*.json")))
    if not json_files:
        print(f"文件夹内未找到 JSON 文件: {batch_dir}")
        return

    dir_name = os.path.basename(batch_dir)
    print(f"\n{'=' * 60}")
    print(f"批量处理文件夹: {batch_dir}")
    print(f"找到 {len(json_files)} 个 JSON 文件")
    print(f"{'=' * 60}")

    # 汇总所有文件的段落
    all_paragraphs = []
    loaded_files = []

    for i, json_file in enumerate(json_files, 1):
        file_basename = os.path.basename(json_file)
        print(f"\n[{i}/{len(json_files)}] 加载文件: {file_basename}")

        try:
            paragraphs = load_filter_result(json_file)
            if not paragraphs:
                print(f"  文件无有效段落，跳过")
                continue
            all_paragraphs.extend(paragraphs)
            loaded_files.append(file_basename)
            print(f"  加载 {len(paragraphs)} 个段落（累计 {len(all_paragraphs)} 个）")

        except Exception as e:
            print(f"  加载异常: {e}")

    print(f"\n{'-' * 60}")
    print(f"汇总完成: 从 {len(loaded_files)}/{len(json_files)} 个文件共加载 {len(all_paragraphs)} 个段落")
    print(f"{'-' * 60}")

    if not all_paragraphs:
        print("所有文件均无有效段落，跳过调用")
        # 写入空结果兜底
        output_base = OUT_BASE
        os.makedirs(output_base, exist_ok=True)
        output_path = os.path.join(output_base, f"{dir_name}.json")
        summary = {
            "source_dir": batch_dir,
            "total_files": len(json_files),
            "loaded_files": 0,
            "total_paragraphs": 0,
            "results": [{"message": "skip: 所有文件均无有效段落", "extraction_result": []}]
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"空结果已保存到: {output_path}")
        return

    # 一次性调用 Service 2
    result = call_service2(
        record_id=record_id,
        paragraphs=all_paragraphs,
        operation_type=operation_type,
        sis_payment_stages=sis_payment_stages if operation_type == "analyze" else None,
        debug=False
    )

    if not result.get("message", "success").startswith("error"):
        pretty_print_result(result, paragraphs=all_paragraphs)
    else:
        print(f"\n[失败] {result.get('message')}")

    # 汇总输出到 output_payment 目录
    output_base = OUT_BASE
    os.makedirs(output_base, exist_ok=True)
    output_path = os.path.join(output_base, f"{dir_name}.json")

    summary = {
        "source_dir": batch_dir,
        "total_files": len(json_files),
        "loaded_files": len(loaded_files),
        "total_paragraphs": len(all_paragraphs),
        "source_file_list": loaded_files,
        "results": [result]
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 60}")
    print(f"批量处理完成，共 {len(json_files)} 个文件，{len(all_paragraphs)} 个段落")
    print(f"汇总结果已保存到: {output_path}")
    print(f"{'=' * 60}")


# ===== 主程序 =====
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Service 2 付款信息提取服务 - 独立测试脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 本地开发模式（需先启动: python main.py）
  python test_service2_standalone.py

  # 百舸平台远程模式
  python test_service2_standalone.py --remote

  # 指定自定义地址
  python test_service2_standalone.py --url http://localhost:8000

  # 指定输入文件
  python test_service2_standalone.py --input ../../../output/filter_result_20260413_112557.json
  python test_service2_standalone.py -f ../../../output/filter_result_20260413_112557.json

  # analyze 模式（提取并对比 GT）
  python test_service2_standalone.py --remote --mode analyze

  # 指定请求 ID
  python test_service2_standalone.py --remote --id my_custom_id

  # 仅打印原始 JSON
  python test_service2_standalone.py --remote --raw
"""
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["extract", "analyze"],
        default="extract",
        help="操作模式: extract(仅提取) 或 analyze(提取并对比)。默认 extract"
    )
    parser.add_argument(
        "--url",
        default=None,
        help="自定义 Service 2 的 base URL，不指定则根据 --remote / 默认选择"
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="使用百舸平台远程模式（远程生产地址 + api_key）"
    )
    parser.add_argument(
        "--id",
        default="test_service2_standalone",
        help="请求 ID（默认 test_service2_standalone）"
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="仅打印原始 JSON 响应，不美化输出"
    )
    parser.add_argument(
        "--input", "-f",
        default=None,
        help="指定 filter_result JSON 文件路径，不指定则自动选择 output 目录下最新的 filter_result_*.json"
    )
    parser.add_argument(
        "--batch-dir", "-d",
        default=None,
        help="指定文件夹路径：若路径下直接含 JSON 文件则处理该文件夹；若路径下含子文件夹（如 2007197/、2009700/）则逐个处理所有子文件夹，跳过非文件夹项"
    )

    args = parser.parse_args()

    # 切换服务配置：--remote > --url > 默认本地
    if args.remote:
        SERVICE2_CONFIG = SERVICE2_REMOTE_CONFIG.copy()
        SERVICE2_URL = f"{SERVICE2_CONFIG['base_url']}{SERVICE2_CONFIG['endpoint']}"
        mode_label = "百舸平台远程"
    elif args.url:
        SERVICE2_URL = args.url.rstrip("/") + SERVICE2_CONFIG.get("endpoint", "/extract_payment_info")
        mode_label = f"自定义 ({args.url})"
    else:
        SERVICE2_CONFIG = SERVICE2_LOCAL_CONFIG.copy()
        SERVICE2_URL = f"{SERVICE2_CONFIG['base_url']}{SERVICE2_CONFIG['endpoint']}"
        mode_label = "本地开发"

    print(f"\n{'#' * 80}")
    print(f"# Service 2 付款信息提取服务 - 独立测试")
    print(f"# URL: {SERVICE2_URL}")
    print(f"# 模式: {args.mode} | 连接方式: {mode_label}")
    print(f"# ID: {args.id}")
    if "api_key" in SERVICE2_CONFIG:
        print(f"# 认证: Bearer Token (已配置)")
    else:
        print(f"# 认证: 无")
    print(f"{'#' * 80}")

    # 批量处理模式
    if args.batch_dir:
        batch_path = os.path.abspath(args.batch_dir)
        # 判断目标路径下是否包含子文件夹（多文件夹模式）还是仅有 JSON 文件（单文件夹模式）
        subdirs = sorted([
            d for d in os.listdir(batch_path)
            if os.path.isdir(os.path.join(batch_path, d))
        ])

        if subdirs:
            # 多文件夹批量模式：遍历所有子文件夹
            print(f"\n{'#' * 80}")
            print(f"# 批量模式：发现 {len(subdirs)} 个子文件夹")
            print(f"# 路径: {batch_path}")
            print(f"{'#' * 80}")
            for idx, subdir in enumerate(subdirs, 1):
                subdir_path = os.path.join(batch_path, subdir)
                print(f"\n>>> [{idx}/{len(subdirs)}] 处理文件夹: {subdir}")
                run_batch_dir(
                    batch_dir=subdir_path,
                    record_id=args.id,
                    operation_type=args.mode,
                    sis_payment_stages=GT_PAYMENT_STAGES if args.mode == "analyze" else None,
                    raw=args.raw
                )
                print(f">>> [{idx}/{len(subdirs)}] 文件夹 {subdir} 处理完成")
            print(f"\n{'#' * 80}")
            print(f"# 全部完成，共处理 {len(subdirs)} 个文件夹")
            print(f"{'#' * 80}")
        else:
            # 单文件夹模式（原逻辑）
            run_batch_dir(
                batch_dir=batch_path,
                record_id=args.id,
                operation_type=args.mode,
                sis_payment_stages=GT_PAYMENT_STAGES if args.mode == "analyze" else None,
                raw=args.raw
            )
        exit(0)

    # 从文件加载段落数据
    paragraphs = load_filter_result(args.input)

    # 选择模式
    if args.mode == "analyze":
        result = call_service2(
            record_id=args.id,
            paragraphs=paragraphs,
            operation_type="analyze",
            sis_payment_stages=GT_PAYMENT_STAGES,
            debug=False
        )
    else:
        result = call_service2(
            record_id=args.id,
            paragraphs=paragraphs,
            operation_type="extract",
            debug=False
        )

    # 输出结果
    if args.raw:
        print("\n--- 原始 JSON 响应 ---")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not result.get("message", "success").startswith("error"):
            pretty_print_result(result, paragraphs=paragraphs)
        else:
            print(f"\n[失败] {result.get('message')}")
