# app/config/env_config.py

import os
import warnings
from typing import Dict, Any, List, Optional, Tuple
from functools import lru_cache

from app.config.business_dict import get_business_dict


# --- 条款分类中英文名称归一化映射 ---
# 用于统一表达 paragraph.clause_class 中可能出现的中英文条款标签
CLAUSE_CLASS_MAPPING: Dict[str, set] = {
    "equipment_payment": {"equipment_payment", "设备付款条款"},
    "installation_payment": {"installation_payment", "安装付款条款"},
    "mixed_payment": {"mixed_payment", "混签付款条款"},
    "warranty": {"warranty", "质保期条款"},
}


def normalize_clause_class(cc: str) -> Optional[str]:
    """将中/英文条款分类名称归一化为英文规范 key。

    未命中任何已知分类时返回 None；调用方自行决定默认值或告警策略。
    """
    if not cc:
        return None
    s = str(cc).strip()
    for canonical, aliases in CLAUSE_CLASS_MAPPING.items():
        if s in aliases:
            return canonical
    # 兼容：包含关系（如 "设备付款条款（含安装）"）按子串命中一次
    for canonical, aliases in CLAUSE_CLASS_MAPPING.items():
        for alias in aliases:
            if alias and alias in s:
                return canonical
    return None


# --- 安装侧 payment_type 白名单与跨类映射 ---
# 真相已迁移到 app/resources/business_dict/v1.yaml；此处仅保留 enforce_* API 与
# 兼容性懒代理（旧调用方 import INSTALL_PAYMENT_TYPE_WHITELIST 时仍可获得只读副本，
# 但会触发 DeprecationWarning）。

def enforce_install_payment_type(payment_type: Optional[str]) -> Tuple[Optional[str], str]:
    """对安装侧 payment_type 做白名单校验与跨类映射兜底。

    返回 (normalized_payment_type, action)，action ∈ {"kept", "mapped", "missing", "dropped"}：
    - "kept"    : 已在白名单内，原样保留
    - "mapped"  : 命中跨类映射表，已转换为合法节点
    - "missing" : 输入为 None / 空字符串（H4 修复：与白名单失败语义区分），
                  调用方应保留节点本体，仅对 payment_type 字段记录 WARNING
    - "dropped" : 既不在白名单也无法映射，调用方应丢弃该节点
    """
    if payment_type is None:
        return None, "missing"
    pt = str(payment_type).strip()
    if not pt:
        return None, "missing"
    install_cfg = get_business_dict().install
    whitelist = install_cfg.payment_type_whitelist
    cross = install_cfg.cross_mapping
    # 1) 已是合法节点
    if pt in whitelist:
        return pt, "kept"
    # 2) 命中跨类映射
    if pt in cross:
        mapped = cross[pt]
        # 二次校验：映射目标必须在白名单内（loader 已强校验，此处兜底）
        if mapped in whitelist:
            return mapped, "mapped"
    # 3) 兜底丢弃
    return pt, "dropped"


# --- 兼容性懒代理：旧调用方 from app.config.env_config import INSTALL_PAYMENT_TYPE_WHITELIST ---
_DEPRECATED_NAMES = {
    "INSTALL_PAYMENT_TYPE_WHITELIST",
    "INSTALL_PAYMENT_TYPE_CROSS_MAPPING",
    "_DEFAULT_CLAUSE_FILTER_KEYWORDS",
}


def __getattr__(name: str):  # PEP 562
    if name == "INSTALL_PAYMENT_TYPE_WHITELIST":
        warnings.warn(
            "INSTALL_PAYMENT_TYPE_WHITELIST 已迁移至业务词典，请改用 "
            "app.config.business_dict.get_business_dict().install.payment_type_whitelist",
            DeprecationWarning, stacklevel=2,
        )
        return set(get_business_dict().install.payment_type_whitelist)
    if name == "INSTALL_PAYMENT_TYPE_CROSS_MAPPING":
        warnings.warn(
            "INSTALL_PAYMENT_TYPE_CROSS_MAPPING 已迁移至业务词典，请改用 "
            "app.config.business_dict.get_business_dict().install.cross_mapping",
            DeprecationWarning, stacklevel=2,
        )
        return dict(get_business_dict().install.cross_mapping)
    if name == "_DEFAULT_CLAUSE_FILTER_KEYWORDS":
        warnings.warn(
            "_DEFAULT_CLAUSE_FILTER_KEYWORDS 已迁移至业务词典 clause_filter.default_keywords",
            DeprecationWarning, stacklevel=2,
        )
        return list(get_business_dict().clause_filter_default_keywords)
    raise AttributeError(f"module 'app.config.env_config' has no attribute {name!r}")

# --- Helper Functions to safely get typed values from environment variables ---
def _get(key: str, default: Any = None) -> Any:
    return os.getenv(key, default)

