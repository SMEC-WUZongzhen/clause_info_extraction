# 合同付款信息提取可视化前端服务 - 项目变更记录

本文档汇总 `clause_agent/web_ui/` 模块从零搭建至当前状态的全部修改内容，按迭代顺序组织。

---

## 迭代 0：需求与初始规划

### 背景
项目原有两个独立 Python 脚本：

- `clause_agent/service1相关脚本/clause_classify_client.py`：调用远程 Service 1，实现合同按行分块、条款分类、上下文提取
- `clause_agent/service2_payment_extractor/test_service2_standalone.py`：调用 Service 2，以 Service 1 输出为输入，提取付款节点（阶段/比例/金额）

### 需求
提供一个可视化 Web 前端，支持：
- 上传单个合同 MD 文件
- 手动指定合同类型（安装 / 设备 / 混签）
- 分步展示 Step 1（Service 1 筛选条款 + 上下文）与 Step 2（Service 2 付款节点）

### 技术选型（需求澄清阶段确认）
| 选项 | 决定 |
|------|------|
| 技术栈 | Flask + 原生 HTML/JS（轻量单体） |
| 文件格式 | MD |
| 合同类型语义 | `override_all`：覆盖 Service 1 对付款条款的分类，再提交 Service 2 |
| Service 2 模式 | 默认远程（百舸） |

### SDD 产出
- `.comate/specs/contract-payment-web-ui/doc.md`
- `.comate/specs/contract-payment-web-ui/tasks.md`
- `.comate/specs/contract-payment-web-ui/summary.md`

---

## 迭代 1：基础功能搭建

### 目录结构
```
clause_agent/web_ui/
├── __init__.py
├── app.py                 # Flask 入口
├── pipeline.py            # Service1/Service2 编排
├── config.py              # 集中配置
├── requirements.txt
├── README.md
├── templates/
│   └── index.html
└── static/
    ├── app.js
    └── style.css
```

### 核心实现

**config.py**
- Service 1 / Service 2 配置（base_url、api_key、timeout）
- Pipeline 参数（LINES_PER_CHUNK=400、LLM_TIMEOUT=300、MAX_CONTEXT_CHARS=300、MAX_WORKERS=2）
- Web 参数（MAX_CONTENT_LENGTH=20MB、HOST、PORT）
- 合同类型映射：`PAYMENT_CLASS_MAP = {installation, equipment, mixed}`
- 所有字段支持环境变量覆盖

**pipeline.py**
- 使用 `importlib.util.spec_from_file_location` 按绝对路径动态加载含中文目录的 `clause_classify_client.py`，规避 package 命名限制
- `run_step1(md_bytes, task_id)`：临时落盘 → `read_and_split_md` → 并发 `_process_single_chunk`（extract → filter_categories → 并发 get_context）→ 按 chunk_index 排序聚合 → 按 clause 文本去重 → 输出 paragraphs
- `apply_contract_type(paragraphs, contract_type)`：将付款类 `clause_class` 统一覆盖为用户所选类型；质保期条款保留
- `run_step2(paragraphs, task_id)`：POST `/extract_payment_info` 到远程 Service 2，带 Bearer 认证；返回结果附加 `_elapsed_seconds`
- 统一异常 `PipelineError(stage, message, detail)`

**app.py**
- `GET /`：渲染 index.html
- `POST /api/upload`：接收文件 + contract_type，校验后存入内存会话 `SESSIONS[session_id]`，返回 `session_id`（带 TTL 5 分钟自动清理）
- `GET /api/process?session_id=...`：**SSE 流式**推送 `status / step1 / step2 / error / done` 事件
- 采用「先 POST 上传 → 再 GET SSE」的两步交互模式（因 `EventSource` 仅支持 GET）
- `413 errorhandler` 友好提示

**前端**
- 表单：文件上传 + 三单选（安装/设备/混签）
- 进度面板：三阶段（上传/Service1/Service2）
- Step 1 表格：#、分类、条款、上下文（长文本可展开）
- Step 2 表格：付款条款（类别、阶段、比例、金额、原文、上下文）+ 质保期
- `EventSource` 订阅 SSE，按事件类型渲染

