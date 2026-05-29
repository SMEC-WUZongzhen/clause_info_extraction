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
from app.states.states import State, WarrantyInfo, ThinkingInfo
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
)
from app.config.config import APP_CONFIG
from app.config.business_dict import get_business_dict
from app.utils.concurrency import llm_guarded_ainvoke, LLM_CALL_TIMEOUT_SEC
import json
import asyncio
from pydantic import BaseModel, RootModel, Field


# ===== LLM 调用默认参数（可由环境变量统一调整，显式配置避免 LangChain 默认值隐患）=====
_LLM_REQUEST_TIMEOUT_SEC = int(os.getenv("LLM_REQUEST_TIMEOUT_SEC", "60"))
_LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))


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
                openai_api_key=llm_config.get("api_key"),
                openai_api_base=llm_config.get("api_base"),
                max_tokens=llm_max_tokens,
                request_timeout=_LLM_REQUEST_TIMEOUT_SEC,
                max_retries=_LLM_MAX_RETRIES,
            )
            
            equipment_prompt = PromptTemplate.from_template(EQUIPMENT_PAYMENT_RATIO_PROMPT)
            install_prompt = PromptTemplate.from_template(INSTALL_PAYMENT_RATIO_PROMPT)
            self.equipment_chain = equipment_prompt | self.llm
            self.install_chain = install_prompt | self.llm
            self.chain = self.equipment_chain  # 向后兼容
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
        解析LLM返回的多节点JSON数组响应。
        
        支持三级降级：
        1. 正常解析：JSON数组 → 多个 {payment_type, ratio, amount} dict
        2. 兼容降级：如果LLM返回单对象（旧格式），包装为长度1的数组
        3. 正则兜底：复用 _parse_payment_info_from_text() 作为最后手段
        
        返回: List[Dict[str, Any]]，每个dict包含 {payment_type, ratio, amount}
        """
        if not text or not text.strip() or text.lower() in ['none', 'null', 'n/a', '无', '未知', '不明确']:
            return []
        
        cleaned_text = text.strip()
        # 移除markdown代码块标记
        cleaned_text = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_text, flags=re.MULTILINE)
        cleaned_text = re.sub(r'\n?```\s*$', '', cleaned_text, flags=re.MULTILINE)
        cleaned_text = cleaned_text.strip()
        
        # 尝试1: 直接解析整段文本为JSON
        json_parsed = self._try_parse_json_nodes(cleaned_text)
        if json_parsed is not None:
            return json_parsed
        
        # 尝试2: 从混合文本中提取 JSON 数组片段（LLM可能在JSON前后附带解释文字）
        array_match = self._extract_json_array_from_text(cleaned_text)
        if array_match:
            json_parsed = self._try_parse_json_nodes(array_match)
            if json_parsed is not None:
                return json_parsed
        
        # 尝试3: 正则兜底 — 使用旧的单节点解析器
        fallback = self._parse_payment_info_from_text(cleaned_text)
        if fallback.get("ratio") is not None or fallback.get("amount") is not None:
            # 尝试从原始文本中提取 payment_type
            if not fallback.get("payment_type"):
                fallback["payment_type"] = self._extract_payment_type_from_text(cleaned_text)
            logger.warning("多节点解析失败，使用正则兜底提取到单节点结果")
            return [fallback]
        
        return []
    
    def _try_parse_json_nodes(self, text: str) -> Optional[List[Dict[str, Any]]]:
        """
        尝试将文本解析为JSON并提取付款节点。
        成功返回节点列表，失败返回 None。
        """
        try:
            data = json.loads(text)
            if isinstance(data, list):
                results = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    node = self._normalize_node_from_json(item)
                    if node:
                        results.append(node)
                if results:
                    logger.info(f"成功解析多节点响应：识别到 {len(results)} 个付款节点")
                    return results
            elif isinstance(data, dict):
                # 兼容降级：LLM返回了旧的单对象格式
                node = self._normalize_node_from_json(data)
                if node:
                    logger.info("LLM返回单对象格式，兼容包装为数组")
                    return [node]
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.debug(f"JSON解析失败: {e}")
        return None
    
    def _extract_json_array_from_text(self, text: str) -> Optional[str]:
        """
        从混合文本中提取第一个完整的JSON数组片段 [...]。
        LLM有时会在JSON前后附带解释性文字，导致直接 json.loads 失败。
        """
        start_idx = text.find('[')
        if start_idx == -1:
            return None
        bracket_count = 0
        for i in range(start_idx, len(text)):
            if text[i] == '[':
                bracket_count += 1
            elif text[i] == ']':
                bracket_count -= 1
                if bracket_count == 0:
                    return text[start_idx:i + 1]
        return None
    
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
        
        # 至少要有ratio或amount才算有效节点
        if node["ratio"] is None and node["amount"] is None:
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
        
        selected_llm_config = payment_ratio_llm_config if payment_ratio_llm_config and payment_ratio_llm_config.get("api_key") else llm_config
        
        if not (selected_llm_config and selected_llm_config.get("api_key")):
            logger.error("LLM配置或API Key缺失，无法提取支付信息。")
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
                        
            # 4. 有条件地保存调试信息
            is_debug_mode = state.get("debug_mode", False)
            await self._debug_show_prompt(payment_type, payment_clause, current_clauses_chunk, rag_examples, is_debug_mode, clause_class)

            # 5. 选择对应分类的调用链并调用LLM
            selected_chain = self.install_chain if "installation" in clause_class else self.equipment_chain
            logger.debug(f"使用{'安装' if 'installation' in clause_class else '设备'}付款提示词")
            response = await llm_guarded_ainvoke(selected_chain, {
                "payment_clause": payment_clause,
                "payment_type": payment_type,
                "current_clauses_chunk": current_clauses_chunk,
                "rag_examples": rag_examples,
            })

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
        """初次提取兜底：原文不含 % / 付清语义、且上下文也无唯一总价时，强制清空 ratio。

        注：算术校核（金额 == 唯一总价 → 100%）已移至复核结果解析后统一应用，
        以避免本步结果被复核 LLM 二次覆盖。
        """
        if not payment_nodes:
            return payment_nodes

        text = str(payment_clause or "")
        _bd_syn = get_business_dict().synonyms
        has_pct_token = any(tok in text for tok in _bd_syn.percent_tokens)
        has_residual = any(tok in text for tok in _bd_syn.residual_tokens)
        has_unique_total = self._has_unique_total_in_context(current_clauses_chunk)

        if has_pct_token or has_residual or has_unique_total:
            return payment_nodes

        for node in payment_nodes:
            ratio_val = node.get("ratio")
            if ratio_val in (None, "", "null"):
                continue
            preview = text.strip().replace("\n", " ")[:60]
            logger.warning(
                f"原文无比例字样且不满足反算条件，强制清空 ratio: 原ratio={ratio_val}, "
                f"原条款={preview}..."
            )
            node["ratio"] = None
        return payment_nodes
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
            if override_cfg and override_cfg.get("api_key"):
                # 用 override 覆盖 app_settings.llm
                from app.config.config_models import LLMConfig
                try:
                    overlay = LLMConfig(**{**self.app_settings.llm.model_dump(), **override_cfg})
                except Exception:  # noqa: BLE001
                    overlay = self.app_settings.llm
                llm_config = overlay
            else:
                payment_ratio_llm_config = getattr(self.app_settings, 'payment_ratio_llm', None)
                llm_config = payment_ratio_llm_config if payment_ratio_llm_config and payment_ratio_llm_config.api_key else self.app_settings.llm
            logger.info(f"PaymentSummaryRatioExtractor 使用 LLM 配置: model={getattr(llm_config, 'model', None) or getattr(llm_config, 'model_name', None)}")

            if not (llm_config and llm_config.api_key):
                logger.warning("LLM API Key 未配置，支付比例批量复核器将不可用。")
                return

            payment_ratio_llm_config_verification = getattr(self.app_settings, 'llm', None)
            llm_config_verification = payment_ratio_llm_config_verification if payment_ratio_llm_config_verification and payment_ratio_llm_config_verification.api_key else self.app_settings.llm
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
                openai_api_key=llm_config.api_key, openai_api_base=llm_config.api_base,
                max_tokens=llm_max_tokens,
                request_timeout=_LLM_REQUEST_TIMEOUT_SEC,
                max_retries=_LLM_MAX_RETRIES,
            )

            llm_verification = ChatOpenAI(
                model=model_identifier_verification, temperature=temperature,
                openai_api_key=llm_config_verification.api_key, openai_api_base=llm_config_verification.api_base,
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
            result_verification_single_group_prompt_template = PromptTemplate(template=RESULT_VERIFICATION_SINGLE_GROUP_PROMPT, input_variables=["group_clauses"])

            # 【重要】我们不再链接脆弱的JsonOutputParser，而是手动解析
            self.llm_chain = prompt_template | llm
            self.install_llm_chain = install_prompt_template | llm
            self.warranty_llm_chain = warranty_prompt_template | llm
            self.result_verification_llm_chain = result_verification_prompt_template | llm
            self.clause_validation_llm_chain = clause_validation_prompt_template | llm
            self.clause_category_llm_chain = clause_category_prompt_template | llm
            self.result_verification_single_group_llm_chain = result_verification_single_group_prompt_template | llm

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
    
    def _clean_json_string(self, json_str: str) -> str:
        """
        清理JSON字符串中的控制字符和换行符，使其能够正确解析。
        支持处理markdown代码块格式（```json ... ```）。
        """
        import re
        
        # 首先处理markdown代码块标记
        cleaned = json_str.strip()
        
        # 方法1: 尝试移除markdown代码块标记
        # 匹配开头的 ```json 或 ```（可能前后有空白字符）
        cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned, flags=re.MULTILINE)
        # 匹配结尾的 ```（可能前后有空白字符）
        cleaned = re.sub(r'\n?```\s*$', '', cleaned, flags=re.MULTILINE)
        
        # 方法2: 如果还有markdown标记，尝试提取JSON内容
        # 查找第一个 [ 或 { 作为JSON开始，然后找到匹配的结束符
        if '```' in cleaned:
            # 查找第一个 [ 或 {
            start_idx = -1
            for i, char in enumerate(cleaned):
                if char in ['[', '{']:
                    start_idx = i
                    break
            
            if start_idx >= 0:
                # 从开始位置查找匹配的结束符，处理嵌套结构
                bracket_stack = []
                end_idx = -1
                in_string = False
                escape_next = False
                
                for i in range(start_idx, len(cleaned)):
                    char = cleaned[i]
                    
                    # 处理转义字符
                    if escape_next:
                        escape_next = False
                        continue
                    if char == '\\':
                        escape_next = True
                        continue
                    
                    # 处理字符串内的字符（不处理括号匹配）
                    if char == '"' and not escape_next:
                        in_string = not in_string
                        continue
                    
                    if in_string:
                        continue
                    
                    # 处理括号匹配
                    if char == '[':
                        bracket_stack.append('[')
                    elif char == ']':
                        if bracket_stack and bracket_stack[-1] == '[':
                            bracket_stack.pop()
                            if not bracket_stack:
                                end_idx = i + 1
                                break
                    elif char == '{':
                        bracket_stack.append('{')
                    elif char == '}':
                        if bracket_stack and bracket_stack[-1] == '{':
                            bracket_stack.pop()
                            if not bracket_stack:
                                end_idx = i + 1
                                break
                
                if end_idx > start_idx:
                    cleaned = cleaned[start_idx:end_idx]
        
        # 移除控制字符（除了必要的空格和制表符）
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)
        
        # 将字符串中的换行符替换为空格，但保持JSON结构
        # 注意：这里需要小心处理，不能破坏JSON的引号结构
        lines = cleaned.split('\n')
        result_lines = []
        
        for line in lines:
            line = line.strip()
            if line:
                result_lines.append(line)
        
        # 重新组合，确保JSON格式正确
        cleaned_json = ' '.join(result_lines)
        
        # 进一步清理：移除多余的空格，但保持JSON结构
        cleaned_json = re.sub(r'\s+', ' ', cleaned_json)
        
        return cleaned_json.strip()

    def _parse_llm_output(self, llm_output_str: str) -> Tuple[List[Dict], Optional[Dict], Optional[str]]:
        """
        一个健壮的解析器，用于从可能包含thinking/result标签的LLM输出中分离出
        支付条款列表、质保期对象和思考过程字符串。
        """
        payment_items, warranty_info, thinking_output = [], None, None

        # 1. 提取思考过程 (优先从 <thinking> 标签)
        thinking_match = re.search(r'<thinking>(.*?)</thinking>', llm_output_str, re.DOTALL)
        if thinking_match:
            thinking_output = thinking_match.group(1).strip()
            logger.info("成功从 <thinking> 标签中提取到思考过程。")

        # 2. 改进的JSON提取逻辑
        json_str = None
        
        # 方法1: 尝试直接解析整个输出
        try:
            data = json.loads(llm_output_str)
            if isinstance(data, list):
                json_str = llm_output_str
            elif isinstance(data, dict) and "clauses" in data:
                # 如果是对象格式，提取clauses数组
                json_str = json.dumps(data.get("clauses", []))
        except json.JSONDecodeError:
            pass
        
        # 方法2: 从 <result> 标签中提取
        if not json_str:
            result_match = re.search(r'<result>(.*?)</result>', llm_output_str, re.DOTALL)
            if result_match:
                json_str = result_match.group(1).strip()
                try:
                    data = json.loads(json_str)
                    if isinstance(data, dict) and "clauses" in data:
                        json_str = json.dumps(data.get("clauses", []))
                except json.JSONDecodeError:
                    json_str = None
        
        # 方法3: 从代码块中提取
        if not json_str:
            json_match = re.search(r'```json\s*\n(.*?)\n```', llm_output_str, re.DOTALL)
            if json_match:
                json_str = json_match.group(1).strip()
                try:
                    data = json.loads(json_str)
                    if isinstance(data, dict) and "clauses" in data:
                        json_str = json.dumps(data.get("clauses", []))
                except json.JSONDecodeError:
                    json_str = None
        
        # 方法4: 提取第一个完整的JSON数组（通过括号匹配）
        if not json_str:
            # 找到第一个 '[' 的位置
            start_idx = llm_output_str.find('[')
            if start_idx != -1:
                bracket_count = 0
                end_idx = start_idx
                for i, char in enumerate(llm_output_str[start_idx:], start_idx):
                    if char == '[':
                        bracket_count += 1
                    elif char == ']':
                        bracket_count -= 1
                        if bracket_count == 0:
                            end_idx = i + 1
                            break
                
                if bracket_count == 0:
                    json_str = llm_output_str[start_idx:end_idx].strip()
                    logger.debug(f"通过括号匹配提取到JSON数组，长度: {len(json_str)}")
        
        # 方法5: 最后的回退策略 - 查找第一个完整的JSON对象
        if not json_str:
            start_idx = llm_output_str.find('{')
            if start_idx != -1:
                brace_count = 0
                end_idx = start_idx
                for i, char in enumerate(llm_output_str[start_idx:], start_idx):
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i + 1
                            break
                
                if brace_count == 0:
                    json_str = llm_output_str[start_idx:end_idx]
                    try:
                        data = json.loads(json_str)
                        if isinstance(data, dict) and "clauses" in data:
                            json_str = json.dumps(data.get("clauses", []))
                    except json.JSONDecodeError:
                        json_str = None

        if not json_str:
            logger.error("在LLM输出中未能找到有效的JSON数组。")
            return [], None, thinking_output

        # 清理JSON字符串中的控制字符和修复payment_clause字段中的JSON对象
        cleaned_json_str = self._clean_json_string(json_str)
        logger.debug(f"清理后的JSON字符串: {cleaned_json_str[:500]}...")  # 只显示前500字符
        
        # 检测并修复可能存在的重复JSON数组（在payment_clause字段中）
        cleaned_json_str = self._fix_duplicate_json_arrays(cleaned_json_str)
        
        # 修复缺失的逗号（在JSON数组中的对象之间）
        cleaned_json_str = self._fix_missing_commas(cleaned_json_str)

        # 去除悬挂逗号，如 `", }"` / `", ]"`，常见于 Qwen 小模型输出
        cleaned_json_str = self._fix_trailing_commas(cleaned_json_str)

        # 尝试预验证JSON格式，提前发现问题
        try:
            json.loads(cleaned_json_str)
            logger.debug("JSON格式预验证通过")
        except json.JSONDecodeError as pre_check_error:
            logger.warning(f"JSON格式预验证失败: {pre_check_error}，将在正式解析时尝试修复")

        try:
            data_array = json.loads(cleaned_json_str)
            # 使用统一的方法处理解析后的数组
            return self._process_parsed_array(data_array, thinking_output)

        except json.JSONDecodeError as e:
            error_msg = str(e)
            error_pos = getattr(e, 'pos', None)
            
            # 提取错误位置信息
            if error_pos is not None:
                logger.error(f"解析提取出的JSON字符串时失败: {error_msg}")
                logger.error(f"错误位置: 字符 {error_pos} (行 {cleaned_json_str[:error_pos].count(chr(10)) + 1})")
                
                # 显示错误位置的上下文
                context_start = max(0, error_pos - 50)
                context_end = min(len(cleaned_json_str), error_pos + 50)
                context = cleaned_json_str[context_start:context_end]
                logger.error(f"错误上下文: ...{context}...")
                logger.error(f"错误位置标记: {' ' * (error_pos - context_start)}^")
            else:
                logger.error(f"解析提取出的JSON字符串时失败: {error_msg}")
            
            logger.warning("尝试多种修复策略...")
            
            # 修复策略1: 修复thinking_output字段中的嵌套JSON
            try:
                fixed_json_str = self._fix_nested_json_in_string(cleaned_json_str)
                if fixed_json_str != cleaned_json_str:
                    logger.info("策略1: 成功修复thinking_output字段中的嵌套JSON，重新尝试解析...")
                    data_array = json.loads(fixed_json_str)
                    return self._process_parsed_array(data_array, thinking_output)
            except json.JSONDecodeError:
                logger.debug("策略1失败，尝试策略2...")
            except Exception as fix_error:
                logger.warning(f"策略1执行时发生错误: {fix_error}")
            
            # 修复策略2: 尝试修复更多缺失的逗号（更激进的修复）
            try:
                fixed_json_str = self._fix_missing_commas_aggressive(cleaned_json_str)
                if fixed_json_str != cleaned_json_str:
                    logger.info("策略2: 使用更激进的逗号修复策略，重新尝试解析...")
                    data_array = json.loads(fixed_json_str)
                    return self._process_parsed_array(data_array, thinking_output)
            except json.JSONDecodeError:
                logger.debug("策略2失败，尝试策略3...")
            except Exception as fix_error:
                logger.warning(f"策略2执行时发生错误: {fix_error}")
            
            # 修复策略3: 尝试移除可能导致问题的特殊字符
            try:
                fixed_json_str = self._fix_special_characters(cleaned_json_str)
                if fixed_json_str != cleaned_json_str:
                    logger.info("策略3: 修复特殊字符，重新尝试解析...")
                    data_array = json.loads(fixed_json_str)
                    return self._process_parsed_array(data_array, thinking_output)
            except json.JSONDecodeError:
                logger.debug("策略3失败")
            except Exception as fix_error:
                logger.warning(f"策略3执行时发生错误: {fix_error}")

            # 修复策略4: 逐对象抢救 —— 数组整体解析失败时，扫描出所有顶层 { ... } 子串
            # 逐个 json.loads，跳过失败的对象，保住其余有效条款。
            try:
                salvaged = self._salvage_objects(cleaned_json_str)
                if salvaged:
                    logger.info(f"策略4: 逐对象抢救成功，共救回 {len(salvaged)} 个对象")
                    return self._process_parsed_array(salvaged, thinking_output)
            except Exception as fix_error:
                logger.warning(f"策略4执行时发生错误: {fix_error}")

            logger.error(f"所有修复策略均失败。待解析的字符串前500字符: {cleaned_json_str[:500]}...")
            if error_pos:
                logger.error(f"完整错误位置附近的文本: {cleaned_json_str[max(0, error_pos-100):min(len(cleaned_json_str), error_pos+100)]}")
            return [], None, thinking_output
    
    def _process_parsed_array(self, data_array: Any, existing_thinking: Optional[str]) -> Tuple[List[Dict], Optional[Dict], Optional[str]]:
        """
        处理已解析的数组数据，提取支付条款、质保期和思考过程。
        
        Args:
            data_array: 解析后的数组数据
            existing_thinking: 已存在的思考过程文本
        
        Returns:
            (支付条款列表, 质保期信息, 思考过程文本)
        """
        payment_items = []
        warranty_info = None
        thinking_output = existing_thinking
        
        if not isinstance(data_array, list):
            if isinstance(data_array, dict):
                logger.warning(f"解析出的JSON是一个字典而不是列表，尝试将其转换为列表。")
                if "payment_clause" in data_array or "id" in data_array:
                    data_array = [data_array]
                elif "warranty" in data_array:
                    data_array = [data_array]
                else:
                    logger.warning(f"无法识别的字典结构，返回空列表。字典键: {list(data_array.keys())}")
                    return [], None, thinking_output
            else:
                logger.warning(f"解析出的JSON不是列表或字典，而是 {type(data_array)}。")
                return [], None, thinking_output
        
        # 遍历解析出的数组，分离不同类型的对象
        for idx, item in enumerate(data_array):
            if not isinstance(item, dict):
                logger.warning(f"数组第{idx}个元素不是字典类型: {type(item)}")
                continue
            
            try:
                if 'error' in item:
                    logger.warning(f"LLM返回包含错误信息: {item.get('error')}")
                    continue
                
                if "payment_clause" in item and "id" in item:
                    item = self._fix_payment_clause_json_objects(item)
                    if "final_amount" in item and item["final_amount"] is not None:
                        item["final_amount"] = str(item["final_amount"])
                    payment_items.append(item)
                elif "warranty" in item:
                    warranty_info = item
                elif "thinking_output" in item and not thinking_output:
                    raw_thinking = item.get("thinking_output")
                    thinking_output = self._fix_thinking_output_json(raw_thinking)
            except (KeyError, TypeError) as e:
                logger.error(f"处理数组第{idx}个元素时发生错误: {e}, item类型: {type(item)}, item内容: {str(item)[:200] if item else 'None'}", exc_info=True)
                continue
        
        logger.success(f"成功解析出 {len(payment_items)} 个支付条款, "
                      f"质保期: {'有' if warranty_info else '无'}, "
                      f"思考过程: {'有' if thinking_output else '无'}")
        
        return payment_items, warranty_info, thinking_output

    def _fix_nested_json_in_string(self, json_str: str) -> str:
        """
        修复JSON字符串中thinking_output字段内的嵌套JSON结构。
        如果thinking_output的值是一个JSON数组或对象字符串，将其转换为转义的字符串。
        """
        try:
            # 使用正则表达式查找thinking_output字段中的嵌套JSON
            # 匹配: "thinking_output": "[ {...}, {...} ]" 或 "thinking_output": "{ ... }"
            pattern = r'"thinking_output"\s*:\s*"(\[(?:[^"\\]|\\.|"\[|"\{)*\])"'
            
            def replace_nested_json(match):
                nested_json = match.group(1)
                # 将嵌套的JSON字符串转义
                escaped = json.dumps(nested_json)
                return f'"thinking_output": {escaped}'
            
            fixed_str = re.sub(pattern, replace_nested_json, json_str, flags=re.DOTALL)
            
            # 如果替换成功，返回修复后的字符串
            if fixed_str != json_str:
                logger.info("检测到并修复了thinking_output字段中的嵌套JSON")
                return fixed_str
            
            # 尝试另一种模式：thinking_output后面直接跟着JSON数组
            pattern2 = r'"thinking_output"\s*:\s*(\[[\s\S]*?\])(?=\s*[,}])'
            def replace_nested_json2(match):
                nested_json = match.group(1)
                # 尝试解析嵌套JSON，如果成功则转义为字符串
                try:
                    json.loads(nested_json)  # 验证是否为有效JSON
                    escaped = json.dumps(nested_json)
                    return f'"thinking_output": {escaped}'
                except:
                    return match.group(0)  # 如果无法解析，保持原样
            
            fixed_str2 = re.sub(pattern2, replace_nested_json2, json_str, flags=re.DOTALL)
            if fixed_str2 != json_str:
                logger.info("使用第二种模式修复了thinking_output字段中的嵌套JSON")
                return fixed_str2
            
            return json_str
        except Exception as e:
            logger.warning(f"修复嵌套JSON时发生错误: {e}")
            return json_str

    def _fix_thinking_output_json(self, thinking_text: Any) -> Optional[str]:
        """
        修复thinking_output字段中可能包含的嵌套JSON结构。
        如果thinking_output包含JSON数组或对象，尝试提取其中的文本内容，或将其转换为纯文本。
        """
        if not thinking_text:
            return None
        
        if not isinstance(thinking_text, str):
            thinking_text = str(thinking_text)
        
        # 检查是否包含JSON数组结构 [ {...}, {...} ]
        if thinking_text.strip().startswith('[') and '{"' in thinking_text:
            logger.warning("检测到thinking_output中包含嵌套的JSON数组，尝试提取文本内容...")
            try:
                # 尝试解析JSON数组
                parsed = json.loads(thinking_text)
                if isinstance(parsed, list):
                    # 提取所有对象的文本内容
                    text_parts = []
                    for obj in parsed:
                        if isinstance(obj, dict):
                            # 提取字典中的值，排除键名
                            for key, value in obj.items():
                                if isinstance(value, str) and key != "thinking_output":
                                    text_parts.append(f"{key}: {value}")
                                elif key == "thinking_output" and isinstance(value, str):
                                    # 递归处理嵌套的thinking_output
                                    nested_text = self._fix_thinking_output_json(value)
                                    if nested_text:
                                        text_parts.append(nested_text)
                    if text_parts:
                        cleaned_text = "\n".join(text_parts)
                        logger.info(f"成功从嵌套JSON中提取思考过程文本，长度: {len(cleaned_text)} 字符")
                        return cleaned_text
            except json.JSONDecodeError:
                # 如果解析失败，尝试提取第一个有效的JSON对象之前的内容
                pass
        
        # 检查是否包含JSON对象结构 { "key": "value" }
        if thinking_text.strip().startswith('{') and '"' in thinking_text:
            # 尝试提取JSON对象中的文本值
            try:
                parsed = json.loads(thinking_text)
                if isinstance(parsed, dict):
                    # 提取所有字符串值
                    text_parts = []
                    for key, value in parsed.items():
                        if isinstance(value, str):
                            text_parts.append(value)
                        elif isinstance(value, (list, dict)):
                            # 递归处理嵌套结构
                            text_parts.append(str(value))
                    if text_parts:
                        cleaned_text = "\n".join(text_parts)
                        logger.info(f"成功从JSON对象中提取思考过程文本，长度: {len(cleaned_text)} 字符")
                        return cleaned_text
            except json.JSONDecodeError:
                pass
        
        # 如果包含JSON结构但无法解析，尝试移除JSON标记
        # 移除 [ { ... } ] 这样的结构
        cleaned = re.sub(r'\[\s*\{[^}]*\}\s*(?:,\s*\{[^}]*\}\s*)*\]', '', thinking_text)
        # 移除 { "key": "value" } 这样的结构
        cleaned = re.sub(r'\{\s*"[^"]*"\s*:\s*"[^"]*"\s*\}', '', cleaned)
        
        if cleaned != thinking_text:
            logger.warning("移除了thinking_output中的JSON结构标记")
            return cleaned.strip() if cleaned.strip() else thinking_text
        
        # 如果无法修复，返回原始文本
        return thinking_text.strip() if thinking_text.strip() else None

    def _fix_duplicate_json_arrays(self, json_str: str) -> str:
        """
        检测并修复JSON字符串中可能存在的重复JSON数组。
        如果检测到重复的数组结构（例如：整个数组被重复输出），只保留第一个完整的数组。
        """
        if not json_str or not isinstance(json_str, str):
            return json_str
        
        # 检测是否有重复的数组开始标记（在字符串中间出现第二个 '['）
        first_bracket_idx = json_str.find('[')
        if first_bracket_idx == -1:
            return json_str
        
        # 找到第一个完整数组的结束位置
        bracket_count = 0
        first_array_end = -1
        for i, char in enumerate(json_str[first_bracket_idx:], first_bracket_idx):
            if char == '[':
                bracket_count += 1
            elif char == ']':
                bracket_count -= 1
                if bracket_count == 0:
                    first_array_end = i + 1
                    break
        
        if first_array_end == -1:
            return json_str
        
        # 检查第一个数组之后是否还有另一个数组开始标记
        remaining_str = json_str[first_array_end:].strip()
        if remaining_str and remaining_str.startswith('['):
            logger.warning("检测到JSON字符串中包含重复的数组，将只保留第一个完整的数组")
            # 只保留第一个完整的数组
            return json_str[:first_array_end].strip()
        
        return json_str

    def _fix_trailing_commas(self, json_str: str) -> str:
        """
        移除 JSON 中的悬挂逗号（trailing commas），例如:
          {"a": 1, }   -> {"a": 1}
          [1, 2, 3, ]  -> [1, 2, 3]
        仅在字符串字面量之外替换，字符串内的 ", }" 不会被误伤。
        """
        if not json_str or not isinstance(json_str, str):
            return json_str
        try:
            out = []
            in_string = False
            escape_next = False
            n = len(json_str)
            i = 0
            fixed_count = 0
            while i < n:
                ch = json_str[i]
                if escape_next:
                    out.append(ch)
                    escape_next = False
                    i += 1
                    continue
                if ch == '\\':
                    out.append(ch)
                    escape_next = True
                    i += 1
                    continue
                if ch == '"':
                    in_string = not in_string
                    out.append(ch)
                    i += 1
                    continue
                if (not in_string) and ch == ',':
                    # 向后跳过空白，看下一个非空白字符是否是 } 或 ]
                    j = i + 1
                    while j < n and json_str[j] in ' \t\r\n':
                        j += 1
                    if j < n and json_str[j] in '}]':
                        # 丢弃该逗号
                        fixed_count += 1
                        i += 1
                        continue
                out.append(ch)
                i += 1
            if fixed_count > 0:
                logger.info(f"去除悬挂逗号 {fixed_count} 处")
            return ''.join(out)
        except Exception as e:
            logger.warning(f"去除悬挂逗号时出错，保留原文: {e}")
            return json_str

    def _salvage_objects(self, json_str: str) -> List[Dict[str, Any]]:
        """
        从一段可能不合法的 JSON 文本中逐个抢救顶层对象。
        扫描出每一对匹配的 `{...}`，各自 json.loads；失败的丢弃。
        用于当整体数组解析失败（如某个尾部对象坏掉）时，不牺牲其余有效条款。
        """
        results: List[Dict[str, Any]] = []
        if not json_str or not isinstance(json_str, str):
            return results
        n = len(json_str)
        i = 0
        in_string = False
        escape_next = False
        depth = 0
        start = -1
        while i < n:
            ch = json_str[i]
            if escape_next:
                escape_next = False
                i += 1
                continue
            if ch == '\\':
                escape_next = True
                i += 1
                continue
            if ch == '"':
                in_string = not in_string
                i += 1
                continue
            if in_string:
                i += 1
                continue
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start != -1:
                    candidate = json_str[start:i + 1]
                    # 先做一次轻量修复：去悬挂逗号
                    candidate_fixed = self._fix_trailing_commas(candidate)
                    try:
                        obj = json.loads(candidate_fixed)
                        if isinstance(obj, dict):
                            results.append(obj)
                    except Exception:
                        # 忽略单个对象解析失败，继续扫描
                        pass
                    start = -1
            i += 1
        return results

    def _fix_missing_commas(self, json_str: str) -> str:
        """
        修复JSON数组中对象之间缺失的逗号。
        例如: [ {...} {...} ] -> [ {...}, {...} ]
        支持多种边界情况：
        - } { 之间缺少逗号
        - }] { 之间缺少逗号（对象后直接跟数组结束）
        - 嵌套对象之间的逗号缺失
        """
        if not json_str or not isinstance(json_str, str):
            return json_str
        
        # 找到数组的开始和结束位置
        start_idx = json_str.find('[')
        if start_idx == -1:
            return json_str
        
        # 找到第一个完整数组的结束位置（处理嵌套数组）
        bracket_count = 0
        end_idx = start_idx
        in_string = False
        escape_next = False
        
        for i in range(start_idx, len(json_str)):
            char = json_str[i]
            
            if escape_next:
                escape_next = False
                continue
            
            if char == '\\':
                escape_next = True
                continue
            
            if char == '"':
                in_string = not in_string
                continue
            
            if not in_string:
                if char == '[':
                    bracket_count += 1
                elif char == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        end_idx = i + 1
                        break
        
        if bracket_count != 0:
            logger.warning("无法找到完整的JSON数组边界，跳过逗号修复")
            return json_str
        
        # 只在数组内部进行修复
        array_content = json_str[start_idx+1:end_idx-1]
        
        # 逐字符检查，跟踪是否在字符串内、对象内、数组内
        result = []
        in_string = False
        escape_next = False
        brace_depth = 0  # 跟踪对象嵌套深度
        bracket_depth = 0  # 跟踪数组嵌套深度
        i = 0
        fixes_applied = 0
        
        while i < len(array_content):
            char = array_content[i]
            
            if escape_next:
                result.append(char)
                escape_next = False
                i += 1
                continue
            
            if char == '\\':
                result.append(char)
                escape_next = True
                i += 1
                continue
            
            if char == '"':
                in_string = not in_string
                result.append(char)
                i += 1
                continue
            
            # 如果不在字符串内，检查结构字符
            if not in_string:
                if char == '{':
                    brace_depth += 1
                    result.append(char)
                    i += 1
                    continue
                elif char == '}':
                    brace_depth -= 1
                    result.append(char)
                    
                    # 检查后面是否需要添加逗号
                    j = i + 1
                    # 跳过空白字符
                    while j < len(array_content) and array_content[j] in ' \t\n\r':
                        j += 1
                    
                    # 如果下一个非空白字符是 { 或 [，说明缺少逗号
                    if j < len(array_content) and array_content[j] in '{[':
                        result.append(',')
                        fixes_applied += 1
                        logger.debug(f"在位置 {start_idx + i + 1} 处检测到缺失的逗号（对象后跟 {array_content[j]}），已修复")
                    
                    i += 1
                    continue
                elif char == '[':
                    bracket_depth += 1
                    result.append(char)
                    i += 1
                    continue
                elif char == ']':
                    bracket_depth -= 1
                    result.append(char)
                    i += 1
                    continue
                elif char == ',':
                    # 检查是否有重复的逗号
                    j = i + 1
                    while j < len(array_content) and array_content[j] in ' \t\n\r':
                        j += 1
                    if j < len(array_content) and array_content[j] == ',':
                        # 跳过重复的逗号
                        logger.debug(f"在位置 {start_idx + i + 1} 处检测到重复的逗号，已移除")
                        i += 1
                        continue
            
            result.append(char)
            i += 1
        
        # 重新组合字符串
        fixed_array = '[' + ''.join(result) + ']'
        fixed_json_str = json_str[:start_idx] + fixed_array + json_str[end_idx:]
        
        if fixes_applied > 0:
            logger.info(f"检测到并修复了 {fixes_applied} 处JSON数组中缺失的逗号")
        
        return fixed_json_str

    def _fix_missing_commas_aggressive(self, json_str: str) -> str:
        """
        更激进的逗号修复策略，用于处理标准方法无法修复的情况。
        包括：
        - 修复对象和数组之间的逗号
        - 修复字符串值后的逗号缺失
        - 处理更复杂的嵌套情况
        """
        if not json_str or not isinstance(json_str, str):
            return json_str
        
        # 先使用标准方法修复
        fixed = self._fix_missing_commas(json_str)
        
        # 如果标准方法已经修复，直接返回
        if fixed != json_str:
            return fixed
        
        # 更激进的修复：使用正则表达式查找更多模式
        # 注意：这种方法可能误修复，需要谨慎使用
        
        # 模式1: } 后直接跟 { 或 [（中间可能有空白）
        pattern1 = r'\}\s+(\{|\[)'
        if re.search(pattern1, fixed):
            fixed = re.sub(pattern1, r'}, \1', fixed)
            logger.debug("使用激进策略修复了对象/数组之间的逗号")
        
        # 模式2: ] 后直接跟 {（数组后跟对象）
        pattern2 = r'\]\s+(\{)'
        if re.search(pattern2, fixed):
            fixed = re.sub(pattern2, r'], \1', fixed)
            logger.debug("使用激进策略修复了数组和对象之间的逗号")
        
        return fixed

    def _fix_special_characters(self, json_str: str) -> str:
        """
        修复可能导致JSON解析失败的特殊字符。
        包括：
        - 移除或替换不可见字符
        - 修复常见的Unicode问题
        - 处理BOM标记
        """
        if not json_str or not isinstance(json_str, str):
            return json_str
        
        fixed = json_str
        
        # 移除BOM标记
        if fixed.startswith('\ufeff'):
            fixed = fixed[1:]
            logger.debug("移除了BOM标记")
        
        # 移除零宽字符（但保留必要的空白）
        zero_width_chars = ['\u200b', '\u200c', '\u200d', '\ufeff']
        for char in zero_width_chars:
            if char in fixed:
                fixed = fixed.replace(char, '')
                logger.debug(f"移除了零宽字符: {repr(char)}")
        
        # 修复常见的引号问题（但需要谨慎，避免误修复）
        # 这里只处理明显错误的引号
        # 注意：不要替换字符串内的引号，只处理结构性的引号问题
        
        # 移除控制字符（除了必要的换行符和制表符）
        import unicodedata
        # 保留换行符和制表符，移除其他控制字符
        cleaned_chars = []
        for char in fixed:
            if ord(char) < 32:
                if char in '\n\r\t':
                    cleaned_chars.append(char)
                else:
                    # 移除其他控制字符
                    logger.debug(f"移除了控制字符: {repr(char)} (U+{ord(char):04X})")
            else:
                cleaned_chars.append(char)
        
        if len(cleaned_chars) != len(fixed):
            fixed = ''.join(cleaned_chars)
            logger.debug("移除了控制字符")
        
        return fixed

    def _fix_payment_clause_json_objects(self, item: Dict) -> Dict:
        """
        修复payment_clause字段中可能包含的未转义JSON对象。
        例如: "本协议签后15日内甲方一次性全額支付给乙方，乙方收款后向[ {"warranty": "36个月"}, {"thinking_output": "..."} ]"
        修复为: "本协议签后15日内甲方一次性全額支付给乙方，乙方收款后向甲方提供有效发票。"
        """
        if not isinstance(item, dict):
            logger.warning(f"_fix_payment_clause_json_objects: item不是字典类型，而是 {type(item)}")
            return item
            
        if "payment_clause" not in item:
            return item
        
        try:
            payment_clause = item["payment_clause"]
        except (KeyError, TypeError) as e:
            logger.error(f"_fix_payment_clause_json_objects: 访问payment_clause时出错: {e}, item类型: {type(item)}, item keys: {list(item.keys()) if isinstance(item, dict) else 'N/A'}")
            return item
            
        if not isinstance(payment_clause, str):
            return item
        
        # 检测并修复payment_clause中的JSON对象
        # 匹配模式: [ {"key": "value"}, {"key2": "value2"} ]
        json_object_pattern = r'\[\s*\{[^}]*\}\s*(?:,\s*\{[^}]*\}\s*)*\]'
        
        # 查找所有匹配的JSON对象
        matches = re.findall(json_object_pattern, payment_clause)
        
        if matches:
            logger.warning(f"检测到payment_clause中包含JSON对象: {matches}")
            
            # 移除所有JSON对象，只保留纯文本部分
            cleaned_clause = payment_clause
            for match in matches:
                cleaned_clause = cleaned_clause.replace(match, "")
            
            # 清理多余的空格和分隔符
            cleaned_clause = re.sub(r'\s*\|\s*$', '', cleaned_clause)  # 移除末尾的 "|"
            cleaned_clause = re.sub(r'\s+', ' ', cleaned_clause)  # 合并多个空格
            cleaned_clause = cleaned_clause.strip()
            
            # 更新item
            item["payment_clause"] = cleaned_clause
            logger.info(f"修复后的payment_clause: {cleaned_clause}")
        
        return item

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
        单组去重：对同一 (clause_category, payment_type) 组的 N 个候选条款，
        由 LLM 挑选一个最合适的 id。单任务单输出，Qwen2.5-9B 友好。

        Args:
            group_items: 候选条款列表，每项至少含 id/payment_clause/payment_type/clause_category。

        Returns:
            {"select_clause_id": <id>, "reason": <str>}。
            LLM 未就绪 / 调用异常 / 解析失败 / 选出的 id 不在候选集合中 → 代码级兜底：按 _pick_best_clause 规则打分选一个。
        """
        candidate_ids = [str(it.get("id", "")) for it in group_items if isinstance(it, dict)]
        candidate_id_set = {cid for cid in candidate_ids if cid}

        def _code_fallback(reason: str) -> Dict[str, Any]:
            # 代码级打分：优先"有动作且有金额/比例"，再按 clause 文本长度
            def _score(it: Dict[str, Any]) -> tuple:
                clause = str(it.get("payment_clause", "") or "")
                has_action = bool(re.search(r'支付|付款|汇入|付清', clause))
                has_amount = bool(re.search(r'\d+(\.\d+)?%|百分之|万元|元整|\d+元', clause))
                score_action = 2 if (has_action and has_amount) else (1 if has_action else 0)
                return (score_action, len(clause))
            best = max(group_items, key=_score) if group_items else {}
            return {"select_clause_id": str(best.get("id", "")), "reason": f"兜底({reason})"}

        if not group_items:
            return {"select_clause_id": "", "reason": "空组"}
        if len(group_items) == 1:
            return {"select_clause_id": candidate_ids[0], "reason": "单条直通"}
        if not self.is_ready or not getattr(self, "result_verification_single_group_llm_chain", None):
            return _code_fallback("LLM未就绪")

        try:
            # 精简负载，避免过长的 clause_context 等字段影响 9B 注意力
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
            input_dict = {"group_clauses": json.dumps(payload, ensure_ascii=False)}
            response_obj = await llm_guarded_ainvoke(
                self.result_verification_single_group_llm_chain, input_dict
            )
            llm_output_str = str(getattr(response_obj, "content", response_obj)).strip()

            # 解析：优先 JSON，失败则兜底
            cleaned = self._clean_json_string(llm_output_str)
            # 去除可能的 markdown 代码块
            m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', cleaned, re.DOTALL | re.IGNORECASE)
            if m:
                cleaned = m.group(1).strip()
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                # regex 二次兜底
                m2 = re.search(r'"select_clause_id"\s*:\s*"([^"]+)"', llm_output_str)
                if m2 and m2.group(1) in candidate_id_set:
                    return {"select_clause_id": m2.group(1), "reason": "regex兜底"}
                return _code_fallback("JSON解析失败")

            if isinstance(parsed, list) and parsed:
                parsed = parsed[0]
            if not isinstance(parsed, dict):
                return _code_fallback("输出结构非法")

            selected = str(parsed.get("select_clause_id", "")).strip()
            reason = str(parsed.get("reason", "") or "")
            if selected not in candidate_id_set:
                logger.warning(f"单组去重：LLM 选出 id={selected} 不在候选 {candidate_ids} 中，代码级兜底")
                return _code_fallback("LLM选出的id不在候选中")
            return {"select_clause_id": selected, "reason": reason}
        except Exception as e:
            logger.warning(f"单组去重 LLM 调用异常: {e}，代码级兜底")
            return _code_fallback(f"LLM异常:{type(e).__name__}")

    def _parse_verification_output(self, llm_output: str) -> Tuple[List[str], str]:
        """
        解析校验LLM的输出，提取挑选出的ID列表和思考过程。
        期望的输出格式：
        [
          {"select_clause_id": "ID1"},
          {"select_clause_id": "ID2"},
          ...,
          {"thinking_output": "思考过程文本"}
        ]
        """
        try:
            # 首先尝试提取JSON代码块
            import re

            # 方法1: 查找```json ... ```代码块
            json_pattern = r'```(?:json)?\s*\n?(.*?)\n?```'
            json_match = re.search(json_pattern, llm_output, re.DOTALL | re.IGNORECASE)

            if json_match:
                json_content = json_match.group(1).strip()
            else:
                # 方法2: 如果没有代码块标记，直接查找JSON结构
                # 查找第一个[或{开始的JSON
                start_idx = llm_output.find('[')
                if start_idx == -1:
                    start_idx = llm_output.find('{')
                if start_idx >= 0:
                    json_content = llm_output[start_idx:]
                else:
                    json_content = llm_output

            # 清理并解析JSON
            cleaned_output = self._clean_json_string(json_content)

            # 尝试解析JSON
            parsed_data = json.loads(cleaned_output)

            if not isinstance(parsed_data, list):
                logger.error(f"LLM输出不是数组格式: {parsed_data}")
                return [], ""

            selected_ids = []
            thinking_text = ""

            # 遍历数组，提取ID和思考过程
            for item in parsed_data:
                if isinstance(item, dict):
                    # 检查是否包含错误信息
                    if 'error' in item:
                        logger.warning(f"LLM返回包含错误信息: {item.get('error')}")
                        continue

                    if "select_clause_id" in item:
                        # 包含挑选出的条款ID
                        selected_ids.append(item["select_clause_id"])
                    elif "thinking_output" in item:
                        # 包含思考过程
                        thinking_text = item["thinking_output"]

            logger.info(f"成功解析校验结果: 挑选出 {len(selected_ids)} 个条款ID")
            return selected_ids, thinking_text

        except json.JSONDecodeError as e:
            logger.error(f"解析LLM校验输出JSON失败: {e}")
            logger.error(f"清理后的输出: {cleaned_output if 'cleaned_output' in locals() else '未定义'}")
            logger.error(f"原始输出: {llm_output}")
            return [], ""
        except Exception as e:
            logger.error(f"解析LLM校验输出时发生错误: {e}")
            return [], ""

    @staticmethod
    def _validation_fallback(clause: Dict[str, Any], reason: str) -> Dict[str, Any]:
        """fail-closed 兜底：解析失败/异常一律视为无效条款（I3）。"""
        return {
            "id": clause.get("id", ""),
            "is_valid": False,
            "reason": reason,
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

            clause_str = json.dumps(clause_for_validation, ensure_ascii=False)

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
        解析单条条款校验LLM的输出。

        期望格式：
        {"id": "ID", "is_valid": true/false, "reason": "..."}
        """
        try:
            import re

            cleaned = llm_output.strip()

            # 移除 markdown 代码块
            cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned, flags=re.MULTILINE)
            cleaned = re.sub(r'\n?```\s*$', '', cleaned, flags=re.MULTILINE)

            # 处理转义
            cleaned = cleaned.replace('\\\\"', '"')
            cleaned = cleaned.replace('\\"', '"')
            cleaned = cleaned.replace('\\n', ' ')
            cleaned = cleaned.replace('\\t', ' ')

            # 查找 JSON 对象
            first_brace = cleaned.find('{')
            if first_brace == -1:
                logger.warning(f"未找到JSON对象，fail-closed ID={original_clause.get('id')}")
                return self._validation_fallback(original_clause, "no_json_object")

            last_brace = cleaned.rfind('}')
            if last_brace == -1:
                logger.warning(f"未找到JSON结束标记，fail-closed ID={original_clause.get('id')}")
                return self._validation_fallback(original_clause, "no_json_close")

            json_str = cleaned[first_brace:last_brace + 1]

            parsed_data = None
            try:
                parsed_data = json.loads(json_str)
            except json.JSONDecodeError:
                # 备用方案：用正则提取 is_valid 字段
                logger.debug(f"JSON解析失败，尝试正则提取 ID={original_clause.get('id')}")
                valid_match = re.search(r'"is_valid"\s*:\s*(true|false)', json_str, re.IGNORECASE)
                reason_match = re.search(r'"reason"\s*:\s*"([^"]*)"', json_str)
                if valid_match:
                    parsed_data = {
                        "id": original_clause.get("id", ""),
                        "is_valid": valid_match.group(1).lower() == "true",
                        "reason": reason_match.group(1) if reason_match else "",
                    }
                else:
                    logger.warning(f"正则提取也失败，fail-closed ID={original_clause.get('id')}")
                    return self._validation_fallback(original_clause, "regex_extract_failed")

            if isinstance(parsed_data, list) and len(parsed_data) > 0:
                parsed_data = parsed_data[0]

            if not isinstance(parsed_data, dict):
                logger.warning(f"条款校验输出格式异常，fail-closed ID={original_clause.get('id')}")
                return self._validation_fallback(original_clause, "non_dict_payload")

            return {
                "id": parsed_data.get("id", original_clause.get("id", "")),
                "is_valid": bool(parsed_data.get("is_valid", False)),
                "reason": parsed_data.get("reason", ""),
                "text": original_clause.get("clause", ""),
                "clause_class": original_clause.get("clause_class", ""),
                "clause_context": original_clause.get("clause_context", ""),
            }

        except Exception as e:
            logger.error(f"解析单条条款校验输出失败 ID={original_clause.get('id')}: {e}")
            return self._validation_fallback(original_clause, f"parse_exception: {type(e).__name__}")

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
        解析归属判定 LLM 输出。
        期望：{"id": "...", "category": "equipment_payment|installation_payment|both", "reason": "..."}
        兜底规则：任何异常或非法枚举 → category="equipment_payment"
        """
        import re as _re
        fallback = {"id": clause_id, "category": "equipment_payment", "reason": "兜底(解析失败或非法枚举)"}
        valid_enum = {"equipment_payment", "installation_payment", "both"}

        try:
            cleaned = llm_output.strip()
            cleaned = _re.sub(r'^```(?:json)?\s*\n?', '', cleaned, flags=_re.MULTILINE)
            cleaned = _re.sub(r'\n?```\s*$', '', cleaned, flags=_re.MULTILINE)

            first_brace = cleaned.find('{')
            last_brace = cleaned.rfind('}')
            if first_brace == -1 or last_brace == -1:
                # 最后一搏：直接在原文找枚举
                for v in valid_enum:
                    if v in cleaned:
                        return {"id": clause_id, "category": v, "reason": "regex-fallback"}
                return fallback

            json_str = cleaned[first_brace:last_brace + 1]
            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError:
                m = _re.search(r'"category"\s*:\s*"(equipment_payment|installation_payment|both)"', json_str)
                if m:
                    return {"id": clause_id, "category": m.group(1), "reason": "regex-extract"}
                return fallback

            if isinstance(parsed, list) and parsed:
                parsed = parsed[0]
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
        except Exception as e:
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
