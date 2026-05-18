# Service 1 - 条款提取服务 API 文档

## 服务概述

**服务名称**: 条款定位与分类服务 (Clause Extractor Service)  
**默认端口**: `2024`  
**基础URL**: `http://<host>:2024`

### 主要功能

从合同文档中自动定位并分类付款相关条款段落，为下游的付款信息提取服务提供结构化的段落数据。

---

## API 端点

### 1. 健康检查

**端点**: `GET /health`

**描述**: 检查服务是否正常运行

**请求示例**:
```bash
curl -X GET http://localhost:2024/health
```

**响应示例**:
```json
{
  "status": "ok",
  "service": "service1_clause_extractor"
}
```

---

### 2. 提取条款段落

**端点**: `POST /extract_clauses`

**描述**: 从合同文档中提取并分类付款条款段落

#### 请求参数

##### 请求头
```
Content-Type: application/json
```

##### 请求体 (JSON)

| 字段 | 类型 | 必填 | 描述 |
|------|------|------|------|
| `id` | string | ✅ | 任务唯一标识符 |
| `bos_path` | string | 三选一 | BOS 存储路径，如 `"contract/doc.pdf"` |
| `bos_bucket_name` | string | ❌ | [可选] 显式指定 BOS bucket，不填则使用环境变量中的默认值 |
| `file_url` | string | 三选一 | 可公开访问的文档 URL |
| `md_text` | string | 三选一 | 直接传入的 Markdown/纯文本内容 |
| `json_text` | string | ❌ | [可选] 与主文档同源的 JSON 结构化文本，用于辅助分析 |

**重要约束**:
- `bos_path`、`file_url`、`md_text` 三者必须且只能提供一个
- `id` 可以添加特殊后缀来触发高级功能：
  - `_debug`: 开启调试模式，保存中间结果
  - `_save_output`: 保存最终输出到本地

#### 请求示例

##### 示例 1: 使用 BOS 路径

```bash
curl -X POST http://localhost:2024/extract_clauses \
  -H "Content-Type: application/json" \
  -d '{
    "id": "task-001",
    "bos_path": "contracts/2024/contract_001.pdf",
    "bos_bucket_name": "my-contracts"
  }'
```

##### 示例 2: 使用公开 URL

```bash
curl -X POST http://localhost:2024/extract_clauses \
  -H "Content-Type: application/json" \
  -d '{
    "id": "task-002",
    "file_url": "https://example.com/public/contract.pdf"
  }'
```

##### 示例 3: 直接传入文本

```bash
curl -X POST http://localhost:2024/extract_clauses \
  -H "Content-Type: application/json" \
  -d '{
    "id": "task-003",
    "md_text": "# 合同正文\n\n## 付款条款\n\n设备款在验收合格后支付合同总价的30%作为首付款。剩余70%在安装调试完成并经甲方验收合格后7个工作日内付清。\n\n## 质保条款\n\n质保期自验收合格之日起12个月。"
  }'
```

##### 示例 4: 开启调试模式

```bash
curl -X POST http://localhost:2024/extract_clauses \
  -H "Content-Type: application/json" \
  -d '{
    "id": "task-004_debug",
    "md_text": "合同签订后支付30%定金..."
  }'
```

#### 响应

##### 成功响应 (200 OK)

```json
{
  "id": "task-001",
  "message": "success",
  "paragraphs": [
    {
      "doc_id": "abc123def456",
      "page_index": 2,
      "chunk_seq": 5,
      "para_seq": 1,
      "start_char": 1200,
      "end_char": 1580,
      "text": "设备款在验收合格后支付合同总价的30%作为首付款。剩余70%在安装调试完成并经甲方验收合格后7个工作日内付清。",
      "clause_class": ["设备付款条款"],
      "confidence": 0.95,
      "metadata": {
        "sub_clauses": [
          {
            "text": "验收合格后支付合同总价的30%作为首付款",
            "type": "equipment_payment"
          },
          {
            "text": "剩余70%在安装调试完成并经甲方验收合格后7个工作日内付清",
            "type": "installation_payment"
          }
        ]
      }
    },
    {
      "doc_id": "abc123def456",
      "page_index": 3,
      "chunk_seq": 8,
      "para_seq": 2,
      "start_char": 2100,
      "end_char": 2250,
      "text": "质保期自验收合格之日起12个月。质保期内如出现非人为因素造成的质量问题，供方负责免费维修或更换。",
      "clause_class": ["质保条款"],
      "confidence": 0.98,
      "metadata": {
        "sub_clauses": [
          {
            "text": "质保期自验收合格之日起12个月",
            "type": "warranty"
          }
        ]
      }
    }
  ]
}
```

