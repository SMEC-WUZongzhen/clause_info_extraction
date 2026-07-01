# AGENTS.md — service2_payment_extractor

> 付款信息提取服务（Service 2）。接收 Service 1 输出的段落列表（`paragraphs`），
> 通过 RAG + LLM 多阶段流水线提取设备/安装付款节点、比例、金额、质保期、付款时效，
> 并可选与 SIS 基准数据比对返回准确率指标。

---

## 1. 技术栈

| 层 | 技术 |
|---|---|
| Web 框架 | FastAPI + Uvicorn |
| 工作流编排 | LangGraph (StateGraph) |
| LLM 调用 | LangChain `ChatOpenAI`（OpenAI 兼容接口，json_schema 强制结构化输出） |
| RAG 检索 | BM25 (rank_bm25) + Milvus 向量检索 + BGE-Reranker 重排 |
| Embedding | 远程 API（千帆 qwen3-embedding-0.6b），非本地加载 |
| 对象存储 | 百度 BOS（模型/提示词/.env 远程拉取） |
| 配置 | Pydantic Settings + .env + YAML 业务词典 |
| 日志 | Loguru（文件轮转 + PII 脱敏） |
| 包管理 | Poetry (Python 3.12) |

---

## 2. 目录结构

```
service2_payment_extractor/
├── main.py                    # CLI 入口：解析参数 → 加载 .env → (可选)BOS 拉取 → 启动 Uvicorn
├── app/
│   ├── api.py                 # FastAPI 应用：4 个端点 + lifespan 启动校验/预热
│   ├── agents/                # LangGraph 节点实现
│   │   ├── payment_info_extractor_node.py  # 核心：12 阶段提取流水线（2077 行）
│   │   ├── comparison_node.py              # analyze 模式：与基准数据比对
│   │   ├── output_node.py                  # 打包提取结果到 processed_items
│   │   └── aggregator_node.py              # 聚合 → final_output
│   ├── graphs/
│   │   └── workflow_graph.py  # StateGraph 定义 + 编译（模块级单例 graph）
│   ├── states/
│   │   └── states.py          # TypedDict State + Pydantic 数据模型 + LLM 输出 Schema
│   ├── config/
│   │   ├── config.py          # 全局配置加载 + 单例管理器（Tiktoken/RAG/Milvus/BM25）
│   │   ├── config_models.py   # Pydantic AppSettings schema
│   │   ├── env_config.py      # .env 读取 + fail-closed 校验 + 白名单/过滤词函数
│   │   ├── graph_config.py    # 节点进度百分比配置
│   │   ├── business_dict.py   # 业务词典 YAML loader + prompt 一致性自检
│   │   ├── prompts.py         # 所有提示词常量（878 行，兜底源）
│   │   └── prompts_loader.py  # 提示词加载器：BOS > 磁盘 > 常量 + 占位符渲染
│   ├── utils/
│   │   ├── payment_ratio_extractor.py  # LLM Chain 构建（PaymentRatioExtractor + PaymentSummaryRatioExtractor，1803 行）
│   │   ├── rag_retriever.py            # RAG 检索：BM25 + Milvus + Rerank 融合
│   │   ├── comparison_helper.py        # 比对逻辑 + 模糊匹配 + 指标计算
│   │   ├── contract_price_comparator.py# 合同金额抽取与比对
│   │   ├── concurrency.py              # LLM/Rerank 并发信号量 + 超时守护
│   │   ├── node_decorator.py           # @node_with_progress 进度装饰器
│   │   ├── debug_helper.py             # 调试快照保存
│   │   ├── log_redact.py               # PII 脱敏
│   │   ├── token_counter.py            # Tiktoken token 计数
│   │   ├── bos_helper.py               # 百度 BOS 客户端
│   │   └── observability.py            # Prometheus/OpenTelemetry（可选）
│   ├── resources/
│   │   ├── business_dict/v1.yaml  # 业务词典唯一真相（节点白名单/跨类映射/过滤词/同义词）
│   │   ├── models/                # 本地模型资产（reranker、bm25 pickle）
│   │   └── prompts/               # 运行时提示词覆盖目录（.txt 文件）
│   └── tests/                     # pytest 测试套件
├── Dockerfile
├── pyproject.toml
└── .env.example
```

---

## 3. 工作流图（LangGraph）

