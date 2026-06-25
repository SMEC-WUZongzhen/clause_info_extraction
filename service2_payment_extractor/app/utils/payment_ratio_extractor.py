# ===== 文件：app/utils/payment_ratio_extractor.py  =====

import re
import os
import aiofiles
import aiofiles.os as aio_os
from datetime import datetime
from loguru import logger
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from app.states.states import (
    State, WarrantyInfo, ThinkingInfo,
    PaymentRatioResult, PaymentSummaryResult, WarrantySummaryResult,
    VerificationResult, ClauseValidationResult, ClauseCategoryResult,
    SingleGroupVerificationResult, PaymentTimingResult,
)
from typing import Optional, List, Union, Tuple, Dict, Any
from app.config.prompts_loader import (
    PAYMENT_RATIO_PROMPT,
    EQUIPMENT_PAYMENT_RATIO_PROMPT,
    INSTALL_PAYMENT_RATIO_PROMPT,
    PAYMENT_SUMMARY_RATIO_PROMPT,
    INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT,
    WARRANTY_SUMMARY,
    RESULT_VERIFICATION_PROMPT,
    RESULT_VERIFICATION_SINGLE_GROUP_PROMPT,
    PAYMENT_CLAUSE_VALIDATION_PROMPT,
    PAYMENT_CLAUSE_CATEGORY_PROMPT,
    PAYMENT_TIMING_EXTRACTION_PROMPT,
    EQUIPMENT_STANDARD_NODES_STR,
    INSTALL_STANDARD_NODES_STR,
)
from app.config.config import APP_CONFIG
from app.utils.token_counter import count_tokens
from app.config.business_dict import get_business_dict
from app.utils.concurrency import llm_guarded_ainvoke, LLM_CALL_TIMEOUT_SEC
import json
import asyncio
from pydantic import BaseModel, RootModel, Field


# ===== LLM 调用默认参数（可由环境变量统一调整，显式配置避免 LangChain 默认值隐患）=====
_LLM_REQUEST_TIMEOUT_SEC = int(os.getenv("LLM_REQUEST_TIMEOUT_SEC", "60"))
_LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))


def _make_json_schema_format(name: str, model: type) -> dict:
    """生成 SGLang / OpenAI compatible json_schema response_format 字典"""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": model.model_json_schema(),
        },
    }


