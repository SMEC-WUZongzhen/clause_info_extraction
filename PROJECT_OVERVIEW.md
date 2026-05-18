# 付款条款节点提取服务 - 项目整体说明文档

## 📋 项目概述

本项目是一个基于 **LangGraph** 和 **FastAPI** 构建的智能合同分析系统，主要用于从合同文档中自动提取付款条款和质保期信息。系统采用**微服务架构**，将复杂的文档分析任务拆分为两个独立的服务，实现了高内聚、低耦合的设计。

### 核心价值

- **自动化提取**：替代人工阅读合同，自动定位和提取关键付款信息
- **结构化输出**：将非结构化的合同文本转换为结构化的数据
- **智能分类**：区分设备付款、安装付款、质保条款等不同类型
- **质量评估**：支持与标准答案对比，提供准确率、召回率等评估指标

---

## 🏗️ 系统架构

### 整体架构图

```
┌─────────────────────────────────────────────────────────────┐
│                        调用方系统                            │
│                    (业务系统/评估系统)                        │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   │ ① 发送合同文档
                   ▼
┌─────────────────────────────────────────────────────────────┐
│              Service 1: 条款提取服务                         │
│                   (Port: 2024)                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  功能：文档解析 → 条款定位 → 条款分类                  │  │
│  │  输入：BOS路径 / URL / 原始文本                        │  │
│  │  输出：付款条款段落列表 (Paragraphs)                   │  │
│  └───────────────────────────────────────────────────────┘  │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   │ ② 传递段落列表
                   ▼
┌─────────────────────────────────────────────────────────────┐
│              Service 2: 付款信息提取服务                     │
│                   (Port: 2025)                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  功能：信息提取 → 付款节点识别 → 质保期提取            │  │
│  │  输入：Paragraphs + [可选]标准答案                     │  │
│  │  输出：结构化付款信息 + [可选]比对评估                 │  │
│  └───────────────────────────────────────────────────────┘  │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   │ ③ 返回结构化结果
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                        调用方系统                            │
│              (获取结构化付款信息和质保期)                    │
└─────────────────────────────────────────────────────────────┘
```

### 技术栈

- **框架**：FastAPI + LangGraph
- **语言**：Python 3.12+
- **AI模型**：支持 OpenAI/自定义 LLM API
- **存储**：BOS (Baidu Object Storage) / URL / 文本
- **向量检索**：Milvus + BM25
- **依赖管理**：Poetry / pip

---

## 📦 服务说明

### Service 1: 条款提取服务

**端口**: `2024`

#### 主要功能

1. **文档解析**：支持从 BOS、URL 或直接文本加载合同文档
2. **条款定位**：利用 LLM 和向量检索技术定位付款相关段落
3. **条款分类**：将段落分类为设备付款、安装付款、质保条款等

#### 工作流程

```
文档输入 → 文档解析 → 全局定位 → 细粒度解析 → 聚合输出 → Paragraphs列表
```

#### 核心组件

- `doc_parser.py`: 文档解析器，处理不同来源的文档
- `global_locator_node.py`: 全局定位节点，快速筛选相关段落
- `fine_grained_parser_node.py`: 细粒度解析，精确提取子条款
- `aggregator_node.py`: 聚合节点，整合所有提取结果

---

### Service 2: 付款信息提取服务

**端口**: `2025`

#### 主要功能

1. **付款节点提取**：从段落中提取具体的付款类型、比例、金额
2. **质保期识别**：识别并提取质保期信息
3. **结果比对**（可选）：与标准答案对比，生成评估指标

#### 工作流程

```
Paragraphs → 付款信息提取 → [可选]与标准答案比对 → 结构化结果
```

#### 核心组件

- `payment_info_extractor_node.py`: 付款信息提取节点
- `comparison_node.py`: 比对节点，计算准确率等指标
- `comparison_helper.py`: 比对辅助工具
- `payment_ratio_extractor.py`: 付款比例提取工具

---

## 🔄 数据流示例

### 完整调用流程

```python
# Step 1: 调用 Service 1 提取条款段落
POST http://localhost:2024/extract_clauses
{
  "id": "task-001",
  "file_url": "https://example.com/contract.pdf"
}

# 响应：
{
  "id": "task-001",
  "message": "success",
  "paragraphs": [
    {
      "text": "设备款在验收合格后支付合同总价的30%...",
      "clause_class": ["设备付款条款"],
      ...
    }
  ]
}

# Step 2: 将 paragraphs 传入 Service 2
POST http://localhost:2025/extract_payment_info
{
  "id": "task-001",
  "operation_type": "extract",
  "paragraphs": [ ... ] # 直接使用 Step 1 的输出
}

# 响应：
{
  "id": "task-001",
  "message": "success",
  "extraction_result": [
    {
      "clause_category": "equipment_payment",
      "payment_type": "到货验收款",
      "payment_ratio": 30.0,
      ...
    },
    {
      "warranty": "12个月",
      "warranty_clause": "质保期自验收合格之日起12个月"
    }
  ]
}
```