##### 响应字段说明

**顶层字段**:

| 字段 | 类型 | 描述 |
|------|------|------|
| `id` | string | 与请求中一致的任务 ID |
| `message` | string | 固定为 `"success"` |
| `paragraphs` | array[object] | 提取的段落列表 |

**Paragraph 对象字段**:

| 字段 | 类型 | 描述 |
|------|------|------|
| `doc_id` | string | 文档唯一标识符 |
| `page_index` | int | 页码索引（从 0 开始） |
| `chunk_seq` | int | 文档分块序号 |
| `para_seq` | int/string | 段落在当前块中的序号 |
| `start_char` | int | 段落在文档中的起始字符位置 |
| `end_char` | int | 段落在文档中的结束字符位置 |
| `text` | string | 段落完整文本 |
| `clause_class` | array[string] | 条款分类标签列表 |
| `confidence` | float/null | 分类置信度（0-1），可能为 null |
| `metadata` | object | 元数据，包含 `sub_clauses` 等信息 |

**metadata.sub_clauses 对象字段**:

| 字段 | 类型 | 描述 |
|------|------|------|
| `text` | string | 子条款原文 |
| `type` | string | 子条款类型 |

**`type` 可能的值**:
- `equipment_payment`: 设备付款条款
- `installation_payment`: 安装付款条款
- `warranty`: 质保条款

##### 错误响应

**400 Bad Request** - 请求参数错误

```json
{
  "detail": "必须且只能提供 'bos_path', 'file_url', 'md_text' 中的一种。"
}
```

**500 Internal Server Error** - 服务器内部错误

```json
{
  "detail": "文档解析失败: <错误详情>"
}
```

---

## 使用场景

### 场景 1: 处理 BOS 上的合同文档

```python
import requests

API_URL = "http://localhost:2024/extract_clauses"

response = requests.post(API_URL, json={
    "id": "contract-2024-001",
    "bos_path": "contracts/2024/purchase_contract_001.pdf",
    "bos_bucket_name": "company-contracts"
})

result = response.json()
paragraphs = result["paragraphs"]
print(f"提取到 {len(paragraphs)} 个相关段落")
```

### 场景 2: 直接分析文本内容

```python
import requests

contract_text = """
## 付款条款

1. 合同签订后7个工作日内，甲方向乙方支付合同总价的30%作为预付款。
2. 设备到货并经甲方验收合格后，支付合同总价的40%。
3. 安装调试完成并通过最终验收后，支付合同总价的25%。
4. 剩余5%作为质保金，质保期满后无质量问题一次性付清。

## 质保条款

质保期为验收合格之日起24个月。
"""

response = requests.post("http://localhost:2024/extract_clauses", json={
    "id": "text-analysis-001",
    "md_text": contract_text
})

result = response.json()
for para in result["paragraphs"]:
    print(f"段落: {para['text'][:50]}...")
    print(f"分类: {para['clause_class']}")
    print(f"置信度: {para['confidence']}")
    print()
```

### 场景 3: 开启调试模式追踪问题

```python
import requests

# 在 id 末尾添加 _debug 后缀
response = requests.post("http://localhost:2024/extract_clauses", json={
    "id": "debug-task-001_debug",
    "md_text": "合同内容..."
})

# 服务器会在 debug_output/debug-task-001_debug/ 目录下
# 保存每个节点的中间结果，便于排查问题
```

---

## 调用注意事项

### 1. 文档源选择

- **BOS 路径**: 适合大量文档的批处理场景
- **URL**: 适合从外部系统获取文档
- **直接文本**: 适合小文档或测试场景

### 2. 性能考虑

- **文档长度**: 建议单个文档不超过 100,000 字符
- **并发请求**: 服务支持并发，但建议控制在 20 个/秒以内
- **超时设置**: 建议客户端设置至少 60 秒超时

