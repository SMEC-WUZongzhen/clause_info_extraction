# Service 2 - 付款信息提取微服务

接收 Service 1 输出的 `paragraphs` 列表，提取设备/安装付款节点（节点名、比例、金额）与质保期信息。
当 `operation_type=analyze` 时还会与基准数据比对并返回准确率指标。

## 目录结构

- `app/api.py` —— FastAPI 入口（`/extract_payment_info`、`/v1/chat/completions`、`/health`）
- `app/graphs/workflow_graph.py` —— LangGraph 工作流：提取 → 比对（可选）→ 输出 → 聚合
- `app/agents/` —— 节点：`payment_info_extractor` / `comparison_node` / `output_node` / `aggregator`
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

## 测试

```bash
poetry run pytest
```

测试集中在 `app/tests/`，使用 `conftest.py` 屏蔽真实网络/BOS。
