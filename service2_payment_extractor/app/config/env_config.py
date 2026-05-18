# app/config/env_config.py

import os
from typing import Dict, Any, List, Optional, Tuple
from functools import lru_cache


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


# --- 安装侧 payment_type 白名单与跨类映射（代码层强制兜底，避免 LLM 透传非法节点） ---
# 安装合同标准节点（12 类）：与 prompts.py 的 _INSTALL_STANDARD_NODES 对齐
INSTALL_PAYMENT_TYPE_WHITELIST: set = {
    "定金",
    "进场前（首付）",
    "进场后",
    "移交前",
    "报验前",
    "公司验收后",
    "当地政府部门验收后",
    "电梯移交用户后",
    "工程整体竣工",
    "特殊付款-移交前",
    "特殊付款-移交后",
    "质保金",
}

# 设备/其他节点 → 安装侧节点的强制映射（保持与 prompts.py 跨类映射规则一致）
INSTALL_PAYMENT_TYPE_CROSS_MAPPING: Dict[str, str] = {
    "预付款": "定金",
    "销售定金": "定金",
    "提货款": "进场前（首付）",
    "货到工地": "进场前（首付）",
    "货到工地款": "进场前（首付）",
    "出货款": "进场前（首付）",
    "结算完成": "工程整体竣工",
    # 含别写括号变体的兜底归一化
    "进场前(首付)": "进场前（首付）",
    "特殊付款—移交前": "特殊付款-移交前",
    "特殊付款—移交后": "特殊付款-移交后",
}


def enforce_install_payment_type(payment_type: Optional[str]) -> Tuple[Optional[str], str]:
    """对安装侧 payment_type 做白名单校验与跨类映射兜底。

    返回 (normalized_payment_type, action)，action ∈ {"kept", "mapped", "dropped"}：
    - "kept"   : 已在 12 类白名单内，原样保留
    - "mapped" : 命中跨类映射表，已转换为合法节点
    - "dropped": 既不在白名单也无法映射，调用方应丢弃该节点
    """
    if payment_type is None:
        return None, "dropped"
    pt = str(payment_type).strip()
    if not pt:
        return None, "dropped"
    # 1) 已是合法节点
    if pt in INSTALL_PAYMENT_TYPE_WHITELIST:
        return pt, "kept"
    # 2) 命中跨类映射
    if pt in INSTALL_PAYMENT_TYPE_CROSS_MAPPING:
        mapped = INSTALL_PAYMENT_TYPE_CROSS_MAPPING[pt]
        # 二次校验：映射目标必须在白名单内
        if mapped in INSTALL_PAYMENT_TYPE_WHITELIST:
            return mapped, "mapped"
    # 3) 兜底丢弃
    return pt, "dropped"

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
    if _get('LLM_API_KEY'): config['api_key'] = _get('LLM_API_KEY')
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


_DEFAULT_CLAUSE_FILTER_KEYWORDS = ["违约金", "罚款", "赔偿损失"]

@lru_cache(maxsize=1)
def get_clause_filter_keywords() -> List[str]:
    """
    从环境变量 CLAUSE_FILTER_KEYWORDS 读取预过滤关键词列表（逗号分隔）。
    未设置时使用内置默认值；显式设置为空则不过滤。
    """
    raw = _get('CLAUSE_FILTER_KEYWORDS')
    if raw is None:
        return list(_DEFAULT_CLAUSE_FILTER_KEYWORDS)
    keywords = [kw.strip() for kw in raw.split(',') if kw.strip()]
    return keywords