import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app.config.prompts_loader import INSTALL_PAYMENT_RATIO_PROMPT, EQUIPMENT_PAYMENT_RATIO_PROMPT

inst = INSTALL_PAYMENT_RATIO_PROMPT
eq = EQUIPMENT_PAYMENT_RATIO_PROMPT


def _judgement_section(prompt: str) -> str:
    """Return only the text between '节点判断注意事项' and the next '## 输出格式要求'."""
    head_idx = prompt.find('节点判断注意事项')
    tail_idx = prompt.find('## 输出格式要求', head_idx)
    if head_idx < 0 or tail_idx < 0:
        return ''
    return prompt[head_idx:tail_idx]


inst_judge = _judgement_section(inst)

checks = {
    'B-多触发表格 (install)': '多触发条件优先识别' in inst,
    'A-install 累计+结算示例 (出现在输出格式段)': '累计支付至 + 结算完成示例' in inst,
    'A-install 工程整体竣工 ratio=15%': '工程整体竣工"' in inst and '"15%"' in inst,
    'C-install 删除否定句': '严禁因两段都含' not in inst,
    'D-install judgement 删除重复 80% 示例': '设备的80%。结算完成后' not in inst_judge,
    'D-install judgement 仅引用 §0/OUTPUT_EXAMPLES': '详见 §0 与 OUTPUT_EXAMPLES' in inst_judge,
    'B-多触发表格 (equipment)': '多触发条件优先识别' in eq,
    'A-equipment 累计+结算示例': '累计支付至 + 结算完成示例' in eq,
    'A-equipment 结算完成 ratio=5%': '结算完成"' in eq and '"5%"' in eq,
    'E-CoT 节点编号约束': 'JSON 数组长度必须等于编号数' in inst,
    'F-§5 合并为 4 类 (否定/拒付/残缺)': '否定/拒付/残缺' in inst,
    'F-关键约束 (取代重要提醒)': '关键约束' in inst and '## ⚠️ 重要提醒' not in inst,
}
for k, v in checks.items():
    print(f"  {'[PASS]' if v else '[FAIL]'}  {k}")
print()
print('ALL PASS' if all(checks.values()) else 'SOME FAILED')