```
                        ┌─────────────────────────────────────────┐
                        │         /extract_payment_info            │
                        │  POST → 构建 initial_state → ainvoke     │
                        └─────────────────┬───────────────────────┘
                                          │
                                    ┌─────▼─────┐
                                    │  ENTRY    │
                                    └─────┬─────┘
                                          │
                          ┌───────────────▼────────────────┐
                          │  payment_info_extractor_node   │  ← 核心 12 阶段流水线
                          │  (progress: 70%–95%)           │
                          └───────────────┬────────────────┘
                                          │
                            route_after_extraction()
                          ┌───────────────┴────────────────┐
                    analyze + ground_truth            extract / 无基准
                          │                                │
                ┌─────────▼─────────┐                     │
                │  comparison_node  │  (progress: 95–98%) │
                └─────────┬─────────┘                     │
                          │                               │
                          └──────────┬────────────────────┘
                                     │
                           ┌─────────▼─────────┐
                           │   output_node     │  (progress: 98–100%)
                           └─────────┬─────────┘
                                     │
                           ┌─────────▼─────────┐
                           │  aggregator_node  │  → final_output
                           └─────────┬─────────┘
                                     │
                                    END
```

**路由逻辑**（`workflow_graph.py:15`）：
- `operation_type == "analyze"` 且 `ground_truth_data` 非空 → 走 `comparison_node`
- 其他情况（`extract` 模式或无基准数据）→ 跳过比对，直接到 `output_node`

**图实例**：模块级单例 `graph = create_workflow_graph()`，在 `workflow_graph.py:60` 编译，
由 `api.py` 导入为 `langgraph_app`。

---

## 4. API 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/extract_payment_info` | **主接口**。接收 paragraphs，返回付款节点 + 质保期；analyze 模式额外返回比对指标 |
| POST | `/v1/chat/completions` | OpenAI 兼容接口，从 messages 解析段落走同一工作流 |
| POST | `/compare_contract_price` | LLM 抽取合同总价，与 SIS 金额比对（阈值 ≤ 10 元） |
| GET | `/health` | 健康检查 |

### 主接口请求/响应

**请求** `ExtractPaymentInfoRequest`（`api.py:200`）：
- `id`: 文档 ID（正则 `^[A-Za-z0-9_\-]{1,64}$`）
- `operation_type`: `"extract"` | `"analyze"`
- `paragraphs`: `List[ParagraphInput]`（clause + clause_context + clause_class + metadata）
- `sis_payment_stages`: analyze 模式的基准数据 `List[GroundTruthItem]`

**响应**：
- extract 模式 → `ExtractionResponse`（id + extraction_result）
- analyze 模式 → `AnalysisResponse`（id + extraction_result + correct/missed/false_payments + evaluation_metrics）
- 抽取失败率超阈值 → 503 `EXTRACTION_PARTIAL_FAILURE`

### lifespan 启动流程（`api.py:75`）

1. `setup_logging()` — Loguru 配置（控制台 + 文件轮转 + PII 脱敏）
2. `assert_required_env()` — fail-closed 必填环境变量校验（prod 11 项 / dev 2 项）
3. `get_business_dict()` — 加载业务词典 YAML，失败拒启
4. `assert_consistency_with_prompts()` — prompt ↔ 业务词典一致性自检（prod 严格 / dev warning）
5. `token_counter_warmup()` — 预热 tiktoken 编码器
6. `setup_observability(app)` — 可选 Prometheus/OTLP
7. 后台 `_preheat()` — 异步预热 tiktoken / RAG 模型 / Milvus / BM25(设备+安装) / SummaryExtractor

---

## 5. 核心数据模型（`states.py`）

### State（LangGraph TypedDict）

| 字段 | 类型 | 说明 |
|---|---|---|
| `document_id` | str | 文档标识 |
| `paragraphs` | List[Paragraph] | 核心输入，由 API 注入 |
| `operation_type` | str | `"extract"` / `"analyze"` |
| `payment_infos` | List[PaymentInfo] | 提取结果 |
| `warranty_info` | WarrantyInfo | 质保期信息 |
| `ground_truth_data` | List[Dict] | analyze 模式基准数据 |
| `current_comparison_result` | Dict | 比对结果 |
| `final_output` | Any | 最终输出（aggregator 写入） |
| `llm_config` / `payment_ratio_llm_config` | Dict | 运行时 LLM 配置 |
| `processed_items` | Annotated[List, operator.add] | 累加器 |

