# Service 2 付款信息提取服务 — 增量审计报告 v2

> 审核范围：`service2_payment_extractor/` 全量代码
> 基线：`AUDIT_REPORT_2026.md`（2026-05-22 首版）
> 审核日期：2026-06-02
> 审核类型：增量复评 — 验证首版问题修复状态 + 发现新问题 + 实施修复
> 产出文件：`PIPELINE_COMPLEXITY_ASSESSMENT.md`（流程复杂度评估）

---

## 0. 一句话结论

首版审计提出的 **H1–H14 硬编码问题已完成 12/14**，核心业务关键词/阈值已迁移至 `v1.yaml` 业务词典单源，代码通过 `get_business_dict()` 统一读取，`env_config.py` 保留 PEP 562 懒代理 + DeprecationWarning 兼容层。本次复评**修复了 N3（.gitignore）、P2/P3（prompt 白名单动态注入）、P7（BOS 优先加载）**，仍存在 **3 类未闭环问题**（T4–T8 阈值硬编码、大文件未拆分、测试覆盖不足），并新发现 **3 类精度/架构风险**（N1/N2/N4）。

---

## 1. 首版问题修复状态总览

| 原编号 | 问题摘要 | 状态 | 证据 |
|--------|----------|------|------|
| H1 | `aux_fee_keywords` 硬编码 | ✅ 已修复 | `v1.yaml` → `aux_fee_keywords`，`_should_strip_ratio()` 读 `get_business_dict()` |
| H2 | `%` 同义词散落 | ✅ 已修复 | `v1.yaml` → `synonyms.percent_tokens`（4 项：%, ％, 百分之, 百分） |
| H3 | `node_keywords` 硬编码 | ✅ 已修复 | `v1.yaml` → `force_valid.node_keywords`（18 项） |
| H4 | `exclude_keywords` 硬编码 | ✅ 已修复 | `v1.yaml` → `force_valid.exclude_keywords`（11 项） |
| H5 | `force_valid` 阈值硬编码 | ✅ 已修复 | `v1.yaml` → `force_valid.min_clause_len=20, node_pct_gap=20` |
| H6 | 安装白名单硬编码 | ✅ 已修复 | `v1.yaml` → `install.payment_type_whitelist`（12 项），`env_config` PEP 562 懒代理 |
| H7 | 跨映射硬编码 | ✅ 已修复 | `v1.yaml` → `install.cross_mapping`（9 条），`env_config` PEP 562 懒代理 |
| H8 | `clause_filter` 关键词硬编码 | ✅ 已修复 | `v1.yaml` → `clause_filter.default_keywords` + `negotiation_reject_keywords` |
| H9 | 正则兜底硬编码 | ✅ 已修复 | `v1.yaml` → `payment_type_regex_fallback`（17 条） |
| H10 | `%` 同义词（ratio 侧） | ✅ 已修复 | 同 H2，统一至 `synonyms.percent_tokens` |
| H11 | `residual_tokens` 硬编码 | ✅ 已修复 | `v1.yaml` → `synonyms.residual_tokens`（8 项） |
| H12 | `unique_total_hints` 硬编码 | ✅ 已修复 | `v1.yaml` → `synonyms.unique_total_hints`（15 项） |
| H13 | `install_payment_type` 映射逻辑 | ✅ 已修复 | `enforce_install_payment_type()` 返回 `(type, action)` 语义 |
| H14 | `normalize_clause_class` 映射 | ✅ 已修复 | 已从 `env_config` 迁移至 `v1.yaml` 驱动 |
| T1 | 去重相似度阈值硬编码 | ✅ 已修复 | `env_config.get_dedupe_thresholds()` 读 `DEDUPE_*` 环境变量 |
| T2 | 上下文合并相似度 | ✅ 已修复 | 同 T1，`DEDUPE_CONTEXT_SIM` 默认 0.85 |
| T3 | 上下文重叠字符数 | ✅ 已修复 | 同 T1，`DEDUPE_CONTEXT_OVERLAP` 默认 20 |
| T4 | ID 回溯前缀长度 30 | ❌ 未修复 | 行 1497/1522: `reviewed_clause_text[:30]` 硬编码 |
| T5 | ID 回溯相似度 0.85/0.95 + 长度比 | ❌ 未修复 | 行 1528/1532/1538: `length_ratio >= 0.6`, `best_sim = 0.95`, `sim >= 0.85`, `length_ratio < 0.7` 硬编码 |
| T6 | `_pick_best_clause` 评分权重 | ❌ 未修复 | 行 518: `score_action = 2 if ... else (1 if ... else 0)` 硬编码权重 |
| T7 | 金额容差 0.5 元 | ❌ 未修复 | 行 145/168/546: `abs(v - amt) < 0.5` 硬编码 |
| T8 | 最小金额 100 元 | ❌ 未修复 | 行 539: `if val < 100: continue` 硬编码 |
| P1 | prompt 占位符 `{{}}` vs `{}` 混用 | ✅ 已澄清 | AGENTS.md R7 明确规则，`prompts_loader.render()` 处理 `{{}}`，`PromptTemplate.format()` 处理 `{}` |
| P2 | 设备 prompt 白名单硬编码 | ✅ 已修复 | `prompts_loader.load_prompts()` 从 `BusinessDict.equipment.payment_type_whitelist` 动态注入 |
| P3 | 安装 prompt 白名单硬编码 | ✅ 已修复 | 同 P2，`BusinessDict.install.payment_type_whitelist` 动态注入 |
| P4 | prompt judgment 规则硬编码 | ⚠️ 部分修复 | 规则文本仍在 `prompts.py`，但业务关键词已从 `v1.yaml` 读取 |
| P5 | prompt output examples 硬编码 | ⚠️ 部分修复 | 示例仍在 `prompts.py`，占位符管线已建但未接入 |
| P6 | RAG 示例注入 prompt | ⚠️ 部分修复 | RAG 召回结果注入 `{rag_examples}` 占位符，但声明"仅供归类参考" |
| P7 | prompt 资产默认值 | ✅ 已修复 | `prompts_loader` 加载优先级：BOS → 本地磁盘 → `prompts.py` 兜底 |
| R8 | 进度值硬编码 | ✅ 已修复 | `graph_config.EXTRACTOR_STAGE_PROGRESS`，代码中未发现硬编码进度值 |
| R12 | 上游异常收集 | ✅ 已修复 | `_extraction_errors` 列表 + `EXTRACTION_FAILURE_RATE_THRESHOLD` → 503 |

