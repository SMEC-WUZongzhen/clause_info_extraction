# app/config/config_models.py

from __future__ import annotations
from typing import Dict, List, Optional, Any, Tuple, Literal

from pydantic import BaseModel, Field, RootModel

from app.config.prompts import (
    PAYMENT_RATIO_PROMPT,
)

# =============================================================================
# 1. 基础配置模型
# =============================================================================

class LLMConfig(BaseModel):
    model_name: str = Field("gpt-3.5-turbo", description="模型的用户友好名称，用于日志和显示。")
    temperature: float = Field(0.1, description="温度参数，控制生成的随机性。值越低，输出越确定。")
    max_tokens: int = Field(8192, description="LLM 单次生成的最大 token 数（OpenAI 协议 max_completion_tokens 语义，仅限输出）。")
    max_output_tokens: int = Field(4096, description="保留字段，目前未被代码读取；如需显式区分输入/输出上限再启用。")
    api_base: Optional[str] = Field(None, description="API基础URL，通过环境变量加载。")
    api_key: Optional[str] = Field(None, description="API密钥，通过环境变量加载，属敏感信息。")
    model: Optional[str] = Field(None, description="调用API时使用的实际模型标识符/端点。")

# =============================================================================
# 2. 文本处理各功能模块配置
# =============================================================================

class ChunkingConfig(BaseModel):
    dynamic_chunking_safe_margin: float = Field(0.8, description="动态计算块大小时，基于LLM上下文的安全系数。")
    token_to_char_ratio_heuristic: float = Field(3.0, description="用于估算字符数的Token-Char转换率（启发式）。")
    default_chunk_size: int = Field(4096, description="默认的目标块大小（以token计）。")
    default_overlap_ratio: float = Field(0.1, description="块与块之间重叠部分的比例。")

class LLMPromptsConfig(BaseModel):
    payment_ratio_extraction: str = Field(PAYMENT_RATIO_PROMPT)

class StructuralPatternsConfig(BaseModel):
    clause_marker_patterns: List[str] = Field(default_factory=list)
    title_like_patterns: List[str] = Field(default_factory=list)

# --- 过滤阈值配置模型 ---
class ParserThresholds(BaseModel):
    payment_confidence_threshold: float = Field(0.7, description="支付置信度阈值")
    top_n_warranty_clauses: int = Field(3, description="保修条款Top N的默认值")

# --- RAG 配置模型 ---
class RAGConfig(BaseModel):
    """RAG相关的所有配置"""
    rerank_local_dir_name: str = Field("bge-reranker-large", description="Rerank模型本地目录名")
    rerank_bos_bucket_name: Optional[str] = Field(None, description="Rerank模型BOS桶")
    rerank_bos_remote_dir: Optional[str] = Field(None, description="Rerank模型BOS路径")

    bm25_local_dir_name: str = Field("bm25_pickle/bm25_data.pkl", description="BM25数据本地文件名")
    bm25_bos_bucket_name: Optional[str] = Field(None, description="BM25数据BOS桶")
    bm25_bos_remote_dir: Optional[str] = Field(None, description="BM25数据BOS路径")
    
    bm25_local_dir_name_installation: str = Field("bm25_pickle/bm25_data_installation.pkl", description="BM25数据本地文件名")
    bm25_bos_bucket_name_installation: Optional[str] = Field(None, description="BM25数据BOS桶")
    bm25_bos_remote_dir_installation: Optional[str] = Field(None, description="BM25数据BOS路径")

    collection_name: str = Field("rag_collection", description="Milvus中的集合名称")
    collection_name_installation: str = Field("rag_collection_installation", description="Milvus中的集合名称")
    use_bm25: bool = Field(True, description="是否启用BM25检索")
    use_vector: bool = Field(True, description="是否启用向量检索")
    use_rerank: bool = Field(True, description="是否启用rerank")
    top_k: int = Field(3, description="RAG检索返回的候选数量")

    # 新增：远程 Milvus 与远程嵌入配置
    milvus_host: Optional[str] = Field(None, description="远程Milvus主机名或IP")
    milvus_port: Optional[int] = Field(None, description="远程Milvus端口")
    milvus_uri: Optional[str] = Field(None, description="远程Milvus URI，如果提供则优先生效，如 'http://host:19530'")
    search_metric_type: Optional[str] = Field("COSINE", description="Milvus检索度量类型，如 COSINE、L2")
    output_fields: List[str] = Field(default_factory=lambda: ["payment_type", "clause_text", "payment_ratio", "clause_context"], description="Milvus中需要返回的字段列表")
    output_fields_installation: List[str] = Field(default_factory=lambda: ["payment_type", "clause_text", "payment_ratio", "payment_amount","clause_context"], description="Milvus中需要返回的字段列表")

    use_remote_embedding: bool = Field(False, description="是否使用远程API获取嵌入向量（替代本地模型）")
    remote_embedding_api_url: Optional[str] = Field(None, description="远程嵌入API地址")
    remote_embedding_api_key: Optional[str] = Field(None, description="远程嵌入API鉴权Key（可选）")
    remote_embedding_model_name: Optional[str] = Field(None, description="远程嵌入模型名称，用于API参数")

    embedding_dimension: int = Field(1024, description="嵌入模型的向量维度")

