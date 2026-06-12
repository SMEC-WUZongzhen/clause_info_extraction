# 付款信息提取服务 API 文档

## 服务概述

**服务名称**: 付款信息提取服务 (Payment Extractor Service)

### 主要功能

接收合同条款段落列表（`paragraphs`），精确识别并提取付款节点信息（付款类型、比例、金额）和质保期信息，支持与标准答案进行比对评估。

### 百舸平台地址

| 配置项 | 值 |
|--------|-----|
| **基础URL** | `http://10.204.2.103/s-r644699c4b7c/8000` |
| **API端点** | `/extract_payment_info` |
| **认证方式** | Bearer Token |

---

## 标准付款节点（payment_type / payment_code）

服务输出的 `payment_type` 和 `payment_code` 基于以下标准节点定义，按设备/安装两类分别管理。

### 设备付款节点

| payment_code | 标准节点名（payment_type） |
|---|---|
| EARNEST | 合同定金 |
| DEPOSIT | 预付款 |
| WITHDRAWAL | 提货款 |
| Z001 | 到货前 |
| Z002 | 货到工地 |
| Z003 | 安装后 |
| Z004 | 公司验收后 |
| Z005 | 当地政府部门验收后 |
| Z020 | 结算完成后 |
| Z024 | 特殊约定付款-移交前 |
| Z016 | 电梯移交用户后 |
| Z011 | 特殊约定付款-移交后 |
| Z017 | 工程整体竣工 |
| Z023 | 质保金1年 |
| Z006 | 质保金2年 |
| Z025 | 特殊约定付款-质保期 |
| Z021 | 保理发票 |
| Z022 | 保理应收 |

### 安装付款节点

| payment_code | 标准节点名（payment_type） |
|---|---|
| Z018 | 安装定金 |
| DOWNPAYMENT | 进场前 |
| Z027 | 进场开工后 |
| Z003 | 安装后 |
| Z019 | 报验前 |
| Z004 | 公司验收后 |
| Z005 | 当地政府部门验收后 |
| Z020 | 结算完成后 |
| Z028 | 移交前 |
| Z024 | 特殊约定付款-移交前 |
| Z016 | 电梯移交用户后 |
| Z011 | 特殊约定付款-移交后 |
| Z017 | 工程整体竣工 |
| Z023 | 质保金1年 |
| Z006 | 质保金2年 |
| Z025 | 特殊约定付款-质保期 |

---

## API 端点

### 1. 健康检查

**端点**: `GET /health`

**描述**: 检查服务是否正常运行

**请求示例**:
```bash
curl -X GET http://10.204.2.103/s-r644699c4b7c/8000/health \
  -H "Authorization: Bearer 7d9b2e17-2290d95b9773-2e862b5cee2c"
```

**响应示例**:
```json
{
  "status": "ok",
  "service": "payment_extractor_service"
}
```

---

### 2. 提取付款信息

**端点**: `POST /extract_payment_info`

**描述**: 从合同条款段落中提取付款信息和质保期信息

#### 请求参数

##### 请求头
```
Content-Type: application/json
```

##### 请求体 (JSON)

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `id` | string | ✅ | 任务唯一标识符 |
| `paragraphs` | array[object] | ✅ | 合同条款段落列表 |
| `operation_type` | string | ❌ | 操作类型：`"extract"`（仅提取）或 `"analyze"`（提取并比对），默认 `"extract"` |
| `sis_payment_stages` | array[object] | 条件必填 | 标准答案列表，`operation_type="analyze"` 时必填 |

**ParagraphInput 对象结构**:

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `clause` | string | ✅ | 原子子条款原文（核心字段） |
| `clause_context` | string | ✅ | 子条款所在段落的完整上下文，用于语义增强检索 |
| `clause_class` | array[string] | ❌ | 条款分类标签，可选值：`["设备付款条款"]`、`["安装付款条款"]`、`["混签付款条款"]`、`["质保期条款"]` |

**GroundTruthItem 对象结构**（仅 `analyze` 模式需要）:

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `stage` | string | ✅ | 付款节点/阶段名称（原始合同用语） |
| `ratio` | float/int/string | 二选一 | 付款比例，支持多种格式：`0.3`（小数）、`30`（整数百分比）、`"30%"`（百分比字符串）；内部统一归一化为 0-1 小数 |
| `stage_amount` | float/int/string | 二选一 | 固定金额，支持数字（`5590`）或字符串（`"5590元"`） |
| `category` | string | ❌ | 条款类型：`"equipment_payment"`（设备付款，默认）或 `"installation_payment"`（安装付款） |

**重要约束**:
- `ratio` 和 `stage_amount` 必须至少提供一个，两者都填更佳
- `ratio` 输入会自动归一化：例如 `30`、`"30%"`、`0.3` 均等价于内部值 `0.3`