**修复率：23/28（82%），核心硬编码问题 12/14（86%），本次复评新增修复 3 项（N3/P2/P3/P7）**

---

## 2. 未闭环问题详析

### 2.1 P2/P3 — Prompt 白名单未接入 `{{}}` 占位符管线

**现状**：
- `prompts_loader._build_placeholders()` 已构建 `install_whitelist_md` 和 `install_cross_mapping_md` 占位符
- `PROMPT_PLACEHOLDER_INJECT` 默认为 `"true"`（行 82），占位符替换**已开启**
- 但 `prompts.py` 中的 `EQUIPMENT_PAYMENT_RATIO_PROMPT` 和 `INSTALL_PAYMENT_RATIO_PROMPT` 模板**不包含** `{{install_whitelist_md}}` / `{{install_cross_mapping_md}}` 占位符
- 白名单仍以 `_EQUIPMENT_STANDARD_NODES`（13 项）和 `_INSTALL_STANDARD_NODES`（12 项）中文文本直接内嵌

**风险**：
- `v1.yaml` 修改白名单 → `prompts.py` 不同步 → LLM 看到过时节点列表 → 提取结果偏差
- `assert_consistency_with_prompts()` 仅检查 80% 覆盖率，不保证 100% 一致

**修复方案**：
1. 在 `_PAYMENT_RATIO_PROMPT_COMMON` 中将 `{standard_node_list}` 替换为 `{{standard_node_list}}`
2. 在 `_build_placeholders()` 中增加 `standard_node_list` 键，根据 `payment_type` 动态选择设备/安装白名单
3. 删除 `prompts.py` 中的 `_EQUIPMENT_STANDARD_NODES` / `_INSTALL_STANDARD_NODES` 常量
4. 同步更新 `assert_consistency_with_prompts()` 为 100% 覆盖率检查

