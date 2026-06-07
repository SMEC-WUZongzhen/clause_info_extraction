# Service 2 条款处理流程复杂度评估

> 评估日期：2026-06-02
> 评估范围：12 阶段流水线关键词决策点、Prompt 长度、LLM 调用链路

---

## 1. 关键词决策点全景

### 1.1 按阶段统计

| 阶段 | 名称 | 关键词/正则数 | LLM 调用 | 硬阈值 | 复杂度评级 |
|------|------|--------------|----------|--------|-----------|
| 0 | 预过滤 | 8 (3 clause_class + 4 filter_kw + 1 neg_reject) | 0 | 0 | 🟢 低 |
| 1 | 有效性验证 | 40 (9 预过滤正则 + 29 force_valid_kw + 2 阈值) | N (并发) | 2 (min_len=20, pct_gap=20) | 🟡 中 |
| 1.5 | 混签归类 | 0 | 0-3 | 0 | 🟢 低 |
| 1.6 | 质保期提取 | 0 | 1 | 0 | 🟢 低 |
| 2 | RAG+LLM 提取 | 45 (7 strip_kw + 21 mapping + 17 regex_fallback) | N (并发) | 0 | 🔴 高 |
| 3 | 上下文 DSU 合并 | 0 | 0 | 3 (sim=0.85, overlap=20, loose=0.8) | 🟡 中 |
| 4 | 重编号 | 0 | 0 | 0 | 🟢 低 |
| 5 | 批量复核 | 0 | 2 | 0 | 🟢 低 |
| 6 | ID 回溯 | 0 | 0 | 4 (prefix=30, sim=0.85, len_ratio=0.6/0.7) | 🟡 中 |
| 7 | 未匹配恢复 | 0 | 0 | 1 (sim=0.85) | 🟡 中 |
| 8 | 后处理 | 7 (strip_kw) | 0 | 3 (tolerance=0.5, min=100, ratio=0) | 🟡 中 |

**总计**：~100 个关键词/正则决策点，12 个硬阈值，~2N+6 次 LLM 调用（N=条款数）

### 1.2 关键词来源分布

| 来源 | 数量 | 用途 |
|------|------|------|
| `v1.yaml` aux_fee_keywords | 3 | 辅助费识别（保养费/指导费/调试费） |
| `v1.yaml` synonyms.percent_tokens | 4 | 比例标记识别（% / ％ / 百分之 / 百分） |
| `v1.yaml` synonyms.residual_tokens | 8 | 尾款/余款识别 |
| `v1.yaml` synonyms.unique_total_hints | 15 | 合同总价提取 |
| `v1.yaml` force_valid.node_keywords | 18 | 白名单强制保留 |
| `v1.yaml` force_valid.exclude_keywords | 11 | 白名单排除 |
| `v1.yaml` install.payment_type_whitelist | 12 | 安装节点归类 |
| `v1.yaml` install.cross_mapping | 9 | 安装节点跨类映射 |
| `v1.yaml` equipment.payment_type_whitelist | 13 | 设备节点归类 |
| `v1.yaml` clause_filter_default_keywords | 4 | 预过滤关键词 |
| `v1.yaml` clause_filter_negotiation_reject_keywords | 1 | 协商被拒关键词 |
| `v1.yaml` payment_type_regex_fallback | 17 | 正则兜底匹配 |
| `prompts.py` 内嵌（设备 judgment） | 12 | 设备节点判定规则 |
| `prompts.py` 内嵌（安装 judgment） | 7 | 安装节点判定规则 |
| 代码内硬编码（预过滤正则） | ~5 | 表格/碎片识别 |
| 代码内硬编码（ID 匹配） | 4 | 前缀/相似度/长度比阈值 |

**v1.yaml 覆盖率**：114/135 = 84% 的关键词已单源化

---

## 2. Prompt 长度分析

### 2.1 各 Prompt 尺寸