#### 请求示例

##### 示例 1: 仅提取模式 (extract)

```bash
curl -X POST http://10.204.2.103/s-r644699c4b7c/8000/extract_payment_info \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 7d9b2e17-2290d95b9773-2e862b5cee2c" \
  -d '{
    "id": "task-001",
    "operation_type": "extract",
    "paragraphs": [
      {
        "clause": "合同签订后7日内，买方支付合同总价款的30%作为预付款。",
        "clause_context": "第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。",
        "clause_class": ["设备付款条款"]
      },
      {
        "clause": "设备到货验收合格后，买方支付合同总价款的60%。",
        "clause_context": "第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。",
        "clause_class": ["设备付款条款"]
      },
      {
        "clause": "质保期为自竣工验收合格之日起24个月。",
        "clause_context": "第七条 质保期\n质保期为自竣工验收合格之日起24个月，质保金为合同总价的5%。",
        "clause_class": ["质保期条款"]
      }
    ]
  }'
```

##### 示例 2: 提取并比对模式 (analyze)

```bash
curl -X POST http://10.204.2.103/s-r644699c4b7c/8000/extract_payment_info \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 7d9b2e17-2290d95b9773-2e862b5cee2c" \
  -d '{
    "id": "task-002",
    "operation_type": "analyze",
    "paragraphs": [
      {
        "clause": "合同签订后3日内，甲方支付合同价款的20%作为定金。",
        "clause_context": "第四条 付款条款\n4.1 合同签订后3日内，甲方支付合同价款的20%作为定金。\n4.2 货物安装调试完成后支付至合同总价款的90%。\n4.3 质保期满无质量问题后，支付剩余10%质保金。",
        "clause_class": ["设备付款条款"]
      },
      {
        "clause": "货物安装调试完成后支付至合同总价款的90%。",
        "clause_context": "第四条 付款条款\n4.1 合同签订后3日内，甲方支付合同价款的20%作为定金。\n4.2 货物安装调试完成后支付至合同总价款的90%。\n4.3 质保期满无质量问题后，支付剩余10%质保金。",
        "clause_class": ["设备付款条款"]
      }
    ],
    "sis_payment_stages": [
      {
        "stage": "合同定金",
        "ratio": "20%",
        "stage_amount": "5590",
        "category": "equipment_payment"
      },
      {
        "stage": "公司验收后",
        "ratio": 0.7,
        "category": "equipment_payment"
      },
      {
        "stage": "质保金1年",
        "ratio": "10%",
        "stage_amount": "2500",
        "category": "equipment_payment"
      }
    ]
  }'
```

##### 示例 3: 包含多种条款类型

```bash
curl -X POST http://10.204.2.103/s-r644699c4b7c/8000/extract_payment_info \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 7d9b2e17-2290d95b9773-2e862b5cee2c" \
  -d '{
    "id": "task-003",
    "operation_type": "extract",
    "paragraphs": [
      {
        "clause": "合同签订后7日内，买方支付合同总价款的20%作为预付款。",
        "clause_context": "第四条 付款方式\n4.1 合同签订后7日内，买方支付合同总价款的20%作为预付款。\n4.2 设备到货验收后支付60%。",
        "clause_class": ["设备付款条款"]
      },
      {
        "clause": "安装队进场前三天，甲方支付安装款的30%作为进场费。",
        "clause_context": "第五条 安装付款\n5.1 安装队进场前三天，甲方支付安装款的30%作为进场费。\n5.2 安装完成验收后支付剩余70%。",
        "clause_class": ["安装付款条款"]
      },
      {
        "clause": "设备款支付总价的70%，安装款支付总价的30%，均需在合同签订后7日内支付。",
        "clause_context": "第六条 综合付款\n6.1 设备款支付总价的70%，安装款支付总价的30%，均需在合同签订后7日内支付。",
        "clause_class": ["混签付款条款"]
      }
    ]
  }'
```

#### 响应

##### 成功响应 - extract 模式 (200 OK)

```json
{
  "id": "task-001",
  "message": "success",
  "extraction_result": [
    {
      "clause_category": "equipment_payment",
      "payment_clause": "合同签订后7日内，买方支付合同总价款的30%作为预付款。",
      "payment_context": "第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。",
      "payment_type": "预付款",
      "payment_code": "DEPOSIT",
      "payment_ratio": 30.0,
      "payment_amount": null,
      "payment_days": null,
      "latest_payment_stage": null,
      "latest_payment_date": null,
      "special_clause_content": null
    },
    {
      "clause_category": "equipment_payment",
      "payment_clause": "设备到货验收合格后，买方支付合同总价款的60%。",
      "payment_context": "第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。",
      "payment_type": "公司验收后",
      "payment_code": "Z004",
      "payment_ratio": 60.0,
      "payment_amount": null,
      "payment_days": null,
      "latest_payment_stage": null,
      "latest_payment_date": null,
      "special_clause_content": null
    },
    {
      "warranty": "24个月",
      "warranty_clause": "质保期为自竣工验收合格之日起24个月。"
    }
  ]
}
```