class PaymentRatioExtractor:
    """
    用于对单个支付条款进行初步比例和金额提取的类。
    它被设计为无状态的，或者说它的状态（LLM实例）是根据每次调用的输入动态管理的。
    """
    def __init__(self):
        """初始化一个空的提取器实例。"""
        self.llm = None
        self.equipment_chain = None
        self.install_chain = None
        self.chain = None  # 向后兼容：默认指向设备链
        self.is_ready = False
        self.current_config = None # 用于缓存当前LLM的配置，避免不必要的重初始化
        
    def _initialize_llm(self, llm_config: dict):
        """
        根据给定的配置初始化或重新初始化LLM实例和调用链。
        """
        try:
            model_identifier = llm_config.get("model") or llm_config.get("model_name")
            temperature = llm_config.get("temperature", 0.0)
            # 优先从入参取（保持 batch_controller 注入的运行时配置生效），缺失时回退到全局 AppSettings
            llm_max_tokens = llm_config.get("max_tokens") or APP_CONFIG.llm.max_tokens

            self.llm = ChatOpenAI(
                model=model_identifier,
                temperature=temperature,
                openai_api_key=llm_config.get("api_key") or "EMPTY",
                openai_api_base=llm_config.get("api_base"),
                max_tokens=llm_max_tokens,
                request_timeout=_LLM_REQUEST_TIMEOUT_SEC,
                max_retries=_LLM_MAX_RETRIES,
            )
            
            equipment_prompt = PromptTemplate.from_template(EQUIPMENT_PAYMENT_RATIO_PROMPT)
            install_prompt = PromptTemplate.from_template(INSTALL_PAYMENT_RATIO_PROMPT)
            _ratio_fmt = _make_json_schema_format("payment_ratio_result", PaymentRatioResult)
            self.equipment_chain = equipment_prompt | self.llm.bind(response_format=_ratio_fmt)
            self.install_chain = install_prompt | self.llm.bind(response_format=_ratio_fmt)
            self.chain = self.equipment_chain  # 向后兼容
            self._max_tokens_value = llm_max_tokens  # 供 token 截断逻辑使用
            self.is_ready = True
            self.current_config = llm_config
            
            logger.info(f"PaymentRatioExtractor LLM初始化成功，使用模型: {model_identifier}, 温度: {temperature}")
            return True
        except Exception as e:
            logger.error(f"PaymentRatioExtractor LLM初始化失败: {e}", exc_info=True)
            self.is_ready = False
            return False
    
    def _should_reinitialize(self, new_config: dict) -> bool:
        """
        检查传入的新配置与当前缓存的配置是否有关键差异。
        如果有差异，则需要重新初始化LLM。
        """
        if not self.current_config:
            return True
        
        keys_to_check = ['model', 'model_name', 'api_key', 'api_base', 'temperature']
        for key in keys_to_check:
            if self.current_config.get(key) != new_config.get(key):
                logger.info(f"PaymentRatioExtractor 检测到配置变化: '{key}' 从 '{self.current_config.get(key)}' 变为 '{new_config.get(key)}'。需要重新初始化。")
                return True
        return False
        
    async def _debug_show_prompt(self, payment_type: str, payment_clause: str, current_clauses_chunk: str, rag_examples: str, is_debug_mode: bool, clause_class: str = "equipment_payment"):
        """根据调试开关，有条件地将生成的完整提示词保存到文件。"""
        if not is_debug_mode:
            return

        selected_prompt = INSTALL_PAYMENT_RATIO_PROMPT if "installation" in clause_class else EQUIPMENT_PAYMENT_RATIO_PROMPT
        formatted_prompt = selected_prompt.format(
            rag_examples=rag_examples, payment_clause=payment_clause,
            payment_type=payment_type, current_clauses_chunk=current_clauses_chunk
        )
        debug_dir = "debug_output"
        await aio_os.makedirs(debug_dir, exist_ok=True)
        filename = f"{debug_dir}/payment_ratio_prompt_debug.md"
        md_content = f"\n---\n## 调试记录 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        md_content += f"**提示词总长度**: {len(formatted_prompt)} 字符\n"
        md_content += f"### 完整提示词内容\n```text\n{formatted_prompt}\n```\n"
        async with aiofiles.open(filename, 'a', encoding='utf-8') as f:
            await f.write(md_content)
        logger.trace(f"调试提示词已异步追加到: {filename}")

    def _parse_payment_info_from_text(self, text: str) -> Dict[str, Any]:
        """
        从LLM返回的文本中稳健地解析出比例和金额信息。
        返回格式: {"ratio": float | None, "amount": str | int | None}
        """
        result = {"ratio": None, "amount": None}
        
        if not text or not text.strip() or text.lower() in ['none', 'null', 'n/a', '无', '未知', '不明确']: 
            return result
        
        # 清理文本，移除可能的转义字符问题
        cleaned_text = text.strip()
        
        # 首先尝试解析JSON格式的响应
        try:
            import json
            # 尝试直接解析JSON
            data = json.loads(cleaned_text)
            if isinstance(data, dict):
                # 检查是否包含错误信息
                if 'error' in data:
                    logger.warning(f"LLM返回包含错误信息: {data.get('error')}")
                    return result
                
                # 解析比例
                ratio_str = data.get('ratio')
                if ratio_str and ratio_str != 'null':
                    # 解析比例字符串，如 "30%" -> 0.3
                    if isinstance(ratio_str, str) and ratio_str.endswith('%'):
                        ratio_value = float(ratio_str[:-1]) / 100
                        result["ratio"] = round(ratio_value, 4) if 0 <= ratio_value <= 1 else None
                    elif isinstance(ratio_str, (int, float)):
                        ratio_value = float(ratio_str)
                        if ratio_value > 1:  # 如果是百分比形式，如30
                            ratio_value = ratio_value / 100
                        result["ratio"] = round(ratio_value, 4) if 0 <= ratio_value <= 1 else None
                
                # 解析金额
                amount = data.get('amount')
                if amount and amount != 'null':
                    result["amount"] = str(amount)  # 确保金额始终是字符串类型
                    
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.debug(f"JSON解析失败，回退到正则表达式解析: {e}")
            pass
        
        # 如果JSON解析失败，回退到原有的正则表达式解析
        if result["ratio"] is None:
            # 优先匹配带百分号的数字，如 "30%" or "15.5%"
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)%", cleaned_text)
            if m: 
                result["ratio"] = round(float(m.group(1)) / 100, 4)
            
            # 匹配小数，如 "0.3"
            if result["ratio"] is None:
                m = re.search(r"([0-1]?\.[0-9]+|1\.0+)", cleaned_text)
                if m:
                    val = float(m.group(0))
                    result["ratio"] = round(val, 4) if 0 <= val <= 1 else None
            
            # 匹配整数，假定为百分比，如 "30"
            if result["ratio"] is None:
                m = re.search(r"\b([0-9]+)\b", cleaned_text)
                if m:
                    val = int(m.group(1))
                    result["ratio"] = round(val / 100, 4) if 0 <= val <= 100 else None
        
        # 如果JSON解析失败，尝试用正则表达式提取金额
        if result["amount"] is None:
            # 匹配各种金额格式
            amount_patterns = [
                r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*元',  # 如 "50,000元"
                r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*万元',  # 如 "50万元"
                r'RMB\s*(\d+(?:,\d{3})*(?:\.\d{2})?)',  # 如 "RMB 50,000"
                r'¥\s*(\d+(?:,\d{3})*(?:\.\d{2})?)',  # 如 "¥50,000"
                r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*万',  # 如 "50万"
            ]
            
            for pattern in amount_patterns:
                m = re.search(pattern, cleaned_text)
                if m:
                    amount_str = m.group(1)
                    # 尝试转换为数字
                    try:
                        # 移除逗号
                        clean_amount = amount_str.replace(',', '')
                        if '万' in pattern:
                            # 如果是万元，转换为元
                            result["amount"] = str(int(float(clean_amount) * 10000))
                        else:
                            result["amount"] = clean_amount
                        break
                    except ValueError:
                        # 如果转换失败，保留原始字符串
                        result["amount"] = amount_str
                        break
        
        return result

    def _parse_multi_node_response(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 LLM 返回的多节点 JSON 响应（json_schema 强制格式）。

        SGLang constrained decoding 保证输出为合法 JSON，格式为：
            {"nodes": [{payment_type, ratio, amount}, ...]}
        直接解析后逐节点调用 _normalize_node_from_json 做业务字段规整。
        """
        if not text or not text.strip():
            return []
        try:
            data = json.loads(text.strip())
            nodes = data.get("nodes", []) if isinstance(data, dict) else []
            results = []
            for item in nodes:
                if not isinstance(item, dict):
                    continue
                node = self._normalize_node_from_json(item)
                if node:
                    results.append(node)
            if results:
                logger.info(f"解析多节点响应：识别到 {len(results)} 个付款节点")
            return results
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error(f"_parse_multi_node_response JSON 解析失败（schema 约束下不应出现）: {e}")
            return []
    
    @staticmethod
    def _extract_payment_type_from_text(text: str) -> Optional[str]:
        """
        从LLM返回的文本中尝试提取 payment_type。
        当JSON解析完全失败、只能用正则兜底时，尝试从文本语义中识别付款类型。

        正则表已迁移到业务词典 payment_type_regex_fallback。命中即记
        [fallback_regex_hit] 埋点，便于评估 SFT 模型 JSON 输出稳定后下线该路径。
        """
        for item in get_business_dict().payment_type_regex_fallback:
            if re.search(item.pattern, text):
                logger.warning(
                    f"[fallback_regex_hit] pattern={item.pattern!r} -> type={item.type!r}"
                )
                return item.type
        return None
    
    def _normalize_node_from_json(self, item: dict) -> Optional[Dict[str, Any]]:
        """
        将JSON对象标准化为内部节点格式 {payment_type, ratio, amount}。
        兼容新格式（含payment_type）和旧格式（仅ratio/amount）。
        """
        if 'error' in item:
            logger.warning(f"LLM返回包含错误信息: {item.get('error')}")
            return None
        
        node: Dict[str, Any] = {
            "payment_type": item.get("payment_type"),
            "ratio": None,
            "amount": None,
        }
        
        # 解析比例
        ratio_str = item.get('ratio')
        if ratio_str and str(ratio_str).lower() not in ('null', 'none', ''):
            if isinstance(ratio_str, str) and ratio_str.endswith('%'):
                try:
                    ratio_value = float(ratio_str[:-1]) / 100
                    node["ratio"] = round(ratio_value, 4) if 0 <= ratio_value <= 1 else None
                except ValueError:
                    pass
            elif isinstance(ratio_str, (int, float)):
                ratio_value = float(ratio_str)
                if ratio_value > 1:
                    ratio_value = ratio_value / 100
                node["ratio"] = round(ratio_value, 4) if 0 <= ratio_value <= 1 else None
        
        # 解析金额
        amount = item.get('amount')
        if amount and str(amount).lower() not in ('null', 'none', ''):
            node["amount"] = str(amount)
        
        # 至少要有 payment_type / ratio / amount 之一才算有效节点
        if node["ratio"] is None and node["amount"] is None and not node.get("payment_type"):
            return None
        
        return node

    async def extract_payment_info(self, payment_clause: str, payment_type: str, rag_results: list, current_clauses_chunk: str, state: State, clause_class: str = "equipment_payment") -> List[Dict[str, Any]]:
        """
        主执行函数，负责提取单个条款中的所有付款节点（支持多节点）。
        
        Args:
            clause_class: 条款分类，"equipment_payment" 或 "installation_payment"，
                          用于选择对应的标准节点提示词。
        返回格式: [{"payment_type": str|None, "ratio": float|None, "amount": str|int|None}, ...]
        """
        # 1. 动态选择LLM配置
        payment_ratio_llm_config = state.get("payment_ratio_llm_config")
        llm_config = state.get("llm_config")
        
        selected_llm_config = payment_ratio_llm_config if payment_ratio_llm_config and payment_ratio_llm_config.get("api_base") else llm_config
        
        if not (selected_llm_config and selected_llm_config.get("api_base")):
            logger.error("LLM配置或API Base缺失，无法提取支付信息。")
            return []
        
        # 2. 检查并按需（重新）初始化LLM
        if not self.is_ready or self._should_reinitialize(selected_llm_config):
            if not self._initialize_llm(selected_llm_config):
                return []
        
        try:
            # 3. 准备RAG示例和提示词
            #    仅展示"条款文本 + payment_type 参考标签"，不展示具体比例与金额，
            #    避免 few-shot 数值套用导致的幻觉（详见 doc.md 方案 2.1）
            rag_examples = ""
            if rag_results:
                for i, item in enumerate(rag_results, 1):
                    rag_examples += (
                        f"# `示例{i}`:\n"
                        f"  ## `条款文本`:\n {item.get('text', '').strip()}\n"
                        f"  ## `payment_type 参考标签`：{item.get('label', '')}\n\n"
                    )
            else:
                rag_examples = "暂无参考示例"

            # --- Token 感知截断：打印输入长度 + 超长时整体丢弃 RAG ---
            # 微调模型 context_length = 16384
            _MODEL_CTX_LIMIT = 16384
            _llm_max_tokens = getattr(self, '_max_tokens_value', None) or 2048
            # prompt 模板固定开销实测为 5000 tokens（含全部标准节点、规则说明、输出格式示例）
            _PROMPT_TEMPLATE_OVERHEAD = 5000
            _max_input_budget = _MODEL_CTX_LIMIT - _llm_max_tokens - _PROMPT_TEMPLATE_OVERHEAD

            _clause_tokens = count_tokens(payment_clause)
            _type_tokens = count_tokens(payment_type)
            _chunk_tokens = count_tokens(current_clauses_chunk)
            _rag_tokens = count_tokens(rag_examples)
            _total_dynamic = _clause_tokens + _type_tokens + _chunk_tokens + _rag_tokens

            # 直接打印输入长度供排查
            logger.info(
                f"[token_check] 动态输入 token 统计: clause={_clause_tokens}, type={_type_tokens}, "
                f"chunk={_chunk_tokens}, rag={_rag_tokens}, 合计={_total_dynamic}, "
                f"预算={_max_input_budget} (模型窗口={_MODEL_CTX_LIMIT} - max_tokens={_llm_max_tokens} - 模板={_PROMPT_TEMPLATE_OVERHEAD})"
            )

            if _total_dynamic > _max_input_budget:
                # 超长 → 整体丢弃 RAG 示例
                rag_examples = "暂无参考示例（因输入过长已丢弃）"
                _new_total = _clause_tokens + _type_tokens + _chunk_tokens + count_tokens(rag_examples)
                logger.warning(
                    f"[token_truncate] 动态输入 {_total_dynamic} tokens 超预算 {_max_input_budget}，"
                    f"已整体丢弃 RAG 示例 ({_rag_tokens} tokens) → 新合计 {_new_total} tokens"
                )
            # --- Token 感知截断结束 ---
                        
            # 4. 有条件地保存调试信息
            is_debug_mode = state.get("debug_mode", False)
            await self._debug_show_prompt(payment_type, payment_clause, current_clauses_chunk, rag_examples, is_debug_mode, clause_class)

            # 5. 选择对应分类的调用链并调用LLM
            selected_chain = self.install_chain if "installation" in clause_class else self.equipment_chain
            logger.debug(f"使用{'安装' if 'installation' in clause_class else '设备'}付款提示词")
            invoke_input = {
                "payment_clause": payment_clause,
                "payment_type": payment_type,
                "current_clauses_chunk": current_clauses_chunk,
                "rag_examples": rag_examples,
            }
            # 瞬时错误重试：模型冷启动 / 上游错误响应 (KeyError('error') / 空响应等) 时自动重试 1 次。
            # ChatOpenAI 自带的 max_retries 只覆盖 HTTP 层错误；这里补充对响应体异常的重试。
            response = None
            _last_invoke_err: Optional[Exception] = None
            for _attempt in range(2):
                try:
                    response = await llm_guarded_ainvoke(selected_chain, invoke_input)
                    _last_invoke_err = None
                    break
                except Exception as _invoke_err:  # noqa: BLE001
                    _last_invoke_err = _invoke_err
                    logger.warning(
                        f"LLM 调用失败（第 {_attempt + 1}/2 次）: {type(_invoke_err).__name__}: {_invoke_err}"
                    )
                    if _attempt == 0:
                        await asyncio.sleep(0.5)  # 短暂退避，避免连环失败
            if response is None:
                logger.error(
                    f"LLM 调用连续 2 次失败，放弃提取本条款。最后异常: {_last_invoke_err}",
                    exc_info=True,
                )
                return []

            # 6. 解析多节点结果
            response_text = str(getattr(response, "content", response)).strip()
            logger.info(f"LLM原始响应: {response_text}")
            
            try:
                payment_nodes = self._parse_multi_node_response(response_text)
                # 6.1 比例兜底清理（详见 doc.md 方案 2.3）：
                # 当 payment_clause 原文不出现 %/％/百分之，
                # 且不含"余款/尾款/付清/结清"等隐含剩余支付语义，
                # 且上下文中无明确"唯一总价"标识时，
                # 强制将 ratio 清空，避免 LLM 借 RAG 示例或上下文比例幻觉填充。
                payment_nodes = self._enforce_ratio_origin_constraint(
                    payment_nodes, payment_clause, current_clauses_chunk
                )
                for idx, node in enumerate(payment_nodes):
                    logger.success(
                        f"从条款中解析的付款节点[{idx}]: 类型={node.get('payment_type')}, "
                        f"比例={node.get('ratio')}, 金额={node.get('amount')}"
                    )
                return payment_nodes
            except Exception as parse_error:
                logger.error(f"解析LLM响应时发生错误: {parse_error}")
                logger.error(f"问题响应文本: {repr(response_text)}")
                return []
        except Exception as e:
            logger.error(f"支付信息抽取失败: {e}", exc_info=True)
            return []

    # === 比例字符必须存在性约束（doc.md 方案 2.3 兜底） ===
    # 同义词集合已迁移至 app/resources/business_dict/v1.yaml；
    # 通过 get_business_dict().synonyms.* 访问，避免与 payment_info_extractor_node
    # 中的同名集合产生分裂真相。

    def _has_unique_total_in_context(self, current_clauses_chunk: Optional[str]) -> bool:
        """粗略判断上下文中是否存在"唯一明确总价"标记。

        采用保守判定：上下文中至少出现一个总价标记词紧邻数字（含元/万/￥/¥）即视为存在。
        判定结果偏宽松，避免误清空合理的反算比例；严格的"口径一致"留给 LLM 与上层校验。
        """
        if not current_clauses_chunk:
            return False
        text = str(current_clauses_chunk)
        for kw in get_business_dict().synonyms.unique_total_hints:
            idx = text.find(kw)
            if idx < 0:
                continue
            # 在关键词后 30 字符窗口里搜数字
            window = text[idx + len(kw): idx + len(kw) + 30]
            if re.search(r"[¥￥]?\s*\d[\d,，.]*\s*(元|万|万元)?", window):
                return True
        return False

    @staticmethod
    def _parse_amount_to_float(amount: Any) -> Optional[float]:
        """把节点 amount 字段（可能是 int / float / "5000" / "5,000" / "50万元"）规整为元（float）。

        无法解析或非法值返回 None，调用方据此跳过算术校核。
        """
        if amount is None:
            return None
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            try:
                v = float(amount)
                return v if v > 0 else None
            except Exception:
                return None
        s = str(amount).strip()
        if not s:
            return None
        # 移除逗号 / 全角逗号 / 货币符号 / 空白
        s_clean = re.sub(r"[,\uff0c¥￥\s]", "", s)
        m = re.search(r"(\d+(?:\.\d+)?)\s*(万元|万)?", s_clean)
        if not m:
            return None
        try:
            base = float(m.group(1))
        except Exception:
            return None
        if m.group(2):  # 万 / 万元
            base *= 10000.0
        return base if base > 0 else None

    @classmethod
    def _extract_unique_total_amount(cls, current_clauses_chunk: Optional[str]) -> Optional[float]:
        """从上下文中提取"唯一数字总价"。

        策略：
        1) 遍历 `_UNIQUE_TOTAL_HINTS` 中每个提示词，扫描其后 0~50 字符窗口；
        2) 只接受**带单位（元/万/万元）或货币符号（¥/￥）**的数字，避免"税率13%"等噪声被误抓；
        3) 把所有命中的数值规整为元；
        4) 若所有命中值在 0.5 元以内一致 → 返回该值；多种不同值 → 返回 None。
        """
        if not current_clauses_chunk:
            return None
        text = str(current_clauses_chunk)
        # 必须带单位的数字捕获：(¥|￥)?数字(元|万|万元)，单位强制存在（前缀或后缀至少一个）
        # 注意：(?=...) 用前瞻确保单位/货币符号存在但不消耗字符，便于多次匹配
        num_re = re.compile(
            r"(?:(?P<cur>[¥￥])\s*)?"
            r"(?P<num>[0-9][0-9,，.]*)"
            r"\s*(?P<unit>万元|万|元)?"
        )
        candidates: List[float] = []
        for kw in get_business_dict().synonyms.unique_total_hints:
            for hit in re.finditer(re.escape(kw), text):
                window = text[hit.end(): hit.end() + 50]
                # 在窗口内逐个尝试匹配，跳过无单位且无货币符号的"裸数字"（如 13% 中的 13）
                for m in num_re.finditer(window):
                    if not m.group("cur") and not m.group("unit"):
                        continue  # 既无 ¥ 前缀也无 元/万 后缀 → 噪声，跳过
                    raw = m.group("num").replace(",", "").replace("，", "")
                    try:
                        val = float(raw)
                    except ValueError:
                        continue
                    if m.group("unit") in ("万元", "万"):
                        val *= 10000.0
                    if val < 100:
                        continue
                    candidates.append(val)
                    break  # 每个 hint 命中后只取窗口内第一个有效数字
        if not candidates:
            return None
        first = candidates[0]
        if all(abs(c - first) < 0.5 for c in candidates):
            return first
        return None

    def _enforce_ratio_origin_constraint(
        self,
        payment_nodes: list,
        payment_clause: Optional[str],
        current_clauses_chunk: Optional[str],
    ) -> list:
        """初次提取兜底：原文不含显式比例依据时，强制清空 ratio。

        判断逻辑（按优先级）：
          1. 原文含显式比例字样（%/％/百分之/百分）→ 放行，ratio 来自原文，可信。
          2. 原文含余款/余额等隐含剩余语义 且 无显式金额 → 放行，
             ratio 由前序节点累计推算（如前序已付80%，当前余款=20%），可信。
          3. 原文含余款语义 但 同时有显式金额 → 强制清空 ratio。
             原因：有具体金额时 LLM 常幻觉出比例（从 RAG 借用或猜测），
             金额字段已足够表达支付信息，ratio 的来源不可信。
          4. 其余情况（仅有金额、无任何比例依据）→ 强制清空 ratio。
        """
        if not payment_nodes:
            return payment_nodes

        text = str(payment_clause or "")
        _bd_syn = get_business_dict().synonyms
        has_pct_token = any(tok in text for tok in _bd_syn.percent_tokens)
        has_residual = any(tok in text for tok in _bd_syn.residual_tokens)
        # 检查原文是否含有显式金额（带单位的数字，如 80000元 / 40.46万元 / ¥5000）
        has_explicit_amount = bool(
            re.search(r'\d[\d,，.]*\s*(万元|万|元)', text)
            or re.search(r'[¥￥]\s*\d', text)
        )

        # 放行条件：有显式 % 或（有余款语义 且 无显式金额）
        if has_pct_token or (has_residual and not has_explicit_amount):
            return payment_nodes

        for node in payment_nodes:
            ratio_val = node.get("ratio")
            if ratio_val in (None, "", "null"):
                continue
            preview = text.strip().replace("\n", " ")[:60]
            logger.warning(
                f"原文无比例字样且无余款语义，强制清空 ratio: 原ratio={ratio_val}, "
                f"原条款={preview}..."
            )
            node["ratio"] = None
        return payment_nodes

    # 为了向后兼容，保留原有的extract_ratio方法
    async def extract_ratio(self, payment_clause: str, payment_type: str, rag_results: list, current_clauses_chunk: str, state: State) -> Optional[float]:
        """
        向后兼容的方法，仅返回第一个节点的比例。
        """
        payment_nodes = await self.extract_payment_info(payment_clause, payment_type, rag_results, current_clauses_chunk, state)
        if payment_nodes:
            return payment_nodes[0].get("ratio")
        return None

# --- Pydantic 模型定义 ---
class PaymentSummaryItem(BaseModel):
    id: str = Field(..., description="用于回溯的唯一ID")
    clause_category: Optional[str] = Field(None, description="支付条款的业务分类")
    payment_clause: str
    payment_type: Optional[str]
    final_ratio: Optional[str]
    final_amount: Optional[str] = Field(None, description="最终金额")

class PaymentSummaryOutput(RootModel[List[PaymentSummaryItem]]):
    root: List[PaymentSummaryItem]

# --- PaymentSummaryRatioExtractor 类 ---
class PaymentSummaryRatioExtractor:
    """
    用于对批量支付条款进行全局复核、矫正和提取的类。
    它的LLM实例在创建时初始化一次。
    """
    def __init__(self, app_settings=None, llm_config_override: Optional[dict] = None):
        self.app_settings = app_settings or APP_CONFIG
        self.llm_chain: Optional[Runnable] = None
        self.is_ready = False
        # 缓存当前生效的 LLM 配置（dict）以支持 _should_reinitialize（I1）
        self.current_llm_config: Optional[dict] = llm_config_override
        self._initialize()

    def _should_reinitialize(self, new_cfg: Optional[dict]) -> bool:
        """与 PaymentRatioExtractor._should_reinitialize 一致：仅当关键字段变化时返回 True。"""
        if new_cfg is None:
            return False
        if not self.current_llm_config:
            return True
        for key in ("model", "model_name", "api_key", "api_base", "temperature"):
            if self.current_llm_config.get(key) != new_cfg.get(key):
                logger.info(
                    f"PaymentSummaryRatioExtractor 检测到配置变化 '{key}'，将重新初始化。"
                )
                return True
        return False

    def _initialize(self):
        """在实例化时，初始化一次LLM和调用链。"""
        llm_max_tokens = self.app_settings.llm.max_tokens
        try:
            logger.info("开始初始化PaymentSummaryRatioExtractor...")
            # 优先使用调用方注入的 override（I1），其次走默认 app_settings
            override_cfg = self.current_llm_config
            if override_cfg and override_cfg.get("api_base"):
                # 用 override 覆盖 app_settings.llm
                from app.config.config_models import LLMConfig
                try:
                    overlay = LLMConfig(**{**self.app_settings.llm.model_dump(), **override_cfg})
                except Exception:  # noqa: BLE001
                    overlay = self.app_settings.llm
                llm_config = overlay
            else:
                payment_ratio_llm_config = getattr(self.app_settings, 'payment_ratio_llm', None)
                llm_config = payment_ratio_llm_config if payment_ratio_llm_config and payment_ratio_llm_config.api_base else self.app_settings.llm
            logger.info(f"PaymentSummaryRatioExtractor 使用 LLM 配置: model={getattr(llm_config, 'model', None) or getattr(llm_config, 'model_name', None)}")

            if not (llm_config and llm_config.api_base):
                logger.warning("LLM API Base 未配置，支付比例批量复核器将不可用。")
                return

            payment_ratio_llm_config_verification = getattr(self.app_settings, 'llm', None)
            llm_config_verification = payment_ratio_llm_config_verification if payment_ratio_llm_config_verification and payment_ratio_llm_config_verification.api_base else self.app_settings.llm
            logger.info(f"PaymentSummaryRatioExtractor使用 {'专用的' if llm_config_verification == payment_ratio_llm_config_verification else '默认的'} LLM配置")

            model_identifier = llm_config.model or llm_config.model_name
            model_identifier_verification = llm_config_verification.model or llm_config_verification.model_name
            temperature = getattr(llm_config, 'temperature', 0.0)
            
            # 注意：OpenAI 接口（包括部分兼容实现）通常会对 max_completion_tokens
            # 做上限校验（如 [1, 8192]），这里不要超过 8192，否则会触发 400 错误。
            # 这里设置为 8192，已经足够覆盖多条款 + 思考过程的长输出。
            # 注意：不要设置 response_format 为 json_object，因为我们需要输出 JSON 数组而不是对象
            # if any(kw in model_identifier for kw in ["gpt", "deepseek", "qwen"]):
            #      model_kwargs["response_format"] = {"type": "json_object"}

            llm = ChatOpenAI(
                model=model_identifier, temperature=temperature,
                openai_api_key=llm_config.api_key or "EMPTY", openai_api_base=llm_config.api_base,
                max_tokens=llm_max_tokens,
                request_timeout=_LLM_REQUEST_TIMEOUT_SEC,
                max_retries=_LLM_MAX_RETRIES,
            )

            llm_verification = ChatOpenAI(
                model=model_identifier_verification, temperature=temperature,
                openai_api_key=llm_config_verification.api_key or "EMPTY", openai_api_base=llm_config_verification.api_base,
                max_tokens=llm_max_tokens,
                request_timeout=_LLM_REQUEST_TIMEOUT_SEC,
                max_retries=_LLM_MAX_RETRIES,
            )

            prompt_template = PromptTemplate(template=PAYMENT_SUMMARY_RATIO_PROMPT, input_variables=["batch_payment_items", "chunk_text_map","rag_examples"])
            install_prompt_template = PromptTemplate(template=INSTALL_PAYMENT_SUMMARY_RATIO_PROMPT, input_variables=["batch_payment_items", "chunk_text_map","rag_examples"])
            warranty_prompt_template = PromptTemplate(template=WARRANTY_SUMMARY, input_variables=["warranty_clauses"])
            result_verification_prompt_template = PromptTemplate(template=RESULT_VERIFICATION_PROMPT, input_variables=["validation_equipment", "validation_install"])
            clause_validation_prompt_template = PromptTemplate(template=PAYMENT_CLAUSE_VALIDATION_PROMPT, input_variables=["validation_clauses"])
            clause_category_prompt_template = PromptTemplate(template=PAYMENT_CLAUSE_CATEGORY_PROMPT, input_variables=["category_clause"])
            result_verification_single_group_prompt_template = PromptTemplate(template=RESULT_VERIFICATION_SINGLE_GROUP_PROMPT, input_variables=["group_clauses", "standard_nodes"])
            payment_timing_prompt_template = PromptTemplate(
                template=PAYMENT_TIMING_EXTRACTION_PROMPT,
                input_variables=["payment_clause", "payment_context", "clause_category", "standard_nodes"],
            )

            # 【重要】使用 json_schema response_format 强制结构化输出，消除 fallback 解析
            _summary_fmt    = _make_json_schema_format("payment_summary_result",     PaymentSummaryResult)
            _warranty_fmt   = _make_json_schema_format("warranty_summary_result",    WarrantySummaryResult)
            _verify_fmt     = _make_json_schema_format("verification_result",        VerificationResult)
            _val_fmt        = _make_json_schema_format("clause_validation_result",   ClauseValidationResult)
            _cat_fmt        = _make_json_schema_format("clause_category_result",     ClauseCategoryResult)
            _sg_fmt         = _make_json_schema_format("single_group_verification",  SingleGroupVerificationResult)
            _timing_fmt     = _make_json_schema_format("payment_timing_result",      PaymentTimingResult)

            self.llm_chain = prompt_template | llm.bind(response_format=_summary_fmt)
            self.install_llm_chain = install_prompt_template | llm.bind(response_format=_summary_fmt)
            self.warranty_llm_chain = warranty_prompt_template | llm.bind(response_format=_warranty_fmt)
            self.result_verification_llm_chain = result_verification_prompt_template | llm.bind(response_format=_verify_fmt)
            self.clause_validation_llm_chain = clause_validation_prompt_template | llm.bind(response_format=_val_fmt)
            self.clause_category_llm_chain = clause_category_prompt_template | llm.bind(response_format=_cat_fmt)
            self.result_verification_single_group_llm_chain = result_verification_single_group_prompt_template | llm.bind(response_format=_sg_fmt)
            self.payment_timing_llm_chain = payment_timing_prompt_template | llm.bind(response_format=_timing_fmt)

            # 记录最终生效的 LLM 配置（用于 _should_reinitialize 比对）
            self.current_llm_config = {
                "model": getattr(llm_config, "model", None),
                "model_name": getattr(llm_config, "model_name", None),
                "api_key": getattr(llm_config, "api_key", None),
                "api_base": getattr(llm_config, "api_base", None),
                "temperature": temperature,
            }
            self.is_ready = True
            logger.success(f"PaymentSummaryRatioExtractor 调用链初始化成功，使用模型: {model_identifier}")
        except Exception as e:
            logger.opt(exception=True).error("初始化 PaymentSummaryRatioExtractor 失败: {err}", err=str(e))
            self.is_ready = False

    def _parse_llm_output(self, llm_output_str: str) -> Tuple[List[Dict], Optional[Dict], Optional[str]]:
        """
        解析 LLM 结构化输出（json_schema 强制格式）。

        Chain 3/4 (PaymentSummaryResult):  {"items": [{id, payment_clause, ...}], "thinking_output": "..."}
        Chain 5  (WarrantySummaryResult):  {"items": [{warranty, effective_conditions, ...}], "thinking_output": "..."}
        Chain 6  (VerificationResult):     {"items": [{select_clause_id}], "thinking_output": "..."}

        返回: (payment_items, warranty_info, thinking_output)
        """
        payment_items: List[Dict] = []
        warranty_info: Optional[Dict] = None
        thinking_output: Optional[str] = None
        try:
            data = json.loads(llm_output_str.strip())
            items = data.get("items", []) if isinstance(data, dict) else []
            thinking_output = data.get("thinking_output") if isinstance(data, dict) else None
            for item in items:
                if not isinstance(item, dict):
                    continue
                if "payment_clause" in item and "id" in item:
                    if "final_amount" in item and item["final_amount"] is not None:
                        item["final_amount"] = str(item["final_amount"])
                    payment_items.append(item)
                elif "warranty" in item:
                    warranty_info = item
                elif "select_clause_id" in item:
                    payment_items.append(item)
            logger.success(
                f"解析出 {len(payment_items)} 个条款, "
                f"质保期: {'有' if warranty_info else '无'}, "
                f"思考: {'有' if thinking_output else '无'}"
            )
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error(f"_parse_llm_output JSON 解析失败（schema 约束下不应出现）: {e}")
        return payment_items, warranty_info, thinking_output

    def _format_rag_examples(self, rag_examples: list, title: str = "RAG参考示例") -> str:
        """
        格式化RAG示例为提示词字符串。
        
        Args:
            rag_examples: RAG示例列表
            title: 标题，默认为"RAG参考示例"
        
        Returns:
            格式化后的提示词字符串
        """
        if not rag_examples:
            return f"暂无{title}"
        
        prompt = f"## {title}\n"
        for i, example in enumerate(rag_examples, 1):
            try:
                if not isinstance(example, dict):
                    logger.warning(f"{title}示例{i}不是字典类型: {type(example)}")
                    continue
                
                clause_text = str(example.get('clause_text') or example.get('text') or '').strip()
                label = str(example.get('label') or example.get('payment_type') or '未知')
                payment_ratio = str(example.get('payment_ratio') or '未知')
                clause_context = str(example.get('clause_context') or '未知')
                
                prompt += f"### 示例{i}:\n"
                prompt += f"- **条款文本**: {clause_text}\n"
                prompt += f"- **支付类型**: {label}\n"
                prompt += f"- **支付比例**: {payment_ratio}\n"
                prompt += f"- **条款文本的上下文**: {clause_context}\n\n"
            except Exception as e:
                logger.error(f"处理{title}示例{i}时发生错误: {e}, example类型: {type(example)}", exc_info=True)
                continue
        
        return prompt

    def _get_chunks_str(self, chunk_text_map: dict, key: str) -> str:
        """
        安全地从chunk_text_map中获取chunks字符串。
        
        Args:
            chunk_text_map: 条款上下文映射
            key: 键名（如'equipment_chunks'或'installation_chunks'）
        
        Returns:
            格式化后的chunks字符串
        """
        chunks_value = chunk_text_map.get(key, '')
        if isinstance(chunks_value, str):
            return chunks_value
        elif isinstance(chunks_value, dict):
            return json.dumps(chunks_value, ensure_ascii=False, indent=2)
        else:
            return str(chunks_value) if chunks_value else ''

    def _get_warranty_clauses(self, chunk_text_map: dict) -> str:
        """
        安全地从chunk_text_map中获取质保期条款。
        
        Args:
            chunk_text_map: 条款上下文映射
        
        Returns:
            格式化后的质保期条款字符串
        """
        warranty_clauses_raw = chunk_text_map.get('warranty_sub_clauses', [])
        if isinstance(warranty_clauses_raw, list):
            warranty_clauses = warranty_clauses_raw
        elif isinstance(warranty_clauses_raw, str):
            warranty_clauses = [warranty_clauses_raw] if warranty_clauses_raw.strip() else []
        else:
            warranty_clauses = []
        
        return "\n".join(str(item) for item in warranty_clauses if item) if warranty_clauses else ""

    def _validate_batch_payment_items(self, batch_items: str, item_type: str = "支付条款") -> None:
        """
        验证批量支付条款的JSON格式。
        
        Args:
            batch_items: 批量支付条款JSON字符串
            item_type: 条款类型描述，用于错误日志
        
        Raises:
            ValueError: 如果JSON格式无效
        """
        if not batch_items or batch_items == '[]':
            return
        
        try:
            items = json.loads(batch_items)
            logger.debug(f"{item_type}输入验证通过，条目数: {len(items)}")
        except json.JSONDecodeError as json_err:
            logger.error(f"{item_type}输入JSON格式错误: {json_err}")
            logger.error(f"问题JSON字符串前500字符: {batch_items[:500]}")
            raise ValueError(f"{item_type}输入JSON格式无效: {json_err}")

    async def _process_payment_batch(
        self,
        batch_items: str,
        chunk_text_map: dict,
        rag_examples: list,
        llm_chain: Runnable,
        clause_category: str,
        chunks_key: str
    ) -> Tuple[List[Dict], Optional[Dict], Optional[str]]:
        """
        处理一批支付条款的通用函数。
        
        Args:
            batch_items: 批量支付条款JSON字符串
            chunk_text_map: 条款上下文映射
            rag_examples: RAG示例列表
            llm_chain: LLM调用链
            clause_category: 条款分类（'equipment_payment'或'installation_payment'）
            chunks_key: chunks键名（'equipment_chunks'或'installation_chunks'）
        
        Returns:
            (支付条款列表, 质保期信息, 思考过程文本)
        """
        if not batch_items or batch_items == '[]':
            return [], None

        # ========================================================================
        # 临时旁路（保留 LLM 复核代码与 prompt 以备恢复）
        # 启用方式：环境变量 SERVICE2_BYPASS_PAYMENT_REVIEW=1
        # 行为：跳过复核 LLM 调用，将 input items 字段直接 passthrough 为输出格式
        #       （init_ratio → final_ratio, init_amount → final_amount，其它原样）
        # 恢复方式：取消该环境变量（或设为 0/false）即可走回完整 LLM 复核流程
        # ========================================================================
        if os.getenv("SERVICE2_BYPASS_PAYMENT_REVIEW", "").strip().lower() in ("1", "true", "yes"):
            try:
                _input_items = json.loads(batch_items)
            except json.JSONDecodeError:
                logger.warning(f"[复核旁路] {clause_category} batch_items 解析失败，返回空")
                return [], None
            _passthrough_items: List[Dict] = []
            for _it in _input_items:
                if not isinstance(_it, dict):
                    continue
                _passthrough_items.append({
                    "id": _it.get("id", ""),
                    "payment_clause": _it.get("payment_clause", ""),
                    "payment_type": _it.get("payment_type", ""),
                    "final_ratio": _it.get("init_ratio"),
                    "final_amount": _it.get("init_amount"),
                    "clause_category": clause_category,
                })
            logger.warning(
                f"[复核旁路] SERVICE2_BYPASS_PAYMENT_REVIEW=1 已启用，"
                f"{clause_category} 跳过 LLM 复核，{len(_passthrough_items)} 条 input items 直接 passthrough"
            )
            return _passthrough_items, None
        # ========================================================================

        # 验证输入
        self._validate_batch_payment_items(batch_items, f"{clause_category}支付条款")
        
        # 准备输入
        chunks_str = self._get_chunks_str(chunk_text_map, chunks_key)
        rag_examples_prompt = self._format_rag_examples(rag_examples, "RAG参考示例" if clause_category == 'equipment_payment' else "安装RAG参考示例")
        
        input_dict = {
            "batch_payment_items": batch_items,
            "chunk_text_map": chunks_str,
            "rag_examples": rag_examples_prompt
        }
        
        logger.debug(f"准备调用{clause_category}LLM链，条目数: {len(json.loads(batch_items))}")

        # 调用LLM
        try:
            response_obj = await llm_guarded_ainvoke(llm_chain, input_dict)
        except KeyError as ke:
            logger.error(f"调用{clause_category}LLM链时发生KeyError: {ke}")
            logger.error(f"输入字典键: {list(input_dict.keys())}")
            raise
        
        raw_output_str = str(getattr(response_obj, "content", response_obj)).strip()
        
        # 检测并处理重复输出（如果输出被截断，LLM可能会重复生成）
        # 使用括号匹配来找到第一个完整的JSON数组，而不是简单的 find(']')
        # 这样可以避免在嵌套结构中错误截断
        start_idx = raw_output_str.find('[')
        if start_idx != -1:
            bracket_count = 0
            first_array_end = -1
            for i, char in enumerate(raw_output_str[start_idx:], start_idx):
                if char == '[':
                    bracket_count += 1
                elif char == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        first_array_end = i + 1
                        break
            
            # 如果找到了第一个完整的JSON数组，检查后续是否有重复内容
            if first_array_end != -1 and first_array_end < len(raw_output_str):
                # 检查后续是否有重复的模式（检查更多字符以确保准确性）
                after_first_array = raw_output_str[first_array_end:first_array_end+200]
                # 检查是否有新的JSON数组开始标记或支付条款对象
                if ('[{' in after_first_array or '{"id":' in after_first_array or 
                    (after_first_array.strip().startswith('[') and len(after_first_array.strip()) > 10)):
                    logger.warning("检测到输出中存在重复内容，截断到第一个完整JSON数组结束位置")
                    raw_output_str = raw_output_str[:first_array_end]
                    logger.debug(f"截断后的字符串长度: {len(raw_output_str)}, 前500字符: {raw_output_str[:500]}...")

        payment_items, _, thinking_text = self._parse_llm_output(raw_output_str)
        # 添加分类字段
        for item in payment_items:
            item['clause_category'] = clause_category
        
        # ID 强校验：检查 LLM 输出的 id 是否与输入一致，不一致则按顺序位置重映射
        try:
            input_items = json.loads(batch_items)
            input_ids = [it.get('id', '') for it in input_items if isinstance(it, dict)]
            output_ids = [it.get('id', '') for it in payment_items if isinstance(it, dict)]
            
            # 检查输出 id 是否全部在输入 id 中
            input_id_set = set(input_ids)
            mismatched_ids = [oid for oid in output_ids if oid not in input_id_set]
            
            if mismatched_ids and len(payment_items) == len(input_ids):
                # 输出数量与输入一致但 ID 不匹配：按顺序位置重映射
                logger.warning(
                    f"LLM 复核输出 ID 与输入不一致（不匹配: {mismatched_ids}），"
                    f"按顺序位置重映射: {output_ids} → {input_ids}"
                )
                for idx_item, correct_id in zip(payment_items, input_ids):
                    idx_item['id'] = correct_id
            elif mismatched_ids and len(payment_items) > len(input_ids):
                # 输出数量 > 输入：LLM幻觉产生了多余节点，过滤掉不属于输入的项
                logger.warning(
                    f"LLM 复核输出多于输入（输入 {len(input_ids)} 条，输出 {len(payment_items)} 条），"
                    f"不匹配ID: {mismatched_ids}，过滤幻觉节点"
                )
                valid_items = [it for it in payment_items if it.get('id', '') in input_id_set]
                if valid_items:
                    removed_count = len(payment_items) - len(valid_items)
                    payment_items = valid_items
                    logger.info(f"过滤掉 {removed_count} 个幻觉节点，保留 {len(payment_items)} 个有效节点")
                else:
                    # 所有ID都不匹配，但数量>=输入数量，尝试按顺序取前N个重映射
                    logger.warning("过滤后无有效节点，按顺序取前N个重映射")
                    payment_items = payment_items[:len(input_ids)]
                    for idx_item, correct_id in zip(payment_items, input_ids):
                        idx_item['id'] = correct_id
            elif mismatched_ids:
                logger.warning(
                    f"LLM 复核输出 ID 部分不匹配: {mismatched_ids}，"
                    f"输入 {len(input_ids)} 条，输出 {len(payment_items)} 条，无法按位置重映射"
                )
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass  # batch_items 解析失败时跳过校验
        
        return payment_items, thinking_text

    async def extract_summary(self, batch_equipment_payment_items: str, batch_install_payment_items: str, chunk_text_map: dict, all_equipment_rag_examples:list, all_installation_rag_examples:list) -> Tuple[List[PaymentSummaryItem], Optional[WarrantyInfo], Optional[ThinkingInfo]]:
        """
        主执行函数，负责批量复核。
        """
        if not self.is_ready or not self.llm_chain or not self.install_llm_chain:
            logger.error("支付比例批量复核器未就绪，跳过。")
            return [], None
        
        try:
            # # 准备质保期条款
            # warranty_clauses = chunk_text_map.get('warranty_sub_clauses', [])
            # warranty_clauses_list = warranty_clauses if isinstance(warranty_clauses, list) else ([warranty_clauses] if isinstance(warranty_clauses, str) and warranty_clauses.strip() else [])
            # warranty_prompt = self._get_warranty_clauses(chunk_text_map)
            
            # 检查batch_equipment_payment_items是否为空
            equipment_batch_empty = False
            if not batch_equipment_payment_items or not batch_equipment_payment_items.strip():
                equipment_batch_empty = True
            else:
                try:
                    equipment_items = json.loads(batch_equipment_payment_items)
                    if not isinstance(equipment_items, list) or len(equipment_items) == 0:
                        equipment_batch_empty = True
                except (json.JSONDecodeError, TypeError):
                    equipment_batch_empty = True
            
            # 检查batch_install_payment_items是否为空
            install_batch_empty = False
            if not batch_install_payment_items or not batch_install_payment_items.strip():
                install_batch_empty = True
            else:
                try:
                    install_items = json.loads(batch_install_payment_items)
                    if not isinstance(install_items, list) or len(install_items) == 0:
                        install_batch_empty = True
                except (json.JSONDecodeError, TypeError):
                    install_batch_empty = True
            
            # 处理设备支付条款（仅当不为空时）
            payment_equipment_items = []
            thinking_equipment_text = None
            if not equipment_batch_empty:
                payment_equipment_items, thinking_equipment_text = await self._process_payment_batch(
                    batch_equipment_payment_items,
                    chunk_text_map,
                    all_equipment_rag_examples,
                    self.llm_chain,
                    'equipment_payment',
                    'equipment_chunks'
                )

            # 处理安装支付条款（仅当不为空时）
            payment_install_items = []
            thinking_install_text = None
            if not install_batch_empty:
                payment_install_items,  thinking_install_text = await self._process_payment_batch(
                    batch_install_payment_items,
                    chunk_text_map,
                    all_installation_rag_examples,
                    self.install_llm_chain,
                    'installation_payment',
                    'installation_chunks'
                )
            # 验证并转换数据
            validated_payments = []
            if payment_equipment_items:
                try:
                    validated_output = PaymentSummaryOutput.model_validate(payment_equipment_items)
                    validated_payments.extend(validated_output.root)
                except Exception as e:
                    logger.warning(f"Pydantic 验证设备支付条款失败: {e}")
            
            if payment_install_items:
                try:
                    validated_output = PaymentSummaryOutput.model_validate(payment_install_items)
                    validated_payments.extend(validated_output.root)
                except Exception as e:
                    logger.warning(f"Pydantic 验证安装支付条款失败: {e}")

            # # 处理质保期信息
            # final_warranty_info = None
            # if warranty_data and warranty_clauses_list:
            #     warranty_data['warranty_clause'] = warranty_prompt
            #     try:
            #         final_warranty_info = WarrantyInfo.model_validate(warranty_data)
            #     except Exception as e:
            #         logger.warning(f"Pydantic 验证质保期信息失败: {e}")
            
            # 合并思考过程
            final_thinking_info = None
            thinking_parts = []
            if thinking_equipment_text and thinking_equipment_text.strip():
                thinking_parts.append(thinking_equipment_text.strip())
            if thinking_install_text and thinking_install_text.strip():
                thinking_parts.append(thinking_install_text.strip())
            
            if thinking_parts:
                combined_thinking_text = "\n\n".join(thinking_parts)
                try:
                    final_thinking_info = ThinkingInfo.model_validate({"thinking_output": combined_thinking_text})
                    logger.success(f"成功合并设备、安装的思考过程，总长度: {len(combined_thinking_text)} 字符")
                except Exception as e:
                    logger.warning(f"Pydantic 验证合并思考过程失败: {e}")

            return validated_payments, final_thinking_info

        except Exception as e:
            logger.error(f"调用LLM或处理其输出时发生错误: {e}, 错误类型: {type(e).__name__}", exc_info=True)
            return [], None


    async def extract_summary_warranty(self, warranty_clauses):
        """
        单独处理质保条款，去除与支付条款相关的逻辑。
        返回 ([], WarrantyInfo | None, ThinkingInfo | None)
        """
        if not self.is_ready or not self.warranty_llm_chain:
            logger.error("质保期复核器未就绪，跳过。")
            return [], None, None

        try:
            # 归一化输入为列表
            warranty_list = warranty_clauses if isinstance(warranty_clauses, list) else (
                [warranty_clauses] if isinstance(warranty_clauses, str) and warranty_clauses.strip() else []
            )
            if not warranty_list:
                logger.info("没有提供质保期条款，跳过质保抽取。")
                return [], None, None

            # 使用现有的帮助函数构建质保提示（返回字符串）
            chunk_text_map = {"warranty_sub_clauses": warranty_list}
            warranty_prompt = self._get_warranty_clauses(chunk_text_map)

            # 调用 LLM= ，输入与 _process_payment_batch 的部分键保持兼容
            input_dict = {
                "warranty_clauses": warranty_prompt,
            }
            try:
                response_obj = await llm_guarded_ainvoke(self.warranty_llm_chain, input_dict)
            except Exception as e:
                logger.error(f"调用质保LLM链失败: {e}", exc_info=True)
                return [], None, None

            raw_output_str = str(getattr(response_obj, "content", response_obj)).strip()
            # 解析LLM输出以提取质保信息与思考过程
            _, warranty_data, thinking_text = self._parse_llm_output(raw_output_str)

            final_warranty_info = None
            if warranty_data:
                # 补充原始质保条款上下文
                warranty_data['warranty_clause'] = warranty_prompt
                try:
                    final_warranty_info = WarrantyInfo.model_validate(warranty_data)
                except Exception as e:
                    logger.warning(f"Pydantic 验证质保期信息失败: {e}")

            final_thinking_info = None
            if thinking_text and thinking_text.strip():
                try:
                    final_thinking_info = ThinkingInfo.model_validate({"thinking_output": thinking_text.strip()})
                except Exception as e:
                    logger.warning(f"Pydantic 验证思考过程失败: {e}")

            return [], final_warranty_info, final_thinking_info

        except Exception as e:
            logger.error(f"单独质保抽取时发生未知错误: {e}", exc_info=True)
            return [], None, None



    async def result_verification(self, validation_equipment: str,  validation_install_str: str) -> Tuple[List[str],  Optional[ThinkingInfo]]:
        """
        检验函数，负责批量输出结果的校验，主要针对重复节点。
        返回挑选出的条款ID列表和思考过程信息。
        """
        if not self.is_ready or not self.result_verification_llm_chain:
            logger.error("支付比例批量复核器未就绪，跳过。")
            return [], None
        
        raw_llm_output_str = ""
        try:
            # 1. 准备提示词输入
            if validation_equipment != '[]' or validation_install_str != '[]':
                input_dict = {
                    "validation_equipment": validation_equipment,
                    "validation_install": validation_install_str,
                }

            # 2. 调用LLM获取原始字符串输出
            response_verification_obj = await llm_guarded_ainvoke(self.result_verification_llm_chain, input_dict)

            llm_verification_output_str = str(getattr(response_verification_obj, "content", response_verification_obj)).strip()

            # 3. 解析LLM输出，提取ID列表和思考过程
            selected_ids, thinking_text = self._parse_verification_output(llm_verification_output_str)
            
            # 4. 处理思考过程信息
            final_thinking_info = None
            if thinking_text and thinking_text.strip():
                try:
                    final_thinking_info = ThinkingInfo.model_validate({"thinking_output": thinking_text.strip()})
                    logger.success(f"复核后验证的思考过程，总长度: {len(thinking_text)} 字符")
                except Exception as e:
                    logger.warning(f"Pydantic 复核后验证的思考校验失败: {e}")

            return selected_ids, final_thinking_info

        except Exception as e:
            logger.error(f"调用LLM或处理其输出时发生未知错误: {e}", exc_info=True)
            logger.error(f"发生错误时的原始LLM输出: {raw_llm_output_str}")
            return [], None

    async def verify_single_group_single(self, group_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        单组去重 / 类型纠正：对同一 payment_type 组的 N 个候选条款，由 LLM 决策：
          - select_one  → 真正重复，保留一条，其余丢弃
          - correct_type → 类型误判（不同 ratio），纠正 payment_type，全部保留

        Returns:
            {
              "action": "select_one" | "correct_type",
              "select_clause_id": <str|None>,
              "corrections": [{"id": ..., "corrected_payment_type": ...}, ...],
              "reason": <str>
            }
            LLM 未就绪 / 调用异常 / 解析失败 → 代码级兜底：select_one，按 _pick_best_clause 规则打分选一个。
        """
        candidate_ids = [str(it.get("id", "")) for it in group_items if isinstance(it, dict)]
        candidate_id_set = {cid for cid in candidate_ids if cid}

        def _code_fallback(reason: str) -> Dict[str, Any]:
            def _score(it: Dict[str, Any]) -> tuple:
                clause = str(it.get("payment_clause", "") or "")
                has_action = bool(re.search(r'支付|付款|汇入|付清', clause))
                has_amount = bool(re.search(r'\d+(\.\d+)?%|百分之|万元|元整|\d+元', clause))
                score_action = 2 if (has_action and has_amount) else (1 if has_action else 0)
                return (score_action, len(clause))
            best = max(group_items, key=_score) if group_items else {}
            return {
                "action": "select_one",
                "select_clause_id": str(best.get("id", "")),
                "corrections": [],
                "reason": f"兜底({reason})",
            }

        if not group_items:
            return {"action": "select_one", "select_clause_id": "", "corrections": [], "reason": "空组"}
        if len(group_items) == 1:
            return {"action": "select_one", "select_clause_id": candidate_ids[0], "corrections": [], "reason": "单条直通"}
        if not self.is_ready or not getattr(self, "result_verification_single_group_llm_chain", None):
            return _code_fallback("LLM未就绪")

        try:
            payload = [
                {
                    "id": str(it.get("id", "")),
                    "payment_clause": (str(it.get("payment_clause", "") or ""))[:500],
                    "payment_type": it.get("payment_type", ""),
                    "final_ratio": it.get("final_ratio", ""),
                    "final_amount": it.get("final_amount", ""),
                }
                for it in group_items
            ]
            # 根据 clause_category 注入对应分类的标准节点列表，避免跨分类节点污染
            category = str(group_items[0].get("clause_category", "") or "") if group_items else ""
            standard_nodes = INSTALL_STANDARD_NODES_STR if category == "installation_payment" else EQUIPMENT_STANDARD_NODES_STR
            input_dict = {
                "group_clauses": json.dumps(payload, ensure_ascii=False),
                "standard_nodes": standard_nodes,
            }
            response_obj = await llm_guarded_ainvoke(
                self.result_verification_single_group_llm_chain, input_dict
            )
            llm_output_str = str(getattr(response_obj, "content", response_obj)).strip()

            try:
                parsed = json.loads(llm_output_str.strip())
            except (json.JSONDecodeError, ValueError):
                return _code_fallback("JSON解析失败")

            if not isinstance(parsed, dict):
                return _code_fallback("输出结构非法")

            action = str(parsed.get("action", "select_one")).strip()

            if action == "correct_type":
                corrections_raw = parsed.get("corrections") or []
                corrections = []
                for corr in corrections_raw:
                    if not isinstance(corr, dict):
                        continue
                    cid = str(corr.get("id", "")).strip()
                    new_type = str(corr.get("corrected_payment_type", "")).strip()
                    if cid in candidate_id_set and new_type:
                        corrections.append({"id": cid, "corrected_payment_type": new_type})
                    else:
                        logger.warning(f"单组去重 correct_type：跳过无效修正项 id={cid}, type={new_type}")
                if not corrections:
                    logger.warning(f"单组去重 correct_type：修正列表为空，降级为兜底 select_one")
                    return _code_fallback("correct_type但corrections为空")
                return {
                    "action": "correct_type",
                    "select_clause_id": None,
                    "corrections": corrections,
                    "reason": str(parsed.get("reason", "") or ""),
                }

            # 默认按 select_one 处理
            selected = str(parsed.get("select_clause_id", "")).strip()
            reason = str(parsed.get("reason", "") or "")
            if selected not in candidate_id_set:
                logger.warning(f"单组去重：LLM 选出 id={selected} 不在候选 {candidate_ids} 中，代码级兜底")
                return _code_fallback("LLM选出的id不在候选中")
            return {
                "action": "select_one",
                "select_clause_id": selected,
                "corrections": [],
                "reason": reason,
            }
        except Exception as e:
            logger.warning(f"单组去重 LLM 调用异常: {e}，代码级兜底")
            return _code_fallback(f"LLM异常:{type(e).__name__}")

    async def extract_payment_timing_single(
        self,
        payment_clause: str,
        payment_context: str,
        clause_category: str = "equipment_payment",
    ) -> Dict[str, Any]:
        """Stage 7：提取单条 (条款, 上下文) 的付款时效三指标。

        输出字段：
            payment_days (int|null)：常规付款周期（天）
            latest_payment_stage (str|null)：最迟付款节点（必须从对应类别的标准节点白名单中选取，
                且条款/上下文必须字面包含"最迟"二字）
            latest_payment_date (int|null)：最迟付款时间（截止天数）

        失败容忍：LLM 调用 / 解析异常一律降级为三字段全 null，不抛错。
        """
        default = {
            "payment_days": None,
            "latest_payment_stage": None,
            "latest_payment_date": None,
        }
        if not getattr(self, "is_ready", False):
            logger.warning("[timing] PaymentSummaryRatioExtractor 未就绪，返回 null")
            return default

        clause_text = (payment_clause or "").strip()
        context_text = (payment_context or "").strip()
        if not clause_text:
            return default

        # 加载对应类别的标准节点白名单（用于 prompt 注入 + 后置校验兜底）
        try:
            bd = get_business_dict()
            if clause_category == "installation_payment":
                whitelist_set = set(bd.install.payment_type_whitelist)
            else:
                whitelist_set = set(bd.equipment.payment_type_whitelist)
            standard_nodes_str = " | ".join(sorted(whitelist_set))
        except Exception as e:
            logger.warning(f"[timing] 业务词典加载失败，跳过白名单约束: {e}")
            whitelist_set = set()
            standard_nodes_str = "（白名单不可用）"

        # 硬约束：若条款 + 上下文均无"最迟"二字，直接返回 payment_days 由 LLM 提取，
        # 但 latest_* 在后置阶段强制置 null，避免 LLM 越权臆测
        full_text_for_check = f"{clause_text}\n{context_text}"
        has_zui_chi = "最迟" in full_text_for_check

        try:
            response_obj = await llm_guarded_ainvoke(
                self.payment_timing_llm_chain,
                {
                    "payment_clause": clause_text,
                    "payment_context": context_text or clause_text,
                    "clause_category": (
                        "安装付款（installation_payment）"
                        if clause_category == "installation_payment"
                        else "设备付款（equipment_payment）"
                    ),
                    "standard_nodes": standard_nodes_str,
                },
            )
            raw = str(getattr(response_obj, "content", response_obj)).strip()
            try:
                data: Optional[Dict[str, Any]] = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                logger.warning(f"[timing] JSON 解析失败（schema 约束下不应出现）: {raw[:200]}")
                return default
            if not isinstance(data, dict):
                logger.warning(f"[timing] JSON 解析失败，原始输出: {raw[:200]}")
                return default

            def _coerce_int(v: Any) -> Optional[int]:
                if v is None:
                    return None
                if isinstance(v, bool):
                    return None
                if isinstance(v, int):
                    return v if v >= 0 else None
                if isinstance(v, float):
                    return int(v) if v >= 0 else None
                if isinstance(v, str):
                    s = v.strip()
                    if not s or s.lower() in ("null", "none"):
                        return None
                    m = re.search(r"\d+", s)
                    if m:
                        try:
                            n = int(m.group(0))
                            return n if n >= 0 else None
                        except Exception:
                            return None
                return None

            def _coerce_str(v: Any) -> Optional[str]:
                if v is None:
                    return None
                if isinstance(v, str):
                    s = v.strip()
                    if not s or s.lower() in ("null", "none"):
                        return None
                    return s
                return None

            payment_days = _coerce_int(data.get("payment_days"))
            latest_stage = _coerce_str(data.get("latest_payment_stage"))
            latest_date = _coerce_int(data.get("latest_payment_date"))

            # ===== 后置硬约束兜底 =====
            # (1) 条款无"最迟"二字 → latest_* 强制置 null（防止 LLM 把普通触发条件误当作最迟节点）
            if not has_zui_chi:
                if latest_stage is not None or latest_date is not None:
                    logger.info(
                        f"[timing] 条款无'最迟'二字，强制清空 latest_payment_stage='{latest_stage}', "
                        f"latest_payment_date={latest_date} → null"
                    )
                latest_stage = None
                latest_date = None

            # (2) latest_payment_stage 不在白名单中 → 强制置 null
            if latest_stage is not None and whitelist_set and latest_stage not in whitelist_set:
                logger.info(
                    f"[timing] latest_payment_stage='{latest_stage}' 不在 {clause_category} "
                    f"白名单中，强制置 null"
                )
                latest_stage = None

            return {
                "payment_days": payment_days,
                "latest_payment_stage": latest_stage,
                "latest_payment_date": latest_date,
            }
        except Exception as e:
            logger.warning(f"[timing] 提取异常: {type(e).__name__}: {e}")
            return default


    def _parse_verification_output(self, llm_output: str) -> Tuple[List[str], str]:
        """
        解析 Chain 6 (VerificationResult) 输出：{"items": [{select_clause_id}], "thinking_output": "..."}
        返回: (selected_ids, thinking_text)
        """
        try:
            data = json.loads(llm_output.strip())
            items = data.get("items", []) if isinstance(data, dict) else []
            thinking_text = data.get("thinking_output", "") if isinstance(data, dict) else ""
            selected_ids = [item["select_clause_id"] for item in items if isinstance(item, dict) and "select_clause_id" in item]
            logger.info(f"解析校验结果：挑选出 {len(selected_ids)} 个条款ID")
            return selected_ids, thinking_text or ""
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error(f"_parse_verification_output JSON 解析失败: {e}")
            return [], ""

    @staticmethod
    def _validation_fallback(clause: Dict[str, Any], reason: str) -> Dict[str, Any]:
        """fail-open 兜底：解析失败/异常时保留条款，交由后续提取/复核阶段判断。"""
        return {
            "id": clause.get("id", ""),
            "is_valid": True,
            "reason": f"fallback_keep: {reason}",
            "text": clause.get("clause", ""),
            "clause_class": clause.get("clause_class", ""),
            "clause_context": clause.get("clause_context", ""),
        }

    async def validate_payment_clause_single(self, clause: Dict[str, Any]) -> Dict[str, Any]:
        """
        校验单条条款是否为有效的付款条款（过滤掉非付款条款如保函、程序性条款等）。

        Args:
            clause: 待校验的条款，包含:
                - id: 条款唯一标识
                - clause: 子条款文本
                - clause_class: 条款分类列表
                - clause_context: 完整段落上下文（可选）

        Returns:
            Dict: 校验结果，包含 id, is_valid, reason 等字段
        """
        if not self.is_ready or not self.clause_validation_llm_chain:
            logger.error("条款校验器未就绪，跳过校验。")
            return self._validation_fallback(clause, "validator_not_ready")

        try:
            clause_class = clause.get("clause_class", [])
            # 确定条款分类
            if "installation_payment" in clause_class or "安装付款条款" in clause_class:
                category = "installation_payment"
            elif "equipment_payment" in clause_class or "设备付款条款" in clause_class:
                category = "equipment_payment"
            else:
                category = clause_class[0] if clause_class else "unknown"

            clause_for_validation = {
                "id": clause.get("id", ""),
                "clause": clause.get("clause", ""),
                "clause_class": category,
            }

            # 注意：此处刻意不使用 json.dumps 序列化为 JSON 对象字符串。
            # 因为完整 JSON 对象（如 {"id":2,"clause":"...","clause_class":"..."}）会让
            # 模型把单条输入误识别为"列表中的一项"，触发自回归续写幻觉，
            # 接龙输出 id=3、id=4 等伪造条款（线上已观测到该错位现象）。
            # 改为 plain key/value 多行文本，与单条校验语义一致，避免诱导列表续写。
            clause_str = (
                f'"id": {clause_for_validation["id"]}\n'
                f'"clause": {clause_for_validation["clause"]}\n'
                f'"clause_class": {clause_for_validation["clause_class"]}'
            )

            input_dict = {
                "validation_clauses": clause_str,
            }
            response_obj = await llm_guarded_ainvoke(self.clause_validation_llm_chain, input_dict)
            llm_output_str = str(getattr(response_obj, "content", response_obj)).strip()
            logger.debug(f"条款校验LLM原始输出 ID={clause_for_validation['id']}: {llm_output_str[:300]}")

            result = self._parse_single_clause_validation_output(llm_output_str, clause_for_validation)
            return result

        except Exception as e:
            logger.error(f"条款校验失败 ID={clause.get('id', '?')}: {e}", exc_info=True)
            return self._validation_fallback(clause, f"exception: {type(e).__name__}")

    def _parse_single_clause_validation_output(self, llm_output: str, original_clause: Dict[str, Any]) -> Dict[str, Any]:
        """
        解析 Chain 7 (ClauseValidationResult) 输出：{"id": "...", "is_valid": bool, "reason": "..."}
        json_schema 强制保证合法 JSON；保留 ID 错位守护业务逻辑。
        """
        try:
            parsed_data = json.loads(llm_output.strip())
            if not isinstance(parsed_data, dict):
                return self._validation_fallback(original_clause, "输出结构非法")

            # ID 错位守护：LLM 选错 id 时判定为幻觉，回退 fail-open 兜底
            parsed_id = str(parsed_data.get("id", "")).strip()
            original_id = str(original_clause.get("id", "")).strip()
            if parsed_id and original_id and parsed_id != original_id:
                logger.warning(
                    f"条款校验ID错位: 输入ID={original_id}, LLM输出ID={parsed_id}，回退至 fail-open 兜底"
                )
                return self._validation_fallback(original_clause, f"id_mismatch:{parsed_id}")

            return {
                "id": parsed_data.get("id", original_clause.get("id", "")),
                "is_valid": bool(parsed_data.get("is_valid", True)),
                "reason": parsed_data.get("reason", ""),
                "text": original_clause.get("clause", ""),
                "clause_class": original_clause.get("clause_class", ""),
                "clause_context": original_clause.get("clause_context", ""),
            }
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error(f"解析单条条款校验输出失败 ID={original_clause.get('id')}: {e}")
            return self._validation_fallback(original_clause, f"parse_exception: {type(e).__name__}")

    def _infer_validity_from_text(self, text: str, original_clause: Dict[str, Any]) -> Dict[str, Any]:
        """从LLM输出的自然语言部分推断 is_valid。

        策略：否定模式仅保留明确写出 "false" 的信号；
        模糊判断词（如"无具体金额"）不再直接视为 false，
        交由调用方用原文特征二次校验。

        返回 dict: is_valid 可能为 True / False / None，
        区分 "LLM明确判断false" 和 "无法推断"。
        """
        import re
        clause_id = original_clause.get("id", "")

        # 否定判断指标：仅保留 LLM 明确写出 false 的信号（避免模糊判断词误杀）
        negative_patterns = [
            r'→\s*false',
            r'is_valid["\s:]*false',
        ]
        for pat in negative_patterns:
            if re.search(pat, text, re.IGNORECASE):
                logger.debug(f"从LLM前置文本推断 is_valid=false, ID={clause_id}, 匹配: {pat}")
                return {
                    "id": clause_id,
                    "is_valid": False,
                    "reason": f"inferred_false: {text[:80]}",
                    "text": original_clause.get("clause", ""),
                    "clause_class": original_clause.get("clause_class", ""),
                    "clause_context": original_clause.get("clause_context", ""),
                }

        # 肯定判断指标
        positive_patterns = [
            r'→\s*true',
            r'is_valid["\s:]*true',
        ]
        for pat in positive_patterns:
            if re.search(pat, text, re.IGNORECASE):
                logger.debug(f"从LLM前置文本推断 is_valid=true, ID={clause_id}")
                return {
                    "id": clause_id,
                    "is_valid": True,
                    "reason": f"inferred_true: {text[:80]}",
                    "text": original_clause.get("clause", ""),
                    "clause_class": original_clause.get("clause_class", ""),
                    "clause_context": original_clause.get("clause_context", ""),
                }

        # 无法推断 → 返回 None（与 fail-open 区分），交由调用方用原文特征决策
        logger.debug(f"LLM输出无法明确推断有效性，返回 None, ID={clause_id}")
        return {
            "id": clause_id,
            "is_valid": None,
            "reason": "cannot_infer_from_text",
            "text": original_clause.get("clause", ""),
            "clause_class": original_clause.get("clause_class", ""),
            "clause_context": original_clause.get("clause_context", ""),
        }

    # ==================================================================================
    # 混签付款条款归属判定（仅对 clause_class 含 "混签付款条款" / "mixed_payment" 的条款触发）
    # 用途：将混签条款精确路由为 equipment_payment / installation_payment / both
    # ==================================================================================
    async def classify_mixed_category_single(self, clause: Dict[str, Any]) -> Dict[str, Any]:
        """
        单条混签条款归属判定，返回 {"id", "category", "reason"}。
        category ∈ {"equipment_payment", "installation_payment", "both"}，
        解析失败或输出非法时兜底为 "equipment_payment"。

        Args:
            clause: {"id", "clause", "clause_context"}
        """
        clause_id = clause.get("id", "")
        # clause_context 做长度截断（9B 长文本理解一般，避免注意力稀释）
        ctx_raw = clause.get("clause_context", "") or ""
        if len(ctx_raw) > 600:
            ctx_raw = ctx_raw[:300] + "..." + ctx_raw[-300:]

        fallback = {"id": clause_id, "category": "equipment_payment", "reason": "兜底(分类器未就绪或解析失败)"}

        if not self.is_ready or not getattr(self, "clause_category_llm_chain", None):
            logger.error(f"混签归属判定器未就绪，兜底→equipment ID={clause_id}")
            return fallback

        try:
            payload = {
                "id": clause_id,
                "clause": clause.get("clause", ""),
                "clause_context": ctx_raw,
            }
            input_dict = {"category_clause": json.dumps(payload, ensure_ascii=False)}
            response_obj = await llm_guarded_ainvoke(self.clause_category_llm_chain, input_dict)
            llm_output_str = str(getattr(response_obj, "content", response_obj)).strip()
            logger.debug(f"混签归属判定LLM原始输出 ID={clause_id}: {llm_output_str[:300]}")

            return self._parse_mixed_category_output(llm_output_str, clause_id)
        except Exception as e:
            logger.error(f"混签归属判定失败 ID={clause_id}: {e}", exc_info=True)
            return fallback

    def _parse_mixed_category_output(self, llm_output: str, clause_id: str) -> Dict[str, Any]:
        """
        解析 Chain 8 (ClauseCategoryResult) 输出：{"id": "...", "category": enum, "reason": "..."}
        兜底规则：非法枚举 → category="equipment_payment"
        """
        valid_enum = {"equipment_payment", "installation_payment", "both"}
        fallback = {"id": clause_id, "category": "equipment_payment", "reason": "兜底(解析失败或非法枚举)"}
        try:
            parsed = json.loads(llm_output.strip())
            if not isinstance(parsed, dict):
                return fallback
            raw_cat = (parsed.get("category") or "").strip()
            if raw_cat in valid_enum:
                return {
                    "id": parsed.get("id", clause_id) or clause_id,
                    "category": raw_cat,
                    "reason": parsed.get("reason", ""),
                }
            return fallback
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error(f"解析混签归属输出失败 ID={clause_id}: {e}")
            return fallback


