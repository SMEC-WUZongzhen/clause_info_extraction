# app/config/config.py

import hashlib
import os
import sys
from pathlib import Path
from typing import Optional, Any
import tiktoken
from tiktoken.core import Encoding
import asyncio
import aiofiles.os as aio_os
import pickle

from loguru import logger
from deepmerge import always_merger
from rank_bm25 import BM25Okapi

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# --- 1. 环境变量加载 ---
try:
    from dotenv import load_dotenv, find_dotenv

    # 优先从环境变量指定的 CONFIG_DIR 目录加载 .env 文件
    config_dir_env = os.getenv('CONFIG_DIR')
    dotenv_path = None

    if config_dir_env:
        # 如果指定了目录，则构建 .env 文件的完整路径
        env_path_in_config_dir = Path(config_dir_env) / '.env'
        if env_path_in_config_dir.is_file():
            dotenv_path = env_path_in_config_dir
            logger.info(f"Found .env in specified CONFIG_DIR: {dotenv_path}")
        else:
            logger.warning(f"CONFIG_DIR '{config_dir_env}' is set, but .env file not found there.")

    # 如果没有从指定目录加载，则回退到原始的自动查找方式（用于本地开发）
    if not dotenv_path:
        dotenv_path = find_dotenv()
        if dotenv_path:
            logger.info(f"Falling back to auto-detected .env file: {dotenv_path}")

    if dotenv_path:
        logger.info(f"Application starting: Loading environment variables from {dotenv_path}")
        load_dotenv(dotenv_path=dotenv_path, override=False)
    else:
        logger.warning("No .env file loaded. Using environment variables from the system.")

except ImportError:
    logger.warning("`python-dotenv` not installed. Cannot load .env file.")

# --- 并发安全设置 ---
try:
    import torch
    torch.set_num_threads(1)
    logger.info(f"PyTorch线程数已强制设置为 1，以避免并发死锁。")
except ImportError:
    logger.debug("PyTorch未安装，跳过线程数设置。")

# --- 2. 全局环境设置 ---
if "TOKENIZERS_PARALLELISM" not in os.environ:
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

# --- 3. 导入必要的模块 ---
from transformers import AutoTokenizer, AutoModel, AutoModelForSequenceClassification
from pymilvus import MilvusClient
from app.config.config_models import AppSettings
from app.utils.bos_helper import BosHandler
from app.config.env_config import (
    get_app_level_config, get_llm_config,get_payment_ratio_llm_config, get_bos_config, get_processing_config
)
from app.config.prompts_loader import (
    PAYMENT_RATIO_PROMPT,
    PAYMENT_SUMMARY_RATIO_PROMPT
)