### 验证
- Python 语法检查通过
- Flask 路由注册：`GET /`、`POST /api/upload`、`GET /api/process`、`/static/<path>`
- `apply_contract_type` 覆盖逻辑测试通过
- `clause_classify_client.py` 动态加载成功

---

## 迭代 2：启动方式调整（用户手动修改）

用户将 `from . import config, pipeline` 改为 `import config, pipeline`，表示从 `web_ui/` 目录直接运行而非作为包。后续改动全部保留此意图，启动命令变为：

```bash
cd clause_agent/web_ui
python app.py
```

---

## 迭代 3：行数阈值与上下文字符数前端可配置

新增两个前端可选参数，对应 `clause_classify_client.py` 的 `--lines` 和 `--max-chars`。

### 改动

**pipeline.py**
- `run_step1(md_bytes, task_id, lines_per_chunk=None, max_chars=None)`：None 时回退到 config 默认
- `_process_single_chunk` / `_fetch_context_for_items` 接收 `max_chars` 逐层透传

**app.py**
- 新增 `_parse_int_field` 辅助函数与范围校验（lines ∈ [50, 5000]、max_chars ∈ [50, 5000]）
- `/api/upload` 从 form 读取两字段，存入 session，透传给 `run_step1`
- `status` 事件 message 附带所用参数便于核对

**templates/index.html**
- 新增「处理参数」区块，两个 `number` input（留空则用默认）

**static/app.js**
- 表单提交时仅在有值时 append 字段

**static/style.css**
- 新增 `.param-group / .param-item` 两列响应式样式

---

## 迭代 4：前端显示顺序优化

用户反馈 Service 2 结果在下方不便查看，希望结果出来后置于顶部。

### 改动（纯样式）

**static/style.css**
- 使用 flex `order` 属性重排卡片：`#step2-card { order: 3 }` 置于 `#step1-card { order: 4 }` 之前
- 保证上传/进度/错误卡片仍在最顶部

**static/app.js**
- Step 2 事件到达后 `scrollIntoView({ behavior: "smooth" })` 自动滚动到该卡片

---

## 迭代 5：Service 2 模式启动时可选

支持启动时选择 Service 2 走本地（localhost:8001）还是百舸远程。

### 改动

**config.py**
- 拆分 `SERVICE2_LOCAL_CONFIG`（本地无认证）与 `SERVICE2_REMOTE_CONFIG`（远程带 Bearer）
- 新增 `SERVICE2_MODE`（默认 remote，受环境变量 `SERVICE2_MODE` 控制）
- 新增 `set_service2_mode(mode)` 函数：运行时切换配置绑定

**app.py**
- `_parse_cli()` 解析 `--mode`/`--service2-mode`、`--host`、`--port`
- 启动时调用 `config.set_service2_mode()` 应用 CLI 参数
- 新增 `GET /api/runtime-info` 接口
- `index()` 向模板注入 `service2_mode`、`service2_base_url`
- 启动控制台打印当前 Service 1/2 URL、Web 监听地址

**templates/index.html**
- 顶栏新增模式徽标（hover 显示完整 URL）

**static/style.css**
- 新增 `.mode-badge` 样式，本地=黄色、远程=绿色

**README.md**
- 更新启动命令（含 `--mode local/remote`）与环境变量表

### 使用方式

```bash
python app.py                       # 默认远程
python app.py --mode local          # 切到本地
python app.py --mode local --port 5050
$env:SERVICE2_MODE="local"; python app.py
```

**关键设计**：`pipeline.run_step2` 通过属性访问 `config.SERVICE2_CONFIG`，启动时重绑定后 pipeline 无需改动。

---

## 迭代 6：混签合同 Step 2 上下文错位修复

### 问题
混签合同中，Service 2 将付款条款细分为 `installation_payment` / `equipment_payment`。原 `findContextByClause` 采用「首个部分包含即返回」策略，当多个原始 paragraphs 含有共同前缀/格式短语时，命中错误的 paragraph，导致上下文错位。

另外 `test_service2_standalone.py` 已注明：Service 2 返回的 `payment_context` 字段等于 `payment_clause`（已知问题），必须由前端从 Step 1 paragraphs 中精确回溯。

### 修复（static/app.js）

