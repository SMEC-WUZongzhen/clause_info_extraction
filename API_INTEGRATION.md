# 付款条款提取服务 - 接口调用文档

## 整体调用链路

```
调用方
  │
  ├─ Step 1 → POST http://<host>:2024/extract_clauses
  │            输入：合同文档（BOS路径 / URL / 原始文本）
  │            输出：付款条款段落列表 (paragraphs)
  │
  └─ Step 2 → POST http://<host>:2025/extract_payment_info
               输入：Step 1 的 paragraphs
               输出：付款节点列表（金额/比例）+ 质保期信息
```

---

## Service 1 — 条款提取服务

- **地址：** `http://<host>:2024`
- **健康检查：** `GET /health` → `{"status": "ok", "service": "service1_clause_extractor"}`

### `POST /extract_clauses`

**请求体：**

```json
{
  "id": "task-001",

  // 以下三选一，必须且只能提供一个
  "bos_path": "path/to/contract.pdf",       // BOS 存储路径
  "bos_bucket_name": "my-bucket",           // [可选] 显式指定 BOS bucket，不填则用默认
  "file_url": "https://example.com/a.pdf",  // 公开可访问的文档 URL
  "md_text": "# 合同正文\n...",             // 直接传入 Markdown/文本内容

  "json_text": "{...}"                      // [可选] 同源文档的 JSON 结构化文本
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | ✅ | 任务唯一标识 |
| `bos_path` | string | 三选一 | BOS 中的文档路径 |
| `file_url` | string | 三选一 | 公开文档 URL |
| `md_text` | string | 三选一 | 原始 Markdown/文本内容 |
| `bos_bucket_name` | string | ❌ | 显式指定 BOS bucket |
| `json_text` | string | ❌ | 同源文档的 JSON 结构化内容 |

**响应体：**

```json
{
  "id": "task-001",
  "message": "success",
  "paragraphs": [
    {
      "doc_id": "abc123",
      "page_index": 2,
      "chunk_seq": 5,
      "para_seq": 1,
      "start_char": 1200,
      "end_char": 1580,
      "text": "设备款在验收合格后支付合同总价的30%...",
      "clause_class": ["设备付款条款"],
      "confidence": 0.95,
      "metadata": {
        "sub_clauses": [
          {
            "text": "验收合格后支付30%",
            "type": "equipment_payment"
          }
        ]
      }
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `doc_id` | string | 文档 ID |
| `page_index` | int | 页码索引 |
| `chunk_seq` | int | 分块序号 |
| `para_seq` | int/string | 段落序号 |
| `start_char` / `end_char` | int | 在文档中的字符位置 |
| `text` | string | 段落文本 |
| `clause_class` | list[string] | 条款分类标签 |
| `confidence` | float/null | 分类置信度 |
| `metadata.sub_clauses` | list | 子条款，含 `text` 和 `type` |

**`type` 可能的值：**
- `equipment_payment` — 设备付款条款
- `installation_payment` — 安装付款条款
- `warranty` — 质保条款

---

## Service 2 — 付款信息提取服务

- **地址：** `http://<host>:2025`
- **健康检查：** `GET /health` → `{"status": "ok", "service": "service2_payment_extractor"}`

### `POST /extract_payment_info`

**请求体（`extract` 模式，直接使用 Service 1 的 `paragraphs`）：**

```json
{
  "id": "task-001",
  "operation_type": "extract",
  "paragraphs": [
    {
      "doc_id": "abc123",
      "page_index": 2,
      "chunk_seq": 5,
      "para_seq": 1,
      "start_char": 1200,
      "end_char": 1580,
      "text": "设备款在验收合格后支付合同总价的30%...",
      "clause_class": ["设备付款条款"],
      "confidence": 0.95,
      "metadata": {}
    }
  ]
}
```

**请求体（`analyze` 模式，需额外提供标准答案）：**

```json
{
  "id": "task-001",
  "operation_type": "analyze",
  "paragraphs": [ ],
  "gt_payment_stages": [
    {
      "stage": "到货验收后",
      "ratio": 30.0,
      "stage_amount": null,
      "category": "equipment_payment"
    },
    {
      "stage": "安装调试完成后",
      "ratio": null,
      "stage_amount": "50000元",
      "category": "installation_payment"
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | ✅ | 任务唯一标识 |
| `paragraphs` | list | ✅ | Service 1 输出的段落列表 |
| `operation_type` | string | ✅ | `"extract"` 或 `"analyze"` |
| `gt_payment_stages` | list | analyze 模式必填 | 标准答案，`ratio` 和 `stage_amount` 至少填一个 |

**响应体（`extract` 模式）：**

```json
{
  "id": "task-001",
  "message": "success",
  "extraction_result": [
    {
      "clause_category": "equipment_payment",
      "payment_clause": "验收合格后支付合同总价的30%",
      "payment_context": "设备款在验收合格后支付合同总价的30%...",
      "payment_type": "到货验收款",
      "payment_ratio": 30.0,
      "payment_amount": null
    },
    {
      "warranty": "12个月",
      "warranty_clause": "质保期自验收合格之日起12个月"
    }
  ]
}
```

**响应体（`analyze` 模式，额外包含比对结果）：**

```json
{
  "id": "task-001",
  "message": "success",
  "extraction_result": [ ],
  "correct_payments": [ ],
  "missed_payments": [ ],
  "false_payments": [ ],
  "evaluation_metrics": {
    "accuracy": 0.85,
    "precision": 0.90,
    "recall": 0.80,
    "f1_score": 0.85
  }
}
```

**`extraction_result` 包含两种类型的条目：**

PaymentItem 字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `clause_category` | string | `equipment_payment` 或 `installation_payment` |
| `payment_clause` | string | 付款条款原文片段 |
| `payment_context` | string | 付款条款上下文 |
| `payment_type` | string | 付款类型（如"到货验收款"）|
| `payment_ratio` | float | 付款比例（百分比，如 `30.0` 表示 30%）|
| `payment_amount` | string | 付款金额（字符串形式）|

WarrantyItem 字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `warranty` | string | 质保期时长（如 `"12个月"`）|
| `warranty_clause` | string | 质保条款原文 |

---

## 完整调用示例（Python）

```python
import requests

BASE_URL_S1 = "http://localhost:2024"
BASE_URL_S2 = "http://localhost:2025"

# Step 1: 调用 Service 1 提取条款段落
resp1 = requests.post(f"{BASE_URL_S1}/extract_clauses", json={
    "id": "task-001",
    "file_url": "https://example.com/contract.pdf"
})
result1 = resp1.json()
paragraphs = result1["paragraphs"]

# Step 2: 将 paragraphs 传入 Service 2 提取付款信息
resp2 = requests.post(f"{BASE_URL_S2}/extract_payment_info", json={
    "id": "task-001",
    "operation_type": "extract",
    "paragraphs": paragraphs
})
result2 = resp2.json()

for item in result2["extraction_result"]:
    if "payment_type" in item:
        print(f"付款节点: {item['payment_type']}, 比例: {item['payment_ratio']}%")
    elif "warranty" in item:
        print(f"质保期: {item['warranty']}")
```

---

## 注意事项

1. **`id` 字段需保持一致** —— 两个请求建议使用相同的 `id`，便于日志追踪。
2. **`paragraphs` 直接透传** —— Service 2 的 `paragraphs` 字段结构与 Service 1 响应的 `paragraphs` 完全一致，可直接赋值，无需转换。
3. **`analyze` 模式** —— 仅用于评估/测试场景，生产环境使用 `extract` 即可。
4. **`gt_payment_stages` 中 `ratio` 和 `stage_amount` 至少填一个**，否则请求会被拒绝。
5. **服务启动默认端口**：Service 1 为 `2024`，Service 2 为 `2025`，部署时注意防火墙配置。