### PaymentInfo（核心输出模型）

```python
clause_category: "equipment_payment" | "installation_payment"
payment_clause: str           # 条款原文
payment_context: str          # 条款上下文
payment_type: str             # 付款节点名（如"预付款""质保金"）
payment_ratio: float          # 比例 0-1
payment_amount: str           # 金额（字符串）
payment_days: int             # 付款天数（Stage 7）
latest_payment_stage: str     # 最迟付款节点（Stage 7）
latest_payment_date: int      # 最迟付款日期（Stage 7）
special_clause_content: str   # 特殊条款汇总（Stage 7）
```

### LLM 结构化输出 Schema

| Schema 模型 | 用途 | 对应 Chain |
|---|---|---|
| `PaymentRatioResult` | 单条款初步提取 | Chain 1/2 (equipment/install) |
| `PaymentSummaryResult` | 批量复核 | Chain 3/4 (summary) |
| `WarrantySummaryResult` | 质保期提取 | Chain 5 |
| `VerificationResult` | 双组去重 | Chain 6 |
| `ClauseValidationResult` | 条款有效性验证 | Chain 7 |
| `ClauseCategoryResult` | 混签归属判定 | Chain 8 |
| `SingleGroupVerificationResult` | 单组去重/类型纠正 | Chain 9 |
| `PaymentTimingResult` | 付款时效提取 | Chain 10 |

---

## 6. 核心提取流水线（`payment_info_extractor_node.py`）

`payment_info_extractor_node` 是整个服务的心脏，包含 **12 个阶段**。
以下按执行顺序描述每个阶段的输入、处理逻辑和输出。

### 阶段 0：段落分流 + 预过滤

1. **段落分流**：按 `clause_class` 分为 `payment_paragraphs`（设备/安装/混签）和 `warranty_paragraphs`（质保期）
2. **质保金条款救赎**：检测被误分类为"质保期条款"的质保金付款条款（含"质量保证金/质保金" + 比例/金额），移回 payment 列表
3. **关键词预过滤**：命中 `CLAUSE_FILTER_KEYWORDS`（违约金/罚款/赔偿损失等）的条款直接丢弃
4. **协商被拒过滤**：`clause_context` 命中"不予调整"等否决关键词的条款丢弃
5. **代码预过滤**：表格分隔线/纯碎片行/纯备注行（不含付款关键词）物理剔除

### 阶段 1：条款有效性验证（LLM Chain 7，progress=5）

- 并发调用 `summary_extractor.validate_payment_clause_single(clause)` 逐条验证
- **fail-closed**：仅 LLM 显式返回 `True` 才视为有效
- **兜底 1**：LLM 返回 None → `_should_force_valid_by_text_features`（含 % + 支付动词 → 强制保留）
- **兜底 2**：LLM 返回 False → `_should_force_valid`（节点关键词 ±20 字符内有百分比 → 强制保留）
- 验证异常 → 默认保留该条款

### 阶段 1.5：混签条款归属判定（LLM Chain 8，progress=8）

- 仅对 `clause_class` 含"混签付款条款"的条款触发
- 并发调用 `summary_extractor.classify_mixed_category_single(clause)`
- LLM 返回 `equipment_payment` / `installation_payment` / `both`
- `both` → 展开为设备+安装两条副本
- 安全兜底：任何残留混签条款统一双轨展开

### 阶段 2：质保期单独抽取（LLM Chain 5）

- 调用 `summary_extractor.extract_summary_warranty(warranty_text_items)`
- 返回 `WarrantyInfo`（warranty / effective_conditions / closed_end_conditions）

### 阶段 3：并发 RAG + LLM 抽取（progress=20）

对每个 `payment_paragraph` 并发执行 `_process_single_payment_paragraph`：

1. **RAG 检索**（`rag_retriever.py:retrieve_payment_type`）：
   - BM25 召回（设备/安装各有独立 pickle 索引）+ Milvus 向量召回，并发执行
   - 融合去重 → 可选 BGE-Reranker 重排 → top_k 截断
   - RAG 投票获取参考 payment_type（仅作 LLM 输入参考）
2. **LLM 初步提取**（`PaymentRatioExtractor.extract_payment_info`，Chain 1/2）：
   - 使用 `EQUIPMENT_PAYMENT_RATIO_PROMPT` 或 `INSTALL_PAYMENT_RATIO_PROMPT`
   - json_schema 强制结构化输出 `PaymentRatioResult`（nodes 列表）
   - 支持单条款提取多个付款节点