def _get_bool(key: str, default: bool = False) -> bool:
    val = _get(key, str(default)).lower()
    return val in ('true', '1', 'yes', 'on')

def _get_int(key: str, default: int = 0) -> int:
    try:
        return int(_get(key, str(default)))
    except (ValueError, TypeError):
        return default

def _get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(_get(key, str(default)))
    except (ValueError, TypeError):
        return default


# =============================================================================
# H6 修复：必填 env 启动期 fail-closed 校验
# =============================================================================
# prod 模式严格校验全量必填项；dev 模式仅要求主 LLM 三件套，避免本地无 Milvus / BOS 时无法启动。
_REQUIRED_ENV_PROD: Tuple[str, ...] = (
    "LLM_API_BASE", "LLM_MODEL",
    "BOS_ACCESS_KEY", "BOS_SECRET_KEY", "BOS_ENDPOINT",
    "RAG_MILVUS_URI", "RAG_COLLECTION_NAME", "RAG_COLLECTION_NAME_INSTALLATION",
    "RAG_REMOTE_EMBEDDING_URL", "RAG_REMOTE_EMBEDDING_KEY",
)
_REQUIRED_ENV_DEV: Tuple[str, ...] = (
    "LLM_API_BASE", "LLM_MODEL",
)


def assert_required_env() -> None:
    """启动期断言必填 env 已配置；缺失即抛 RuntimeError，由 lifespan 捕获后退出。

    - production 模式：校验全量 11 项（LLM / BOS / Milvus / Embedding）
    - 其它模式：仅校验主 LLM 三件套
    """
    env_mode = (_get("ENVIRONMENT", "development") or "development").strip().lower()
    required = _REQUIRED_ENV_PROD if env_mode == "production" else _REQUIRED_ENV_DEV
    missing = [k for k in required if not (_get(k) and str(_get(k)).strip())]
    if missing:
        raise RuntimeError(
            f"必填环境变量缺失（mode={env_mode}）：{missing}；请检查 .env 或部署配置"
        )



# --- Configuration Loading Functions ---
# Each function is responsible for loading a specific part of the config from .env
# Using @lru_cache(maxsize=1) ensures that .env is read only once.

@lru_cache(maxsize=1)
def get_app_level_config() -> Dict[str, Any]:
    """获取应用级别的配置。"""
    return {
        'environment': _get('ENVIRONMENT', 'development'),
    }

@lru_cache(maxsize=1)
def get_llm_config() -> Dict[str, Any]:
    """获取LLM的配置，覆盖代码中的默认值。"""
    config = {}
    if _get('LLM_MODEL_NAME'): config['model_name'] = _get('LLM_MODEL_NAME')
    if _get('LLM_TEMPERATURE'): config['temperature'] = _get_float('LLM_TEMPERATURE')
    if _get('LLM_MAX_TOKENS'): config['max_tokens'] = _get_int('LLM_MAX_TOKENS')
    if _get('LLM_MAX_OUTPUT_TOKENS'): config['max_output_tokens'] = _get_int('LLM_MAX_OUTPUT_TOKENS')
    if _get('LLM_API_BASE'): config['api_base'] = _get('LLM_API_BASE')
    # api_key 允许为空：百舸等无鉴权网关留空即可，下游会传占位符
    config['api_key'] = _get('LLM_API_KEY', '') or ''
    if _get('LLM_MODEL'): config['model'] = _get('LLM_MODEL')
    return config

@lru_cache(maxsize=1)
def get_payment_ratio_llm_config() -> Dict[str, Any]:
    """获取专用于支付比例提取的LLM配置，如果未配置则返回空字典。"""
    config = {}
    # 检查是否有任何专用的payment ratio配置
    has_payment_ratio_config = False
    # 优先使用专用的payment ratio配置
    if _get('PAYMENT_RATIO_LLM_MODEL_NAME'): 
        config['model_name'] = _get('PAYMENT_RATIO_LLM_MODEL_NAME')
        has_payment_ratio_config = True
    if _get('PAYMENT_RATIO_LLM_MODEL'): 
        config['model'] = _get('PAYMENT_RATIO_LLM_MODEL')
        has_payment_ratio_config = True
    if _get('PAYMENT_RATIO_LLM_TEMPERATURE'): 
        config['temperature'] = _get_float('PAYMENT_RATIO_LLM_TEMPERATURE')
        has_payment_ratio_config = True
    if _get('PAYMENT_RATIO_LLM_API_BASE'): 
        config['api_base'] = _get('PAYMENT_RATIO_LLM_API_BASE')
        has_payment_ratio_config = True
    if _get('PAYMENT_RATIO_LLM_API_KEY'): 
        config['api_key'] = _get('PAYMENT_RATIO_LLM_API_KEY')
        has_payment_ratio_config = True
    if _get('PAYMENT_RATIO_LLM_MAX_TOKENS'): 
        config['max_tokens'] = _get_int('PAYMENT_RATIO_LLM_MAX_TOKENS')
        has_payment_ratio_config = True
    if _get('PAYMENT_RATIO_LLM_MAX_OUTPUT_TOKENS'): 
        config['max_output_tokens'] = _get_int('PAYMENT_RATIO_LLM_MAX_OUTPUT_TOKENS')
        has_payment_ratio_config = True
    
    # 如果没有任何专用配置，返回空字典（表示使用默认配置）
    if not has_payment_ratio_config:
        return {}
    
    # 如果有专用配置，用默认配置补全缺失项
    default_config = get_llm_config()
    for key, value in default_config.items():
        if key not in config:
            config[key] = value
            
    return config