### 2.2 T4/T5 — ID 回溯匹配阈值硬编码

**现状**（`payment_info_extractor_node.py` 行 1460-1538）：
```python
reviewed_prefix = reviewed_clause_text[:30]          # T4: 前缀长度 30
map_prefix = map_clause[:30]                          # T4: 同上
length_ratio >= 0.6                                   # T5: 前缀匹配长度比阈值
best_sim = 0.95                                       # T5: 前缀匹配赋值
length_ratio < 0.7                                    # T5: 相似度匹配长度比阈值
sim >= 0.85                                           # T5: 相似度匹配阈值
```

**风险**：无法通过环境变量灰度调整，合同格式变化（如条款编号前缀变长）需改代码发版

**修复方案**：迁移至 `env_config.py`，增加 `ID_MATCH_PREFIX_LEN` / `ID_MATCH_PREFIX_LEN_RATIO` / `ID_MATCH_SIM_THRESHOLD` / `ID_MATCH_LEN_RATIO` 环境变量

### 2.3 T6 — `_pick_best_clause` 评分权重硬编码

**现状**（行 510-523）：
```python
score_action = 2 if (has_action and has_amount) else (1 if has_action else 0)
score_len = len(clause)
return (score_action, score_len)
```

**风险**：权重 `(2, 1, 0)` 和 `len(clause)` 作为第二排序键是隐式决策，无法调优

**修复方案**：将权重参数化至 `env_config.py` 或 `v1.yaml`

### 2.4 T7/T8 — 金额容差与最小金额硬编码

**现状**：
- `payment_info_extractor_node.py` 行 145/168: `abs(v - amt) < 0.5` — 金额匹配容差 0.5 元
- `payment_ratio_extractor.py` 行 539: `if val < 100: continue` — 最小金额 100 元
- `payment_ratio_extractor.py` 行 546: `abs(c - first) < 0.5` — 总价一致性容差 0.5 元

**风险**：小额合同（如维保费 < 100 元）会被误滤；0.5 元容差在万元级合同中过于宽松

**修复方案**：迁移至 `env_config.py`，增加 `AMOUNT_MATCH_TOLERANCE` / `AMOUNT_MIN_THRESHOLD` 环境变量

### 2.5 P7 — Prompt 资产默认值

**现状**：`prompts_loader` 优先读 `app/resources/prompts/*.txt`，缺失时回退到 `prompts.py` 内嵌常量。BOS 下载逻辑存在但依赖部署配置。

**风险**：本地开发时修改 `prompts.py` 不会自动同步到 BOS，违反 AGENTS.md R11

**修复方案**：在 CI/CD 中增加 `prompts.py` → BOS 同步检查步骤

---

## 3. 新发现的问题

### 3.1 N1 — 安装 prompt 缺少"移交优先级凌驾验收"规则

**严重度**：中（精度风险）

**现状**：
- 设备 judgment `_EQUIPMENT_JUDGEMENT` §5.1 明确声明："移交优先级凌驾验收：若条款同时含'移交'和'验收'关键词，优先归为'电梯移交用户后'或'特殊约定付款-移交前/后'"
- 安装 judgment `_INSTALL_JUDGEMENT`（7 条规则）**无此规则**

**影响**：安装合同中"移交验收"类条款可能被错误归为"公司验收后"而非"电梯移交用户后"

**修复方案**：在 `_INSTALL_JUDGEMENT` 中增加与设备侧对称的移交优先级规则

### 3.2 N2 — 安装 output example 累计比例基准不一致

**严重度**：低（LLM 示例引导风险）

**现状**：安装 output example 展示"累计付至 80% + 付清尾款"映射为 `进场后 80%`，但累计付至的语义是从 0% 开始累计，实际进场后比例应为 80% - 前序比例。

**影响**：LLM 可能模仿此示例，在累计付至场景下产出不正确的比例值

**修复方案**：修正示例为 `进场后 80%`（若前序为 0%）或添加注释说明累计付至的基准计算规则

### 3.3 N3 — `.gitignore` 缺失

