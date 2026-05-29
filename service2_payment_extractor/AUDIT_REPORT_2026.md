# Service 2 付款信息提取服务 — 审核报告与优化意见

> 审核范围：`e:\DEMO_CODE\付款条款节点提取服务_smec\service2_payment_extractor\`
> 重点文件：`app/agents/payment_info_extractor_node.py`、`app/config/prompts.py`、`.env`、`app/utils/payment_ratio_extractor.py`、`app/config/env_config.py`
> 审核日期：2026-05-22
> 审核类型：架构与代码质量审计（聚焦硬编码风险）

---

## 0. 一句话结论

服务整体功能完整、链路清晰（FastAPI → LangGraph → 多阶段 LLM + RAG），可观测性、并发控制和兜底设计在工程上**已经达到生产可用门槛**；但**核心业务逻辑高度依赖硬编码的中文关键词、正则、阈值与 prompt 内嵌示例**，在以下三个维度风险显著：
1. **业务规则与代码强耦合**：~40+ 处中文关键词 / 类型枚举散落在 `payment_info_extractor_node.py`、`payment_ratio_extractor.py`、`env_config.py`、`prompts.py` 中，单一规则变更需多文件同步修改。
2. **Prompt 与代码白名单未单源化**：`INSTALL_PAYMENT_TYPE_WHITELIST`、`INSTALL_PAYMENT_TYPE_CROSS_MAPPING` 既写在 `env_config.py`，又以中文文字形式硬编码在 `prompts.py`，存在漂移风险。
3. **关键阈值用 `int/float` 写死在函数体内**：如 `_FORCE_VALID_MIN_LEN=20`、`_FORCE_VALID_NODE_PCT_GAP=20`、相似度三级回退 `0.85/0.95/30%/40%` 等没有进入 `.env`，难以在不发版的情况下灰度调整。

---

## 1. 整体处理流程（梳理）

```
HTTP /extract_payment_info  ──▶ FastAPI (app/api.py)
        │
        │  ParagraphInput 校验 + State 注入
        ▼
LangGraph (app/graphs/workflow_graph.py)
  payment_info_extractor ─┬─▶ comparison_node ─▶ output_node ─▶ aggregator ─▶ END
                          └─▶ output_node (非 analyze 模式)
