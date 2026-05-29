"""
test_llm_direct.py - 直接测试底层大模型是否可用（OpenAI 兼容协议）
用法: python test_llm_direct.py
"""
import json
import requests

# ===== 配置 =====
# 底层模型地址（OpenAI 兼容，百舸平台远程）
BASE_URL = "http://106.13.172.186/s-rfd3b09c8a04/8000"
ENDPOINT = "/v1/chat/completions"
# 远程网关需要 Bearer token；不带 "Bearer " 前缀，代码里会自动拼
API_KEY = "c6898da8-35572cf72ce6-ba9ceee63805"
# 模型名通过 GET /v1/models 查到，必须用完整 id 字符串
MODEL_NAME = "/bos/smec-ai-model/models/rag/payment_info_extraction_model/payment_info_qwen35_9B_sft_V2_amv-jb39ihsutj64_2026-05-21-19:25:31/1"

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
                    "你是一位顶级的合同支付条款分析专家，"
                    "任务是精准识别并提取给定条款中的所有付款节点（一个条款可能包含多个独立的付款动作）。"
                    "对每个节点，请输出：payment_type（节点类型，如预付款/进度款/验收款/质保金等）、"
                    "ratio（比例，如 0.3）、amount（金额）、reason（依据原文的简要说明）。"
                    "请以 JSON 数组形式返回。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请从下面这条合同条款中提取所有付款节点：\n\n"
                    "4.1预付款：合同签订后、维修基金开通后十天内，"
                    "甲方向乙方支付本合同总价的30%的合同预付款，"
                    "即人民币大写：叁拾叁万叁仟伍佰捌拾伍元(RMB:333,585元)。"
                ),
            },
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
    }

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