##### 成功响应 - analyze 模式 (200 OK)

```json
{
  "id": "task-002",
  "message": "success",
  "extraction_result": [
    {
      "clause_category": "equipment_payment",
      "payment_clause": "合同签订后3日内，甲方支付合同价款的20%作为定金。",
      "payment_context": "第四条 付款条款\n4.1 合同签订后3日内，甲方支付合同价款的20%作为定金。\n4.2 货物安装调试完成后支付至合同总价款的90%。\n4.3 质保期满无质量问题后，支付剩余10%质保金。",
      "payment_type": "合同定金",
      "payment_code": "EARNEST",
      "payment_ratio": 20.0,
      "payment_amount": null,
      "payment_days": null,
      "latest_payment_stage": null,
      "latest_payment_date": null,
      "special_clause_content": null
    },
    {
      "clause_category": "equipment_payment",
      "payment_clause": "货物安装调试完成后支付至合同总价款的90%。",
      "payment_context": "第四条 付款条款\n4.1 合同签订后3日内，甲方支付合同价款的20%作为定金。\n4.2 货物安装调试完成后支付至合同总价款的90%。\n4.3 质保期满无质量问题后，支付剩余10%质保金。",
      "payment_type": "公司验收后",
      "payment_code": "Z004",
      "payment_ratio": 90.0,
      "payment_amount": null,
      "payment_days": null,
      "latest_payment_stage": null,
      "latest_payment_date": null,
      "special_clause_content": null
    }
  ],
  "correct_payments": [
    {
      "payment_type": "公司验收后",
      "payment_code": "Z004",
      "payment_ratio": 90.0,
      "payment_amount": null,
      "source": "AI提取",
      "payment_days": null,
      "latest_payment_stage": null,
      "latest_payment_date": null,
      "special_clause_content": null
    }
  ],
  "missed_payments": [
    {
      "payment_type": "合同定金",
      "payment_code": "EARNEST",
      "payment_ratio": 7.0,
      "payment_amount": "5590",
      "source": "SIS系统",
      "payment_days": null,
      "latest_payment_stage": null,
      "latest_payment_date": null,
      "special_clause_content": null
    }
  ],
  "false_payments": [
    {
      "payment_type": "合同定金",
      "payment_code": "EARNEST",
      "payment_ratio": 5.0,
      "payment_amount": "5590",
      "source": "AI提取",
      "payment_days": null,
      "latest_payment_stage": null,
      "latest_payment_date": null,
      "special_clause_content": null
    }
  ],
  "evaluation_metrics": {
    "accuracy": 0.5,
    "precision": 0.5,
    "recall": 0.5,
    "f1_score": 0.5
  }
}
```

##### 响应字段说明

**extract 模式响应字段**:

| 字段 | 类型 | 描述 |
|------|------|------|
| `id` | string | 任务 ID |
| `message` | string | 固定为 `"success"` |
| `extraction_result` | array | 提取结果列表，包含 PaymentItem 和 WarrantyItem |

**analyze 模式额外响应字段**:

| 字段 | 类型 | 描述 |
|------|------|------|
| `correct_payments` | array | 与基准答案匹配的付款节点 |
| `missed_payments` | array | 基准答案中存在但未提取到的节点 |
| `false_payments` | array | 提取到但基准答案中不存在的节点 |
| `evaluation_metrics` | object | 评估指标 |

**CorrectPaymentItem 对象字段**（`correct_payments` 数组元素）:

| 字段 | 类型 | 描述 |
|------|------|------|
| `payment_type` | string | 付款类型（**标准节点名称**，经映射后输出） |
| `payment_code` | string/null | 付款节点编码 |
| `payment_ratio` | float | 提取出的付款比例（百分比，如 `95.0` 表示 95%） |
| `payment_amount` | string | 提取出的付款金额 |
| `source` | string | 数据来源，固定为 `"AI提取"` |
| `payment_days` | int/null | 付款天数（correct 分支从提取结果回填，missed/false 为 null） |
| `latest_payment_stage` | string/null | 最迟付款节点（correct 分支回填） |
| `latest_payment_date` | int/null | 最迟付款时间-天数（correct 分支回填） |
| `special_clause_content` | string/null | 特殊条款内容（文档级汇总，统一回填） |

**MissedPaymentItem 对象字段**（`missed_payments` 数组元素）:

