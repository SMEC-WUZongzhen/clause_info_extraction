# 付款条款节点提取服务

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/release/python-3120/)

本项目是一个基于 **LangGraph** 和 **FastAPI** 构建的智能合同分析系统，采用**微服务架构**，能够从合同文档中自动提取付款条款和质保期信息。

## 🏗️ 系统架构

本系统由**两个独立的微服务**组成：

### Service 1: 条款提取服务 (Port: 2024)
- **功能**: 从合同文档中定位并分类付款相关条款段落
- **输入**: BOS路径 / URL / 原始文本
- **输出**: 结构化的段落列表（Paragraphs）

### Service 2: 付款信息提取服务 (Port: 2025)
- **功能**: 从段落中提取付款节点、比例、金额和质保期信息
- **输入**: Service 1 输出的段落列表
- **输出**: 结构化的付款信息 + [可选]评估报告



## 📁 整理后的最终项目结构

```
付款条款节点提取服务/clause_agent/
│
├── service1_clause_extractor/          # Service 1: 条款提取（完整独立）
│   ├── app/                            # 应用代码
│   ├── .env                            ✓ 环境配置
│   ├── main.py                         # 启动入口
│   ├── Dockerfile                      # Docker 配置
│   ├── entrypoint.sh                   # Docker 入口脚本
│   └── pyproject.toml                  # 依赖配置
│
├── service2_payment_extractor/         # Service 2: 付款信息提取（完整独立）
│   ├── app/                            # 应用代码
│   ├── .env                            ✓ 环境配置
│   ├── main.py                         # 启动入口
│   ├── Dockerfile                      # Docker 配置
│   ├── entrypoint.sh                   # Docker 入口脚本
│   └── pyproject.toml                  # 依赖配置
│
├── tests/                              # 测试套件
│   ├── mock/                           ✓ 测试数据集 1
│   ├── mock2/                          ✓ 测试数据集 2
│   ├── __init__.py
│   └── test_services_integration.py    ✓ 新的集成测试
│
├── docs/                               # 文档目录（可选）
│
├── .env.shared                         ✓ 共享配置参考
├── .env示例                             ✓ 配置模板
├── .gitignore                          ✓ Git 忽略配置
├── pyproject.toml                      ✓ 项目整体配置
│
├── README.md                           ✓ 快速开始指南
├── PROJECT_OVERVIEW.md                 ✓ 项目整体说明
├── SERVICE1_API.md                     ✓ Service 1 接口文档
├── SERVICE2_API.md                     ✓ Service 2 接口文档
├── API_INTEGRATION.md                  ✓ API 集成文档
└── FINAL_CLEANUP_SUMMARY.md            ✓ 本整理总结
```

---

## 📋 核心概念

系统采用**两步式调用链路**：

```
Step 1: 调用 Service 1                Step 2: 调用 Service 2
文档 → 条款定位与分类 → Paragraphs → 付款信息提取 → 结构化结果
```

Service 2 支持两种操作模式：
-   `"extract"`: **仅提取**信息，不进行评估比对
-   `"analyze"`: **提取并比对**，与标准答案比对并生成评估指标

---

## 🚀 快速开始

### 完整调用示例

#### Step 1: 调用 Service 1 提取条款段落

```bash
curl -X POST http://localhost:2024/extract_clauses \
  -H "Content-Type: application/json" \
  -d '{
    "id": "task-001",
    "md_text": "## 付款条款\n\n1. 合同签订后支付30%定金\n2. 设备到货验收后支付40%\n3. 安装调试完成后支付25%\n4. 质保金5%\n\n## 质保条款\n\n质保期自验收合格之日起12个月。"
  }'
```

**响应示例**:
```json
{
  "id": "task-001",
  "message": "success",
  "paragraphs": [
    {
      "doc_id": "abc123",
      "page_index": 0,
      "chunk_seq": 1,
      "para_seq": 1,
      "start_char": 0,
      "end_char": 150,
      "text": "1. 合同签订后支付30%定金\n2. 设备到货验收后支付40%\n3. 安装调试完成后支付25%\n4. 质保金5%",
      "clause_class": ["设备付款条款"],
      "confidence": 0.95,
      "metadata": {
        "sub_clauses": [
          {"text": "合同签订后支付30%定金", "type": "equipment_payment"},
          {"text": "设备到货验收后支付40%", "type": "equipment_payment"}
        ]
      }
    },
    {
      "doc_id": "abc123",
      "page_index": 0,
      "chunk_seq": 2,
      "para_seq": 2,
      "start_char": 150,
      "end_char": 180,
      "text": "质保期自验收合格之日起12个月。",
      "clause_class": ["质保条款"],
      "confidence": 0.98,
      "metadata": {}
    }
  ]
}
```

#### Step 2: 调用 Service 2 提取付款信息