3. **结果构造**：每个节点生成 dict，含 `sub_clause_index`（"i.s" 格式）、payment_type、ratio、amount
4. **硬规则兜底**：辅助费目（保养费/指导费）+ 无显式比例 → 强制清空 ratio

**失败率裁决**（H2 修复）：
- 每个段落 RAG/LLM 失败上报到 `state["_extraction_errors"]`
- 失败率 > `EXTRACTION_FAILURE_RATE_THRESHOLD`（默认 0.5）→ 标记 `extraction_partial=True`，API 返回 503

### 阶段 4：字符串严格去重

- `_remove_duplicate_payment_items`：基于 SequenceMatcher 相似度（阈值 0.95）去重

### 阶段 5：上下文 DSU 去重合并

- 按设备/安装分组，对 `clause_context` 做并查集合并
- 合并条件：完全相同 / 子串包含 / 前后缀重叠且相似度 ≥ 0.85
- 生成去重后的 `chunk_text_map`（设备 chunks + 安装 chunks，带序号）

### 阶段 6：批量复核（LLM Chain 3/4，progress=60）

- 将初步提取结果按设备/安装分组，分类内连续编号（`eq_0`, `in_0`...）
- 调用 `summary_extractor.extract_summary(...)`：
  - 设备链 `PAYMENT_SUMMARY_RATIO_PROMPT` → `PaymentSummaryResult`
  - 安装链 `INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT` → `PaymentSummaryResult`
  - 批量复审所有条款，输出 final_ratio / final_amount / payment_type
  - 同时返回 thinking_info（LLM 推理过程）

### 阶段 7：结果校验 + 去重（LLM Chain 9，progress=70）

`_validate_extraction_results`：
1. **硬编码比较**：按 `(clause_category, payment_type)` 分组找重复节点
2. **无重复** → 直接返回
3. **有重复** → 每组并发调用 `summary_extractor.verify_single_group_single(payload)`
4. LLM 判定：
   - `select_one`：真正重复，保留一条，移除其余
   - `correct_type`：类型误判，纠正 payment_type，全部保留
5. **代码级强制兜底**：`_enforce_unique_payment_type` 确保同 category 下每个 payment_type 只保留一个

### 阶段 8：复核结果回溯 + 后处理

- **ID 回溯**：LLM 可能改写 id → 用 hash 桶 + 子串/前缀/相似度三级回退匹配
- **白名单兜底**：
  - 安装侧：`enforce_install_payment_type`（白名单校验 + 跨类映射）
  - 设备侧：`enforce_equipment_payment_type`（白名单校验，丢弃安装专属节点）
- **金额幻觉防护**：summary final_amount 必须能在条款原文匹配到数字，否则拒绝覆盖
- **未匹配条款恢复**：LLM 未处理的条款用初步提取结果兜底恢复（跳过已去重/已存在的）
- **算术校核**：原文无 % 且 amount == 上下文唯一总价 → 强制 ratio=1.0
- **零金额/空壳清理**：amount=0 或 ratio+amount 均无值 → 丢弃

### 阶段 9：付款时效提取 + 特殊条款汇总（LLM Chain 10，progress=85）

- **special_clause_content**：按 clause_category 分组，组内 payment_clause 去重保序拼接
- **付款时效**：按 `(clause, context, category)` 唯一键并发调用 `extract_payment_timing_single`
  - 提取 `payment_days` / `latest_payment_stage` / `latest_payment_date`
  - **硬门控**：`latest_payment_stage` / `latest_payment_date` 仅当条款原文含"最迟"二字才保留

### 返回值

```python
{
    "payment_infos": List[PaymentInfo],
    "warranty_info": Optional[WarrantyInfo],
    "thinking_info": Optional[ThinkingInfo],
    "current_step": "payment_info_success",
    "paragraphs": all_paragraphs
}
```

---

## 7. 后续节点

### comparison_node（`comparison_node.py`）

- 仅 analyze 模式执行
- 调用 `ComparisonHelper.compare(extracted_items, ground_truth_items)`
- 模糊匹配 + 近义词词典（alias_rules）+ 指标计算（F1/比例一致率/金额一致率）
- 输出 `correct_payments` / `missed_payments` / `false_payments` / `evaluation_metrics`