@lru_cache(maxsize=1)
def get_bos_config() -> Dict[str, Any]:
    """获取BOS的配置。"""
    return {
        # 通用模型下载配置
        'access_key_id': _get('BOS_ACCESS_KEY'),
        'secret_access_key': _get('BOS_SECRET_KEY'),
        'endpoint': _get('BOS_ENDPOINT'),
        'default_bucket_name': _get('BOS_BUCKET_NAME', 'smec-ai-model-bos'), # 模型桶

        # 合同文件源配置
        'contract_source_bucket': _get('CONTRACT_SOURCE_BOS_BUCKET'),
        'contract_path_template': _get('CONTRACT_SOURCE_BOS_PATH_TEMPLATE', 'smec-contract/{file_id}')
    }

@lru_cache(maxsize=1)
def get_processing_config() -> Dict[str, Any]:
    """
    获取文本处理相关的配置，并支持通过环境变量覆盖模型路径。
    这允许通过 .env 文件来控制模型版本，而无需修改代码。
    """
    # 初始化一个完整的嵌套结构，以防止 KeyError
    config = {
        "rag": {},
    }

    # --- RAG (Embedding Model) 路径覆盖 ---
    if _get('RAG_EMBEDDING_DIMENSION'):
        config['rag']['embedding_dimension'] = _get_int('RAG_EMBEDDING_DIMENSION')

    # --- RAG (Rerank Model) 模型路径覆盖 ---
    if _get('RAG_RERANK_LOCAL_DIR'):
        config['rag']['rerank_local_dir_name'] = _get('RAG_RERANK_LOCAL_DIR')
    if _get('RAG_RERANK_BOS_BUCKET'):
        config['rag']['rerank_bos_bucket_name'] = _get('RAG_RERANK_BOS_BUCKET')
    if _get('RAG_RERANK_BOS_REMOTE_DIR'):
        config['rag']['rerank_bos_remote_dir'] = _get('RAG_RERANK_BOS_REMOTE_DIR')
        
    # --- RAG (BM25 Data) 路径覆盖 ---
    if _get('RAG_BM25_LOCAL_DIR'):
        config['rag']['bm25_local_dir_name'] = _get('RAG_BM25_LOCAL_DIR')
    if _get('RAG_BM25_BOS_BUCKET'):
        config['rag']['bm25_bos_bucket_name'] = _get('RAG_BM25_BOS_BUCKET')
    if _get('RAG_BM25_BOS_REMOTE_DIR'):
        config['rag']['bm25_bos_remote_dir'] = _get('RAG_BM25_BOS_REMOTE_DIR')

    if _get('RAG_BM25_LOCAL_DIR_INSTALLATION'):
        config['rag']['bm25_local_dir_name_installation'] = _get('RAG_BM25_LOCAL_DIR_INSTALLATION')
    if _get('RAG_BM25_BOS_BUCKET_INSTALLATION'):
        config['rag']['bm25_bos_bucket_name_installation'] = _get('RAG_BM25_BOS_BUCKET_INSTALLATION')
    if _get('RAG_BM25_BOS_REMOTE_DIR_INSTALLATION'):
        config['rag']['bm25_bos_remote_dir_installation'] = _get('RAG_BM25_BOS_REMOTE_DIR_INSTALLATION')

    # --- 读取所有其他的 RAG 配置 ---
    if _get('RAG_COLLECTION_NAME'):
        config['rag']['collection_name'] = _get('RAG_COLLECTION_NAME')
    if _get('RAG_COLLECTION_NAME_INSTALLATION'):
        config['rag']['collection_name_installation'] = _get('RAG_COLLECTION_NAME_INSTALLATION')
    if _get('USE_BM25') is not None: # 检查 is not None 以允许设置 USE_BM25=false
        config['rag']['use_bm25'] = _get_bool('USE_BM25')
    if _get('USE_VECTOR') is not None:
        config['rag']['use_vector'] = _get_bool('USE_VECTOR')
    if _get('USE_RERANK') is not None:
        config['rag']['use_rerank'] = _get_bool('USE_RERANK')
    if _get('RAG_TOP_K'):
        config['rag']['top_k'] = _get_int('RAG_TOP_K')
        
    if _get('RAG_MILVUS_HOST'):
        config['rag']['milvus_host'] = _get('RAG_MILVUS_HOST')
    if _get('RAG_MILVUS_PORT'):
        config['rag']['milvus_port'] = _get_int('RAG_MILVUS_PORT')
    if _get('RAG_MILVUS_URI'):
        config['rag']['milvus_uri'] = _get('RAG_MILVUS_URI')
    if _get('RAG_SEARCH_METRIC'):
        config['rag']['search_metric_type'] = _get('RAG_SEARCH_METRIC')
        
    if _get('RAG_USE_REMOTE_EMBEDDING') is not None:
        config['rag']['use_remote_embedding'] = _get_bool('RAG_USE_REMOTE_EMBEDDING')
    if _get('RAG_REMOTE_EMBEDDING_URL'):
        config['rag']['remote_embedding_api_url'] = _get('RAG_REMOTE_EMBEDDING_URL')
    if _get('RAG_REMOTE_EMBEDDING_KEY'):
        config['rag']['remote_embedding_api_key'] = _get('RAG_REMOTE_EMBEDDING_KEY')
    if _get('RAG_REMOTE_EMBEDDING_MODEL'):
        config['rag']['remote_embedding_model_name'] = _get('RAG_REMOTE_EMBEDDING_MODEL')

    return config