# =============================================================================
# 进程级单例 getter：避免每请求 / 每段落反复构造 ChatOpenAI + PromptTemplate 链。
# 典型场景下 APP_CONFIG 在进程生命周期内不变，因此使用最简单的懒加载单例。
# =============================================================================

_summary_extractor_singleton: Optional["PaymentSummaryRatioExtractor"] = None
_summary_extractor_lock = asyncio.Lock()

_ratio_extractor_singleton: Optional["PaymentRatioExtractor"] = None
_ratio_extractor_lock = asyncio.Lock()


async def get_summary_extractor(llm_config: Optional[dict] = None) -> "PaymentSummaryRatioExtractor":
    """返回进程级 PaymentSummaryRatioExtractor 单例。

    若传入 llm_config 且与缓存差异显著，会按 _should_reinitialize 规则重建。
    """
    global _summary_extractor_singleton
    async with _summary_extractor_lock:
        if _summary_extractor_singleton is None:
            _summary_extractor_singleton = PaymentSummaryRatioExtractor(llm_config_override=llm_config)
        elif (
            llm_config is not None
            and (
                not _summary_extractor_singleton.is_ready
                or _summary_extractor_singleton._should_reinitialize(llm_config)
            )
        ):
            _summary_extractor_singleton = PaymentSummaryRatioExtractor(llm_config_override=llm_config)
    return _summary_extractor_singleton


async def get_ratio_extractor(llm_config: Optional[dict] = None) -> "PaymentRatioExtractor":
    """
    返回进程级 PaymentRatioExtractor 单例。如果传入 llm_config 且与现有缓存不一致，
    会按 _should_reinitialize 规则重建 LLM（加锁保护，避免并发竞态）。
    """
    global _ratio_extractor_singleton
    if _ratio_extractor_singleton is None:
        async with _ratio_extractor_lock:
            if _ratio_extractor_singleton is None:
                _ratio_extractor_singleton = PaymentRatioExtractor()
    if llm_config is not None:
        if (not _ratio_extractor_singleton.is_ready) or _ratio_extractor_singleton._should_reinitialize(llm_config):
            async with _ratio_extractor_lock:
                if (not _ratio_extractor_singleton.is_ready) or _ratio_extractor_singleton._should_reinitialize(llm_config):
                    _ratio_extractor_singleton._initialize_llm(llm_config)
    return _ratio_extractor_singleton
