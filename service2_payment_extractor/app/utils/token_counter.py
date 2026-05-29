"""token 计数工具（P0-4）。

为 OpenAI 兼容入口 `/v1/chat/completions` 提供真实的 token 计数。
- 优先使用 tiktoken `cl100k_base` 编码器（与 OpenAI 一致）
- tiktoken 加载失败时 fallback 为字符比估算（中文 ~2.5 字符/token）

设计要点：
- `_encoder()` lru_cache 单次加载，无锁竞争
- 计数失败必须降级而非抛异常（usage 是辅助字段，不应破坏主流程）
"""
from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Optional

from loguru import logger


_FALLBACK_RATIO = float(os.getenv("USAGE_TOKEN_FALLBACK_RATIO", "2.5"))


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[token_counter] tiktoken 不可用，降级字符比估算: {e}")
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[token_counter] cl100k_base 加载失败，降级字符比: {e}")
        return None


def warmup() -> None:
    """供 lifespan 调用，提前触发 encoder 加载。"""
    _encoder()


def count_tokens(text: Optional[str]) -> int:
    """对 text 进行 token 计数；text 为 None / 空字符串返回 0。"""
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[token_counter] tiktoken encode 失败，降级估算: {e}")
    # fallback：中文经验比 2.5 字符/token，向上取整，至少 1
    return max(1, math.ceil(len(text) / _FALLBACK_RATIO))


__all__ = ["count_tokens", "warmup"]