| Prompt | 字符数 | 估算 Token (中文1.5char/token) | 用途 |
|--------|--------|-------------------------------|------|
| EQUIPMENT_PAYMENT_RATIO_PROMPT | 6,854 | ~4,570 | 单条款设备提取 |
| INSTALL_PAYMENT_RATIO_PROMPT | 6,373 | ~4,249 | 单条款安装提取 |
| PAYMENT_SUMMARY_RATIO_PROMPT | 4,431 | ~2,954 | 设备批量复核 |
| INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT | 5,897 | ~3,931 | 安装批量复核 |
| WARRANTY_SUMMARY | 1,872 | ~1,248 | 质保期提取 |
| RESULT_VERIFICATION_PROMPT | 670 | ~447 | 去重校验 |
| PAYMENT_CLAUSE_VALIDATION_PROMPT | 7,234 | ~4,823 | 条款有效性验证 |
| PAYMENT_CLAUSE_CATEGORY_PROMPT | 4,152 | ~2,768 | 混签归类 |
| RESULT_VERIFICATION_SINGLE_GROUP_PROMPT | 408 | ~272 | 单组去重 |

### 2.2 单次 LLM 调用输入 Token 估算

| 调用类型 | Prompt Token | 变量 Token | 总输入 | 输出 Token |
|----------|-------------|-----------|--------|-----------|
| 条款提取 (设备) | ~4,570 | ~800 (clause+RAG) | ~5,370 | ~500 |
| 条款提取 (安装) | ~4,249 | ~800 | ~5,049 | ~500 |
| 批量复核 (设备) | ~2,954 | ~1,500 (nodes) | ~4,454 | ~1,000 |
| 批量复核 (安装) | ~3,931 | ~1,500 | ~5,431 | ~1,000 |
| 有效性验证 | ~4,823 | ~300 | ~5,123 | ~50 |
| 混签归类 | ~2,768 | ~300 | ~3,068 | ~50 |

### 2.3 Prompt 长度评估

**结论：Prompt 长度适中，未超出 9B 模型上下文窗口（典型 32K token）。**

- 最长单次调用 ~5,500 token（安装批量复核），远低于 32K 限制
- 但 **PAYMENT_CLAUSE_VALIDATION_PROMPT（7,234 字符）是最大的单 prompt**，且每条条款独立调用，10 条款 = 10 次并发
- 设备/安装提取 prompt 有 ~4,800 字符 COMMON 模板共享，结构合理

**潜在风险**：
- RAG 召回示例可能使输入膨胀（极端情况 RAG 返回 10+ 示例，每例 ~200 字符 = +2,000 token）
- 批量复核中所有节点拼接后可能超长（30+ 节点 × 200 字符 = +6,000 token）

### 2.4 Prompt 结构冗余

| 冗余对 | 共享内容 | 差异内容 | 冗余率 |
|--------|---------|---------|--------|
| EQUIPMENT vs INSTALL | COMMON 模板 (~4,800 chars) | 节点列表 + examples + judgment | ~70% 共享 |
| PAYMENT_SUMMARY vs INSTALL_PAYMENT_SUMMARY | 6 步骤框架 | 标准类型 + 判定规则 | ~50% 共享 |

**建议**：当前 COMMON 模板已良好抽象，无需进一步拆分。Summary prompt 的差异主要在业务规则，拆分收益不大。

---

## 3. LLM 调用链路追踪

### 3.1 完整调用链（10 条款合同典型路径）