| 字段 | 类型 | 描述 |
|------|------|------|
| `payment_type` | string | 基准答案中的付款类型（尝试映射为标准节点名，未命中时保持 GT 原名） |
| `payment_code` | string/null | 付款节点编码（GT 名称未命中映射时为 `null`） |
| `payment_ratio` | float | 基准答案中的付款比例（百分比） |
| `payment_amount` | string | 基准答案中的付款金额 |
| `source` | string | 数据来源，固定为 `"SIS系统"` 或其他基准来源标识 |
| `payment_days` | int/null | 付款天数（missed 分支无提取来源，为 null） |
| `latest_payment_stage` | string/null | 最迟付款节点（null） |
| `latest_payment_date` | int/null | 最迟付款时间（null） |
| `special_clause_content` | string/null | 特殊条款内容（文档级汇总） |

**FalsePaymentItem 对象字段**（`false_payments` 数组元素）:

| 字段 | 类型 | 描述 |
|------|------|------|
| `payment_type` | string | 付款类型（**标准节点名称**，经映射后输出） |
| `payment_code` | string/null | 付款节点编码 |
| `payment_ratio` | float | 提取出的付款比例（百分比） |
| `payment_amount` | string | 提取出的付款金额 |
| `source` | string | 数据来源，固定为 `"AI提取"` |
| `payment_days` | int/null | 付款天数（false 分支无提取来源，为 null） |
| `latest_payment_stage` | string/null | 最迟付款节点（null） |
| `latest_payment_date` | int/null | 最迟付款时间（null） |
| `special_clause_content` | string/null | 特殊条款内容（文档级汇总） |

**EvaluationMetrics 对象字段**:

| 字段 | 类型 | 描述 |
|------|------|------|
| `accuracy` | float | 准确率（0-1），实际等于 F1 分数 |
| `precision` | float | 精确率（0-1） |
| `recall` | float | 召回率（0-1） |
| `f1_score` | float | F1 分数（0-1） |

**PaymentItem 对象字段**:

| 字段 | 类型 | 描述 |
|------|------|------|
| `clause_category` | string | `"equipment_payment"` 或 `"installation_payment"` |
| `payment_clause` | string | 付款条款原文片段 |
| `payment_context` | string | 付款条款所在段落的完整上下文（对应输入的 `clause_context`） |
| `payment_type` | string | 付款类型（**标准节点名称**，参见下方"标准节点映射表"） |
| `payment_code` | string/null | 付款节点编码（与 `payment_type` 对应，如 `EARNEST`、`Z023`） |
| `payment_ratio` | float/null | 付款比例（百分比） |
| `payment_amount` | string/null | 付款金额 |
| `payment_days` | int/null | 付款天数（自付款触发条件起的常规周期天数） |
| `latest_payment_stage` | string/null | 最迟付款节点名称（事件/状态描述） |
| `latest_payment_date` | int/null | 最迟付款时间（截止天数） |
| `special_clause_content` | string/null | 特殊条款内容（文档内全部付款条款文本汇总） |

**WarrantyItem 对象字段**:

| 字段 | 类型 | 描述 |
|------|------|------|
| `warranty` | string | 质保期时长（如 `"12个月"`、`"24个月"`) |
| `warranty_clause` | string | 质保条款原文 |

##### 错误响应

**400 Bad Request** - 请求参数错误

```json
{
  "detail": "'analyze' 操作必须提供 'sis_payment_stages'。"
}
```

**422 Unprocessable Entity** - 数据验证错误

```json
{
  "detail": [
    {
      "loc": ["body", "sis_payment_stages", 0],
      "msg": "'ratio' 和 'stage_amount' 必须至少提供一个。",
      "type": "value_error"
    }
  ]
}
```

**500 Internal Server Error** - 服务器内部错误

```json
{
  "detail": "提取失败: <错误详情>"
}
```

---

### 3. OpenAI 兼容接口

**端点**: `POST /v1/chat/completions`

**描述**: OpenAI Chat Completions API 兼容接口，支持通过标准 OpenAI 格式调用付款信息提取服务。

#### 请求示例

```bash
curl -X POST http://10.204.2.103/s-r644699c4b7c/8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 7d9b2e17-2290d95b9773-2e862b5cee2c" \
  -d '{
    "model": "payment-extractor",
    "messages": [
      {
        "role": "user",
        "content": "请提取以下条款中的付款信息：\n\n第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。"
      }
    ],
    "temperature": 0.1,
    "max_tokens": 4096
  }'
```

#### 响应示例

```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "payment-extractor",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "已提取到以下付款节点：\n1. 预付款 - 30%\n2. 到货款 - 60%"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 150,
    "completion_tokens": 80,
    "total_tokens": 230
  }
}
```