### 3. 结果使用

Service 1 的输出 `paragraphs` 可以**直接作为 Service 2 的输入**，无需任何格式转换：

```python
# Step 1: 调用 Service 1
s1_response = requests.post("http://localhost:2024/extract_clauses", json={
    "id": "task-001",
    "md_text": "合同文本..."
})
paragraphs = s1_response.json()["paragraphs"]

# Step 2: 直接将 paragraphs 传给 Service 2
s2_response = requests.post("http://localhost:2025/extract_payment_info", json={
    "id": "task-001",
    "operation_type": "extract",
    "paragraphs": paragraphs  # 直接使用
})
```

### 4. 错误处理

```python
import requests
from requests.exceptions import RequestException

try:
    response = requests.post(API_URL, json=payload, timeout=60)
    response.raise_for_status()
    result = response.json()
except RequestException as e:
    print(f"请求失败: {e}")
    if hasattr(e, 'response') and e.response is not None:
        print(f"错误详情: {e.response.text}")
```

---

## 调试技巧

### 启用详细日志

服务使用 `loguru` 记录日志，可以通过环境变量控制日志级别：

```bash
export LOG_LEVEL=DEBUG
python main.py
```

### 保存中间结果

在 `id` 末尾添加 `_debug` 后缀：

```json
{
  "id": "my-task_debug",
  ...
}
```

中间结果会保存到 `debug_output/my-task_debug/` 目录。

### 保存最终输出

在 `id` 末尾添加 `_save_output` 后缀：

```json
{
  "id": "my-task_save_output",
  ...
}
```

最终结果会保存到 `outputs/my-task_save_output/` 目录。

---

## 完整 Python 客户端示例

```python
import requests
import json
from typing import Optional, Dict, Any

class ClauseExtractorClient:
    def __init__(self, base_url: str = "http://localhost:2024"):
        self.base_url = base_url
        self.api_url = f"{base_url}/extract_clauses"
    
    def health_check(self) -> bool:
        """检查服务健康状态"""
        try:
            response = requests.get(f"{self.base_url}/health", timeout=5)
            return response.status_code == 200
        except:
            return False
    
    def extract_from_bos(
        self,
        task_id: str,
        bos_path: str,
        bos_bucket_name: Optional[str] = None,
        json_text: Optional[str] = None
    ) -> Dict[str, Any]:
        """从 BOS 提取条款"""
        payload = {
            "id": task_id,
            "bos_path": bos_path
        }
        if bos_bucket_name:
            payload["bos_bucket_name"] = bos_bucket_name
        if json_text:
            payload["json_text"] = json_text
        
        return self._call_api(payload)
    
    def extract_from_url(
        self,
        task_id: str,
        file_url: str
    ) -> Dict[str, Any]:
        """从 URL 提取条款"""
        payload = {
            "id": task_id,
            "file_url": file_url
        }
        return self._call_api(payload)
    
    def extract_from_text(
        self,
        task_id: str,
        md_text: str
    ) -> Dict[str, Any]:
        """从文本提取条款"""
        payload = {
            "id": task_id,
            "md_text": md_text
        }
        return self._call_api(payload)
    
    def _call_api(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """内部 API 调用方法"""
        try:
            response = requests.post(
                self.api_url,
                json=payload,
                timeout=60
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"API 调用失败: {e}")
            if hasattr(e, 'response') and e.response:
                print(f"错误详情: {e.response.text}")
            raise

# 使用示例
if __name__ == "__main__":
    client = ClauseExtractorClient()
    
    # 检查服务状态
    if not client.health_check():
        print("服务未启动")
        exit(1)
    
    # 提取条款
    result = client.extract_from_text(
        task_id="demo-001",
        md_text="合同签订后支付30%定金。验收合格后支付剩余70%。质保期12个月。"
    )
    
    print(json.dumps(result, indent=2, ensure_ascii=False))
```

---

## 版本历史

- **v1.0.0** (2024-01) - 初始版本
  - 支持 BOS、URL、文本三种输入方式
  - 实现条款定位和分类功能
  - 支持调试模式和输出保存

---

## 技术支持

如有问题，请联系：
- Email: zhuyichen@smec.com
- 项目仓库: [内部 GitLab]