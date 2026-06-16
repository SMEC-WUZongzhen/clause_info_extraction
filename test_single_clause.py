"""
test_llm_direct.py - 直接测试底层大模型是否可用（OpenAI 兼容协议）
用法: python test_llm_direct.py
"""
import json
import requests

# ===== 配置 =====
# 底层模型地址（OpenAI 兼容，百舸平台远程）
BASE_URL = "http://10.204.2.21:168"
ENDPOINT = "/v1/chat/completions"
# 远程网关需要 Bearer token；不带 "Bearer " 前缀，代码里会自动拼
API_KEY = ""
# 模型名通过 GET /v1/models 查到，必须用完整 id 字符串
MODEL_NAME = "/bos/smec-ai-model/models/rag/payment_info_extraction_model/payment_info_qwen35_9B_sft_V3_amv-rf9a88eptvev_2026-06-04-08:58:36/1"

TIMEOUT = 60


def main():
    url = BASE_URL.rstrip("/") + ENDPOINT
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "system",
                "content": (
                    ""
                ),
            },
            {
                "role": "user",
                "content": (
                    "设备合同和安装合同你可以做些什么？"
                ),
            },
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
    }
    # payload = {
    #     "model": MODEL_NAME,
    #     "messages": [
    #         {
    #             "role": "system",
    #             "content": (
    #                 '''
    #                 ## 任务
    #                 判断"待验证条款"本身是否为有效付款条款。
    #                 有效付款条款必须**同时包含**：①正向支付动作（"支付""付款"等）+ ②具体金额或比例（X万元、X%等数值）。
    #                 两者缺一则 is_valid=false。

    #                 ## 直接排除（命中任一 → is_valid=false）
    #                 - 含"违约金/罚款/赔偿/赔偿损失/违约责任"
    #                 - 含"逾期"且含"利息/利率/滞纳金"
    #                 - 含"质量问题/质量缺陷"
    #                 - 含"有权不支付/拒付/暂停支付"
    #                 - 含"保函/担保函"但无"支付X%/X元"的实际付款
    #                 - 仅描述价格/报价/总价/单价，无支付动作
    #                 - 仅描述支付方式/工具，无金额比例
    #                 - 仅描述付款前置条件/依据/验收要求，无支付动作和金额
    #                 - 仅描述供货期/交货期/工期等时间安排，无支付动作
    #                 - 概述性/引导性条款，无具体金额
    #                 - 指引条款（如"详见第X条"）
    #                 - 单据要求（如"正本X份，副本X份"）
    #                 - 附件/目录/表格标题
    #                 - 不完整的句子片段

    #                 ## 输出
    #                 只输出JSON对象，无其他文字。
    #                 {{"id": "输入的id", "is_valid": true/false, "reason": "简短理由"}}

    #                 '''
    #             ),
    #         },
    #         {
    #             "role": "user",
    #             "content": (
    #                 '''
    #                 ## 待验证条款
    #                 "id":70,
    #                 "clause":	2.3若因非乙方原因无法按甲方审核确认的按照计划完成电梯安装工程，甲方应在正式开工安装后10个月内向乙方付清本合同余款，但不免除乙方完成剩余电梯安装义务、调试、检验检测、成品保护和维保等义务。
    #                 "clause_class": 安装付款条款
    #                 '''
    #             ),
    #         },
    #     ],
    #     "temperature": 0.1,
    #     "max_tokens": 2048,
    # }

    print(f"POST {url}")
    print(f"model={MODEL_NAME}")

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        print(f"[失败] 无法连接: {e}")
        return
    except requests.exceptions.Timeout:
        print(f"[失败] 超时（{TIMEOUT}s）")
        return

    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"[失败] 响应体: {resp.text[:500]}")
        return

    data = resp.json()
    print("\n===== 完整响应 =====")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    try:
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        print("\n===== 模型回复 =====")
        print(content)
        print(f"\ntokens: prompt={usage.get('prompt_tokens')} "
              f"completion={usage.get('completion_tokens')} "
              f"total={usage.get('total_tokens')}")
        print("\n[成功] 模型可用")
    except (KeyError, IndexError):
        print("[警告] 响应格式不是标准 OpenAI 格式")


if __name__ == "__main__":
    main()