```
HTTP /extract_payment_info
  │
  ▼
FastAPI (api.py)
  │
  ▼
LangGraph (workflow_graph.py)
  │
  ▼
payment_info_extractor_node (12 阶段)
  │
  ├── 阶段0: 预过滤 (纯代码, 0 LLM)
  │     ├── clause_class 分类 (3 类)
  │     ├── 违约金等关键词过滤 (4 kw)
  │     └── 协商被拒过滤 (1 kw)
  │
  ├── 阶段1: 有效性验证 (N LLM 并发)
  │     ├── 代码预过滤: _is_table_or_non_clause() (5 regex + 4 kw)
  │     ├── LLM: PAYMENT_CLAUSE_VALIDATION_PROMPT × N (并发)
  │     └── force_valid 白名单兜底 (18+11 kw, 2 阈值)
  │
  ├── 阶段1.5: 混签归类 (0-3 LLM)
  │     └── LLM: PAYMENT_CLAUSE_CATEGORY_PROMPT × M (M=混签条款数)
  │
  ├── 阶段1.6: 质保期提取 (1 LLM)
  │     └── LLM: WARRANTY_SUMMARY × 1
  │
  ├── 阶段2: RAG+LLM 提取 (N LLM 并发)
  │     ├── RAG: Milvus + BM25 + bge-reranker → top-k 示例
  │     ├── LLM: EQUIPMENT_PAYMENT_RATIO_PROMPT × N_eq (并发)
  │     ├── LLM: INSTALL_PAYMENT_RATIO_PROMPT × N_inst (并发)
  │     ├── _should_strip_ratio (4+3 kw)
  │     ├── enforce_install_payment_type (12+9 mapping)
  │     └── regex_fallback (17 regex)
  │
  ├── 阶段3: 上下文 DSU 合并 (0 LLM, 3 阈值)
  │     └── _deduplicate_and_merge_contexts (sim>=0.85, overlap>=20)
  │
  ├── 阶段4: 重编号 (0 LLM)
  │
  ├── 阶段5: 批量复核 (2 LLM)
  │     ├── LLM: PAYMENT_SUMMARY_RATIO_PROMPT × 1 (设备)
  │     └── LLM: INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT × 1 (安装)
  │
  ├── 阶段6: ID 回溯 + PaymentInfo 创建 (0 LLM, 4 阈值)
  │     └── 3 级匹配: 子串包含 > 前缀30 > 相似度0.85
  │
  ├── 阶段7: 未匹配恢复 (0 LLM, 1 阈值)
  │
  ├── 阶段8: 后处理 (0 LLM, 3 阈值)
  │     ├── _remove_duplicate_payment_items (sim>=0.8/0.95)
  │     ├── _validate_extraction_results (2 LLM: RESULT_VERIFICATION × 2)
  │     └── _postprocess_final_payment_infos (算术校核)
  │
  └── 返回 State
```

### 3.2 LLM 调用次数统计（10 条款合同）

| 阶段 | LLM 调用 | 并发度 | 模型 |
|------|---------|--------|------|
| 1 有效性验证 | 10 | N (全并发) | 9B |
| 1.5 混签归类 | 0-3 | M (全并发) | 9B |
| 1.6 质保期 | 1 | 1 | 9B |
| 2 条款提取 | 10 | N (全并发) | 9B |
| 5 批量复核 | 2 | 2 (设备+安装) | 9B |
| 8 去重校验 | 0-5 | 按组并发 | 9B |
| 8 结果校验 | 2 | 2 | 9B |
| **合计** | **25-33** | | |

### 3.3 关键路径延迟（估算）

| 阶段 | 延迟 | 说明 |
|------|------|------|
| 0 预过滤 | <10ms | 纯代码 |
| 1 有效性验证 | ~2s | 10 并发 × 9B 推理 ~2s/条 |
| 1.5 混签归类 | ~1s | 0-3 并发 |
| 1.6 质保期 | ~2s | 单次 9B |
| 2 RAG+LLM 提取 | ~5s | RAG ~1s + 10 并发 × 9B ~4s |
| 3-4 合并/重编号 | <50ms | 纯代码 |
| 5 批量复核 | ~4s | 2 并发 × 9B ~4s |
| 6-7 ID 回溯/恢复 | <100ms | 纯代码 |
| 8 后处理 | ~3s | 去重 LLM + 算术 |
| **总延迟** | **~17s** | 典型 10 条款合同 |

---

## 4. 复杂度评估与简化建议

### 4.1 当前复杂度评级：🟡 中等偏高

**优点**：
- 12 阶段划分清晰，每阶段职责单一
- 关键词 84% 已单源化至 v1.yaml
- LLM 调用充分并发化
- 3 层去重确保结果质量