def get_default_config_dict() -> dict:
    """定义代码级别的默认配置。"""
    base_config = {
        "llm": {}, # 由 env_config 填充
        "text_processing": {
            "fine_grained_parser_thresholds": {
                "payment_confidence_threshold": 0.7,  # 降低阈值，减少误过滤
                "top_n_warranty_clauses": 2 
            },
            "chunking": {
                "dynamic_chunking_safe_margin": 0.8,
                "token_to_char_ratio_heuristic": 3.0,
                "default_chunk_size": 4096,
                "default_overlap_ratio": 0.1,
            },
            "llm_prompts": {
                "payment_ratio_extraction": PAYMENT_RATIO_PROMPT,
                "payment_summary_ratio": PAYMENT_SUMMARY_RATIO_PROMPT
            },
            "structural_patterns": {
                "clause_marker_patterns": [
                    r"^\s*第\s*[一二三四五六七八九十百千万零]+\s*条",
                    r"^\s*\d+[\.\d+]*",
                    r"^\s*[（\(]\s*[一二三四五六七八九十百千万零\d]+\s*[）\)]",
                    r"^\s*[一二三四五六七八九十百千万零]+\s*、",
                    r"^\s*\d+\s*、",
                    r"^\s*[a-zA-Z]\s*[\.、\)]",
                    r"^\s*[-*•]\s+",
                    r"^\s*\d+\s*\)",
                    r"^\s*(附件|附图|附表)\s*\d+\s*[:：]?"
                ],
                "title_like_patterns": [
                    r"^##+\s+.*", r"^第[一二三四五六七八九十百千万零]+章",
                    r"^\s*(风险提示|法律适用|争议解决|保密条款|不可抗力|违约责任|知识产权|通知与送达|附则|定义|鉴于|声明|前言)",
                    r"^\s*[\u4e00-\u9fa5]{2,8}[：:]\s*$",
                ],
            },
            "rag": {
                "rerank_local_dir_name": "bge-reranker-large",
                "rerank_bos_bucket_name": "smec-ai-model-bos",
                "rerank_bos_remote_dir": "models/rag/bge-reranker-large",

                # 提供硬编码的默认值
                "bm25_local_dir_name": "bm25_pickle/bm25_pkl_816.pkl",
                "bm25_bos_bucket_name": "smec-ai-model-bos",
                "bm25_bos_remote_dir": "models/rag/bm25_pickle/bm25_pkl_816.pkl",

                "bm25_local_dir_name_installation": "bm25_pickle/bm25_pkl_installation_v1.pkl",
                "bm25_bos_bucket_name_installation": "smec-ai-model-bos",
                "bm25_bos_remote_dir_installation": "models/rag/bm25_pickle/bm25_pkl_installation_v1.pkl",

                # 提供合理的 None 或 False 作为默认值
                "collection_name": None,
                "collection_name_installation": None,
                "use_bm25": False, 
                "use_vector": True, # 假设向量检索是默认开启的
                "use_rerank": False, 
                "top_k": 5,
                
                "milvus_host": None,
                "milvus_port": None,
                "milvus_uri": None,
                "search_metric_type": "COSINE",
                "output_fields": ["payment_type", "clause_text", "payment_ratio", "clause_context"],
                "output_fields_installation": ["payment_type", "clause_text", "payment_ratio", "payment_amount","clause_context"],
                "use_remote_embedding": True, # 假设远程是默认
                "remote_embedding_api_url": "https://qianfan.baidubce.com/v2/embeddings",
                "remote_embedding_api_key": None,
                "remote_embedding_model_name": "qwen3-embedding-0.6b"
            },
            # 添加 ground_truth 的默认配置
            "ground_truth": {
                "bos_bucket_name": "smec-ai-model-bos",
                "bos_remote_path": "data/ground_truth/payment_nodes.csv"
            },
            # 比对节点的配置，包含近义词词典
            "comparison": {
                "match_threshold": 85, # 模糊匹配的相似度阈值
                "alias_rules": [
                    {
                        "name": "标准层节点别名",
                        "mapping": {
                            "当地政府部门验收后": ["当地政府验收", "当地政府验收后", "当地政府部门验收", "当地政府部门验收后", "政府部门验收后", "技监部门验收后", "验收合格后付款"],
                            "销售定金": ["销售定金", "合同定金", "定金"],
                            "预付款": ["排产款"],
                            "安装后": ["安装后", "安装完成后", "安装完后","安装完毕"],
                            "结算完成": ["结算完成", "结算完成后", "结算完后"],
                            "质保金": ["质保金一年","质保金两年", "质保金1年","质保金2年", "质保金 （2 年）", "质保金（2年）", "质保金 (2年)", "质保金(2年)","质保金 （1 年）", "质保金（1年）", "质保金 (1年)", "质保金(1年)","特殊约定付款-质保期", "特殊（质保期）"],
                            "电梯移交用户后": ["电梯移交用户", "电梯移交用户后"],
                            "特殊约定付款-移交前": ["特殊约定付款-移交前", "特殊（移交前）"],
                            "特殊约定付款-移交后": ["特殊约定付款-移交后", "特殊（移交后）"],
                            "进场前（首付）": ["进场前", "进场前（首付）","进场前(首付)"],
                            "公司验收后": ["公司验收后", "公司验收"],
                        }
                    }
                ]
            }
        }
    }
    import copy
    return copy.deepcopy(base_config)

