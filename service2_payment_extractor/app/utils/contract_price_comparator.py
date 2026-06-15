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


def _coerce_number(v) -> Optional[float]:
    """把 LLM 可能输出的金额值（int / float / 带逗号空格的字符串）转成 float。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace(" ", "").replace("，", "")
        # 去掉常见货币前缀
        s = re.sub(r"^(?:￥|¥|RMB|人民币)", "", s, flags=re.IGNORECASE)
        if not s or s.lower() == "null":
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _parse_price_from_response(text: str) -> Optional[float]:
    """从 LLM 输出中稳健地解析 contract_price。

    LLM 实际可能输出形如：
        【节点识别】... 一大段思考 ...
        {"contract_price": "4308000"}
    或带 ```json ... ``` 包裹、值为字符串 / 带千分位逗号 / 整数 / 小数 等多种形态。
    解析顺序：
      1. 整段直接 json.loads
      2. 用 JSONDecoder 滑窗扫描所有 {...} 块，取**最后一个**含 contract_price 的对象
      3. 正则匹配 "contract_price": <数字 | "字符串" | null>
    """
    if not text:
        return None
    raw = text.strip()
    # 去除可能的 ```json ... ``` 包裹（含中间）
    raw = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
    raw = raw.strip("`").strip()

    # 1) 整段 json.loads
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "contract_price" in data:
            return _coerce_number(data["contract_price"])
    except (TypeError, ValueError):
        pass

    # 2) 滑窗扫描所有顶层 JSON 对象，优先取最后一个命中的（LLM 常把 JSON 放在最后）
    decoder = json.JSONDecoder()
    n = len(raw)
    i = 0
    last_hit: Optional[float] = None
    while i < n:
        if raw[i] == "{":
            try:
                obj, end = decoder.raw_decode(raw, i)
                if isinstance(obj, dict) and "contract_price" in obj:
                    last_hit = _coerce_number(obj["contract_price"])
                i = end
                continue
            except json.JSONDecodeError:
                i += 1
                continue
        i += 1
    if last_hit is not None:
        return last_hit

    # 3) 正则兜底：支持 number / "string" / null 三种值形态
    m = re.search(
        r'"contract_price"\s*:\s*(null|"([^"]*)"|(-?\d[\d,\s]*(?:\.\d+)?))',
        raw,
    )
    if m:
        if m.group(1) == "null":
            return None
        return _coerce_number(m.group(2) if m.group(2) is not None else m.group(3))

    # 4) 最终兜底：捕获形如 4,308,000 / 4308000 / 4308000.50 的连续数字串（允许逗号千分位）
    m2 = re.search(r"-?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?", raw)
    return _coerce_number(m2.group(0)) if m2 else None


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