重写 `findContextByClause`，采用优先级级联匹配：
1. **精确相等**优先
2. **正向包含**（paragraph 完整包含 payment_clause）→ 取 clause 长度最短者
3. **反向包含**（payment_clause 完整包含 clause）→ 取 clause 最长者
4. **最长公共子串打分**：重叠比 = LCS / min(len)，要求绝对字符 ≥ 10 且比例 ≥ 60%，取最高分段落

新增 `longestCommonSubstringLen(a, b)` DP 实现（空间优化仅保留前一行）。

---

## 迭代 7：Step 1 排序、耗时统计、运行动画

### 1. Step 1 条款排序
`renderStep1` 中稳定排序：付款条款在前，质保期条款在后，组内保持原始顺序。

### 2. 两服务耗时统计
**static/app.js** 新增 `timers` 计时模块：
- 打点时机：`step1_running` 事件记录起点；`step1`/`step2_running` 事件计算 Step 1 耗时；`step2` 事件计算 Step 2 耗时（若服务端返回 `_elapsed_seconds` 则优先使用）
- 200ms ticker 实时刷新：运行中显示 `3.42s …`，完成后定格 `3.87s`
- 格式化：`fmtSec()` 大于 10 秒保留 1 位小数，否则 2 位

**templates/index.html**
- 进度卡内新增 `<div id="timer-display">` 显示 `Service 1: 3.87s | Service 2: 12.4s`

### 3. 运行动画
**templates/index.html**
- 进度项内包装 `<span class="stage-label">` 和 `<span class="stage-spinner">`

**static/style.css**
- `.stage-spinner`：14px 旋转圈，`@keyframes spin` 0.8s 线性
- `.progress-list li.active`：`@keyframes pulse-border` 1.6s 蓝色呼吸光晕
- 完成标记 `✓` 移至 `.stage-label::after`
- `.timer-display` 等宽字体展示

---

## 迭代 8：前端可指定任务 ID

支持用户在前端页面手动指定 `task_id`（即发送给 Service 1/2 的 `id` / `doc_id` 字段）；未指定时默认使用 `webui-{int(time.time())}`（不再拼接文件名）。

### 改动

**templates/index.html**
- 「处理参数」区块新增第三个输入项：`<input type="text" id="task_id" maxlength="128">`，placeholder 提示"留空则自动生成 webui-{时间戳}"

**static/app.js**
- 表单提交时读取 `#task_id`，非空才 `fd.append("task_id", ...)`

**app.py**
- `POST /api/upload`：读取 `request.form.get("task_id")`，校验长度 ≤ 128，存入 SESSION 并回显
- `GET /api/process`：`task_id = (session.get("task_id") or "").strip() or f"webui-{int(time.time())}"`
- 默认值由原 `webui-{ts}-{filename}` 改为 `webui-{ts}`

### 说明
该 `task_id` 同时作为 Service 1（内部再拼接 `-chunk{idx}`）与 Service 2 (`payload["id"]`) 的任务标识，便于日志追踪与按 ID 关联两端记录。

---

## 迭代 9：Service 1 实时进度推送（总块数 / 已处理 / 百分比）

### 问题
Service 1 分块并发处理过程中，前端仅显示一条静态"Service 1 处理中..."，无法看到实际进度，用户体验不直观，长任务时疑似卡死。

### 改动

**pipeline.py**
- `run_step1` 新增 `on_progress: Optional[Callable[[int, int], None]] = None` 参数
- 启动后立即回调 `(0, total_chunks)`，告知前端总分块数
- `as_completed` 循环中每个 chunk 完成后回调 `(done_count, total_chunks)`

**app.py**
- `/api/process` 改造为 worker 线程模式：
  - 子线程内运行 `pipeline.run_step1(..., on_progress=_on_progress)`
  - `on_progress` 回调向 `queue.Queue` 投递 `("progress", done, total)`
  - 主 SSE 生成器循环消费队列，遇 SENTINEL 退出
  - 异常通过 `result_holder` 传回主线程统一处理
- 新增 SSE 事件：
  ```
  event: step1_progress
  data: {"done": 3, "total": 16, "percent": 18.8}
  ```