def load_config() -> AppSettings:
    """加载、合并并验证全局应用配置，优先使用.env中的值。"""
    logger.info("开始加载和合并应用配置...")
    
    # 1. 获取代码中定义的默认配置
    config_dict = get_default_config_dict()
    
    # 2. 获取环境变量中定义的配置
    env_app_config = get_app_level_config()
    env_llm_config = get_llm_config()
    payment_ratio_llm_config = get_payment_ratio_llm_config()
    env_processing_config = get_processing_config() # 这个现在包含了模型路径

    # 3. 使用 deepmerge 深度合并配置
    #    always_merger 会递归地合并字典，.env 中的值会覆盖默认值。
    config_dict = always_merger.merge(config_dict, env_app_config)
    config_dict['llm'] = always_merger.merge(config_dict.get('llm', {}), env_llm_config)
    config_dict['payment_ratio_llm'] = always_merger.merge(config_dict.get('payment_ratio_llm', {}), payment_ratio_llm_config)
    config_dict['text_processing'] = always_merger.merge(
        config_dict.get('text_processing', {}), 
        env_processing_config
    )

    try:
        # 4. 使用Pydantic进行最终验证
        settings = AppSettings.model_validate(config_dict)
        logger.success("应用配置加载并验证成功！")
        
        return settings
    except Exception as e:
        logger.error(f"应用配置验证失败: {e}", exc_info=True)
        raise

# 创建全局唯一的配置实例
APP_CONFIG: AppSettings = load_config()

class LocalModelAssetManager:
    """通用的本地模型资产管理器。"""
    _bos_downloader: Optional[BosHandler] = None
    _project_root = Path(__file__).resolve().parent.parent

    @classmethod
    def get_bos_downloader(cls) -> Optional[BosHandler]:
        if cls._bos_downloader is None:
            bos_configs = get_bos_config()
            required_keys = ['access_key_id', 'secret_access_key', 'endpoint', 'default_bucket_name']
            missing_keys = [k for k in required_keys if not bos_configs.get(k)]
            if not missing_keys:
                handler_config = {
                    "access_key": bos_configs.get('access_key_id'),
                    "secret_key": bos_configs.get('secret_access_key'),
                    "endpoint": bos_configs.get('endpoint'),
                    "bucket_name": bos_configs.get('default_bucket_name')
                }
                # 把这个构造好的字典作为单个参数传进去
                cls._bos_downloader = BosHandler(handler_config)
            else:
                logger.warning(
                    f"BOS下载功能不可用，因为缺少以下环境变量: {', '.join(missing_keys)}."
                    f" (请检查 .env 文件或系统环境变量)"
                )
                cls._bos_downloader = None
        return cls._bos_downloader

    @classmethod
    def ensure_asset_local(cls, local_model_dir_name: str, bos_bucket: Optional[str], bos_remote_path: Optional[str]) -> Optional[Path]:
        """
        确保与配置匹配的本地资产存在。如果不存在，则从BOS下载。
        能正确区分文件和目录的下载。
        """
        local_path = cls._project_root / "resources" / "models" / local_model_dir_name
        logger.info(f"检查资产: 期望本地路径为 '{local_path}'")

        # FIX: 对于文件，检查文件是否存在；对于目录，检查目录是否存在且非空
        asset_exists = False
        if local_path.exists():
            if local_path.is_file():
                asset_exists = True
            elif local_path.is_dir() and any(f for f in local_path.iterdir() if f.name != '.gitkeep'):
                asset_exists = True

        if asset_exists:
            logger.success(f"发现并使用本地已存在的资产: '{local_path}'")
            return local_path
        
        logger.warning(f"本地未找到资产 '{local_path}'。将尝试从BOS下载...")
        
        if not bos_remote_path:
            logger.error(f"本地资产 '{local_model_dir_name}' 缺失且未配置BOS远程路径。")
            return None
        
        downloader = cls.get_bos_downloader()
        if not downloader:
            logger.error("BOS下载器未初始化，无法下载资产。")
            return None

        # FIX: 正确判断远程是文件还是目录，并调用相应的方法
        # 一个简单的判断方法：如果远程路径的最后一部分包含'.'，我们认为它是一个文件。
        if "." in Path(bos_remote_path).name:
            logger.debug(f"检测到远程路径 '{bos_remote_path}' 为文件，调用 download_file。")
            success = downloader.download_file(
                remote_file=bos_remote_path,
                local_file=local_path,
                bucket_name=bos_bucket
            )
        else:
            logger.debug(f"检测到远程路径 '{bos_remote_path}' 为目录，调用 download_directory。")
            success = downloader.download_directory(
                remote_dir=bos_remote_path,
                local_dir=local_path,
                bucket_name=bos_bucket
            )

        if success:
            logger.success(f"资产已成功下载到: {local_path}")
            return local_path
        else:
            logger.error(f"从 'bos://{bos_bucket}/{bos_remote_path}' 下载资产失败。")
            return None

