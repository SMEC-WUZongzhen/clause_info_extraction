"""
test_llm_direct.py - 直接测试底层大模型是否可用（OpenAI 兼容协议）
用法: python test_llm_direct.py
"""
import json
import requests

# ===== 配置 =====
# 底层模型地址（OpenAI 兼容，百舸平台远程）
BASE_URL = "http://10.204.2.21:168"
ENDPOINT = "/v1/chat/completions"
# 远程网关需要 Bearer token；不带 "Bearer " 前缀，代码里会自动拼
API_KEY = ""
# 模型名通过 GET /v1/models 查到，必须用完整 id 字符串
MODEL_NAME = "/bos/smec-ai-model/models/rag/payment_info_extraction_model/payment_info_qwen35_9B_sft_V3_amv-rf9a88eptvev_2026-06-04-08:58:36/1"

TIMEOUT = 60


def main():
    url = BASE_URL.rstrip("/") + ENDPOINT
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个专业的电梯安装合同付款条款解析器。# 任务"
                    "从给定的安装付款条款中，按照以下步骤提取支付节点信息。"
                ),
            },
            {
                "role": "user",
                "content": (
                    """# 步骤1：条款有效性判断
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
    "payment_type": "从上述288个节点中选择",
    "payment_ratio": "X%或空字符串",
    "payment_amount": "纯数字或空字符串"
  }
]
若非支付节点，输出：[]

# 输入数据
<需要判断的付款条款>
3.2.1 乙方应根据工期要求给予书面的进场时间节点建议。乙方按甲方书面指定之日期进场安装， 
在进场开始安装后十五天内甲方将安装工程款总额的50%付给乙方
</需要判断的付款条款>

<合同条款上下文>
的非现金支付合同结算总价低于第3.1.1条款所述之现金支付合同总价的，不受此限制。


3.1.7 双方确认，未经甲方同意，乙方承诺不得将因履行本协议以及各相关分项合同形成的对甲方 
或其他相关主体的债权（包括但不限于应收账款等）以任何方式进行融资（包括但不限于担 
保、抵押、质押），否则甲方有权向乙方收取合同总价的5%作为违约金。


3.2 甲方应按以下所规定的付款计划完成安装合同价的支付。


3.2.1 乙方应根据工期要求给予书面的进场时间节点建议。乙方按甲方书面指定之日期进场安装， 
在进场开始安装后十五天内甲方将安装工程款总额的50%付给乙方；


3.2.2 双方验收合格并在市场监督管理局等相关政府部门检验合格取得相关证书后15天内支付到工 
程款的85%；


3.2.3 电梯移交给物业公司并完成结算后十五天内支付到工程款的100%；


3.2.4 收取银行保函：本工程质保期30个月，无质保金，采取由保利发展控股集团统一收取质量保 
函的质保方式。如在合同工程保修期内发生索赔费用，该费用经甲方与供方共同确认后，由 
甲方按以下方式收取：


a. 在其他合同的进度款、结算款中扣除；


b. 如已无合同款可扣除，则由供应商向甲方补缴索赔费用。


C. 如供应商未在规定时间内补缴索赔费用，甲方将情况及时反馈给保利发展控股集团，由
</合同条款上下文>"""
                ),
            },
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
    }
    # payload = {
    #     "model": MODEL_NAME,
    #     "messages": [
    #         {
    #             "role": "system",
    #             "content": (
    #                 '''
    #                 ## 任务
    #                 判断"待验证条款"本身是否为有效付款条款。
    #                 有效付款条款必须**同时包含**：①正向支付动作（"支付""付款"等）+ ②具体金额或比例（X万元、X%等数值）。
    #                 两者缺一则 is_valid=false。

    #                 ## 直接排除（命中任一 → is_valid=false）
    #                 - 含"违约金/罚款/赔偿/赔偿损失/违约责任"
    #                 - 含"逾期"且含"利息/利率/滞纳金"
    #                 - 含"质量问题/质量缺陷"
    #                 - 含"有权不支付/拒付/暂停支付"
    #                 - 含"保函/担保函"但无"支付X%/X元"的实际付款
    #                 - 仅描述价格/报价/总价/单价，无支付动作
    #                 - 仅描述支付方式/工具，无金额比例
    #                 - 仅描述付款前置条件/依据/验收要求，无支付动作和金额
    #                 - 仅描述供货期/交货期/工期等时间安排，无支付动作
    #                 - 概述性/引导性条款，无具体金额
    #                 - 指引条款（如"详见第X条"）
    #                 - 单据要求（如"正本X份，副本X份"）
    #                 - 附件/目录/表格标题
    #                 - 不完整的句子片段

    #                 ## 输出
    #                 只输出JSON对象，无其他文字。
    #                 {{"id": "输入的id", "is_valid": true/false, "reason": "简短理由"}}

    #                 '''
    #             ),
    #         },
    #         {
    #             "role": "user",
    #             "content": (
    #                 '''
    #                 ## 待验证条款
    #                 "id":70,
    #                 "clause":	2.3若因非乙方原因无法按甲方审核确认的按照计划完成电梯安装工程，甲方应在正式开工安装后10个月内向乙方付清本合同余款，但不免除乙方完成剩余电梯安装义务、调试、检验检测、成品保护和维保等义务。
    #                 "clause_class": 安装付款条款
    #                 '''
    #             ),
    #         },
    #     ],
    #     "temperature": 0.1,
    #     "max_tokens": 2048,
    # }

    print(f"POST {url}")
    print(f"model={MODEL_NAME}")

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        print(f"[失败] 无法连接: {e}")
        return
    except requests.exceptions.Timeout:
        print(f"[失败] 超时（{TIMEOUT}s）")
        return

    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"[失败] 响应体: {resp.text[:500]}")
        return

    data = resp.json()
    print("\n===== 完整响应 =====")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    try:
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        print("\n===== 模型回复 =====")
        print(content)
        print(f"\ntokens: prompt={usage.get('prompt_tokens')} "
              f"completion={usage.get('completion_tokens')} "
              f"total={usage.get('total_tokens')}")
        print("\n[成功] 模型可用")
    except (KeyError, IndexError):
        print("[警告] 响应格式不是标准 OpenAI 格式")


if __name__ == "__main__":
    main()