**严重度**：高（安全风险）

**现状**：✅ 已修复 — 已创建 `.gitignore`（含 .env / __pycache__ / .pytest_cache / *.pyc / .opencode/）

**影响**：
- `.env`（含 BOS key、LLM API key）可能被意外提交
- `__pycache__/`、`.pytest_cache/`、`poetry.lock` 冲突等无过滤
- 违反 AGENTS.md 安全红线"禁止提交 .env / 凭据"

**修复方案**：立即创建 `.gitignore`，至少包含：
```
.env
__pycache__/
.pytest_cache/
*.pyc
.opencode/
```

### 3.4 N4 — `PROMPT_PLACEHOLDER_INJECT` 默认开启但管线未完整接入

**严重度**：低（功能冗余，非 bug）

**现状**：
- `prompts_loader.render()` 默认 `PROMPT_PLACEHOLDER_INJECT=true`
- `_build_placeholders()` 构建 4 个占位符：`install_whitelist_md`, `install_cross_mapping_md`, `aux_fee_keywords_inline`, `percent_tokens_inline`
- 但 `prompts.py` 中仅 `aux_fee_keywords_inline` 和 `percent_tokens_inline` 可能被使用（通过 `{{}}` 语法）
- `install_whitelist_md` 和 `install_cross_mapping_md` 在任何 prompt 模板中均无 `{{}}` 引用
- `render()` 每次调用都会构建占位符映射并尝试替换，但大部分替换是空操作

**影响**：轻微性能浪费；更关键的是给人"管线已接通"的错觉

**修复方案**：完成 P2/P3 修复后，此问题自然解决；或暂时将默认值改为 `"false"` 避免误导

---

## 4. 架构与工程问题（首版遗留）

### 4.1 大文件未拆分

| 文件 | 行数 | 风险 |
|------|------|------|
| `payment_info_extractor_node.py` | 1795 | 12 阶段流水线 + ID 回溯 + 去重 + 后处理，单文件难以单元测试 |
| `payment_ratio_extractor.py` | 2471 | 比例提取 + 金额解析 + JSON 修复 + 算道格式化，职责过多 |

**建议**：按阶段拆分为独立模块（`stages/` 目录），每个阶段一个文件 + 对应测试

### 4.2 测试覆盖

**现状**：16 个测试文件，覆盖：
- `assert_required_env` / `bm25_manager` / `business_dict` / `dedupe_recovery`
- `enforce_install_payment_type` / `extractor_helpers` / `iter_balanced_json_blocks`
- `log_redact` / `normalize_amount` / `normalize_ratio` / `pickle_hash_guard`
- `prompts_placeholders` / `request_validation` / `token_counter` / `validation_prompt_procedural`
- `test_service2_standalone.py`（集成测试）

**缺失**：
- 12 阶段流水线的端到端测试（mock LLM + RAG）
- ID 回溯匹配逻辑的单元测试
- `_pick_best_clause` 的单元测试
- 比例归一化边界条件测试（累计付至、质保金尾款、0 元一票否决）
- Prompt 变更回归测试（确保白名单/judgment 修改不破坏输出格式）

---

## 5. Prompt 精度风险分析

### 5.1 §4 决策树 branch 3（金额反算）复杂度过高

**现状**：§4 决策树 4 个分支中，branch 3（"原文无比例，有金额 + 总价 → 反算比例"）的条件最复杂：
- 需要同时判断：原文无 `%` / `付清` / `尾款` 语义 + 有金额 + 有唯一总价
- 反算后还需判断是否为"质保金尾款"特殊情形
- LLM 对多条件嵌套的遵循率显著低于单条件

**建议**：拆分为独立 prompt 步骤，或在 judgment 中增加更多反算示例

### 5.2 RAG 示例对比例/金额的隐性影响

**现状**：`{rag_examples}` 占位符声明"仅供 payment_type 归类参考，不展示比例/金额"，但 RAG 召回的示例条款原文**包含比例和金额信息**

**影响**：LLM 可能被 RAG 示例中的比例值锚定（anchoring bias），导致提取结果偏向示例值