```bash
curl -X POST http://localhost:2025/extract_payment_info \
  -H "Content-Type: application/json" \
  -d '{
    "id": "task-001",
    "operation_type": "extract",
    "paragraphs": [
      {
        "doc_id": "contract_XY_2024",
        "chunk_seq": [1],
        "para_seq": [3],
        "clause": "合同签订后7日内，买方支付合同总价款的30%作为预付款。",
        "clause_context": "第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。",
        "clause_class": ["设备付款条款"]
      },
      {
        "doc_id": "contract_XY_2024",
        "chunk_seq": [1],
        "para_seq": [4],
        "clause": "设备到货验收合格后，买方支付合同总价款的60%。",
        "clause_context": "第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。",
        "clause_class": ["设备付款条款"]
      },
      {
        "doc_id": "contract_XY_2024",
        "chunk_seq": [2],
        "para_seq": [8],
        "clause": "质保期为自竣工验收合格之日起24个月。",
        "clause_context": "第七条 质保期\n质保期为自竣工验收合格之日起24个月，质保金为合同总价的5%。",
        "clause_class": ["质保期条款"]
      }
    ]
  }'
```

**响应示例**:
```json
{
  "id": "task-001",
  "message": "success",
  "extraction_result": [
    {
      "clause_category": "equipment_payment",
      "doc_id": "contract_001",
      "chunk_seq": [0],
      "para_seq": [1],
      "payment_clause": "合同签订后7日内，买方支付合同总价款的30%作为预付款。",
      "payment_context": "第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。",
      "payment_type": "预付款",
      "payment_ratio": 30.0,
      "payment_amount": null
    },
    {
      "clause_category": "equipment_payment",
      "doc_id": "contract_001",
      "chunk_seq": [0],
      "para_seq": [2],
      "payment_clause": "设备到货验收合格后，买方支付合同总价款的60%。",
      "payment_context": "第五条 付款方式\n5.1 合同签订后7日内，买方支付合同总价款的30%作为预付款。\n5.2 设备到货验收合格后，支付合同总价款的60%。",
      "payment_type": "到货款",
      "payment_ratio": 60.0,
      "payment_amount": null
    },
    {
      "warranty": "24个月",
      "warranty_clause": "质保期为自竣工验收合格之日起24个月。"
    }
  ]
}
```

---

## 🔧 高级功能

### 调试模式

在 `id` 末尾添加 `_debug` 后缀可保存中间结果：

```bash
# Service 1
curl -X POST http://localhost:2024/extract_clauses \
  -H "Content-Type: application/json" \
  -d '{"id": "debug-task_debug", "md_text": "..."}'

# 中间结果保存在: debug_output/debug-task_debug/
```

### 评估模式（仅 Service 2）

使用 `operation_type: "analyze"` 可与标准答案比对：

```bash
curl -X POST http://localhost:2025/extract_payment_info \
  -H "Content-Type: application/json" \
  -d '{
    "id": "eval-001",
    "operation_type": "analyze",
    "paragraphs": [
      {
        "doc_id": "contract_AB_2024",
        "chunk_seq": [1],
        "para_seq": [2],
        "clause": "合同签订后3日内，甲方支付合同价款的20%作为定金。",
        "clause_context": "第四条 付款条款\n4.1 合同签订后3日内，甲方支付合同价款的20%作为定金。\n4.2 货物安装调试完成后支付至合同总价款的90%。",
        "clause_class": ["设备付款条款"]
      }
    ],
    "gt_payment_stages": [
      {"stage": "定金", "ratio": "20%", "category": "equipment_payment"},
      {"stage": "验收款", "ratio": 0.7, "category": "equipment_payment"},
      {"stage": "质保金", "ratio": "10%", "category": "equipment_payment"}
    ]
  }'
```

**响应会额外包含评估指标**:
```json
{
  "correct_payments": [
    {
      "payment_type": "验收款",
      "payment_ratio": 90.0,
      "payment_amount": null,
      "source": "AI提取"
    }
  ],
  "missed_payments": [
    {
      "payment_type": "销售定金",
      "payment_ratio": 7.0,
      "payment_amount": "5590",
      "source": "SIS系统"
    }
  ],
  "false_payments": [
    {
      "payment_type": "销售定金",
      "payment_ratio": 5.0,
      "payment_amount": "5590",
      "source": "AI提取"
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

---

## 💻 本地开发部署

### 1. 环境准备

-   **Python 3.12+**
-   克隆代码库

### 2. 配置环境变量

在项目根目录创建 `.env` 文件：

```env
# LLM 配置（必需）
LLM_API_BASE=https://your-llm-provider/v1
LLM_API_KEY=sk-your-api-key
LLM_MODEL=your-model-name

# BOS 配置（可选，使用 BOS 路径时需要）
BOS_AK=your-bos-access-key
BOS_SK=your-bos-secret-key
BOS_BUCKET_NAME=your-bucket-name

# Milvus 配置（可选，用于 RAG 检索）
MILVUS_HOST=localhost
MILVUS_PORT=19530
```

### 3. 启动服务

#### 启动 Service 1（条款提取服务）

```bash
cd service1_clause_extractor

# 创建虚拟环境并安装依赖
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .

# 启动服务
python main.py --port 2024 --reload
```

访问 Swagger UI: http://localhost:2024/docs

#### 启动 Service 2（付款信息提取服务）

```bash
cd service2_payment_extractor

# 创建虚拟环境并安装依赖
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .

# 启动服务
python main.py --port 2025 --reload
```

访问 Swagger UI: http://localhost:2025/docs

---

## 🐳 Docker 部署

### 构建镜像

```bash
# Service 1
cd service1_clause_extractor
docker build -t clause-extractor:latest .

# Service 2
cd service2_payment_extractor
docker build -t payment-extractor:latest .
```

### 运行容器

```bash
# Service 1
docker run -d -p 2024:2024 \
  --env-file .env \
  --name clause-extractor \
  clause-extractor:latest

# Service 2
docker run -d -p 2025:2025 \
  --env-file .env \
  --name payment-extractor \
  payment-extractor:latest
```

---

## 📖 Python 客户端示例

### 完整的双服务调用

```python
import requests

# Step 1: 调用 Service 1 提取条款段落
s1_response = requests.post("http://localhost:2024/extract_clauses", json={
    "id": "task-001",
    "md_text": """
    ## 付款条款
    1. 合同签订后支付30%定金
    2. 设备到货验收后支付40%
    3. 安装调试完成后支付25%
    4. 质保金5%

    ## 质保条款
    质保期自验收合格之日起12个月。
    """
})

paragraphs = s1_response.json()["paragraphs"]
print(f"提取到 {len(paragraphs)} 个段落")

# Step 2: 将段落传入 Service 2 提取付款信息
s2_response = requests.post("http://localhost:2025/extract_payment_info", json={
    "id": "task-001",
    "operation_type": "extract",
    "paragraphs": paragraphs  # 直接使用 Service 1 的输出
})

result = s2_response.json()
print(f"提取到 {len(result['extraction_result'])} 个节点")

for item in result["extraction_result"]:
    if "payment_type" in item:
        print(f"  付款: {item['payment_type']} - {item['payment_ratio']}%")
    elif "warranty" in item:
        print(f"  质保: {item['warranty']}")
```

### 评估模式（与标准答案比对）

```python
import requests

# 标准答案（Ground Truth）
ground_truth = [
    {"stage": "定金", "ratio": "30%", "category": "equipment_payment"},
    {"stage": "到货款", "ratio": 40.0, "category": "equipment_payment"},
    {"stage": "质保金", "ratio": "5%", "category": "equipment_payment"}
]

# 准备合同条款段落（每个段落包含 clause 和 clause_context）
paragraphs = [
    {
        "doc_id": "contract_001",
        "chunk_seq": [1],
        "para_seq": [1],
        "clause": "合同签订后支付30%定金",
        "clause_context": "付款条款\n1. 合同签订后支付30%定金。\n2. 设备到货验收后支付40%。",
        "clause_class": ["设备付款条款"]
    },
    {
        "doc_id": "contract_001",
        "chunk_seq": [1],
        "para_seq": [2],
        "clause": "设备到货验收后支付40%",
        "clause_context": "付款条款\n1. 合同签订后支付30%定金。\n2. 设备到货验收后支付40%。",
        "clause_class": ["设备付款条款"]
    }
]

# 提取并评估
s2_response = requests.post("http://localhost:2025/extract_payment_info", json={
    "id": "eval-001",
    "operation_type": "analyze",
    "paragraphs": paragraphs,
    "gt_payment_stages": ground_truth
})

result = s2_response.json()

# 查看评估指标
metrics = result.get("evaluation_metrics", {})
print(f"精确率: {metrics.get('precision', 0):.2%}")
print(f"召回率: {metrics.get('recall', 0):.2%}")
print(f"F1分数: {metrics.get('f1_score', 0):.2%}")

# 分析漏提取和错误提取
if result.get("missed_payments"):
    print("\n漏提取:")
    for item in result["missed_payments"]:
        print(f"  - {item['payment_type']}: {item['payment_ratio']}%")

if result.get("false_payments"):
    print("\n错误提取:")
    for item in result["false_payments"]:
        print(f"  - {item['payment_type']}: {item['payment_ratio']}%")
```

---

## 📚 文档

- **[PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)** - 项目整体说明和架构文档
- **[SERVICE1_API.md](SERVICE1_API.md)** - Service 1 完整接口文档
- **[SERVICE2_API.md](SERVICE2_API.md)** - Service 2 完整接口文档
- **[API_INTEGRATION.md](API_INTEGRATION.md)** - API 集成调用指南

---

## 🧪 测试

```bash
# 运行单元测试
pytest tests/ -v

# 运行完整流程测试
python tests/test_installation_payment_flow.py
```

---

## 🤝 贡献者

- **Zhu Yichen** (zhuyichen@smec.com) - 项目负责人
- **Niu Zihan** (niuzihan@smec.com) - 核心开发
- **Wu Zongzhen** (wuzongzhen@smec.com) - 核心开发

---

## 📞 技术支持

如有问题，请联系项目维护团队或查阅详细文档。