> **注意**: `/v1/chat/completions` 接口适合作为 OpenAI 兼容客户端（如 LangChain、AutoGen 等）的后端使用。对于结构化的 API 调用，建议仍使用 `POST /extract_payment_info` 接口。

---

### 4. 合同总金额一致性比对

**端点**: `POST /compare_contract_price`

**描述**: 调用 LLM 从「合同总价条款」文本中提取合同总金额，并与 SIS 系统中记录的合同金额做硬编码差值比对（阈值固定为 `10.0` 元，不可配置）。

#### 请求参数

##### 请求头
```
Content-Type: application/json
```

##### 请求体 (JSON)

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `id` | string | ✅ | 数据标识，仅允许 `[A-Za-z0-9_-]{1,64}` |
| `contract_price_clause` | string | ✅ | 合同总价条款原文 |
| `contract_price_clause_context` | string/null | ❌ | 合同总价条款上下文文本，可为 `null` 或空串 |
| `sis_contract_price` | float | ✅ | SIS 系统记录的合同金额（单位：元） |

#### 请求示例

```bash
curl -X POST http://10.204.2.103/s-r644699c4b7c/8000/compare_contract_price \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 7d9b2e17-2290d95b9773-2e862b5cee2c" \
  -d '{
    "id": "price-001",
    "contract_price_clause": "合同总价款人民币壹拾贰万元整（￥120000元）",
    "contract_price_clause_context": "第三条 合同总价\n3.1 合同总价款人民币壹拾贰万元整（￥120000元）。",
    "sis_contract_price": 120000
  }'
```

#### 响应

##### 成功响应 (200 OK)

```json
{
  "id": "price-001",
  "contract_price_clause": "合同总价款人民币壹拾贰万元整（￥120000元）",
  "contract_price_clause_context": "第三条 合同总价\n3.1 合同总价款人民币壹拾贰万元整（￥120000元）。",
  "sis_contract_price": 120000.0,
  "contract_price": 120000.0,
  "comparison_result": true
}
```

##### 响应字段说明

| 字段 | 类型 | 描述 |
|------|------|------|
| `id` | string | 原样回带请求 ID |
| `contract_price_clause` | string | 原样回带请求条款文本 |
| `contract_price_clause_context` | string/null | 原样回带请求上下文 |
| `sis_contract_price` | float | 原样回带 SIS 金额 |
| `contract_price` | float/null | LLM 提取出的合同总金额；条款无金额或解析失败时为 `null` |
| `comparison_result` | boolean | 是否一致：`abs(contract_price - sis_contract_price) ≤ 10.0` 时为 `true`；`contract_price` 为 `null` 时一律为 `false` |

##### 错误响应

- **422 Unprocessable Entity**：请求字段缺失/类型错误（如 `id` 含非法字符、`sis_contract_price` 非数值）。
- **504 Gateway Timeout**：LLM 调用超时（受 `REQUEST_TIMEOUT_SEC` 约束，默认 300s）。
- **500 Internal Server Error**：LLM/服务内部异常，返回 `trace_id` 便于排查。

> **注意**：比对阈值 `10.0` 为硬编码常量，位于 `app/utils/contract_price_comparator.py` 中的 `_PRICE_DIFF_TOLERANCE`，**不读取环境变量、不接受配置覆盖**。

---

## 使用场景

### 场景 1: 提取付款信息

```python
import requests

# 百舸平台配置
BASE_URL = "http://10.204.2.103/s-r644699c4b7c/8000"
API_KEY = "7d9b2e17-2290d95b9773-2e862b5cee2c"

paragraphs = [
    {
        "clause": "合同签订后7日内，买方支付合同总价款的30%作为预付款。",
        "clause_context": "第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。",
        "clause_class": ["设备付款条款"]
    },
    {
        "clause": "设备到货验收合格后，买方支付合同总价款的60%。",
        "clause_context": "第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。",
        "clause_class": ["设备付款条款"]
    },
    {
        "clause": "质保期为自竣工验收合格之日起24个月。",
        "clause_context": "第七条 质保期\n质保期为自竣工验收合格之日起24个月，质保金为合同总价的5%。",
        "clause_class": ["质保期条款"]
    }
]

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}"
}

response = requests.post(
    f"{BASE_URL}/extract_payment_info",
    json={
        "id": "task-001",
        "operation_type": "extract",
        "paragraphs": paragraphs
    },
    headers=headers,
    timeout=600
)

result = response.json()
print(f"提取到 {len(result['extraction_result'])} 个节点")

for item in result["extraction_result"]:
    if "payment_type" in item:
        print(f"  - 付款: {item['payment_type']} {item['payment_ratio']}%")
    elif "warranty" in item:
        print(f"  - 质保: {item['warranty']}")
```