class TiktokenManager:
    """异步、非阻塞、线程安全地管理 tiktoken 编码器单例。"""
    _instance: Optional[Encoding] = None
    _lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls) -> Optional[Encoding]:
        if cls._instance is not None:
            return cls._instance

        async with cls._lock:
            if cls._instance is not None:
                return cls._instance

            logger.info("首次初始化 tiktoken 编码器...")
            try:
                cls._instance = await asyncio.to_thread(
                    tiktoken.get_encoding, "cl100k_base"
                )
                logger.success("tiktoken 编码器 (cl100k_base) 加载成功！")
            except Exception as e:
                logger.critical(f"加载 tiktoken 编码器失败: {e}", exc_info=True)
                cls._instance = None
            return cls._instance

# --- 新增 RAG 资源管理器 ---

class RAGModelManager:
    """管理RAG所需的Rerank模型，确保异步安全 (已简化)。"""
    _rerank_model: Optional[AutoModelForSequenceClassification] = None
    _rerank_tokenizer: Optional[AutoTokenizer] = None
    _lock = asyncio.Lock()

    @classmethod
    async def get_models(cls):
        async with cls._lock:
            if cls._rerank_model is None or cls._rerank_tokenizer is None:
                logger.info("首次初始化RAG模型 (仅Reranker)...")
                try:
                    config = APP_CONFIG.text_processing.rag
                    
                    # Embedding模型不再本地加载
                    logger.info("Embedding通过API访问，跳过本地加载。")
 
                    # 加载Rerank模型（保持本地加载）
                    rerank_path = await asyncio.to_thread(
                        LocalModelAssetManager.ensure_asset_local,
                        config.rerank_local_dir_name, config.rerank_bos_bucket_name, config.rerank_bos_remote_dir
                    )
                    if not rerank_path:
                        raise RuntimeError("Rerank模型资源无法获取")
                    cls._rerank_model = await asyncio.to_thread(AutoModelForSequenceClassification.from_pretrained, str(rerank_path))
                    cls._rerank_tokenizer = await asyncio.to_thread(AutoTokenizer.from_pretrained, str(rerank_path))
                    if cls._rerank_model is not None:
                        cls._rerank_model.eval()
 
                    logger.success("RAG模型初始化完成（Embedding: 远程=%s, Reranker: 本地已加载）" % ("是" if config.use_remote_embedding else "否"))
                except Exception as e:
                    logger.critical(f"加载RAG模型失败: {e}", exc_info=True)
            return (None, None, cls._rerank_model, cls._rerank_tokenizer)

