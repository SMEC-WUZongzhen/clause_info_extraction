"""提示词加载器（N2 / P2/P3 / P7）。

优先从 BOS 拉取最新 prompt 文件（P7），失败时回退到本地
`app/resources/prompts/` 目录的 `*.txt` 文件，再缺失时回退到
`prompts.py` 中的常量。

P2/P3 增强：设备/安装标准节点列表从 `get_business_dict()` 动态注入，
不再依赖 `prompts.py` 中硬编码的 `_EQUIPMENT_STANDARD_NODES` /
`_INSTALL_STANDARD_NODES`。组装由本模块 `load_prompts()` 完成。

P0-3 增强：模板支持业务词典占位符 `{{install_whitelist_md}}` /
`{{install_cross_mapping_md}}` / `{{aux_fee_keywords_inline}}` /
`{{percent_tokens_inline}}`，由 `render()` 在加载期完成替换。
通过 env `PROMPT_PLACEHOLDER_INJECT=true` 灰度开启。
"""
from __future__ import annotations

import os
import re
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


def _load_from_bos(name: str) -> str | None:
    """P7: 从 BOS 拉取最新 prompt 文件。

    环境变量：
      - PROMPT_BOS_ENABLED: 是否启用 BOS 拉取（默认 true）
      - PROMPT_BOS_BUCKET: BOS 桶名（默认 smec-ai-model-bos）
      - PROMPT_BOS_PREFIX: BOS 对象前缀（默认 models/rag/prompts_file/）

    拉取策略：在前缀下查找名为 `{name}.py` 或 `{name}.txt` 的最新文件，
    下载到临时位置后读取内容。
    """
    enabled = (os.getenv("PROMPT_BOS_ENABLED", "true") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if not enabled:
        return None

    bucket = os.getenv("PROMPT_BOS_BUCKET", "smec-ai-model-bos").strip()
    prefix = os.getenv("PROMPT_BOS_PREFIX", "models/rag/prompts_file/").strip()

    try:
        from app.config.env_config import get_bos_config
        from app.utils.bos_helper import BosHandler, HAS_BCE_SDK

        if not HAS_BCE_SDK:
            logger.debug("[prompts] BOS SDK 未安装，跳过 BOS 拉取")
            return None

        bos_config = get_bos_config()
        handler = BosHandler({
            "access_key": bos_config.get("access_key_id", ""),
            "secret_key": bos_config.get("secret_access_key", ""),
            "endpoint": bos_config.get("endpoint", ""),
            "bucket_name": bucket,
        })

        # 查找最新文件
        target_prefix = f"{prefix}{name}"
        latest = handler._find_latest_file_recursively(
            root_prefix=target_prefix,
            bucket_name=bucket,
            target_extensions=[".py", ".txt"],
        )
        if latest is None:
            logger.debug(f"[prompts] BOS 中未找到 {target_prefix}")
            return None

        # 下载到临时位置并读取
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = handler._download_bos_obj(
                latest, bucket, Path(tmpdir)
            )
            if local_path and local_path.is_file():
                content = local_path.read_text(encoding="utf-8")
                logger.info(
                    f"[prompts] 已从 BOS 拉取 {name} "
                    f"(bos_key={latest.get('key', '?')}, size={len(content)})"
                )
                return content
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[prompts] BOS 拉取 {name} 失败，将使用本地兜底: {e}")
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
    # M4：替换后若仍残留 `{{name}}`，说明有占位符未在词典中提供值；
    # 仅记录 warning，不抛错以保持兼容（可能是文档示例中误写的转义）
    leftover = re.findall(r"\{\{\s*\w+\s*\}\}", out)
    if leftover:
        logger.warning(f"[prompts.render] 未注入的双括号占位符: {sorted(set(leftover))}")
    return out


def _format_node_list(nodes: Iterable[str]) -> str:
    """将节点白名单格式化为 "1.节点A | 2.节点B | ..." 形式，与原 prompts.py 格式一致。"""
    return " | ".join(f"{i}.{n}" for i, n in enumerate(nodes, 1))


def _build_node_list_from_business_dict(bd) -> Tuple[str, str]:
    """从 BusinessDict 构建设备/安装标准节点列表文本。

    Returns:
        (equipment_nodes_str, install_nodes_str)
    """
    equip_nodes = _format_node_list(sorted(bd.equipment.payment_type_whitelist))
    install_nodes = _format_node_list(sorted(bd.install.payment_type_whitelist))
    return equip_nodes, install_nodes


def load_prompts() -> Dict[str, str]:
    """加载全部提示词。

    加载优先级（P7）：
      1. BOS 远程资产（若 PROMPT_BOS_ENABLED=true）
      2. 本地磁盘 `app/resources/prompts/*.txt`
      3. `prompts.py` 内嵌常量（兜底）

    P2/P3：设备/安装提取 prompt 使用 BusinessDict 动态注入节点列表，
    不再依赖 prompts.py 中硬编码的 _EQUIPMENT_STANDARD_NODES / _INSTALL_STANDARD_NODES。

    加载完成后对所有 prompt 统一调用 render() 注入业务词典占位符。
    """
    from app.config import prompts as _legacy

    out: Dict[str, str] = {}

    # --- P2/P3: 用 BusinessDict 动态组装设备/安装提取 prompt ---
    try:
        from app.config.business_dict import get_business_dict
        bd = get_business_dict()
        equip_nodes, install_nodes = _build_node_list_from_business_dict(bd)
        logger.info(
            f"[prompts] P2/P3: 从 BusinessDict 动态注入节点列表 "
            f"(设备={len(bd.equipment.payment_type_whitelist)}类, "
            f"安装={len(bd.install.payment_type_whitelist)}类)"
        )
    except Exception as e:
        logger.warning(f"[prompts] BusinessDict 不可用，使用 prompts.py 兜底节点列表: {e}")
        equip_nodes = None
        install_nodes = None

    for name in _PROMPT_NAMES:
        # P7: BOS 优先
        from_bos = _load_from_bos(name)
        if from_bos is not None:
            raw = from_bos
            logger.info(f"[prompts] 已从 BOS 加载 {name}")
            # BOS prompt 已包含完整内容（含节点列表），跳过 P2/P3 重新组装
        else:
            # 本地磁盘
            from_disk = _load_from_disk(name)
            if from_disk is not None:
                raw = from_disk
                logger.debug(f"[prompts] 已从磁盘加载 {name}")
            else:
                raw = getattr(_legacy, name)

            # P2/P3: 对设备/安装提取 prompt，用 BusinessDict 节点列表重新组装
            # 仅在非 BOS 来源时执行，避免覆盖 BOS 已注入的完整 prompt
            if equip_nodes is not None and name == "EQUIPMENT_PAYMENT_RATIO_PROMPT":
                raw = _legacy._PAYMENT_RATIO_PROMPT_COMMON.format(
                    standard_node_list=equip_nodes,
                    output_examples=_legacy._EQUIPMENT_OUTPUT_EXAMPLES,
                    judgement=_legacy._EQUIPMENT_JUDGEMENT,
                )
            elif install_nodes is not None and name == "INSTALL_PAYMENT_RATIO_PROMPT":
                raw = _legacy._PAYMENT_RATIO_PROMPT_COMMON.format(
                    standard_node_list=install_nodes,
                    output_examples=_legacy._INSTALL_OUTPUT_EXAMPLES,
                    judgement=_legacy._INSTALL_JUDGEMENT,
                )

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
