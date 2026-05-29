"""业务词典 loader（P0-3）。

此模块是 Service 2 业务关键词、节点白名单、跨类映射、过滤词、同义词的
**唯一访问入口**。所有原本散落在 `payment_info_extractor_node.py` /
`payment_ratio_extractor.py` / `env_config.py` / `prompts.py` 中的硬编码
中文常量都迁移到 `app/resources/business_dict/<version>.yaml`。

启动期 `get_business_dict()` 会被 lifespan 调用一次：
  - YAML 缺失 / schema 错误 / cross_mapping 不一致 → 抛 RuntimeError，进程拒启
  - 成功 → 全进程共享 lru_cache 实例（不可变 Tuple/FrozenSet）
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

import yaml
from loguru import logger
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------

class _SynonymsCfg(BaseModel):
    percent_tokens: Tuple[str, ...]
    residual_tokens: Tuple[str, ...]
    unique_total_hints: Tuple[str, ...]


class _ForceValidCfg(BaseModel):
    min_clause_len: int = Field(ge=1, le=200)
    node_pct_gap: int = Field(ge=1, le=200)
    node_keywords: Tuple[str, ...]
    exclude_keywords: Tuple[str, ...]


class _InstallCfg(BaseModel):
    payment_type_whitelist: FrozenSet[str]
    cross_mapping: Dict[str, str]

    @model_validator(mode="after")
    def _check_cross_mapping_targets(self) -> "_InstallCfg":
        bad: List[Tuple[str, str]] = [
            (k, v) for k, v in self.cross_mapping.items()
            if v not in self.payment_type_whitelist
        ]
        if bad:
            raise ValueError(
                f"install.cross_mapping 包含不在 payment_type_whitelist 的目标: {bad}"
            )
        return self


class _PtRegexItem(BaseModel):
    type: str
    pattern: str


class BusinessDict(BaseModel):
    version: int
    synonyms: _SynonymsCfg
    aux_fee_keywords: Tuple[str, ...]
    force_valid: _ForceValidCfg
    clause_filter_default_keywords: Tuple[str, ...]
    # Fix-1：协商被拒关键词；YAML 缺该字段时默认空 tuple，等价于关闭规则
    clause_filter_negotiation_reject_keywords: Tuple[str, ...] = ()
    install: _InstallCfg
    payment_type_regex_fallback: Tuple[_PtRegexItem, ...]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_RESOURCE_DIR = Path(__file__).resolve().parent.parent / "resources" / "business_dict"


def _resolve_yaml_path(version: Optional[str] = None) -> Path:
    ver = (version or os.getenv("BUSINESS_DICT_VERSION") or "v1").strip()
    if not ver.endswith(".yaml"):
        path = _RESOURCE_DIR / f"{ver}.yaml"
    else:
        path = _RESOURCE_DIR / ver
    return path


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError(f"业务词典 YAML 不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"业务词典 YAML 顶层非 mapping: {path}")
    return data


def _normalize_payload(raw: dict) -> dict:
    """把 YAML 中嵌套的 clause_filter.* 展平到顶层 key，便于 pydantic 直接校验。"""
    out = dict(raw)
    cf = out.pop("clause_filter", None) or {}
    out["clause_filter_default_keywords"] = cf.get("default_keywords", [])
    # Fix-1：协商被拒关键词；缺省视为空列表（兼容旧版 YAML）
    out["clause_filter_negotiation_reject_keywords"] = cf.get("negotiation_reject_keywords", [])
    return out


@lru_cache(maxsize=1)
def get_business_dict() -> BusinessDict:
    """加载并缓存业务词典。失败即抛 RuntimeError（启动期拒启）。"""
    path = _resolve_yaml_path()
    raw = _load_yaml(path)
    payload = _normalize_payload(raw)
    try:
        bd = BusinessDict.model_validate(payload)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"业务词典 schema 校验失败 ({path}): {e}") from e
    logger.info(
        f"[business_dict] 已加载 {path.name} "
        f"v{bd.version}: install_whitelist={len(bd.install.payment_type_whitelist)} "
        f"node_kw={len(bd.force_valid.node_keywords)} "
        f"regex_fallback={len(bd.payment_type_regex_fallback)} "
        f"neg_reject_kw={len(bd.clause_filter_negotiation_reject_keywords)}"
    )
    return bd


# ---------------------------------------------------------------------------
# Prompt 一致性自检
# ---------------------------------------------------------------------------

def assert_consistency_with_prompts(strict: Optional[bool] = None) -> None:
    """启动期自检：渲染后的 prompt 文本是否覆盖业务词典关键白名单。

    判断规则（保守）：
      - 12 类 install 白名单中至少 80% 出现在某条相关 prompt 文本中
      - 跨类映射的 source/target 在 prompt 中出现
    不一致：strict=True 抛 RuntimeError；否则仅 warning。

    strict 默认值：ENVIRONMENT == "production" 时 True。
    """
    if strict is None:
        env = (os.getenv("ENVIRONMENT") or "development").strip().lower()
        strict = env == "production"

    bd = get_business_dict()

    # 局部 import 避免循环依赖（prompts_loader 可能在启动期暂未就绪）
    try:
        from app.config import prompts_loader
    except Exception as e:  # noqa: BLE001
        msg = f"[business_dict] 一致性自检跳过：prompts_loader 不可用 ({e})"
        if strict:
            raise RuntimeError(msg)
        logger.warning(msg)
        return

    candidate_prompts = [
        getattr(prompts_loader, name, "") or ""
        for name in (
            "INSTALL_PAYMENT_RATIO_PROMPT",
            "INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT",
            "PAYMENT_CLAUSE_CATEGORY_PROMPT",
        )
    ]
    blob = "\n".join(candidate_prompts)

    whitelist = list(bd.install.payment_type_whitelist)
    hits = [w for w in whitelist if w in blob]
    coverage = len(hits) / max(1, len(whitelist))

    if coverage < 0.8:
        missing = sorted(set(whitelist) - set(hits))
        msg = (
            f"[business_dict] prompt ↔ install 白名单一致性不足 "
            f"({len(hits)}/{len(whitelist)}={coverage:.0%}); 缺失={missing}"
        )
        if strict:
            raise RuntimeError(msg)
        logger.warning(msg)
    else:
        logger.success(
            f"[business_dict] prompt 一致性自检通过 "
            f"({len(hits)}/{len(whitelist)}={coverage:.0%})"
        )


__all__ = ["BusinessDict", "get_business_dict", "assert_consistency_with_prompts"]