**问题**：
- ~100 个关键词决策点，调试时需追踪多个来源
- 12 个硬阈值（T4-T8）无法灰度调整
- 阶段 1 的代码预过滤 `_is_table_or_non_clause()` 有 5 个正则 + 4 个关键词，逻辑较密
- 阶段 2 的 `enforce_install_payment_type()` 有 12 白名单 + 9 跨映射 + 17 正则兜底，3 层回退
- 阶段 6 的 ID 回溯有 3 级匹配（子串→前缀→相似度），每级有独立阈值

### 4.2 简化建议

| 建议 | 影响 | 风险 | 优先级 |
|------|------|------|--------|
| **S1**: 合并阶段 0 和阶段 1 的关键词过滤为统一"过滤管道" | 减少 2 次遍历 | 低 | P3 |
| **S2**: 将 `_is_table_or_non_clause()` 的 5 正则合并为 1 个复合正则 | 减少正则编译次数 | 中（正则可读性下降） | P4 |
| **S3**: 将 `enforce_install_payment_type()` 的 3 层回退简化为 2 层（删除 regex_fallback，依赖 LLM 归类） | 减少 17 条正则 | 高（9B 模型归类准确率可能下降） | P4 |
| **S4**: 将阶段 6 的 3 级 ID 匹配简化为 2 级（删除前缀匹配，保留子串+相似度） | 减少代码复杂度 | 中（前缀匹配是 O(1) 桶查找，删除后性能下降） | P4 |
| **S5**: 将阶段 8 的 3 层去重合并为 2 层（删除 LLM 去重，依赖代码去重+算术校核） | 减少 2-5 次 LLM 调用 | 高（LLM 去重处理语义重复，代码去重仅处理字面重复） | P4 |

### 4.3 不建议简化的部分

| 部分 | 原因 |
|------|------|
| force_valid 白名单兜底 | LLM 有效性验证的 fail-closed 设计需要硬规则救回 |
| _should_strip_ratio | 辅助费比例反算不可靠是业务事实，必须强制清空 |
| 3 层去重 | 每层处理不同类型的重复（字面/语义/算术），缺一不可 |
| RAG 召回 | 提供同类合同参考，显著提升 LLM 归类准确率 |

---

## 5. 调用逻辑追踪图

```
┌─────────────────────────────────────────────────────────────────┐
│                    payment_info_extractor_node                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─阶段0──┐   ┌─阶段1──────────┐   ┌─阶段1.5──┐   ┌─阶段1.6──┐│
│  │预过滤   │──▶│有效性验证       │──▶│混签归类   │──▶│质保期    ││
│  │(代码)   │   │(代码+LLM×N)    │   │(LLM×M)   │   │(LLM×1)  ││
│  └────────┘   └────────────────┘   └──────────┘   └──────────┘│
│       │                                                        │
│       ▼                                                        │
│  ┌─阶段2──────────────┐   ┌─阶段3──────┐   ┌─阶段4──┐        │
│  │RAG+LLM 提取         │──▶│DSU 合并    │──▶│重编号   │        │
│  │(RAG + LLM×N + kw)  │   │(代码+阈值) │   │(代码)  │        │
│  └─────────────────────┘   └────────────┘   └─────────┘        │
│       │                                                        │
│       ▼                                                        │
│  ┌─阶段5──────┐   ┌─阶段6──────────┐   ┌─阶段7──────┐        │
│  │批量复核     │──▶│ID 回溯+创建    │──▶│未匹配恢复  │        │
│  │(LLM×2)     │   │(代码+阈值)     │   │(代码+阈值) │        │
│  └────────────┘   └────────────────┘   └────────────┘        │
│       │                                                        │
│       ▼                                                        │
│  ┌─阶段8──────────────────────────────────────────────┐        │
│  │后处理: 代码去重(sim) → LLM去重 → 算术校核 → 零节点清理 │        │
│  └─────────────────────────────────────────────────────┘        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 6. 变更记录

| 日期 | 变更 |
|------|------|
| 2026-06-02 | 初版：条款处理流程复杂度评估 |