### 场景 2: 评估模式 - 与标准答案对比

```python
import requests

# 百舸平台配置
BASE_URL = "http://10.204.2.103/s-r644699c4b7c/8000"
API_KEY = "7d9b2e17-2290d95b9773-2e862b5cee2c"

# 请求头
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}"
}

# 准备标准答案
ground_truth = [
    {"stage": "合同定金", "ratio": "20%", "category": "equipment_payment"},
    {"stage": "公司验收后", "ratio": 0.7, "category": "equipment_payment"},
    {"stage": "质保金1年", "ratio": "10%", "category": "equipment_payment"}
]

# 准备合同条款段落
paragraphs = [
    {
        "clause": "合同签订后3日内，甲方支付合同价款的20%作为定金。",
        "clause_context": "第四条 付款条款\n4.1 合同签订后3日内，甲方支付合同价款的20%作为定金。\n4.2 货物安装调试完成后支付至合同总价款的90%。",
        "clause_class": ["设备付款条款"]
    },
    {
        "clause": "货物安装调试完成后支付至合同总价款的90%。",
        "clause_context": "第四条 付款条款\n4.1 合同签订后3日内，甲方支付合同价款的20%作为定金。\n4.2 货物安装调试完成后支付至合同总价款的90%。",
        "clause_class": ["设备付款条款"]
    }
]

# 提取并评估
response = requests.post(
    f"{BASE_URL}/extract_payment_info",
    json={
        "id": "eval-001",
        "operation_type": "analyze",
        "paragraphs": paragraphs,
        "sis_payment_stages": ground_truth
    },
    headers=headers,
    timeout=600
)

result = response.json()

# 查看评估结果
metrics = result["evaluation_metrics"]
print(f"精确率: {metrics['precision']:.2%}")
print(f"召回率: {metrics['recall']:.2%}")
print(f"F1分数: {metrics['f1_score']:.2%}")

# 分析漏提取
if result["missed_payments"]:
    print("\n漏提取:")
    for item in result["missed_payments"]:
        print(f"  - {item['payment_type']}: {item['payment_ratio']:.0f}%")

# 分析错误提取
if result["false_payments"]:
    print("\n错误提取:")
    for item in result["false_payments"]:
        print(f"  - {item['payment_type']}: {item['payment_ratio']:.0f}%")
```

### 场景 3: 批量处理

```python
import requests
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any

# 百舸平台配置
BASE_URL = "http://10.204.2.103/s-r644699c4b7c/8000"
API_KEY = "7d9b2e17-2290d95b9773-2e862b5cee2c"

def process_single_contract(contract_data: Dict[str, Any]) -> Dict[str, Any]:
    """处理单个合同"""
    paragraphs = contract_data["paragraphs"]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }

    response = requests.post(
        f"{BASE_URL}/extract_payment_info",
        json={
            "id": contract_data["id"],
            "operation_type": "extract",
            "paragraphs": paragraphs
        },
        headers=headers,
        timeout=600
    )

    return {
        "id": contract_data["id"],
        "result": response.json()
    }

# 批量处理
contracts = [
    {"id": "contract-001", "paragraphs": [...]},
    {"id": "contract-002", "paragraphs": [...]},
    {"id": "contract-003", "paragraphs": [...]},
]

with ThreadPoolExecutor(max_workers=5) as executor:
    results = list(executor.map(process_single_contract, contracts))

for result in results:
    print(f"{result['id']}: {len(result['result']['extraction_result'])} 个节点")
```

---

## 调用注意事项

### 1. 操作模式选择

- **`extract` 模式**: 生产环境使用，仅提取信息
- **`analyze` 模式**: 评估/测试场景使用，需要提供标准答案

### 2. ParagraphInput 格式要求

`paragraphs` 中的每个对象即为一个待提取的合同条款段落，格式要求如下：

| 字段 | 说明 |
|------|------|
| `clause` | 原子子条款原文，如"合同签订后7日内支付30%预付款" |
| `clause_context` | 该子条款所在段落的完整上下文，用于语义增强检索 |

> `clause_context` 应包含条款所在段落的完整文本，而非仅截取条款本身，以帮助 LLM 理解付款触发条件和相关联条款。

### 3. 标准答案格式 (analyze 模式)

当使用 `analyze` 模式时：
- `ratio` 和 `stage_amount` 至少填一个
- `ratio` 支持多种输入格式，会自动归一化：`0.3`（小数）、`30`（整数）、`"30%"`（百分比字符串）→ 内部统一为 `0.3`
- `stage_amount` 为字符串时建议带单位，如 `"50000元"`
- `category` 必须是 `"equipment_payment"` 或 `"installation_payment"`

