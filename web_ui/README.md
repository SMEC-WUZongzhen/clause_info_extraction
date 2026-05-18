# 合同付款信息提取 - 可视化前端

基于 Flask 的轻量 Web 服务，编排 Service 1（条款分类）+ Service 2（付款信息提取），提供上传 MD 合同文件、选择合同类型、分步展示提取结果的可视化界面。

## 依赖

```bash
pip install -r requirements.txt
```

## 启动

进入 `web_ui/` 目录直接运行：

```bash
cd clause_agent/web_ui
python app.py                       # 默认：Service 2 走百舸远程
python app.py --mode local          # Service 2 走本地 http://localhost:8001
python app.py --mode remote         # Service 2 走百舸远程（显式指定）
python app.py --mode local --port 5050
```

也可通过环境变量：

```powershell
$env:SERVICE2_MODE="local"; python app.py
```

默认监听 `http://localhost:5000`。启动时控制台会打印当前使用的 Service 2 URL，页面顶栏也会显示模式徽标。

## 使用

1. 打开浏览器访问 `http://localhost:5000`
2. 选择一个 `.md` 合同文件
3. 选择合同类型：
   - **安装合同** — 付款条款统一标注为「安装付款条款」
   - **设备合同** — 付款条款统一标注为「设备付款条款」
   - **混签合同** — 付款条款统一标注为「混签付款条款」
4. 点击「开始处理」：
   - Step 1：显示 Service 1 筛选出的付款相关条款 + 上下文
   - Step 2：显示 Service 2 提取的付款节点（阶段/比例/金额）

## 配置

通过环境变量覆盖（可选）：

| 变量 | 默认值 |
|------|--------|
| `SERVICE1_BASE_URL` | `http://10.204.2.21:2251` |
| `SERVICE2_MODE`     | `remote`（可选 `local`） |
| `SERVICE2_LOCAL_BASE_URL`  | `http://localhost:8001` |
| `SERVICE2_REMOTE_BASE_URL` | `http://106.13.172.186/s-r644699c4b7c/8000` |
| `SERVICE2_API_KEY`  | 内置默认 key（仅 remote 使用） |
| `LINES_PER_CHUNK`   | 200 |
| `MAX_CONTEXT_CHARS` | 400 |
| `LLM_TIMEOUT`       | 300 |
| `WEB_PORT`          | 5000 |