### output_node（`output_node.py`）

- 收集 payment_infos / warranty_info / thinking_info / comparison_result
- 打包到 `processed_items`（Annotated 累加器）

### aggregator_node（`aggregator_node.py`）

- 单任务模式：直接返回 `processed_items[0]` 作为 `final_output`

---

## 8. LLM Chain 架构（`payment_ratio_extractor.py`）

### PaymentRatioExtractor（初步提取，Chain 1/2）

- 进程级单例（`get_ratio_extractor()`），按需重初始化（配置变化时）
- 两条链：`equipment_chain` / `install_chain`
- 使用 `EQUIPMENT_PAYMENT_RATIO_PROMPT` / `INSTALL_PAYMENT_RATIO_PROMPT`
- json_schema response_format 强制输出 `PaymentRatioResult`
- 内置三级 JSON 解析降级 + 正则兜底

### PaymentSummaryRatioExtractor（批量复核 + 辅助任务，Chain 3-10）

- 进程级单例（`get_summary_extractor()`）
- 管理多条链：
  - `llm_chain` / `install_llm_chain`：批量复核（Chain 3/4）
  - `warranty_llm_chain`：质保期提取（Chain 5）
  - `result_verification_llm_chain`：双组去重（Chain 6，已弃用主路径）
  - `clause_validation_llm_chain`：条款有效性（Chain 7）
  - `clause_category_llm_chain`：混签归属（Chain 8）
  - `result_verification_single_group_llm_chain`：单组去重（Chain 9）
  - `payment_timing_llm_chain`：付款时效（Chain 10）

### 并发控制（`concurrency.py`）

- `llm_guarded_ainvoke`：LLM 调用信号量 + 超时守护
- `LLM_CALL_TIMEOUT_SEC`（默认 60s）
- `LLM_CONCURRENCY`（默认 16）

---

## 9. 提示词系统

### 加载优先级（`prompts_loader.py`）

```
BOS 远程资产 (PROMPT_BOS_ENABLED=true)
    ↓ 未命中
本地磁盘 app/resources/prompts/*.txt
    ↓ 未命中
prompts.py 内嵌常量（兜底）
```

### 占位符语法（两套并存，勿混用）

| 语法 | 替换时机 | 用途 |
|---|---|---|
| `{{name}}` | 加载期（`prompts_loader.render()`） | 业务词典注入 |
| `{name}` | 调用期（`PromptTemplate.format()`） | 业务变量注入 |

### 业务词典注入（P2/P3）

设备/安装提取 prompt 的标准节点列表不再硬编码，由 `prompts_loader.load_prompts()` 从
`get_business_dict()` 动态注入。`{{install_whitelist_md}}` / `{{install_cross_mapping_md}}`
等占位符在加载期替换为 Markdown 表格。

### 提示词常量清单（`prompts.py`）

| 常量 | 用途 |
|---|---|
| `_PAYMENT_RATIO_PROMPT_COMMON` | 共享模板（节点列表 + 输出格式 + 判定规则） |
| `EQUIPMENT_PAYMENT_RATIO_PROMPT` | 设备付款初步提取（Chain 1） |
| `INSTALL_PAYMENT_RATIO_PROMPT` | 安装付款初步提取（Chain 2） |
| `PAYMENT_SUMMARY_RATIO_PROMPT` | 设备批量复核（Chain 3） |
| `INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT` | 安装批量复核（Chain 4） |
| `WARRANTY_SUMMARY` | 质保期提取（Chain 5） |
| `RESULT_VERIFICATION_PROMPT` | 双组去重（Chain 6） |
| `PAYMENT_CLAUSE_VALIDATION_PROMPT` | 条款有效性验证（Chain 7） |
| `PAYMENT_CLAUSE_CATEGORY_PROMPT` | 混签归属判定（Chain 8） |
| `RESULT_VERIFICATION_SINGLE_GROUP_PROMPT` | 单组去重/类型纠正（Chain 9） |
| `PAYMENT_TIMING_EXTRACTION_PROMPT` | 付款时效提取（Chain 10） |
| `CONTRACT_PRICE_EXTRACTION_PROMPT` | 合同金额抽取（`/compare_contract_price`） |

---

## 10. 业务词典（`resources/business_dict/v1.yaml`）

**所有业务关键词、节点白名单、跨类映射的唯一真相。** 代码侧通过
`get_business_dict()` 加载，prompt 侧通过 `render()` 注入。

