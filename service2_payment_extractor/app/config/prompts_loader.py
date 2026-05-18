"""提示词加载器（N2）。

优先从 `app/resources/prompts/` 目录加载 `*.txt` 文件，缺失时回退到 `prompts.py` 中的常量。
这允许运维侧通过部署 .txt 数据文件来热替换提示词，而不再需要覆盖 Python 源代码。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from loguru import logger


_PROMPT_DIR = Path(__file__).resolve().parent.parent / "resources" / "prompts"

_PROMPT_NAMES = (
    "EQUIPMENT_PAYMENT_RATIO_PROMPT",
    "INSTALL_PAYMENT_RATIO_PROMPT",
    "PAYMENT_RATIO_PROMPT",
    "PAYMENT_SUMMARY_RATIO_PROMPT",
    "INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT",
    "WARRANTY_SUMMARY",
    "RESULT_VERIFICATION_PROMPT",
    "PAYMENT_CLAUSE_VALIDATION_PROMPT",
    "PAYMENT_CLAUSE_CATEGORY_PROMPT",
    "RESULT_VERIFICATION_SINGLE_GROUP_PROMPT",
)


def _load_from_disk(name: str) -> str | None:
    p = _PROMPT_DIR / f"{name}.txt"
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[prompts] 读取 {p} 失败：{e}")
    return None


def load_prompts() -> Dict[str, str]:
    """加载全部提示词。任一名字若有同名 .txt 则使用它，否则回退到 prompts.py 常量。"""
    from app.config import prompts as _legacy

    out: Dict[str, str] = {}
    for name in _PROMPT_NAMES:
        from_disk = _load_from_disk(name)
        if from_disk is not None:
            out[name] = from_disk
            logger.debug(f"[prompts] 已从磁盘加载 {name}")
        else:
            out[name] = getattr(_legacy, name)
    return out


# 模块级单次加载，便于其他模块直接 from app.config.prompts_loader import PAYMENT_RATIO_PROMPT
_LOADED = load_prompts()
EQUIPMENT_PAYMENT_RATIO_PROMPT = _LOADED["EQUIPMENT_PAYMENT_RATIO_PROMPT"]
INSTALL_PAYMENT_RATIO_PROMPT = _LOADED["INSTALL_PAYMENT_RATIO_PROMPT"]
PAYMENT_RATIO_PROMPT = _LOADED["PAYMENT_RATIO_PROMPT"]
PAYMENT_SUMMARY_RATIO_PROMPT = _LOADED["PAYMENT_SUMMARY_RATIO_PROMPT"]
INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT = _LOADED["INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT"]
WARRANTY_SUMMARY = _LOADED["WARRANTY_SUMMARY"]
RESULT_VERIFICATION_PROMPT = _LOADED["RESULT_VERIFICATION_PROMPT"]
PAYMENT_CLAUSE_VALIDATION_PROMPT = _LOADED["PAYMENT_CLAUSE_VALIDATION_PROMPT"]
PAYMENT_CLAUSE_CATEGORY_PROMPT = _LOADED["PAYMENT_CLAUSE_CATEGORY_PROMPT"]
RESULT_VERIFICATION_SINGLE_GROUP_PROMPT = _LOADED["RESULT_VERIFICATION_SINGLE_GROUP_PROMPT"]