```

### 1.1 `payment_info_extractor_node` 内部 8 阶段

| 阶段 | 关键动作 | 关键代码 |
|---|---|---|
| 0 段落分流 | 仅保留 4 类条款；`get_clause_filter_keywords()` 预过滤 | `payment_info_extractor_node.py:872-928` |
| 1 表格/碎片预过滤 + LLM 有效性 | `_is_table_or_non_clause` + `validate_payment_clause_single`，fail-closed；白名单兜底 `_should_force_valid` | `:937-1095` |
| 1.5 混签归属 | `classify_mixed_category_single` 三选一；`both` 双轨展开 | `:1097-1163` |
| 1.6 质保期独立抽取 | `extract_summary_warranty` 单独走 `WARRANTY_SUMMARY` | `:1174-1219` |
| 2 RAG+LLM 多节点抽取 | `retrieve_payment_type` → 多数投票 → `extract_payment_info` | `:1230-1255` |
| 3 上下文 DSU 合并 | 子串/前后缀重叠 + 相似度≥0.85 | `:1272-1322` |
| 4 复核前编号 + 快照 | `eq_{i}` / `in_{i}` 重排 | `:1323-1391` |
| 5 `extract_summary` 复核 + `_validate_extraction_results` | 重复节点单组并发 LLM 仲裁 | `:1392-1409` |
| 6 回溯创建 PaymentInfo | 三级回退 ID 匹配（包含 / 30 字符前缀 / 0.85 相似度） | `:1419-1588` |
| 7 未匹配条款恢复 | 相似度三重判定，跳过 0 元 | `:1590-1687` |
| 8 算术校核 + 零节点清理 | `_postprocess_final_payment_infos` | `:1689-1710` |

### 1.2 调用链关键依赖
- LLM：`PaymentRatioExtractor`（条款级抽取）+ `PaymentSummaryRatioExtractor`（6 条 chain：设备复核 / 安装复核 / 质保 / 结果验证 / 条款验证 / 混签分类）
- RAG：`rag_retriever.retrieve_payment_type` = 远程 Embedding (qwen3-embedding-8b) + Milvus + BM25 + bge-reranker-large
- 并发：进程级 `LLM_CONCURRENCY=16` / `EMBED_CONCURRENCY=4` / `RERANK_CONCURRENCY=2` 信号量
- 可观测性：`observability.py` 可选 Prometheus / OTLP

---

## 2. 硬编码问题清单（核心审核重点）

> 下表按"风险等级 × 触达模块"组织。**Sev=H** 表示一旦上游业务（标书话术）变化、容易直接造成线上漏抽/误抽。

### 2.1 业务关键词 / 节点枚举（最高风险）

| # | 位置 | 内容 | 用途 | Sev |
|---|---|---|---|---|
| H1 | `payment_info_extractor_node.py:44-48` | `_AUX_FEE_KEYWORDS=("保养费","免保期保养费","指导费")` | 触发 ratio 反算清空硬规则 | **H** |
| H2 | `payment_info_extractor_node.py:49` | `_EXPLICIT_RATIO_TOKENS=("%","％","百分之","百分")` | 显式比例语义判定 | M |
| H3 | `payment_info_extractor_node.py:74-93` | `_PAYMENT_NODE_KEYWORDS`（18 个节点） | 白名单兜底"覆盖 LLM is_valid=false" | **H** |
| H4 | `payment_info_extractor_node.py:94-106` | `_FORCE_VALID_EXCLUDE_KEYWORDS`（11 个，包含违约金/保函/利息…） | 与 `CLAUSE_FILTER_KEYWORDS` 默认值**部分重复但不完全一致**，存在两套真相 | **H** |
| H5 | `payment_info_extractor_node.py:107-108` | `_FORCE_VALID_MIN_LEN=20`、`_FORCE_VALID_NODE_PCT_GAP=20` | 与字符长度强相关，未 env 化 | M |
| H6 | `env_config.py:39-52` | `INSTALL_PAYMENT_TYPE_WHITELIST`（12 类） | 强制白名单丢弃非法节点；同时**完全复刻在 prompts.py 多处** | **H** |
| H7 | `env_config.py:55-67` | `INSTALL_PAYMENT_TYPE_CROSS_MAPPING`（8 条跨类映射） | 设备节点→安装节点强制改名 | **H** |
| H8 | `env_config.py:288` | `_DEFAULT_CLAUSE_FILTER_KEYWORDS=["违约金","罚款","赔偿损失","质保期保养预留费"]` | 与 H4 重叠且语义错位（`赔偿损失` vs `赔偿`） | M |
| H9 | `payment_ratio_extractor.py:310-339` | 16 条标准付款类型→正则映射表 | JSON 解析失败时降级使用 | M |
| H10 | `payment_ratio_extractor.py:464` | `_RATIO_TEXT_TOKENS=("%","％","百分之")` | 与 H2 不一致（缺"百分"）| L |
| H11 | `payment_ratio_extractor.py:466-475` | `_RESIDUAL_TOKENS`（8 个尾款语义） | 决定能否给 ratio 反算 | M |
| H12 | `payment_ratio_extractor.py:476-492` | `_UNIQUE_TOTAL_HINTS`（15 个总价提示词） | 决定上下文唯一总价能否启用算术校核 | M |
| H13 | `payment_ratio_extractor.py:543-587` | "金额≥100" 最小阈值、"必须带元/万元/¥单位"硬规则 | 唯一总价提取 | L |
| H14 | `_postprocess_final_payment_infos` `:199` | 局部又写一遍 `("%","％","百分之")` | 第三处 % 同义集合 | L |

> **共识缺失**：服务里至少有 **3 套**"百分比同义词"列表（H2、H10、H14），3 套"违约金/保函"排除列表（H4、H8、prompt 内描述），2 套"安装节点白名单"（H6 + prompt 内表格）。

### 2.2 阈值类硬编码（去重 / 相似度 / 长度）

| # | 位置 | 当前值 | 是否可 env 覆盖 | Sev |
|---|---|---|---|---|
| T1 | `payment_info_extractor_node.py:_remove_duplicate_payment_items :406-505` | `item_similarity_loose=0.8`（默认） | ✅ `DEDUPE_ITEM_SIM_LOOSE` | OK |
| T2 | 阶段 2 调用 `_remove_duplicate_payment_items(strict=True)` | `0.95` | ✅ `DEDUPE_ITEM_SIM_STRICT` | OK |
| T3 | DSU 上下文合并 | `0.85` / `overlap=20` 字符 | ✅ `DEDUPE_CONTEXT_SIM` / `DEDUPE_CONTEXT_OVERLAP` | OK |
| T4 | 阶段 6 ID 三级回退 | 前缀 30 字符、长度差 40%、相似度 0.85、长度差 30% | ❌ **未 env 化** | **H** |
| T5 | 阶段 7 恢复模式三重相似度 (`:1641-1666`) | 多个 magic number | ❌ | **H** |
| T6 | `_pick_best_clause :550-563` 评分权重 | 写死 | ❌ | M |
| T7 | `_amount_appears_in_text` 的 `< 0.5` 容差 | 元为单位 | ❌ | L |
| T8 | `_extract_unique_total_amount` 最小 100 元 | 写死 | ❌ | L |
| T9 | `payment_ratio_extractor.py:34-35` | `LLM_REQUEST_TIMEOUT_SEC=60` / `MAX_RETRIES=2` | ✅ env | OK |

### 2.3 Prompt 中的硬编码（`prompts.py`，702 行）

仅以最关键的几类列出（**所有"中文规则块"目前都直接以字面文本嵌入 prompt 字符串**）：

| # | Prompt | 风险点 |
|---|---|---|
| P1 | `_PAYMENT_RATIO_PROMPT_COMMON` | 例子 `"合同生效后支付10%作为首期款；交货前支付60%出货款；验收合格后支付30%验收款"` 写死，**修改示例需重发版** |
| P2 | `INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT` 等 | 12 类安装节点白名单**重复列举**，与 `env_config.INSTALL_PAYMENT_TYPE_WHITELIST` 不同源 |
| P3 | 跨类映射规则 | 与 `INSTALL_PAYMENT_TYPE_CROSS_MAPPING` 重复，且 prompt 中以自然语言描述 → 任一边变更必漂移 |
| P4 | `WARRANTY_SUMMARY` | 质保期判定语义、单位、month/year 同义词散落 prompt 中，没有 schema 校验 |
| P5 | `RESULT_VERIFICATION_PROMPT` | 用例数量与 `extract_payment_info` 解析格式强耦合 |
| P6 | `validate_payment_clause` | 排除关键词在 prompt 内再写一份（与 H4、H8 重复） |
| P7 | `prompts_loader.py` | 已支持远程 prompt 资产，但**默认仍走代码内字符串**，没启用版本灰度 |

### 2.4 与 `.env` 不一致 / 文档缺口

| 现象 | 位置 |
|---|---|
| `.env` 没有暴露 H1–H4、H7、H9–H12、T4–T8 等关键参数 | `.env:174` `CLAUSE_FILTER_KEYWORDS=` 留空，但代码里依然有内置默认 → 运维以为关闭实际未关闭 |
| `LLM_API_KEY` 真实密钥写在 `.env` 中（`.env:54`、`.env:83`） | **泄漏风险**：仓库 git status 显示 `.env` 在工程内，需立即确认 `.gitignore` 与历史提交 |
| `RAG_BM25_MIN_SCORE=20.0` 与代码注释"BM25 分越高越相似"语义统一，但 `RAG_VECTOR_MIN_DISTANCE=0.5` 在 COSINE 下 0.5 是中等阈值，未做单元测试 | `env_config.py:269` |
| `LLM_CONCURRENCY=16` 与 `EMBED_CONCURRENCY=4`、`RERANK_CONCURRENCY=2` 没有联动校验 | 高 LLM 并发遇上低嵌入并发会被 RAG 阻塞 |
| `BM25_REQUIRE_HASH=0` 默认未强制校验完整性 | 生产应=1 |
| `SERVICE2_DEBUG_SNAPSHOT=0` 注释提示"生产必须 0"，但代码里阶段 4 仍调 `DebugHelper.save_snapshot` | 需确认 helper 内层判断 env，否则磁盘泄漏风险 |

### 2.5 OpenAI 兼容入口

`api.py:729-734` `usage.prompt_tokens/completion_tokens` 硬编码为 0；如果上游调用方按 token 计费会被低估。

---

## 3. 设计/工程类问题

### 3.1 模块组织
- `payment_info_extractor_node.py` 单文件 1710 行、`payment_ratio_extractor.py` 单文件 2510 行，违反 SRP。建议按 **预过滤 / RAG / 抽取 / 复核 / 去重 / 后处理** 拆分子模块（每模块 ≤300 行）。
- `_remove_duplicate_payment_items`、`_validate_extraction_results`、`_enforce_unique_payment_type` 三套去重逻辑相互耦合，覆盖路径多但缺少集成测试。

### 3.2 异常 / 一致性
- 阶段 1 LLM 失败 fail-closed（默认丢弃）+ 阶段 1 白名单 fail-open（强制保留）共存，结合阶段 7 恢复 → **同一条款可能在三处出现/消失**，定位困难。建议统一用 `decision_trace` 字段记录决策链路。
- `enforce_install_payment_type` 返回 `("kept" | "mapped" | "dropped")`，但调用方对 `dropped` 直接静默忽略 (`:1516-1529`)，未发指标埋点。
- `_amount_appears_in_text` 容差 `< 0.5` 元，对于带"万元"单位的小数四舍五入存在 4999 元误差边界。

### 3.3 并发与超时
- 阶段 2 `asyncio.gather` 全段落并发，无外层 batch 限速，仅靠 `LLM_CONCURRENCY` 信号量控制；段落数巨大时会瞬时排起长队列，导致 `LLM_CALL_TIMEOUT_SEC=60` 频繁超时。
- 顶层 `REQUEST_TIMEOUT_SEC=300` 与 `LLM_CALL_TIMEOUT_SEC=60` 无联动检查，最坏路径"阶段 1+2+5"串行 LLM ≥ 3×60s = 180s，留给 RAG/Rerank 时间不足。

### 3.4 可观测性
- 已有 `observability.py`，但核心节点中只有 `loguru`；缺少：决策类型计数（force_valid 命中 / strip_ratio 命中 / dedup 删除）、stage 耗时分布。
- `DebugHelper.save_snapshot` 落盘是同步 IO 还是异步 IO 未在节点中体现，建议确认非阻塞。

### 3.5 测试覆盖
- `app/tests/` 仅 6 个单测，且都是工具类（dedupe_recovery / normalize_amount / normalize_ratio / pickle_hash_guard / request_validation / bm25_manager）。
- **无端到端用例**覆盖：辅助费目兜底 / 混签双轨 / 安装白名单丢弃 / 阶段 7 恢复。
- `test_service2_standalone.py` 是黑盒手测脚本，未接 CI。

### 3.6 Dockerfile / 依赖
- `Dockerfile` 已 `M` 修改未提交；建议确认是否启用了 multi-stage build 并清理 poetry cache。
- `poetry.lock` 已 `M`，需确认依赖升级是否经过验证（langchain / langgraph 在小版本间 API 多变）。

---

## 4. 优化建议（按优先级）

### P0（必须做，影响线上正确性 / 安全）
1. **机密剥离**：`.env` 中 `LLM_API_KEY` / `BOS_SECRET_KEY` 立即轮换并改用密钥管理（KMS / Vault / K8s Secret），仓库内只留 `.env.example`，确认 `.gitignore` 与历史提交无泄漏。
2. **生产开关收紧**：`BM25_REQUIRE_HASH=1`、`SERVICE2_DEBUG_SNAPSHOT=0` 在 prod profile 强制生效；`DebugHelper` 内部需基于 env 早返回，避免代码路径残留。
3. **白名单单源化**：将 `INSTALL_PAYMENT_TYPE_WHITELIST` / `INSTALL_PAYMENT_TYPE_CROSS_MAPPING` / `_AUX_FEE_KEYWORDS` / `_PAYMENT_NODE_KEYWORDS` / `_FORCE_VALID_EXCLUDE_KEYWORDS` / `CLAUSE_FILTER_KEYWORDS` 抽取到 **YAML/JSON 业务词典**（如 `app/resources/business_dict/v1.yaml`），prompt 模板用占位符 `{{install_whitelist_md_table}}` 注入，**保证代码与 prompt 同一份真相**。
4. **`usage` token 修正**：`api.py:729-734` 用真实 prompt/response 长度计费（先用估算 token=ceil(len/2.5) 也比 0 好）。

### P1（强烈推荐，提升可维护性）
5. **阈值全 env 化**：把 T4–T8（前缀 30 字符、长度差 40%/30%、最小金额 100、容差 0.5、_FORCE_VALID_MIN_LEN/GAP）写入 `env_config.get_business_thresholds()`，`.env` 文档化默认值。
6. **决策埋点**：在 `_should_force_valid`、`_should_strip_ratio`、`enforce_install_payment_type`、`_remove_duplicate_payment_items`、阶段 7 恢复处加 Prometheus counter（`service2_decision_total{action,stage}`），便于 SRE 灰度回滚。
7. **拆分大文件**：
   - `payment_info_extractor_node.py` → `pre_filter.py / mixed_classifier.py / extractor_runner.py / dedupe.py / validator.py / postprocess.py`
   - `payment_ratio_extractor.py` → `chains/{ratio,summary,warranty,verification,validation,mixed}.py`
8. **prompt 资产化**：默认走 `prompts_loader` 远程资产，按 `LLM_MODEL` 维度版本化（已在 `prompts_loader.py` 留好钩子，需要默认开启 + 灰度切流）。
9. **统一 % / 同义词字典**（消除 H2/H10/H14 三处分裂）：在业务词典中定义 `synonyms.percent_tokens / synonyms.residual_tokens / synonyms.total_hints`。

### P2（中长期）
10. **集成测试矩阵**：CI 内构造典型条款 fixture（≥30 条覆盖辅助费目、混签双轨、安装白名单丢弃、ID 三级回退、阶段 7 恢复）+ 黄金输出对比。
11. **段落并发分批**：阶段 2 用 `asyncio.Semaphore + chunked gather`（chunk_size = LLM_CONCURRENCY × 2），避免大合同瞬时排队拖死超时。
12. **决策追踪字段**：`PaymentInfo` 增加 `decision_trace: List[str]`（如 `["stage1.llm.invalid","stage1.force_valid.kept","stage5.summary.merged"]`），便于业务质检定位。
13. **白名单与 prompt 一致性 lint**：CI 中加脚本，从 `business_dict.yaml` 渲染 prompt 后做字符串比对，确保业务字典是唯一真相。
14. **去重链路精简**：合并 `_remove_duplicate_payment_items` × strict/loose 两种调用 + `_enforce_unique_payment_type` + `_validate_extraction_results` 为单一 pipeline，减少四次 O(N²) 比较。
15. **金额单位统一**：在 `PaymentInfo` 内层始终用 `Decimal`（元），出口再格式化；目前字符串/float 混用导致容差 0.5 元的人工兜底。
16. **配置类型校验**：用 `pydantic-settings` 替换 `_get_int / _get_float`，在启动期把 `RAG_VECTOR_MIN_DISTANCE` / `BM25_MIN_SCORE` 范围校验失败 → 进程拒启。

### P3（可选 / 体验）
17. `_DSU` 实现可改为 `from collections import defaultdict` 简洁版，减少 30 行。
18. `_pick_best_clause` 的评分公式可外置为 YAML 权重，便于 A/B。
19. `graph_config.WORKFLOW_PROGRESS_RANGES` 当前包含 `initializer/doc_parser` 但实际工作流没有这两个节点（`workflow_graph.py` 只有 4 个），需要清理。
20. `comparison_node` 与 `aggregator_node` 都很薄，且 `aggregator` 只取 `processed_items[0]`，对未来批量比对场景埋下隐患。

---

## 5. 硬编码索引（速查表）

| 索引 | File:Line | 一句话 |
|---|---|---|
| H1 | `agents/payment_info_extractor_node.py:44-48` | 辅助费目关键词 |
| H2 | `agents/payment_info_extractor_node.py:49` | 显式比例 token v1 |
| H3 | `agents/payment_info_extractor_node.py:74-93` | 18 个节点白名单 |
| H4 | `agents/payment_info_extractor_node.py:94-106` | 11 个排除关键词 |
| H5 | `agents/payment_info_extractor_node.py:107-108` | force_valid 长度/距离阈值 |
| H6 | `config/env_config.py:39-52` | 12 类安装白名单 |
| H7 | `config/env_config.py:55-67` | 8 条跨类映射 |
| H8 | `config/env_config.py:288` | 默认预过滤 4 词 |
| H9 | `utils/payment_ratio_extractor.py:310-339` | 16 条类型正则 |
| H10 | `utils/payment_ratio_extractor.py:464` | 显式比例 token v2 |
| H11 | `utils/payment_ratio_extractor.py:466-475` | 8 个尾款语义 |
| H12 | `utils/payment_ratio_extractor.py:476-492` | 15 个总价提示 |
| H13 | `utils/payment_ratio_extractor.py:543-587` | 唯一总价 100 元下限 |
| H14 | `agents/payment_info_extractor_node.py:199` | 显式比例 token v3 |
| T4 | `agents/payment_info_extractor_node.py:1437-1488` | ID 三级回退阈值 |
| T5 | `agents/payment_info_extractor_node.py:1641-1666` | 阶段 7 三重相似度 |
| P1-P7 | `config/prompts.py` 全文 | prompt 内嵌中文规则与示例 |

---

## 6. 行动清单（建议按周交付）

| Sprint | 内容 |
|---|---|
| S1 | P0-1 / P0-2 / P0-4：安全收口 |
| S2 | P0-3 + P1-9：业务词典抽取 + prompt 占位符注入 |
| S3 | P1-5 + P1-6：阈值 env 化 + 决策埋点 |
| S4 | P1-7 + P1-8：大文件拆分 + prompt 资产灰度 |
| S5 | P2-10 + P2-11 + P2-12：CI 测试矩阵 + 并发分批 + 决策追踪 |
| S6 | P2-13~16 / P3：长尾治理 |

---

> 报告完。如需对其中任意一条优化建议进入 Spec-Driven 实施流程（生成 doc.md → tasks.md → 实施），请指明编号（如 "实施 P0-3"）。
