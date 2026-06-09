# demo_call_service2.py
import json
import requests

# # ===== 百舸平台地址（按需替换） =====
# BASE_URL = "http://106.13.172.186/s-r644699c4b7c/8000"
# API_KEY  = "7d9b2e17-2290d95b9773-2e862b5cee2c"

# ===== 本地 =====
BASE_URL = "http://localhost:8001"


HEADERS = {
    "Content-Type": "application/json",
}


def pretty(title, obj):
    print(f"\n===== {title} =====")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------- 0. 健康检查 ----------
def health():
    r = requests.get(f"{BASE_URL}/health", headers=HEADERS, timeout=10)
    pretty("GET /health", r.json())


# ---------- 1. extract 模式 ----------
def demo_extract():
    payload = {
        "id": "demo-extract-001",
        "operation_type": "extract",
        "paragraphs": [
            {
                "clause": "合同签订后7日内，买方支付合同总价款的30%作为预付款。",
                "clause_context": (
                    "第五条 付款方式\n"
                    "5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n"
                    "5.2 设备到货验收合格后，支付合同总价款的60%。\n"
                    "5.3 质保期满后支付剩余10%质保金。"
                ),
                "clause_class": ["设备付款条款"],
            },
            {
                "clause": "设备到货验收合格后，买方支付合同总价款的60%。",
                "clause_context": (
                    "第五条 付款方式\n"
                    "5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n"
                    "5.2 设备到货验收合格后，支付合同总价款的60%。\n"
                    "5.3 质保期满后支付剩余10%质保金。"
                ),
                "clause_class": ["设备付款条款"],
            },
            {
                "clause": "质保期为自竣工验收合格之日起24个月。",
                "clause_context": "第七条 质保期\n质保期为自竣工验收合格之日起24个月，质保金为合同总价的5%。",
                "clause_class": ["质保期条款"],
            },
        ],
    }

    r = requests.post(
        f"{BASE_URL}/extract_payment_info",
        json=payload, headers=HEADERS, timeout=600,
    )
    print(f"HTTP {r.status_code}")
    data = r.json()
    pretty("extract 模式响应", data)

    print("\n--- 字段速览 ---")
    for i, item in enumerate(data.get("extraction_result", []), 1):
        if "payment_type" in item:
            print(
                f"[{i}] PaymentItem | category={item.get('clause_category')} "
                f"| type={item.get('payment_type')} | ratio={item.get('payment_ratio')}% "
                f"| amount={item.get('payment_amount')} "
                f"| payment_days={item.get('payment_days')} "
                f"| latest_payment_stage={item.get('latest_payment_stage')} "
                f"| latest_payment_date={item.get('latest_payment_date')} "
                f"| special_clause_content={item.get('special_clause_content')}"
            )
        else:
            print(f"[{i}] WarrantyItem | warranty={item.get('warranty')} | clause={item.get('warranty_clause')}")


# ---------- 2. analyze 模式 ----------
def demo_analyze():
    payload = {
        "id": "demo-analyze-001",
        "operation_type": "analyze",
        "paragraphs": [
            {
                "clause": "合同签订后3日内，甲方支付合同价款的20%作为定金。",
                "clause_context": (
                    "第四条 付款条款\n"
                    "4.1 合同签订后3日内，甲方支付合同价款的20%作为定金。\n"
                    "4.2 设备安装验收合格后支付至合同总价款的90%。\n"
                    "4.3 质保期满无质量问题后，支付剩余10%质保金。"
                ),
                "clause_class": ["设备付款条款"],
            },
            {
                "clause": "4.2设备安装验收合格后支付至合同总价款的90%。4.3 质保期满无质量问题后，支付剩余10%质保金。",
                "clause_context": (
                    "第四条 付款条款\n"
                    "4.1 合同签订后3日内，甲方支付合同价款的20%作为定金。\n"
                    "4.2 设备安装验收合格后支付至合同总价款的90%。\n"
                    "4.3 质保期满无质量问题后，支付剩余10%质保金。"
                ),
                "clause_class": ["设备付款条款"],
            },
        ],
        "gt_payment_stages": [
            {"stage": "合同定金",   "ratio": "20%", "stage_amount": "5590", "category": "equipment_payment"},
            {"stage": "当地政府部门验收后", "ratio": 0.7,                          "category": "equipment_payment"},
            {"stage": "质保金1年", "ratio": "10%", "stage_amount": "2500", "category": "equipment_payment"},
        ],
    }

    r = requests.post(
        f"{BASE_URL}/extract_payment_info",
        json=payload, headers=HEADERS, timeout=600,
    )
    print(f"HTTP {r.status_code}")
    data = r.json()
    pretty("analyze 模式响应", data)

    metrics = data.get("evaluation_metrics", {})
    print(f"\n--- 评估指标 --- precision={metrics.get('precision')} | "
          f"recall={metrics.get('recall')} | f1={metrics.get('f1_score')}")


if __name__ == "__main__":
    health()
    demo_extract()
    demo_analyze()