class MilvusClientManager:
    """管理Milvus客户端单例实例。"""
    _instance: Optional[MilvusClient] = None
    _lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls) -> Optional[MilvusClient]:
        async with cls._lock:
            if cls._instance is None:
                logger.info("首次初始化Milvus客户端...")
                try:
                    config = APP_CONFIG.text_processing.rag
                    # 优先使用配置的URI，否则根据host/port拼装
                    uri = config.milvus_uri
                    if not uri and config.milvus_host and config.milvus_port:
                        uri = f"http://{config.milvus_host}:{config.milvus_port}"
                    if not uri:
                        raise RuntimeError("未配置远程Milvus连接信息（milvus_uri 或 milvus_host/milvus_port）")
                    cls._instance = await asyncio.to_thread(MilvusClient, uri)
                    logger.success(f"Milvus客户端连接成功: {uri}")
                except Exception as e:
                    logger.critical(f"加载Milvus客户端失败: {e}", exc_info=True)
            return cls._instance

def _verify_pickle_sha256(local_path: Path, bucket: Optional[str], remote_dir: Optional[str], label: str) -> None:
    """对从 BOS 下载的 pickle 做 sha256 校验（C6）。

    - 期望同前缀 sidecar：`<remote>.sha256`，内容为十六进制摘要（首 token）
    - sidecar 不存在：默认 warning 放行；若 BM25_REQUIRE_HASH=1 则致命
    - sidecar 存在但摘要不一致：始终致命
    """
    require = os.getenv("BM25_REQUIRE_HASH", "0").strip().lower() in ("1", "true", "yes", "on")

    sidecar_local = local_path.with_suffix(local_path.suffix + ".sha256")
    expected_hex: Optional[str] = None

    # 1) 优先读本地 sidecar
    if sidecar_local.exists():
        try:
            expected_hex = sidecar_local.read_text(encoding="utf-8").strip().split()[0].lower()
        except Exception as e:
            logger.warning(f"[BM25-{label}] 本地 sidecar 读取失败: {e}")

    # 2) 否则尝试从 BOS 下载 sidecar
    if expected_hex is None and bucket and remote_dir:
        try:
            downloader = LocalModelAssetManager.get_bos_downloader()
            if downloader is not None:
                ok = downloader.download_file(
                    remote_file=remote_dir + ".sha256",
                    local_file=sidecar_local,
                    bucket_name=bucket,
                    quiet_on_missing=True,
                )
                if ok and sidecar_local.exists():
                    expected_hex = sidecar_local.read_text(encoding="utf-8").strip().split()[0].lower()
        except Exception as e:
            logger.warning(f"[BM25-{label}] sidecar 拉取失败: {e}")

    if expected_hex is None:
        msg = f"[BM25-{label}] 缺少 sha256 sidecar，未做完整性校验"
        if require:
            raise RuntimeError(msg + "（BM25_REQUIRE_HASH=1）")
        logger.warning("security: " + msg)
        return

    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest().lower()
    if actual != expected_hex:
        raise RuntimeError(f"[BM25-{label}] sha256 不匹配: expected={expected_hex[:12]}..., actual={actual[:12]}...")
    logger.info(f"[BM25-{label}] sha256 校验通过 ({actual[:12]}...)")


