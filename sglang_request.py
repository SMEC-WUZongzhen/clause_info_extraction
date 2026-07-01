
import time
import json
from openai import OpenAI
from pydantic import BaseModel, Field
from typing import List, Optional

# ========== 1. 定义你期望的 JSON 输出结构 ==========
class PaymentNode(BaseModel):
    payment_type: str = Field(..., description="标准化支付类型，从12个标准节点中选择")
    payment_ratio: Optional[str] = Field("", description="支付比例，格式为X%或空字符串")
    payment_amount: Optional[str] = Field("", description="支付金额，纯数字字符串或空字符串")

class PaymentResult(BaseModel):
    nodes: List[PaymentNode] = Field(default_factory=list, description="支付节点列表")

# ========== 2. 配置客户端 ==========
client = OpenAI(
    base_url="http://10.204.2.103/s-rfd3b09c8a04/8000/v1",   # 你的 SGLang 服务地址
    api_key="c6898da8-35572cf72ce6-ba9ceee63805",                       # SGLang 默认不需要 API key，但必须传一个字符串
)

# ========== 3. 构造请求 ==========
# 公共前缀（长文本，每次不变）
system_prompt = """你是一个专业的电梯安装合同付款条款解析器。
# 任务
从给定的安装付款条款中，按照以下步骤提取支付节点信息。

# 步骤1：条款有效性判断
分析条款是否包含支付动作：
- **有效支付节点**：包含"支付、付款、结算、付清、定金、预付、进度款、尾款、余款、至X%"等支付动作或义务
- **非支付节点**：发票开具说明、保函/担保条款、纯定义描述、质保期说明（无支付动作）→ 输出 []
- **多节点识别**：若一句话包含多个独立的支付触发条件（如"到货支付至80%，安装完成再付10%"），需拆分为多个节点对象

# 步骤2：标准化支付类型判断（payment_type）
**必须从以下列表中选择最匹配的一项**（按付款顺序排列）：
1. 定金
2. 进场前（首付）
3. 进场后
4. 移交前
5. 报验前
6. 公司验收后
7. 当地政府部门验收后
8. 电梯移交用户后
9. 工程整体竣工
10. 特殊付款-移交前
11. 特殊付款-移交后
12. 质保金

**判断原则**：
- 优先依据 <需要判断的付款条款> 中的触发条件关键词
- 若原文触发条件不完全匹配标准名称，选择语义最接近的一项

# 步骤3：支付比例提取（payment_ratio）
按优先级尝试以下方法：
1. **直接识别**：识别"30%"、"百分之三十"、"三成(=30%)"、"0.3(=30%)"等表述，统一转换为"X%"格式
2. **累计推算**：
   - "支付至X%"：本次比例 = X% - 前序已支付累计比例
   - "余款/尾款"：本次比例 = 100% - 前序已支付累计比例
3. **金额反算**（仅当条款只有金额无比例时）：
   - 在 <合同条款上下文> 中查找安装总价
   - 计算：本次比例 = (条款金额 / 安装总价) × 100%
4. 若以上方法均无法获取，填 ""

# 步骤4：支付金额提取（payment_amount）
识别并规范化金额：
- 支持格式：50,000元、5万元、伍万元、￥5,000、RMB 5,000
- 处理规则：
  1. 去掉货币符号（￥、RMB）和分隔符（逗号）
  2. 中文大写/中文数字转换为阿拉伯数字
  3. "万元"换算为"元"（例如：5万元 → 50000）
  4. 只输出纯数字字符串，不带单位
- 若无法提取明确金额，填 ""

# 输出格式
只输出JSON数组，禁止输出任何解释、Markdown语法、代码块符号（```）。
格式如下：
[
  {
    "payment_type": "从上述292个节点中选择",
    "payment_ratio": "X%或空字符串",
    "payment_amount": "纯数字或空字符串"
  }
]
若非支付节点，输出：[]
"""

# 当前请求的差异化后缀（短文本，每个请求不同）
user_query = """
# 输入数据
<需要判断的付款条款>
3.1验收款：电梯安装完成经政府验收合格后，且收到国债补贴后一周内支付100%安装款，但最迟不得晚于货到工地之日起12个月，否则由买方全款垫资支付。超出补贴部分在电梯验收后一周内支付。
</需要判断的付款条款>

<合同条款上下文>

2.5.5本合同施工费总价仅为正常工作时间合理工期内的施工费用，如委托方有额外夜间加班、双休日或/和国定节假日施工的特殊要求的，委托方应支付相应的赶工费用。委托方应在施工前告知受托方，双方在施工前另行协商书面确定赶工费的金额和支付方式。

## 三、支付方式

3.1验收款：电梯安装完成经政府验收合格后，且收到国债补贴后一周内支付100%安装款，但最迟不得晚于货到工地之日起12个月，否则由买方全款垫资支付。超出补贴部分在电梯验收后一周内支付。

3.2产品如分批次验收，委托方按每批次比例分批支付施工费余额。为确保受托方及时确认委托方支付的施工费款项，按时安排进场、施工、验收和交付，委托方应在付款后，将付款凭证传真或寄送至受托方的合同经办人，并注明付款单位名称、合同编号。

</合同条款上下文>
"""

# 使用 OpenAI 兼容的 chat.completions 接口
start_time = time.time()

response = client.chat.completions.create(
    model="payment-model",                 # 必须与启动时的 --served-model-name 一致
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query}
    ],
    temperature=0.0,                       # 提取任务建议用 0，保证确定性
    max_tokens=2048,
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "payment_extraction",
            "schema": PaymentResult.model_json_schema(),
        }
    }
)

elapsed = time.time() - start_time

# ========== 4. 处理结果 ==========
result_content = response.choices[0].message.content
print(f"✅ 耗时: {elapsed:.2f} 秒")
print(f"📦 原始返回:\n{result_content}")

# 解析为 Python 对象（可选）
try:
    parsed = json.loads(result_content)
    print("\n📊 解析后的字典:")
    print(json.dumps(parsed, indent=2, ensure_ascii=False))
except json.JSONDecodeError:
    print("⚠️ 返回内容不是合法的 JSON，请检查模型输出。")