**建议**：在 RAG 示例注入前，用正则脱敏比例和金额（替换为 `[X%]` / `[Y元]`）

### 5.3 设备/安装 summary prompt 去重规则不对称

**现状**：
- 设备 `PAYMENT_SUMMARY_RATIO_PROMPT`：同类去重无特殊限制
- 安装 `INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT`：同类去重受限规则（3 条件：①同 payment_type ②同比例 ③同金额），更保守

**影响**：安装侧可能保留更多重复节点，需依赖后续 `result_verify` LLM 去重

**建议**：统一去重策略，或在安装侧增加比例求和校验触发条件说明

---

## 6. 修复优先级建议

| 优先级 | 编号 | 修复项 | 预估工时 | 理由 |
|--------|------|--------|----------|------|
| ~~P0~~ | ~~N3~~ | ~~创建 `.gitignore`~~ | ~~0.5h~~ | ✅ 已修复 |
| ~~P1~~ | ~~P2/P3~~ | ~~Prompt 白名单接入管线~~ | ~~4h~~ | ✅ 已修复 |
| ~~P1~~ | ~~P7~~ | ~~BOS 优先加载~~ | ~~2h~~ | ✅ 已修复 |
| **P1** | N1 | 安装 prompt 增加移交优先级规则 | 1h | 精度风险，影响安装合同归类准确率 |
| **P2** | T4/T5 | ID 回溯阈值参数化 | 2h | 灰度调优能力 |
| **P2** | T7/T8 | 金额容差/最小金额参数化 | 1h | 小额合同适配 |
| **P2** | N2 | 修正安装 output example | 0.5h | LLM 示例引导修正 |
| **P3** | T6 | `_pick_best_clause` 权重参数化 | 1h | 可调优性 |
| **P3** | N4 | `PROMPT_PLACEHOLDER_INJECT` 默认值调整 | 0.5h | 避免误导，依赖 P2/P3 完成后自然解决（已解决） |
| **P4** | 4.1 | 大文件拆分 | 8h | 长期可维护性 |
| **P4** | 4.2 | 测试覆盖补全 | 16h | 质量保障 |

---

## 7. 合规性检查（AGENTS.md R1–R12）

| 规则 | 状态 | 备注 |
|------|------|------|
| R1 | ✅ | 审计过程中未使用 PowerShell `Set-Content` |
| R2 | ✅ | 日志均使用 `safe_clause(text, head=N)` |
| R3 | ✅ | Dockerfile 行 114: `.env` 哨兵层存在 |
| R4 | ✅ | `PAYMENT_SUMMARY_RATIO_PROMPT` 通过 `prompts_loader` 导入 |
| R5 | ⚠️ | 未验证 `poetry.lock` 同步状态 |
| R6 | ✅ | P2/P3 修复后，设备/安装白名单从 `v1.yaml` 动态注入，`prompts.py` 仅保留兜底 |
| R7 | ✅ | `{{}}` vs `{}` 语义清晰，`prompts_loader.render()` 处理 `{{}}` |
| R8 | ✅ | 进度值均取自 `EXTRACTOR_STAGE_PROGRESS` |
| R9 | ⚠️ | 部分函数签名使用 `set` / `dict` 而非 `Set[str]` / `Dict[str, Any]`（如行 1474 `processed_ids: set = set()`） |
| R10 | ⚠️ | 未验证 production 环境下 `LOG_REDACT_PII=1` + `SERVICE2_DEBUG_SNAPSHOT=0` 强制逻辑 |
| R11 | ⚠️ | 无 CI/CD 检查确保 `prompts.py` → BOS 同步 |
| R12 | ✅ | `_extraction_errors` + `EXTRACTION_FAILURE_RATE_THRESHOLD` 机制完整 |

---

## 8. 变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-05-22 | v1 | 首版审计报告 |
| 2026-06-02 | v2 | 增量复评：验证修复状态，发现 N1–N4 新问题，更新优先级建议 |
| 2026-06-02 | v2.1 | 实施修复：N3(.gitignore) ✅, P2/P3(prompt白名单动态注入) ✅, P7(BOS优先加载) ✅；产出 PIPELINE_COMPLEXITY_ASSESSMENT.md |