### 关键配置块

| 块 | 内容 |
|---|---|
| `synonyms` | percent_tokens（% / ％ / 百分之）、residual_tokens（付清/尾款等）、unique_total_hints（合同总价等） |
| `aux_fee_keywords` | 保养费 / 指导费 → 无显式比例时强制清空 ratio |
| `force_valid` | 白名单兜底：min_clause_len / node_pct_gap / node_keywords(18个) / exclude_keywords(12个) |
| `clause_filter.default_keywords` | 违约金 / 罚款 / 赔偿损失 / 质保期保养预留费 |
| `clause_filter.negotiation_reject_keywords` | 不予调整 |
| `equipment.payment_type_whitelist` | 13 类设备标准节点 |
| `install.payment_type_whitelist` | 12 类安装标准节点 |
| `install.cross_mapping` | 设备/其他节点 → 安装侧节点映射 |
| `equipment/install.payment_type_mapping` | 内部节点 → 标准节点 code + name（API 输出映射） |
| `payment_type_regex_fallback` | JSON 解析三级降级失败时的正则兜底 |

### 一致性自检（`assert_consistency_with_prompts`）

启动期校验 prompt 文本是否覆盖 ≥ 80% 白名单节点。
- production 模式：不通过 → 拒启
- dev 模式：仅 warning

---

## 11. 配置系统

### 配置加载流程（`config.py`）

```
get_default_config_dict()   # 代码级默认值
    ↓ deepmerge
env_config.get_*_config()   # .env 环境变量覆盖
    ↓ Pydantic 验证
AppSettings (config_models.py)
```

全局单例：`APP_CONFIG = load_config()`

### 单例管理器

| 管理器 | 职责 |
|---|---|
| `TiktokenManager` | tiktoken 编码器（cl100k_base） |
| `RAGModelManager` | BGE-Reranker 模型 + tokenizer（本地加载） |
| `MilvusClientManager` | Milvus 客户端连接 |
| `BM25Manager` | BM25 索引（设备/安装独立缓存 + sha256 校验） |
| `LocalModelAssetManager` | 本地模型资产管理（缺失时从 BOS 下载） |

### 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ENVIRONMENT` | development | production 时启用严格校验 |
| `LLM_API_BASE` / `LLM_MODEL` | — | 主 LLM 端点（必填） |
| `PAYMENT_RATIO_LLM_*` | — | 比例提取专用 LLM（空则继承主 LLM） |
| `RAG_MILVUS_URI` | — | Milvus 连接 URI（prod 必填） |
| `RAG_COLLECTION_NAME` / `_INSTALLATION` | — | 设备/安装向量集合名（prod 必填） |
| `RAG_REMOTE_EMBEDDING_URL` / `_KEY` | — | 远程 Embedding API（prod 必填） |
| `USE_BM25` / `USE_VECTOR` / `USE_RERANK` | true/true/false | RAG 开关 |
| `LLM_CONCURRENCY` | 16 | LLM 并发信号量 |
| `REQUEST_TIMEOUT_SEC` | 300 | 单请求超时 |
| `EXTRACTION_FAILURE_RATE_THRESHOLD` | 0.5 | 失败率阈值（超限返回 503） |
| `DEDUPE_*` | 见 .env.example | 去重/合并阈值 |
| `CLAUSE_FILTER_KEYWORDS` | 违约金,罚款,赔偿损失 | 预过滤关键词 |
| `BM25_REQUIRE_HASH` | 0 | pickle sha256 校验（prod 建议 1） |

完整列表见 `.env.example`。

---

## 12. 关键约定与模式

### 日志

- 统一前缀 `f"{_NODE_TAG} ..."`（`[Service2-Extractor]`），便于 ELK 过滤
- `log_redact.safe_clause(text, head=N)` — 条款原文脱敏截断
- `LOG_REDACT_PII=1` — production 强制脱敏

### 错误处理

- **fail-closed**：启动期（env / 业务词典 / prompt 一致性）失败 → 拒启
- **fail-open**：运行期单条款 RAG/LLM 失败 → 跳过该条款，上报错误，继续处理
- **失败率裁决**：超阈值 → `extraction_partial=True` → API 503

### 并发模式

- 段落级并发：`asyncio.gather` 并发处理多个 payment_paragraph
- LLM 调用并发：`llm_guarded_ainvoke` 信号量控制
- Embedding 并发：per-loop httpx client + semaphore