**static/app.js**
- 新增 `step1_progress` 事件监听器，更新 `#status-msg`：
  ```
  Service 1 处理中... 已处理 3/16 块（18.8%）
  ```

### 设计要点
- 粒度为 chunk（与 `as_completed` 完成节奏一致），与终端日志中的 `/classify/extract` 调用次数对应
- chunk 内部的 `_fetch_context_for_items` 并发上下文获取暂未单独上报，保持回调签名简单
- 部署兼容：Flask dev server `threaded=True` 已开启；gunicorn 长连接需 `gthread` 或 `gevent` worker

---

## 当前文件清单

```
clause_agent/web_ui/
├── __init__.py
├── app.py                 # Flask 入口，CLI 参数解析，SSE 流式端点
├── pipeline.py            # Service1/2 编排，动态加载中文路径脚本
├── config.py              # 分环境 Service2 配置，运行时切换
├── requirements.txt       # flask, requests
├── README.md              # 启动说明、环境变量表
├── CHANGELOG.md           # 本文件
├── templates/
│   └── index.html         # 顶栏模式徽标 / 上传表单 / 进度 / Step1 / Step2
└── static/
    ├── app.js             # SSE 订阅、Step1/2 渲染、计时器、上下文匹配
    └── style.css          # 卡片布局、spinner、脉冲动画、order 重排

.comate/specs/contract-payment-web-ui/
├── doc.md                 # 需求文档
├── tasks.md               # 任务计划（全部完成）
└── summary.md             # 首轮交付总结
```

---

## 启动与使用速查

```bash
# 安装依赖（一次性）
pip install -r clause_agent/web_ui/requirements.txt

# 启动（选一）
cd clause_agent/web_ui
python app.py                            # 默认：远程 Service 2
python app.py --mode local               # 本地 Service 2
python app.py --mode remote --port 5050  # 显式远程 + 自定义端口

# 浏览器打开
http://localhost:5000
```

### 操作流程
1. 顶栏可见当前 Service 2 模式（本地 / 百舸远程）
2. 选择 `.md` 合同文件
3. 选择合同类型（默认混签）
4. 可选：填写每份行数阈值、上下文最大字符数（留空用默认 400 / 300）
5. 点击「开始处理」：
   - 上传 → Service 1 运行（spinner + 脉冲动画 + 实时耗时）
   - Step 1 结果展示（付款条款在上，质保期在下）
   - Service 2 运行
   - Step 2 结果展示（自动滚动至顶部显示）
6. 每项付款节点下方展示从 Step 1 精确回溯的上下文

### 关键配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SERVICE1_BASE_URL` | `http://10.204.2.21:2251` | Service 1 地址 |
| `SERVICE2_MODE` | `remote` | local / remote |
| `SERVICE2_LOCAL_BASE_URL` | `http://localhost:8001` | 本地 Service 2 |
| `SERVICE2_REMOTE_BASE_URL` | `http://106.13.172.186/s-r644699c4b7c/8000` | 远程 Service 2 |
| `SERVICE2_API_KEY` | 内置默认 key | 仅远程模式使用 |
| `LINES_PER_CHUNK` | 400 | 每份行数默认 |
| `MAX_CONTEXT_CHARS` | 300 | 上下文字符数默认 |
| `LLM_TIMEOUT` | 300 | LLM 超时 |
| `WEB_PORT` | 5000 | Web 监听端口 |

---

## 设计决策备注

1. **未修改既有脚本**：`clause_classify_client.py`、`test_service2_standalone.py` 保持原样，web_ui 通过 importlib 按路径加载，完全解耦。
2. **SSE 而非 WebSocket**：单向进度推送足够，SSE 协议更简单、Flask 原生支持。
3. **两步交互**：受 `EventSource` 仅支持 GET 限制，采用 `POST /api/upload → GET /api/process` 两步模式，内存会话 TTL 5 分钟。
4. **上下文匹配**：因服务端 `payment_context` 存在已知缺陷，改由前端以 LCS 打分回溯，健壮性显著优于首个部分匹配。
5. **运行时配置切换**：`config.SERVICE2_CONFIG` 为模块级绑定，`pipeline` 通过属性访问读取，启动时 `set_service2_mode()` 重绑定即可全局生效。
