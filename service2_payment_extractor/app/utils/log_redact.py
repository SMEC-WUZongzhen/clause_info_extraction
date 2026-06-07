# app/utils/log_redact.py
"""日志脱敏工具（修复 S3）。

避免在 INFO/DEBUG 日志中持久化合同原文 / PII。

约定：
- 生产模式（ENVIRONMENT=production）默认开启脱敏；
- 开发模式默认关闭以便排障；
- 可通过 `LOG_REDACT_PII=1/0` 显式覆盖。

使用方式：
    from app.utils.log_redact import safe_clause
    logger.debug(f"clause: {safe_clause(text)}")
"""

import hashlib
import os
from functools import lru_cache
from typing import Optional


@lru_cache(maxsize=1)
def _redact_enabled() -> bool:
    """生产环境默认开启脱敏；开发可通过 LOG_REDACT_PII=0 关闭以排障。"""
    raw = os.getenv("LOG_REDACT_PII")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    env = os.getenv("ENVIRONMENT", "development").lower()
    return env == "production"


def safe_clause(text: Optional[str], head: int = 30) -> str:
    """对外日志安全的条款片段表示。

    - 关闭脱敏：返回 text[:head] + 省略号（保留旧 DEBUG 体验）
    - 开启脱敏：仅返回 len 与 sha8 指纹，不带任何原文
    """
    if not text:
        return "<empty>"
    s = str(text)
    if not _redact_enabled():
        return f"{s[:head]}..." if len(s) > head else s
    digest = hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"<clause len={len(s)} sha8={digest}>"


def reset_redact_cache() -> None:
    """供单测使用：清空 lru_cache 以便切换 env 后重新求值。"""
    _redact_enabled.cache_clear()