```python
# 正确示例
sis_payment_stages = [
    {
        "stage": "预付款",
        "ratio": "30%",        # 等价于 0.3
        "stage_amount": "30000元",
        "category": "equipment_payment"
    },
    {
        "stage": "质保金1年",
        "ratio": "10%",
        "stage_amount": "50000元",  # 固定金额
        "category": "installation_payment"
    }
]
```

### 4. 性能考虑

- **段落数量**: 建议单次请求不超过 50 个段落
- **超时设置**: 百舸平台建议设置至少 600 秒超时
- **并发请求**: 可以并发调用，但建议控制在 20 个/秒以内

### 5. 错误处理

```python
import requests
from requests.exceptions import RequestException

try:
    # 百舸平台配置
    BASE_URL = "http://10.204.2.103/s-r644699c4b7c/8000"
    API_KEY = "7d9b2e17-2290d95b9773-2e862b5cee2c"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }

    response = requests.post(
        f"{BASE_URL}/extract_payment_info",
        json=payload,
        headers=headers,
        timeout=600
    )
    response.raise_for_status()
    result = response.json()

    # 检查是否有提取结果
    if not result.get("extraction_result"):
        print("警告: 未提取到任何付款信息")

except RequestException as e:
    print(f"请求失败: {e}")
    if hasattr(e, 'response') and e.response is not None:
        error_detail = e.response.json().get("detail", "未知错误")
        print(f"错误详情: {error_detail}")
```

### 6. LLM 内部处理说明（了解即可）

Service 2 在内部将付款条款分为 **设备付款（equipment_payment）** 和 **安装付款（installation_payment）** 两条链路并行处理，并将结果合并输出。对于 `clause_class=["混签付款条款"]` 的条款，系统会先经过 **Stage 1.5 混签归属判定 LLM**，输出 `equipment_payment` / `installation_payment` / `both` 三种结果；仅在判定为 `both` 时才展开为双轨（设备+安装）副本，避免对原本单归属条款做无谓的重复提取。最终输出的 `extraction_result` 中每个节点都标注了明确的 `clause_category` 字段。

此外，LLM 在 Stage 2 可能将单条输入拆分为多个节点（如"定金 + 进度款"拆成两条独立记录），随后经过 **按组并发的去重校验** 与代码侧 `_enforce_unique_payment_type` 兜底，最终确保同一 (`clause_category`, `payment_type`) 组合唯一。上下文合并采用并查集（DSU）基于相似度与前后缀重叠阈值对候选上下文进行聚类合并。

---

## 评估指标说明

### Accuracy (准确率)

```
准确率 = (正确提取数 + 正确未提取数) / 总数
```

衡量整体正确性。

### Precision (精确率)

```
精确率 = 正确提取数 / (正确提取数 + 错误提取数)
```

衡量提取结果的可信度（提取的有多少是对的）。

### Recall (召回率)

```
召回率 = 正确提取数 / (正确提取数 + 漏提取数)
```

衡量覆盖率（应该提取的有多少被找到了）。

### F1 Score (F1分数)

```
F1 = 2 × (精确率 × 召回率) / (精确率 + 召回率)
```

精确率和召回率的调和平均数，综合评估。

---

## 节点映射回退规则

服务在输出 `payment_type` 与 `payment_code` 字段时，按 `clause_category` 查表转换至文档开头的"标准付款节点"表。

- 若 LLM 输出的内部节点未命中映射表，`payment_type` 回退为原名，`payment_code` 为 `null`。
- `correct_payments` / `missed_payments` / `false_payments` 中的 `payment_type` 同样经过映射（GT 来源的名称未命中时保持 GT 原名）。
- 标记为"保留节点"的标准节点（如 Z001 到货前、Z006 质保金2年、Z021 保理发票、Z022 保理应收、Z025 特殊约定付款-质保期）当前内部白名单无对应输出，仅作为标准节点保留在配置中，便于后续扩展。

---

