# app/utils/concurrency.py - service2_payment_extractor
"""
并发控制工具：
- 进程级 LLM 并发信号量（避免瞬时打爆下游 LLM / httpx 连接池）
- 进程级 Rerank 并发信号量（避免 CPU 端多线程抢占）

用法:
    from app.utils.concurrency import llm_guarded_ainvoke
    resp = await llm_guarded_ainvoke(chain, input_dict)

环境变量:
    LLM_CONCURRENCY (int, 默认 16)
    RERANK_CONCURRENCY (int, 默认 2)
    LLM_CALL_TIMEOUT_SEC (int, 默认 60) —— 单次 LLM 调用硬超时
"""

from __future__ import annotations

import asyncio
import os
import weakref
from typing import Any, Dict, Optional

from loguru import logger


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except Exception:
        return default


LLM_CONCURRENCY: int = _env_int("LLM_CONCURRENCY", 16)
RERANK_CONCURRENCY: int = _env_int("RERANK_CONCURRENCY", 2)
LLM_CALL_TIMEOUT_SEC: int = _env_int("LLM_CALL_TIMEOUT_SEC", 60)

# 按 event loop 缓存 Semaphore，避免模块级单例在多 loop 场景报错（C8）。
_llm_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_rerank_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _get_or_create(cache: "weakref.WeakKeyDictionary", limit: int, label: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = cache.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        cache[loop] = sem
        logger.info(f"[concurrency] {label} 信号量已创建（loop={id(loop)}），并发上限={limit}")
    return sem


def get_llm_semaphore() -> asyncio.Semaphore:
    return _get_or_create(_llm_sems, LLM_CONCURRENCY, "LLM")


def get_rerank_semaphore() -> asyncio.Semaphore:
    return _get_or_create(_rerank_sems, RERANK_CONCURRENCY, "Rerank")


async def llm_guarded_ainvoke(chain: Any, input_dict: Dict[str, Any], *, timeout: Optional[int] = None) -> Any:
    """
    对 LangChain Runnable 的 ainvoke 做并发信号量 + 硬超时包裹。

    Args:
        chain: LangChain Runnable (prompt | llm)
        input_dict: ainvoke 入参
        timeout: 覆盖默认 LLM_CALL_TIMEOUT_SEC 的单次超时秒数

    Raises:
        asyncio.TimeoutError: 单次调用超过 timeout
    """
    sem = get_llm_semaphore()
    to = timeout if timeout is not None else LLM_CALL_TIMEOUT_SEC
    async with sem:
        return await asyncio.wait_for(
            chain.ainvoke(input_dict, config={"callbacks": []}),
            timeout=to,
        )