---

## 📂 项目结构

```
clause_agent/
├── service1_clause_extractor/          # 服务1：条款提取
│   ├── app/
│   │   ├── agents/                     # 工作流节点
│   │   ├── config/                     # 配置和提示词
│   │   ├── core/                       # 核心组件（向量库）
│   │   ├── graphs/                     # LangGraph 工作流定义
│   │   ├── states/                     # 状态定义
│   │   ├── utils/                      # 工具函数
│   │   └── api.py                      # FastAPI 接口
│   ├── main.py                         # 服务启动入口
│   ├── Dockerfile                      # Docker 镜像定义
│   └── pyproject.toml                  # 依赖配置
│
├── service2_payment_extractor/         # 服务2：付款信息提取
│   ├── app/
│   │   ├── agents/                     # 工作流节点
│   │   ├── config/                     # 配置和提示词
│   │   ├── graphs/                     # LangGraph 工作流定义
│   │   ├── states/                     # 状态定义
│   │   ├── utils/                      # 工具函数
│   │   └── api.py                      # FastAPI 接口
│   ├── main.py                         # 服务启动入口
│   ├── Dockerfile                      # Docker 镜像定义
│   └── pyproject.toml                  # 依赖配置
│
├── tests/                              # 测试用例
│   ├── mock/                           # 测试数据
│   └── test_*.py                       # 测试脚本
│
├── docs/                               # 文档目录
├── README.md                           # 快速开始指南
├── API_INTEGRATION.md                  # API 接口详细文档
├── PROJECT_OVERVIEW.md                 # 本文档
└── pyproject.toml                      # 项目整体配置
```

---

## 🚀 快速启动

### 环境要求

- Python 3.12+
- 依赖的 LLM API 服务（OpenAI 兼容接口）
- [可选] BOS 存储服务
- [可选] Milvus 向量数据库（用于 RAG 检索）

### 本地开发启动

#### 1. 克隆项目并安装依赖

```bash
cd 付款条款节点提取服务/clause_agent
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r temp_requirements.txt
```

#### 2. 配置环境变量

创建 `.env` 文件：

```env
# LLM 配置
LLM_API_BASE=https://your-llm-provider/v1
LLM_API_KEY=sk-your-api-key
LLM_MODEL=your-model-name

# [可选] BOS 配置
BOS_AK=your-bos-access-key
BOS_SK=your-bos-secret-key
BOS_BUCKET_NAME=your-bucket-name

# [可选] Milvus 配置
MILVUS_HOST=localhost
MILVUS_PORT=19530
```

#### 3. 启动服务

**启动 Service 1（条款提取服务）**

```bash
cd service1_clause_extractor
python main.py --port 2024 --reload
```

**启动 Service 2（付款信息提取服务）**

```bash
cd service2_payment_extractor
python main.py --port 2025 --reload
```

#### 4. 访问 API 文档

- Service 1 Swagger UI: http://localhost:2024/docs
- Service 2 Swagger UI: http://localhost:2025/docs

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

## ?? 测试

### 运行单元测试

```bash
pytest tests/ -v
```

### API 测试

```bash
# 测试 Service 1
python tests/test_api.py

# 测试完整流程
python tests/test_installation_payment_flow.py
```

---

## 📊 性能指标

### Service 1 性能

- **平均响应时间**: 2-5秒（取决于文档长度）
- **支持文档长度**: 最大 100,000 字符
- **并发处理能力**: 10+ 请求/秒

### Service 2 性能

- **平均响应时间**: 1-3秒（取决于段落数量）
- **准确率**: 85-95%（在测试集上）
- **召回率**: 80-90%

---

## 🔧 配置说明

### LLM 配置

支持任何 OpenAI 兼容的 API 接口，包括：
- OpenAI GPT-4/GPT-3.5
- 百度文心一言
- 阿里通义千问
- 自部署的本地模型

### 向量检索配置

- **Milvus**: 用于 RAG 检索相似案例
- **BM25**: 用于关键词匹配

### 调试模式

在请求 `id` 末尾添加 `_debug` 可开启调试模式，会保存中间结果到 `debug_output/<id>/` 目录。

---

## 🤝 贡献者

- **Zhu Yichen** (zhuyichen@smec.com) - 项目负责人
- **Niu Zihan** (niuzihan@smec.com) - 核心开发
- **Wu Zongzhen** (wuzongzhen@smec.com) - 核心开发

---

## 📄 许可证

内部项目，未开源。

---

## 📞 支持

如有问题，请联系项目维护团队或查阅以下文档：
- [README.md](README.md) - 快速开始
- [API_INTEGRATION.md](API_INTEGRATION.md) - 详细 API 文档
- [SERVICE1_API.md](SERVICE1_API.md) - Service 1 接口文档
- [SERVICE2_API.md](SERVICE2_API.md) - Service 2 接口文档