## 版本历史

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| **v1.8.1** | 2026-06 | **Breaking**: `analyze` 模式请求字段 `gt_payment_stages` 重命名为 `sis_payment_stages`；422 错误信息中的 `loc` 同步更新；其余字段语义、归一化规则与响应结构保持不变。客户端需同步替换字段名，旧字段不再兼容。 |
| **v1.8.0** | 2026-06 | 新增 `POST /compare_contract_price` 接口：基于 LLM 从合同总价条款抽取金额（提示词 `CONTRACT_PRICE_EXTRACTION_PROMPT` 位于 `app/config/prompts.py`），与 `sis_contract_price` 做硬编码差值比对（阈值固定 `10.0` 元）。新增 Service 层 `app/utils/contract_price_comparator.py`；DTO `ContractPriceCompareRequest` / `ContractPriceCompareResponse` 定义于 `app/api.py`。 |
| **v1.7.1** | 2026-06 | `analyze` 模式的 `correct_payments` / `missed_payments` / `false_payments` 现统一应用标准节点映射（`payment_type` 输出映射后名称），新增 `payment_code`、`payment_days`、`latest_payment_stage`、`latest_payment_date`、`special_clause_content` 字段（后 4 个为占位，当前返回 `null`），与 `extraction_result` 中 `PaymentItem` 输出字段保持一致。 |
| **v1.7.0** | 2026-06 | 节点输出标准化：`PaymentItem.payment_type` 现统一返回**标准节点名称**（如内部 `"销售定金"` → 输出 `"合同定金"`，`"进场前（首付）"` → `"进场前"`），并新增 `payment_code` 字段携带节点编码（如 `EARNEST`、`Z023`）。映射表来自《节点映射.xlsx》并同步至业务词典 `equipment.payment_type_mapping` / `install.payment_type_mapping`，按 `clause_category` 路由查表，未命中时 `payment_type` 回退原名、`payment_code` 为 `null`。本次仅调整 API 输出层，内部 LangGraph 状态、去重、跨类映射、比对均保持原逻辑。 |
| **v1.6.1** | 2026-06 | `PaymentItem` 新增 4 个占位字段：`payment_days`（付款天数）、`latest_payment_stage`（最迟付款节点）、`latest_payment_date`（最迟付款时间）、`special_clause_content`（特殊条款内容）。当前版本统一返回 `null`，提取逻辑将由后续迭代补充。响应序列化由 `exclude_none=True` 调整为 `exclude_none=False`，以便上述新字段以 `null` 形式显式出现。 |
| **v1.6.0** | 2026-10 | 内部提取链路重构（对外 API 不变）：新增 Stage 1.5 混签归属判定 LLM，仅对判定为 `both` 的混签条款展开双轨；上下文合并改为并查集(DSU)聚类，基于相似度+前后缀重叠；去重校验由批量调用改为按组并发（`verify_single_group_single`），提升 9B 模型稳定性；`PaymentRatioExtractor` / `SummaryExtractor` 单例化复用；引入 LLM 调用信号量与整体请求超时（默认 300s，超时返回 504）；启动 lifespan 中预热 tiktoken / RAG / Milvus / BM25；Stage 3 JSON 解析新增悬挂逗号修复与对象抢救；Docker 镜像替换 torch 为 CPU-only 版本。 |
| **v1.5.1** | 2026-07 | 预过滤关键词移至 `.env` 配置（`CLAUSE_FILTER_KEYWORDS`），无需改代码即可调整；比例提取提示词新增"仅描述支付方式/工具"排除规则；精简 API 文档，删除重复章节 |
| **v1.5.0** | 2026-07 | 修复 `payment_context` 返回值：现在正确返回输入的 `clause_context`（此前错误地复制了 `payment_clause`）；新增硬编码预过滤，含"违约金/罚款/赔偿损失/违约责任"关键词的条款在 LLM 验证前即被排除；优化付款条款验证提示词，精简至约 50 行 |
| **v1.4.0** | 2026-03 | 新增 OpenAI 兼容接口 `/v1/chat/completions`；`comparison_node` 支持按提取顺序排列比对报告；`comparison_helper` 新增三种比对状态（`fully_matched`/`node_matched_data_mismatch`/`node_mismatch`），并按 `equipment_payment`/`installation_payment` 分组比对 |
| **v1.3.1** | 2026-03 | 补充"调用注意事项"第6节：说明混签条款双轨处理及 LLM 内部去重行为，确保用户理解为何每条输入不一定只对应一条输出 |
| **v1.3.0** | 2026-03 | 依据实际测试结果修正响应字段描述：`correct_payments`/`missed_payments`/`false_payments` 字段改为 `payment_type`/`payment_ratio`/`payment_amount`/`source`；`EvaluationMetrics` 精简为基础 4 指标；删除"标准付款节点名称参考"章节 |
| **v1.2.0** | 2026-03 | 服务定位为独立服务，移除所有 Service 1 相关描述；使用场景示例改为直接传入 `paragraphs`，不再调用 Service 1 |
| **v1.1.0** | 2026-03 | **Breaking**: `ParagraphInput` 移除废弃字段 `page_index`/`start_char`/`end_char`/`confidence`/`text`；新增必填字段 `clause`（原子子条款原文）和 `clause_context`（完整段落上下文）；`payment_ratio` 输出格式统一为百分比数值（如 `30.0` 表示 30%） |
| v1.0.0 | 2024-01 | 初始版本，支持 extract 和 analyze 两种模式 |

---

## 技术支持

如有问题，请联系：
- Email: niuzihan@smec.com, wuzongzhen@smec.com
- 项目仓库: [内部 GitLab]