@lru_cache(maxsize=1)
def get_rag_thresholds() -> Dict[str, float]:
    """RAG 召回阈值（I9）。可由 env 覆盖。"""
    return {
        "bm25_min_score": _get_float("RAG_BM25_MIN_SCORE", 20.0),
        "vector_min_distance": _get_float("RAG_VECTOR_MIN_DISTANCE", 0.5),
    }


@lru_cache(maxsize=1)
def get_dedupe_thresholds() -> Dict[str, float]:
    """去重 / 上下文合并阈值（I9）。"""
    return {
        "context_similarity": _get_float("DEDUPE_CONTEXT_SIM", 0.85),
        "context_overlap_chars": float(_get_int("DEDUPE_CONTEXT_OVERLAP", 20)),
        "item_similarity_strict": _get_float("DEDUPE_ITEM_SIM_STRICT", 0.95),
        "item_similarity_loose": _get_float("DEDUPE_ITEM_SIM_LOOSE", 0.8),
    }


_DEFAULT_CLAUSE_FILTER_KEYWORDS_FALLBACK = ("违约金", "罚款", "赔偿损失", "质保期保养预留费")


@lru_cache(maxsize=1)
def get_failure_rate_threshold() -> float:
    """H2 修复：抽取阶段失败率阈值。

    当 RAG/LLM 失败段落比例超过此阈值时，节点会把 ``extraction_partial=True``
    写入 state，API 层据此返回 503 而非 200。默认 0.5。
    """
    raw = _get("EXTRACTION_FAILURE_RATE_THRESHOLD")
    if raw is None or str(raw).strip() == "":
        return 0.5
    try:
        v = float(raw)
        if 0.0 < v <= 1.0:
            return v
    except (TypeError, ValueError):
        pass
    return 0.5


@lru_cache(maxsize=1)
def get_clause_filter_keywords() -> List[str]:
    """
    从环境变量 CLAUSE_FILTER_KEYWORDS 读取预过滤关键词列表（逗号分隔）。
    未设置时使用业务词典 clause_filter.default_keywords；显式设置为空则不过滤。
    """
    raw = _get('CLAUSE_FILTER_KEYWORDS')
    if raw is None:
        try:
            return list(get_business_dict().clause_filter_default_keywords)
        except Exception:
            # 业务词典加载失败时使用 hardcoded fallback，保证服务可启动
            return list(_DEFAULT_CLAUSE_FILTER_KEYWORDS_FALLBACK)
    keywords = [kw.strip() for kw in raw.split(',') if kw.strip()]
    return keywords


@lru_cache(maxsize=1)
def get_negotiation_reject_keywords() -> List[str]:
    """Fix-1：协商被拒关键词列表。

    若条款的 clause_context 中命中任一关键词，则该条款应被前置过滤。
    优先级：env CLAUSE_NEGOTIATION_REJECT_KEYWORDS > 业务词典 > 空列表（关闭规则）。
    显式设置为空字符串可关闭规则，方便回滚。
    """
    raw = _get('CLAUSE_NEGOTIATION_REJECT_KEYWORDS')
    if raw is None:
        try:
            return list(get_business_dict().clause_filter_negotiation_reject_keywords)
        except Exception:
            return []
    keywords = [kw.strip() for kw in raw.split(',') if kw.strip()]
    return keywords