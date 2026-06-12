# app/utils/contract_price_comparator.py
"""合同总金额抽取与一致性比对（Service 层）。

- `extract_contract_price`: 调用 LLM 从合同总价条款中抽取金额。
- `compare_contract_price`: 与 SIS 系统金额做硬编码差值（≤ 10.0）比对。
"""

from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from langchain_openai import ChatOpenAI
from loguru import logger

from app.config.config import APP_CONFIG
from app.config.prompts import CONTRACT_PRICE_EXTRACTION_PROMPT


# 比对阈值，按需求硬编码；不读 env / 不读配置
_PRICE_DIFF_TOLERANCE: float = 10.0


def compare_contract_price(extracted: Optional[float], sis: float) -> Tuple[bool, Optional[float]]:
    """比对 LLM 抽取金额与 SIS 金额。

    返回 (是否一致, 差值绝对值)。LLM 抽取失败（None）时一律视为不一致。
    """
    if extracted is None:
        return False, None
    diff = abs(float(extracted) - float(sis))
    return diff <= _PRICE_DIFF_TOLERANCE, diff


def _parse_price_from_response(text: str) -> Optional[float]:
    """从 LLM 输出中稳健地解析 contract_price。"""
    if not text:
        return None
    raw = text.strip()
    # 去除可能的 ```json ... ``` 包裹
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)

    # 1) 直接 JSON 解析
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "contract_price" in data:
            v = data["contract_price"]
            if v is None:
                return None
            return float(v)
    except (TypeError, ValueError):
        pass

    # 2) 兜底：正则提取首个数字
    m = re.search(r'"contract_price"\s*:\s*(null|-?\d+(?:\.\d+)?)', raw)
    if m:
        val = m.group(1)
        return None if val == "null" else float(val)

    m2 = re.search(r"-?\d+(?:\.\d+)?", raw)
    return float(m2.group(0)) if m2 else None


async def extract_contract_price(
    contract_price_clause: str,
    contract_price_clause_context: Optional[str],
) -> Optional[float]:
    """调用 LLM 抽取合同总金额。失败 / 解析不到数字 → 返回 None。"""
    clause = (contract_price_clause or "").strip()
    if not clause:
        return None

    # 健壮处理可空上下文
    context = (contract_price_clause_context or "").strip() or "（无）"

    llm_cfg = APP_CONFIG.llm
    if not llm_cfg or not llm_cfg.api_base:
        logger.error("LLM 配置缺失 api_base，无法执行合同金额抽取")
        return None

    prompt = CONTRACT_PRICE_EXTRACTION_PROMPT.format(
        contract_price_clause=clause,
        contract_price_clause_context=context,
    )

    llm = ChatOpenAI(
        model=llm_cfg.model or llm_cfg.model_name,
        temperature=0.0,
        openai_api_key=llm_cfg.api_key or "EMPTY",
        openai_api_base=llm_cfg.api_base,
        max_tokens=256,
    )

    try:
        msg = await llm.ainvoke(prompt)
        content = getattr(msg, "content", "") or ""
    except Exception as e:  # noqa: BLE001
        logger.error(f"合同金额抽取 LLM 调用失败: {e}")
        return None

    price = _parse_price_from_response(content)
    logger.info(f"合同金额抽取完成: raw={content!r} parsed={price}")
    return price