class BM25Manager:
    """管理 BM25 数据单例。设备 / 安装两类语料各自独立缓存与锁，避免串扰。"""
    _equipment: dict = {"bm25": None, "meta": None, "entities": None}
    _installation: dict = {"bm25": None, "meta": None, "entities": None}
    _equipment_lock = asyncio.Lock()
    _installation_lock = asyncio.Lock()

    @classmethod
    async def _load(
        cls,
        slot: dict,
        lock: asyncio.Lock,
        local_dir: str,
        bucket: str,
        remote_dir: str,
        label: str,
    ):
        async with lock:
            if slot["bm25"] is not None:
                return slot["bm25"], slot["meta"], slot["entities"]

            logger.info(f"首次加载 BM25 数据 [{label}] ...")
            try:
                local_path = await asyncio.to_thread(
                    LocalModelAssetManager.ensure_asset_local,
                    local_dir, bucket, remote_dir,
                )
                if not local_path:
                    raise RuntimeError(f"BM25 数据文件无法获取 [{label}]")

                # sha256 校验（C6）
                await asyncio.to_thread(_verify_pickle_sha256, local_path, bucket, remote_dir, label)

                def _load_pickle():
                    with open(local_path, "rb") as f:
                        return pickle.load(f)

                bm25_data = await asyncio.to_thread(_load_pickle)
                tokenized_corpus = bm25_data["bm25_corpus_tokenized"]
                slot["bm25"] = await asyncio.to_thread(BM25Okapi, tokenized_corpus)
                slot["meta"] = bm25_data["bm25_corpus"]
                slot["entities"] = bm25_data["entities"]
                logger.success(f"BM25 数据加载并初始化成功 [{label}] (entities={len(slot['entities'])})")
            except Exception as e:
                logger.critical(f"加载 BM25 数据失败 [{label}]: {e}", exc_info=True)

            return slot["bm25"], slot["meta"], slot["entities"]

    @classmethod
    async def get_data(cls):
        cfg = APP_CONFIG.text_processing.rag
        return await cls._load(
            cls._equipment, cls._equipment_lock,
            cfg.bm25_local_dir_name, cfg.bm25_bos_bucket_name, cfg.bm25_bos_remote_dir,
            "equipment",
        )

    @classmethod
    async def get_data_installation(cls):
        cfg = APP_CONFIG.text_processing.rag
        return await cls._load(
            cls._installation, cls._installation_lock,
            cfg.bm25_local_dir_name_installation,
            cfg.bm25_bos_bucket_name_installation,
            cfg.bm25_bos_remote_dir_installation,
            "installation",
        )

# --- 5. 便捷函数 ---
def get_app_config() -> AppSettings:
    return APP_CONFIG

async def get_tiktoken_encoder() -> Optional[Encoding]:
    return await TiktokenManager.get_instance()

async def get_rag_models():
    return await RAGModelManager.get_models()

async def get_milvus_client():
    return await MilvusClientManager.get_instance()

async def get_bm25_data():
    return await BM25Manager.get_data()

async def get_bm25_data_installation():
    return await BM25Manager.get_data_installation()

# --- 6. 日志设置 ---
import logging

# 尝试导入 langchain.globals，如果失败则使用 langchain_core.globals
try:
    from langchain.globals import set_debug, set_verbose
except (ImportError, ModuleNotFoundError):
    try:
        from langchain_core.globals import set_debug, set_verbose
    except (ImportError, ModuleNotFoundError):
        # 如果都导入失败，定义空函数
        def set_debug(value):
            pass
        def set_verbose(value):
            pass
        logger.warning("无法导入 LangChain globals 模块，跳过全局调试设置")