# # --- 对 PaymentInfo 模型的微调 ---
# class PaymentInfo(BaseModel):
#     """支付信息模型类"""
#     doc_id: str = Field(..., description="关联的文档ID")
#     chunk_seq: int = Field(..., description="关联的块序号")
#     para_seq: int = Field(..., description="关联的段落序号")
#     payment_clause: str = Field(..., description="支付信息条款")
#     payment_type: Optional[str] = Field(None, description="支付类型")
#     # 支付比例，可以先接收任意类型，在业务逻辑中解析
#     payment_ratio: Any = Field(None, description="支付比例, 可能是数字、None或字符串")
#     payment_amount: Optional[str] = Field(None, description="支付金额")
#     payment_condition: Optional[str] = Field(None, description="支付条件")
#     due_date: Optional[str] = Field(None, description="支付期限")
#     metadata: Dict[str, Any] = Field(default_factory=dict, description="支付信息元数据")

# 定义别名规则的数据模型
class AliasRule(BaseModel):
    name: str = Field(..., description="规则集的名称，例如 '标准层节点别名'")
    mapping: Dict[str, List[str]] = Field(..., description="别名映射，键是标准名称，值是别名列表")

# 定义比对节点的配置模型
class ComparisonConfig(BaseModel):
    match_threshold: int = Field(85, description="归一化后用于模糊匹配的相似度阈值")
    alias_rules: List[AliasRule] = Field(default_factory=list, description="别名映射规则列表")

class ProcessingConfig(BaseModel):
    chunking: ChunkingConfig
    llm_prompts: LLMPromptsConfig
    fine_grained_parser_thresholds: ParserThresholds = Field(default_factory=ParserThresholds)
    structural_patterns: StructuralPatternsConfig = Field(default_factory=StructuralPatternsConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)
    comparison: ComparisonConfig = Field(default_factory=ComparisonConfig)

# =============================================================================
# 3. 主应用配置模型
# =============================================================================
class AppSettings(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    payment_ratio_llm: Optional[LLMConfig] = Field(None, description="专用于支付比例提取的LLM配置，如果为空则使用默认LLM配置")
    text_processing: ProcessingConfig
    environment: str = Field("development", description="运行环境。")

# =============================================================================
# 4. 新架构LLM输出Pydantic模型与解析器
# =============================================================================

# --- 模型 for GlobalLocatorNode ---
class LocatedClause(BaseModel):
    """单个被定位的关键条款范围"""
    clause_type: Literal["equipment_payment", "installation_payment", "warranty"] = Field(..., description="条款类型，必须是 'equipment_payment'、'installation_payment' 或 'warranty'")
    start_char: int = Field(..., description="条款在【整个文档中】的起始字符索引")
    end_char: int = Field(..., description="条款在【整个文档中】的结束字符索引")
    source_section_type: Literal["general", "special", "unknown"] = Field(
        "unknown",
        description="条款来源章节类型：'general' (通用条款), 'special' (专用条款/附件), 'unknown' (未知)"
    )
    rationale: str = Field(..., description="AI做出此判断的简要理由，例如'这是一个完整的预付款条款'或'这是一个专用条款，可能覆盖通用条款'")

class GlobalLocatorOutput(BaseModel):
    """全局定位器在一个处理批次中的输出模型"""
    clauses: List[LocatedClause] = Field(..., description="在当前文本块中新发现的所有关键条款的范围列表")
    updated_summary: str = Field(..., description="更新后的、包含了本次新发现的合同内容摘要，用于传递给下一个处理块")
    continue_scanning: bool = Field(..., description="一个布尔标志，指示是否需要继续扫描文档的后续部分")

# --- 模型 for FineGrainedParserNode ---
class ParsedClause(BaseModel):
    """单个原子化条款的解析结果，匹配原始Prompt"""
    clause_type: Literal["equipment_payment", "installation_payment", "warranty"] = Field(..., description="条款类型，必须是 'equipment_payment'、'installation_payment' 或 'warranty'")
    clause_text: str = Field(..., description="重建后的、干净的、不含信标的原子节点文本。")
    beacon_ids: List[str] = Field(..., description="构成该原子节点的信标ID列表。")
    score: float = Field(..., description="条款置信度分数，0.0-1.0")

class FineGrainedParserOutput(BaseModel):
    """阶段二(Parser)的输出模型，用于单个ROI的解析结果。"""
    clauses: List[ParsedClause] = Field(..., description="从带信标的文本中解析出的原子条款列表。")