### 进度跟踪

- `@node_with_progress` 装饰器：节点级进度（基于 `WORKFLOW_PROGRESS_RANGES`）
- `emit_running()`：节点内阶段级进度（基于 `EXTRACTOR_STAGE_PROGRESS`）

### 调试快照

- `DebugHelper.save_snapshot(doc_id, step_name, content, is_debug_enabled)`
- 通过 `X-Debug-Snapshot: on` Header + `SERVICE2_DEBUG_SNAPSHOT=1` env 启用
- 快照保存到 `debug_output/` 目录

---

## 13. 测试

```
app/tests/
├── conftest.py
├── test_assert_required_env.py      # 环境变量校验
├── test_bm25_manager.py            # BM25 加载 + sha256
├── test_business_dict.py           # 业务词典 schema + 一致性
├── test_dedupe_recovery.py         # 去重 + 恢复逻辑
├── test_enforce_install_payment_type.py
├── test_extractor_helpers.py       # 提取辅助函数
├── test_iter_balanced_json_blocks.py
├── test_log_redact.py              # PII 脱敏
├── test_normalize_amount.py
├── test_normalize_ratio.py
├── test_pickle_hash_guard.py
├── test_prompts_placeholders.py    # 占位符渲染
├── test_request_validation.py      # API 请求校验
├── test_token_counter.py
└── test_validation_prompt_procedural.py
```

运行：`pytest app/tests/`
集成测试：`python test_service2_standalone.py`

---

## 14. 常见开发任务

### 修改提示词

1. 修改 `prompts.py` 中对应常量（兜底源）
2. 如需运行时覆盖，放 `.txt` 文件到 `app/resources/prompts/`
3. 如需 BOS 远程更新，推送到 `PROMPT_BOS_PREFIX` 路径
4. 确保业务词典一致性自检通过（白名单覆盖率 ≥ 80%）

### 修改节点白名单 / 跨类映射

1. 编辑 `app/resources/business_dict/v1.yaml`
2. 启动期 `assert_consistency_with_prompts` 自动校验
3. 跑 `test_business_dict.py` + `test_service2_standalone.py` 回归

### 新增 LLM Chain

1. 在 `states.py` 定义 Pydantic 输出 Schema
2. 在 `prompts.py` 添加提示词常量
3. 在 `prompts_loader.py` 的 `_PROMPT_NAMES` 注册
4. 在 `payment_ratio_extractor.py` 的 `PaymentSummaryRatioExtractor` 添加链构建 + 调用方法
5. 在 `payment_info_extractor_node.py` 对应阶段调用

### 调整去重/过滤阈值

- 预过滤关键词：`CLAUSE_FILTER_KEYWORDS` env 或 `v1.yaml clause_filter.default_keywords`
- 去重相似度：`DEDUPE_*` env 变量
- 失败率阈值：`EXTRACTION_FAILURE_RATE_THRESHOLD` env

---

## 15. 文件快速索引

| 文件 | 行数 | 核心职责 |
|---|---|---|
| `app/agents/payment_info_extractor_node.py` | 2077 | 12 阶段提取流水线 |
| `app/utils/payment_ratio_extractor.py` | 1803 | 10 条 LLM Chain 构建 + 调用 |
| `app/config/prompts.py` | 878 | 所有提示词常量 |
| `app/api.py` | 1033 | FastAPI 应用 + 4 端点 + lifespan |
| `app/config/config.py` | 653 | 全局配置 + 单例管理器 |
| `app/config/env_config.py` | 406 | .env 读取 + fail-closed + 白名单函数 |
| `app/config/business_dict.py` | 289 | 业务词典 loader + 一致性自检 |
| `app/config/prompts_loader.py` | 288 | 提示词加载 + 占位符渲染 |
| `app/utils/rag_retriever.py` | 291 | BM25 + Milvus + Rerank 融合检索 |
| `app/states/states.py` | 232 | State + 数据模型 + LLM Schema |
| `app/graphs/workflow_graph.py` | 62 | StateGraph 定义 + 编译 |
| `app/agents/comparison_node.py` | 145 | 基准比对 |
| `app/agents/output_node.py` | 56 | 结果打包 |
| `app/agents/aggregator_node.py` | 26 | 结果聚合 |
| `app/resources/business_dict/v1.yaml` | 227 | 业务词典唯一真相 |