async def setup_logging():

    # 1. 禁用 LangChain 的详细日志和流式标准输出
    try:
        set_debug(False)
        set_verbose(False)
        logger.info("已将 LangChain 全局 debug 和 verbose 模式强制设置为 False。")
    except Exception as e:
        logger.debug(f"设置 LangChain 全局模式时出错（可忽略）: {e}")

    # 2. 精确控制底层库的日志级别
    noisy_libraries = ["httpx", "openai", "httpcore", "baidubce"]
    for lib_name in noisy_libraries:
        lib_logger = logging.getLogger(lib_name)
        lib_logger.setLevel(logging.WARNING)
        lib_logger.propagate = False
    logger.info(f"已将 {', '.join(noisy_libraries)} 等库的日志级别强制设置为 WARNING。")
    
    # --- Loguru 的配置 ---
    # 优先从环境变量获取日志配置，否则使用默认值
    log_dir_env = os.getenv('LOG_DIR')
    if log_dir_env:
        log_dir = Path(log_dir_env)
        # 如果传入的是相对路径，基于项目根目录解析
        if not log_dir.is_absolute():
            log_dir = PROJECT_ROOT / log_dir
        logger.info(f"Using specified LOG_DIR for logs: {log_dir}")
    else:
        log_dir = PROJECT_ROOT / "logs"
        logger.info(f"LOG_DIR not set. Falling back to default log directory: {log_dir}")

    # 确保日志目录存在
    await aio_os.makedirs(log_dir, exist_ok=True)
    
    # 移除所有默认处理器，以便完全自定义
    logger.remove()

    is_development = APP_CONFIG.environment == "development"

    # 从环境变量读取日志级别，支持 DEBUG | INFO | WARNING | ERROR | CRITICAL
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    console_level_env = os.getenv('LOG_CONSOLE_LEVEL', '').strip().upper()
    if console_level_env and console_level_env in valid_levels:
        console_level = console_level_env
    else:
        console_level = "DEBUG" if is_development else "INFO"

    file_level_env = os.getenv('LOG_FILE_LEVEL', '').strip().upper()
    if file_level_env and file_level_env in valid_levels:
        file_level = file_level_env
    else:
        file_level = "DEBUG"

    enqueue_mode = not is_development # 在生产环境(非开发)开启队列以提高性能

    def formatter(record):
        base_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{process}</cyan> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan>"
        )
        if "task_id" in record["extra"]:
            task_format = " | <blue>task:{extra[task_id]}</blue>"
            return f"{base_format}{task_format} - <level>{{message}}</level>\n"
        return f"{base_format} - <level>{{message}}</level>\n"

    # 添加控制台日志处理器
    logger.add(sys.stderr, level=console_level, colorize=True, format=formatter, enqueue=False)

    # 是否启用文件日志（默认启用；显式设置为 false/0/no/off 则关闭）
    log_enable_file_raw = (os.getenv("LOG_ENABLE_FILE", "true") or "true").strip().lower()
    log_enable_file = log_enable_file_raw in ("1", "true", "yes", "on")

    # 从环境变量读取日志轮转、压缩、保留配置
    log_rotation_env = os.getenv('LOG_ROTATION', '').strip()
    log_rotation = log_rotation_env if log_rotation_env else "10 MB"

    log_compression_env = os.getenv('LOG_COMPRESSION', '').strip()
    log_compression = log_compression_env if log_compression_env else "zip"

    log_retention_env = os.getenv('LOG_RETENTION', '').strip()

    # Loguru 的文件名模板已经支持时间戳，每次重启服务会生成新文件
    log_file_path = log_dir / "app_{time:YYYY-MM-DD_HH-mm-ss}.log"

    if log_enable_file:
        # 构建文件日志处理器参数
        file_handler_kwargs = {
            "sink": str(log_file_path),
            "level": file_level,
            "rotation": log_rotation,
            "compression": log_compression,
            "encoding": "utf-8",
            "enqueue": enqueue_mode,
            "serialize": False,
            "format": formatter,
            "backtrace": True,
            "diagnose": is_development,
            "catch": True
        }
        if log_retention_env:
            file_handler_kwargs["retention"] = log_retention_env

        # 添加文件日志处理器，并配置轮转和压缩
        await asyncio.to_thread(logger.add, **file_handler_kwargs)
    else:
        logger.warning("已根据 LOG_ENABLE_FILE=false 关闭文件日志输出（仅保留控制台输出）。")

    logger.info(f"全局日志记录器配置完成。")
    logger.info(f"运行环境: {APP_CONFIG.environment}")
    logger.info(f"控制台日志级别: {console_level}, 文件日志级别: {file_level} (enable_file={log_enable_file})")
    if log_enable_file:
        logger.info(f"日志文件将保存在: {log_dir.resolve()}")