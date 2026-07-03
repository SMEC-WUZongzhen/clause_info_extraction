# Service 2 - 付款信息提取微服务

接收合同条款段落列表（`paragraphs`），提取设备/安装付款节点（标准节点名、编码、比例、金额）与质保期信息。
当 `operation_type=analyze` 时还会与基准数据比对并返回准确率指标。

## 核心输出字段

每个付款节点输出以下字段：

| 字段 | 说明 |
|---|---|
| `clause_category` | `equipment_payment` 或 `installation_payment` |
| `payment_type` | 标准节点名称（经映射表转换） |
| `payment_code` | 节点编码（如 `EARNEST`、`COMPACCEPT`） |
| `payment_ratio` | 付款比例（百分比） |
| `payment_amount` | 付款金额 |
| `payment_clause` | 条款原文 |
| `payment_context` | 条款上下文 |
| `payment_days` | 付款天数（占位） |
| `latest_payment_stage` | 最迟付款节点（占位） |
| `latest_payment_date` | 最迟付款时间（占位） |
| `special_clause_content` | 特殊条款内容（占位） |

> 标准节点及 `payment_code` 的完整对照表见 `SERVICE2_API.md` 开头"标准付款节点"章节，映射配置见 `app/resources/business_dict/v1.yaml`。

## 目录结构

- `app/api.py` —— FastAPI 入口（`/extract_payment_info`、`/v1/chat/completions`、`/health`）
- `app/graphs/workflow_graph.py` —— LangGraph 工作流：提取 → 比对（可选）→ 输出 → 聚合
- `app/agents/` —— 节点：`payment_info_extractor` / `comparison_node` / `output_node` / `aggregator`
- `app/config/business_dict.py` —— 业务词典加载（含标准节点映射 `get_payment_type_mapping`）
- `app/resources/business_dict/v1.yaml` —— 业务词典唯一真相（白名单、映射表、同义词、过滤规则）
- `app/utils/` —— RAG 检索、比对、并发、BOS、调试快照、提示词加载
- `app/config/` —— 配置加载（含 `prompts.py` 与 `prompts_loader.py`）
- `app/resources/prompts/` —— 提示词数据文件（缺省时回退到 `prompts.py`）
- `app/tests/` —— 单元测试

## 运行方式

### 本地

```powershell
poetry install
copy .env.example .env  # 按需修改
python main.py --host 0.0.0.0 --port 8000
```

### Docker

```bash
docker build -t payment-extractor:latest .
docker run --rm -p 8000:8000 \
  --env-file .env \
  --user $(id -u):$(id -g) \
  -v $(pwd)/logs:/app/logs \
  payment-extractor:latest
```

镜像默认以 `appuser`（uid/gid=1000）运行，必要时通过构建参数覆盖：
`docker build --build-arg USER_UID=1500 --build-arg USER_GID=1500 .`

## 关键环境变量

完整列表见 `.env.example`。重点：

| 类别 | 变量 | 说明 |
|---|---|---|
| 服务 | `REQUEST_TIMEOUT_SEC` | 单请求端到端超时（秒） |
| 服务 | `MAX_PARAGRAPHS` / `MAX_CLAUSE_LEN` | 入参限额 |
| 服务 | `SERVICE2_DEBUG_SNAPSHOT` | 仅在置 1 时允许 `X-Debug-Snapshot` 触发快照落盘 |
| LLM | `LLM_API_KEY` / `LLM_API_BASE` / `LLM_MODEL` | LLM 接入 |
| LLM | `LLM_MAX_TOKENS` / `LLM_CONCURRENCY` / `LLM_CALL_TIMEOUT_SEC` | 调用上限/并发/单次超时 |
| RAG | `RAG_BM25_MIN_SCORE` / `RAG_VECTOR_MIN_DISTANCE` | 召回阈值 |
| RAG | `EMBED_CONCURRENCY` / `EMBED_MAX_RETRIES` | 嵌入并发与退避重试 |
| 安全 | `BM25_REQUIRE_HASH` | 强制要求 BM25 pickle 的 sha256 sidecar |
| BOS | `BOS_PRESIGN_TTL_SEC` | 预签名 URL 有效期（默认 24h，clamp `[60, 7d]`） |

## API 简介

详见 `SERVICE2_API.md`。

- `POST /extract_payment_info` —— 主入口，`id` 必须匹配 `^[A-Za-z0-9_-]{1,64}$`
- `POST /v1/chat/completions` —— OpenAI 兼容
- `GET /health` —— 健康检查

输出的 `payment_type` 统一为标准节点名（经 `v1.yaml` 映射表转换），`payment_code` 为对应编码。未命中映射时 `payment_type` 回退原名、`payment_code` 为 `null`。

## 测试

```bash
poetry run pytest
```

测试集中在 `app/tests/`，使用 `conftest.py` 屏蔽真实网络/BOS。
