"""提示词加载器（N2）。

优先从 `app/resources/prompts/` 目录加载 `*.txt` 文件，缺失时回退到
`prompts.py` 中的常量。这允许运维侧通过部署 .txt 数据文件来热替换提示词。

P0-3 增强：模板支持业务词典占位符 `{{install_whitelist_md}}` /
`{{install_cross_mapping_md}}` / `{{aux_fee_keywords_inline}}` /
`{{percent_tokens_inline}}`，由 `render()` 在加载期完成替换。
通过 env `PROMPT_PLACEHOLDER_INJECT=true` 灰度开启。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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


# ---------------------------------------------------------------------------
# Render（P0-3）
# ---------------------------------------------------------------------------

def _md_table(headers: List[str], rows: Iterable[Iterable[str]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = "\n".join("| " + " | ".join(str(c) for c in r) + " |" for r in rows)
    return "\n".join([head, sep, body])


def _build_placeholders(bd) -> Dict[str, str]:
    """从 BusinessDict 构建占位符 → 替换文本 的映射。"""
    install_whitelist_md = _md_table(
        ["节点"], [[n] for n in sorted(bd.install.payment_type_whitelist)]
    )
    install_cross_mapping_md = _md_table(
        ["原节点", "→ 安装侧节点"],
        list(bd.install.cross_mapping.items()),
    )
    aux_fee_keywords_inline = "、".join(bd.aux_fee_keywords)
    percent_tokens_inline = " / ".join(bd.synonyms.percent_tokens)
    return {
        "install_whitelist_md": install_whitelist_md,
        "install_cross_mapping_md": install_cross_mapping_md,
        "aux_fee_keywords_inline": aux_fee_keywords_inline,
        "percent_tokens_inline": percent_tokens_inline,
    }


def render(template: str, bd=None) -> str:
    """对单个 prompt 模板做占位符替换。

    若 `PROMPT_PLACEHOLDER_INJECT` env 关闭或 BusinessDict 加载失败，原样返回。
    """
    enabled = (os.getenv("PROMPT_PLACEHOLDER_INJECT", "true") or "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if not enabled or not template or "{{" not in template:
        return template
    if bd is None:
        try:
            from app.config.business_dict import get_business_dict
            bd = get_business_dict()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[prompts.render] 业务词典未就绪，跳过占位符替换: {e}")
            return template
    repl = _build_placeholders(bd)
    out = template
    for k, v in repl.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def load_prompts() -> Dict[str, str]:
    """加载全部提示词。任一名字若有同名 .txt 则使用它，否则回退到 prompts.py 常量。
    加载完成后对所有 prompt 统一调用 render() 注入业务词典占位符。
    """
    from app.config import prompts as _legacy

    out: Dict[str, str] = {}
    for name in _PROMPT_NAMES:
        from_disk = _load_from_disk(name)
        if from_disk is not None:
            raw = from_disk
            logger.debug(f"[prompts] 已从磁盘加载 {name}")
        else:
            raw = getattr(_legacy, name)
        out[name] = render(raw)
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
