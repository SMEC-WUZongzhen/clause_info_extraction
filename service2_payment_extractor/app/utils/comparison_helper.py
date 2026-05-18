# app/utils/comparison_helper.py

from __future__ import annotations
import re
import pandas as pd
from loguru import logger
from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field
from fuzzywuzzy import fuzz
from app.config.config import APP_CONFIG

# --- 数据结构定义 ---

class PaymentStage(BaseModel):
    """表示单个支付节点（用于比对结果）的模型。"""
    payment_type: str
    payment_ratio: Optional[float] = None
    payment_amount: Optional[str] = None
    source: Optional[str] = Field(None, description="数据来源: 'AI提取' 或 'SIS系统'")

class EvaluationMetrics(BaseModel):
    """评估指标的模型。"""
    accuracy: float
    precision: float
    recall: float
    f1_score: float
    
class ComparisonItem(BaseModel):
    status: str = Field(..., description="比对状态: 'fully_matched' (完全一致), 'node_matched_data_mismatch' (节点一致数据不一致), 'node_mismatch' (节点不一致)")
    extracted_clause: Optional[str] = Field(None, description="模型提取的原始条款文本")
    extracted_type: Optional[str] = Field(None, description="模型提取的支付类型")
    extracted_ratio: Optional[float] = Field(None, description="模型提取的支付比例 (0-1的小数)")
    extracted_amount: Optional[str] = Field(None, description="模型提取的支付金额")
    ground_truth_node: Optional[str] = Field(None, description="基准数据中的付款节点名称")
    ground_truth_ratio: Optional[float] = Field(None, description="基准数据中的金额比例 (0-1的小数)")
    ground_truth_amount: Optional[str] = Field(None, description="基准数据中的金额")
    match_score: Optional[float] = Field(None, description="文本相似度分数 (0-100)")
    is_ratio_match: Optional[bool] = Field(None, description="比例是否一致")
    is_amount_match: Optional[bool] = Field(None, description="金额是否一致")
    is_fully_matched: Optional[bool] = Field(None, description="是否完全匹配")
    ground_truth_row_index: Optional[int] = Field(None, description="基准数据在CSV中的行号")
    ai_payment_type: Optional[str] = Field(None, description="AI提取的标准化支付类型")
    sis_payment_type: Optional[str] = Field(None, description="SIS系统的标准化支付类型")
    ai_source: Optional[str] = Field(None, description="AI数据来源标识")
    sis_source: Optional[str] = Field(None, description="SIS数据来源标识")

class ComparisonSummary(BaseModel):
    document_id: str
    total_extracted: int
    total_ground_truth: int
    fully_matched_count: int
    node_matched_data_mismatch_count: int
    node_mismatch_count: int
    overall_accuracy: float = Field(description="F1-score like metric")
    ratio_match_rate: float = Field(description="在匹配项中，比例一致的比例")
    amount_match_rate: float = Field(description="在匹配项中，金额一致的比例")

class ComparisonResult(BaseModel):
    summary: ComparisonSummary
    comparison_details: List[ComparisonItem]

# --- 核心辅助类 ---
class ComparisonHelper:

    def __init__(self):
        self.config = APP_CONFIG.text_processing.comparison
        self._reverse_alias_map: Dict[str, str] = {}
        for rule in self.config.alias_rules:
            for standard_name, aliases in rule.mapping.items():
                if standard_name.lower() not in self._reverse_alias_map:
                    self._reverse_alias_map[standard_name.lower()] = standard_name
                for alias in aliases:
                    if alias.lower() not in self._reverse_alias_map:
                        self._reverse_alias_map[alias.lower()] = standard_name
        logger.info(f"ComparisonHelper 初始化，加载了 {len(self._reverse_alias_map)} 条别名规则。")
    
    # 中文金额单位（C7）
    _UNIT = {
        "亿": 1e8, "萬": 1e4, "万": 1e4, "千": 1e3, "百": 1e2,
        "k": 1e3, "K": 1e3, "w": 1e4, "W": 1e4,
    }
    _AMOUNT_RE = re.compile(
        r"^\s*([+-]?\d+(?:\.\d+)?)\s*([亿萬万千百kKwW]?)\s*(?:元|圆|RMB|CNY)?\s*$",
        re.IGNORECASE,
    )

    def _normalize_amount(self, amount_val: Any) -> Optional[str]:
        """将各种格式的金额归一化为纯数字字符串，便于比较。

        支持：`5590` / `5590元` / `50万` / `50万元` / `1.5亿` / `1,234.56` / `¥100`。
        无法解析返回 None（warning 一次）。
        """
        if amount_val is None:
            return None
        raw = str(amount_val)
        if raw.strip() == "":
            return None
        cleaned = re.sub(r"[,\s¥￥$]", "", raw)
        if not cleaned:
            return None
        m = self._AMOUNT_RE.match(cleaned)
        if m:
            base = float(m.group(1))
            unit = m.group(2) or ""
            return str(base * self._UNIT.get(unit, 1.0))
        # 退化：纯数字（含科学计数法）
        try:
            return str(float(cleaned))
        except (ValueError, TypeError):
            logger.warning(f"无法将金额值 '{amount_val}' 归一化为数字字符串，将忽略此值。")
            return None

    def _amount_match(self, ai_amount: Any, gt_amount: Any) -> Optional[bool]:
        """金额是否一致（C7）。任一不可解析返回 None。"""
        a = self._normalize_amount(ai_amount)
        g = self._normalize_amount(gt_amount)
        if a is None or g is None:
            return None
        try:
            af, gf = float(a), float(g)
        except (TypeError, ValueError):
            return None
        tol = max(0.5, abs(gf) * 1e-3)
        return abs(af - gf) <= tol

    def _normalize_payment_type(self, payment_type: str) -> str:
        if not payment_type: return ""
        return self._reverse_alias_map.get(str(payment_type).lower(), str(payment_type))

    def _normalize_ratio(self, ratio_val: Any) -> Optional[float]:
        """将各种格式的比例值归一化为 [0,1] 小数。

        支持：`0.8` / `80` / `"80%"` / `"80.00 %"`。归一化后必须落在 `[0, 1.0001]`，
        否则视为非法返回 None（防止 1.5、150 这种噪声值被错误归一）。
        """
        if ratio_val is None or str(ratio_val).strip() == "":
            return None
        try:
            numeric_val = float(str(ratio_val).replace("%", "").strip())
        except (ValueError, TypeError):
            logger.warning(f"无法将比例值 '{ratio_val}' 归一化为浮点数，将忽略此值。")
            return None

        if numeric_val > 1.0:
            numeric_val = numeric_val / 100.0
        if numeric_val < 0.0 or numeric_val > 1.0001:
            logger.warning(f"比例值 '{ratio_val}' 归一化后超出 [0,1] 范围，将忽略此值。")
            return None
        return round(min(numeric_val, 1.0), 4)
        
    @staticmethod
    def calculate_metrics_from_details(
        details: List[ComparisonItem]
    ) -> Tuple[List[PaymentStage], List[PaymentStage], List[PaymentStage], EvaluationMetrics]:
        """
        根据详细的比对项列表，计算严格的评估指标。
        此方法封装了新的三种状态分类的核心业务规则。

        Returns:
            一个元组，包含 (correct_payments, missed_payments, false_payments, evaluation_metrics)。
        """
        correct, missed, false = [], [], []
        # 定义一个内部辅助函数来处理比例格式化
        ratio_pct = lambda r: round(r * 100, 2) if r is not None else None

        for item in details:
            if item.status == 'fully_matched':
                # 完全匹配的情况
                correct.append(PaymentStage(
                    payment_type=item.extracted_type or "", 
                    payment_ratio=ratio_pct(item.extracted_ratio),
                    payment_amount=item.extracted_amount,
                    source="AI提取"
                ))
            elif item.status == 'node_matched_data_mismatch':
                # 节点匹配但数据不匹配，视为一次漏提 + 一次多提
                missed.append(PaymentStage(
                    payment_type=item.ground_truth_node or "", 
                    payment_ratio=ratio_pct(item.ground_truth_ratio),
                    payment_amount=item.ground_truth_amount,
                    source="SIS系统"
                ))
                false.append(PaymentStage(
                    payment_type=item.extracted_type or "", 
                    payment_ratio=ratio_pct(item.extracted_ratio),
                    payment_amount=item.extracted_amount,
                    source="AI提取"
                ))
            elif item.status == 'node_mismatch':
                # 节点不匹配，分别作为漏提和多提
                if item.ground_truth_node:
                    missed.append(PaymentStage(
                        payment_type=item.ground_truth_node or "", 
                        payment_ratio=ratio_pct(item.ground_truth_ratio),
                        payment_amount=item.ground_truth_amount,
                        source="SIS系统"
                    ))
                if item.extracted_type:
                    false.append(PaymentStage(
                        payment_type=item.extracted_type or "", 
                        payment_ratio=ratio_pct(item.extracted_ratio),
                        payment_amount=item.extracted_amount,
                        source="AI提取"
                    ))

        # 计算评估指标
        tp, fp, fn = len(correct), len(false), len(missed)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
        metrics = EvaluationMetrics(accuracy=f1, precision=precision, recall=recall, f1_score=f1)
        
        return correct, missed, false, metrics

    def _find_matches_with_tolerance(self, ai_items: List[Dict], sis_items: List[Dict], tolerance: float = 0.01) -> List[Tuple[Dict, Dict, str]]:
        """
        返回匹配项列表，每个元素为 (ai_item, sis_item, match_type)
        match_type: 'exact', 'tolerance', 'node_match_data_mismatch', 'no_match'
        """
        matches = []
        used_ai_indices = set()
        used_sis_indices = set()
        
        # 首先尝试精确匹配和容差匹配
        for i, ai_item in enumerate(ai_items):
            if i in used_ai_indices:
                continue
            ai_type = self._normalize_payment_type(ai_item.get('payment_type', ''))
            ai_ratio = self._normalize_ratio(ai_item.get('payment_ratio'))
            if not ai_type or ai_ratio is None:
                continue
                
            for j, sis_item in enumerate(sis_items):
                if j in used_sis_indices:
                    continue
                sis_type = self._normalize_payment_type(sis_item.get('付款节点', ''))
                sis_ratio = self._normalize_ratio(sis_item.get('金额比例'))
                if not sis_type or sis_ratio is None:
                    continue
                    
                if ai_type == sis_type:
                    if abs(ai_ratio - sis_ratio) < 1e-6:  # 精确匹配
                        matches.append((ai_item, sis_item, 'exact'))
                        used_ai_indices.add(i)
                        used_sis_indices.add(j)
                        break
                    elif abs(ai_ratio - sis_ratio) <= tolerance:  # 容差匹配
                        matches.append((ai_item, sis_item, 'tolerance'))
                        used_ai_indices.add(i)
                        used_sis_indices.add(j)
                        break
                    else:  # 节点匹配但比例超出容差
                        matches.append((ai_item, sis_item, 'node_match_data_mismatch'))
                        used_ai_indices.add(i)
                        used_sis_indices.add(j)
                        break
        
        return matches

    async def compare(
        self,
        document_id: str,
        extracted_items: List[Dict],
        ground_truth_items: List[Dict]
    ) -> ComparisonResult:
        """实现三种比对结果的比较逻辑"""
        gt_items_for_doc = ground_truth_items if ground_truth_items is not None else []
        
        # 过滤掉比例或金额为0的数据
        def is_valid_gt_item(item):
            ratio = self._normalize_ratio(item.get('金额比例'))
            amount = self._normalize_amount(item.get('金额'))
            if ratio is not None and abs(ratio) < 0.001:
                return False
            if amount is not None and float(amount) == 0:
                return False
            return True
        
        def is_valid_extracted_item(item):
            ratio = self._normalize_ratio(item.get('payment_ratio'))
            amount = self._normalize_amount(item.get('payment_amount'))
            if ratio is not None and abs(ratio) < 0.001:
                return False
            if amount is not None and float(amount) == 0:
                return False
            return True
        
        # 过滤数据
        original_gt_count = len(gt_items_for_doc)
        gt_items_for_doc = [item for item in gt_items_for_doc if is_valid_gt_item(item)]
        filtered_gt_count = original_gt_count - len(gt_items_for_doc)
        if filtered_gt_count > 0:
            logger.info(f"过滤掉{filtered_gt_count}个比例或金额为0的基准数据节点")
        
        original_ext_count = len(extracted_items)
        extracted_items = [item for item in extracted_items if is_valid_extracted_item(item)]
        filtered_ext_count = original_ext_count - len(extracted_items)
        if filtered_ext_count > 0:
            logger.info(f"过滤掉{filtered_ext_count}个比例或金额为0的提取数据节点")
        
        # 按category分类
        equipment_payment_items = []
        installation_payment_items = []
        
        for item in gt_items_for_doc:
            category = item.get('category', 'equipment_payment')
            if category == 'equipment_payment':
                equipment_payment_items.append(item)
            elif category == 'installation_payment':
                installation_payment_items.append(item)
        
        equipment_extracted_items = []
        installation_extracted_items = []
        
        for item in extracted_items:
            category = item.get('clause_category', 'equipment_payment')
            if category == 'equipment_payment':
                equipment_extracted_items.append(item)
            elif category == 'installation_payment':
                installation_extracted_items.append(item)
        
        logger.info(f"基准数据分类结果: equipment_payment={len(equipment_payment_items)}项, installation_payment={len(installation_payment_items)}项")
        logger.info(f"提取数据分类结果: equipment_payment={len(equipment_extracted_items)}项, installation_payment={len(installation_extracted_items)}项")
        
        # 步骤1: 进行同类别之间的比较 - 使用带容差的匹配
        comparison_details: List[ComparisonItem] = []

        # 处理 equipment_payment 类别
        if len(equipment_payment_items) > 0 and len(equipment_extracted_items) > 0:
            matches = self._find_matches_with_tolerance(equipment_extracted_items, equipment_payment_items, tolerance=0.01)
            
            # 处理匹配的项
            for ai_item, sis_item, match_type in matches:
                if match_type == 'exact':
                    status = 'fully_matched'
                    is_ratio_match = True
                    is_fully_matched = True
                    match_score = 100.0
                elif match_type == 'tolerance':
                    status = 'fully_matched'
                    is_ratio_match = True
                    is_fully_matched = True
                    match_score = 95.0
                elif match_type == 'node_match_data_mismatch':
                    status = 'node_matched_data_mismatch'
                    is_ratio_match = False
                    is_fully_matched = False
                    match_score = 80.0  # 给一个中等分数
                else:
                    continue  # 跳过未知类型
                
                comparison_details.append(ComparisonItem(
                    status=status,
                    extracted_clause=ai_item.get('payment_clause'),
                    extracted_type=ai_item.get('payment_type'),
                    extracted_ratio=self._normalize_ratio(ai_item.get('payment_ratio')),
                    extracted_amount=ai_item.get('payment_amount'),
                    ground_truth_node=sis_item.get('付款节点'),
                    ground_truth_ratio=self._normalize_ratio(sis_item.get('金额比例')),
                    ground_truth_amount=sis_item.get('金额'),
                    match_score=match_score,
                    is_ratio_match=is_ratio_match,
                    is_amount_match=self._amount_match(ai_item.get('payment_amount'), sis_item.get('金额')),
                    is_fully_matched=is_fully_matched,
                    ground_truth_row_index=int(sis_item.get('row_index', -1)),
                    ai_payment_type=self._normalize_payment_type(ai_item.get('payment_type', '')),
                    sis_payment_type=self._normalize_payment_type(sis_item.get('付款节点', '')),
                    ai_source='equipment_payment',
                    sis_source='equipment_payment'
                ))
            
            # 处理未匹配的AI项
            matched_ai_items = {id(ai_item) for ai_item, _, _ in matches}
            for ai_item in equipment_extracted_items:
                if id(ai_item) not in matched_ai_items:
                    comparison_details.append(ComparisonItem(
                        status='node_mismatch',
                        extracted_clause=ai_item.get('payment_clause'),
                        extracted_type=ai_item.get('payment_type'),
                        extracted_ratio=self._normalize_ratio(ai_item.get('payment_ratio')),
                        extracted_amount=ai_item.get('payment_amount'),
                        ground_truth_node=None,
                        ground_truth_ratio=None,
                        ground_truth_amount=None,
                        match_score=None,
                        is_ratio_match=None,
                        is_amount_match=None,
                        is_fully_matched=None,
                        ground_truth_row_index=None,
                        ai_payment_type=self._normalize_payment_type(ai_item.get('payment_type', '')),
                        sis_payment_type=None,
                        ai_source='equipment_payment',
                        sis_source=None
                    ))
            
            # 处理未匹配的SIS项
            matched_sis_items = {id(sis_item) for _, sis_item, _ in matches}
            for sis_item in equipment_payment_items:
                if id(sis_item) not in matched_sis_items:
                    comparison_details.append(ComparisonItem(
                        status='node_mismatch',
                        ground_truth_node=sis_item.get('付款节点'),
                        ground_truth_ratio=self._normalize_ratio(sis_item.get('金额比例')),
                        ground_truth_amount=sis_item.get('金额'),
                        ground_truth_row_index=int(sis_item.get('row_index', -1)),
                        extracted_clause=None,
                        extracted_type=None,
                        extracted_ratio=None,
                        extracted_amount=None,
                        match_score=None,
                        is_ratio_match=None,
                        is_amount_match=None,
                        is_fully_matched=None,
                        ai_payment_type=None,
                        sis_payment_type=self._normalize_payment_type(sis_item.get('付款节点', '')),
                        ai_source=None,
                        sis_source='equipment_payment'
                    ))

        # 处理 installation_payment 类别（同样的逻辑）
        if len(installation_payment_items) > 0 and len(installation_extracted_items) > 0:
            matches = self._find_matches_with_tolerance(installation_extracted_items, installation_payment_items, tolerance=0.01)
            
            # 处理匹配的项
            for ai_item, sis_item, match_type in matches:
                if match_type == 'exact':
                    status = 'fully_matched'
                    is_ratio_match = True
                    is_fully_matched = True
                    match_score = 100.0
                elif match_type == 'tolerance':
                    status = 'fully_matched'
                    is_ratio_match = True
                    is_fully_matched = True
                    match_score = 95.0
                elif match_type == 'node_match_data_mismatch':
                    status = 'node_matched_data_mismatch'
                    is_ratio_match = False
                    is_fully_matched = False
                    match_score = 80.0
                else:
                    continue  # 跳过未知类型
                
                comparison_details.append(ComparisonItem(
                    status=status,
                    extracted_clause=ai_item.get('payment_clause'),
                    extracted_type=ai_item.get('payment_type'),
                    extracted_ratio=self._normalize_ratio(ai_item.get('payment_ratio')),
                    extracted_amount=ai_item.get('payment_amount'),
                    ground_truth_node=sis_item.get('付款节点'),
                    ground_truth_ratio=self._normalize_ratio(sis_item.get('金额比例')),
                    ground_truth_amount=sis_item.get('金额'),
                    match_score=match_score,
                    is_ratio_match=is_ratio_match,
                    is_amount_match=self._amount_match(ai_item.get('payment_amount'), sis_item.get('金额')),
                    is_fully_matched=is_fully_matched,
                    ground_truth_row_index=int(sis_item.get('row_index', -1)),
                    ai_payment_type=self._normalize_payment_type(ai_item.get('payment_type', '')),
                    sis_payment_type=self._normalize_payment_type(sis_item.get('付款节点', '')),
                    ai_source='installation_payment',
                    sis_source='installation_payment'
                ))
            
            # 处理未匹配的AI项
            matched_ai_items = {id(ai_item) for ai_item, _, _ in matches}
            for ai_item in installation_extracted_items:
                if id(ai_item) not in matched_ai_items:
                    comparison_details.append(ComparisonItem(
                        status='node_mismatch',
                        extracted_clause=ai_item.get('payment_clause'),
                        extracted_type=ai_item.get('payment_type'),
                        extracted_ratio=self._normalize_ratio(ai_item.get('payment_ratio')),
                        extracted_amount=ai_item.get('payment_amount'),
                        ground_truth_node=None,
                        ground_truth_ratio=None,
                        ground_truth_amount=None,
                        match_score=None,
                        is_ratio_match=None,
                        is_amount_match=None,
                        is_fully_matched=None,
                        ground_truth_row_index=None,
                        ai_payment_type=self._normalize_payment_type(ai_item.get('payment_type', '')),
                        sis_payment_type=None,
                        ai_source='installation_payment',
                        sis_source=None
                    ))
            
            # 处理未匹配的SIS项
            matched_sis_items = {id(sis_item) for _, sis_item, _ in matches}
            for sis_item in installation_payment_items:
                if id(sis_item) not in matched_sis_items:
                    comparison_details.append(ComparisonItem(
                        status='node_mismatch',
                        ground_truth_node=sis_item.get('付款节点'),
                        ground_truth_ratio=self._normalize_ratio(sis_item.get('金额比例')),
                        ground_truth_amount=sis_item.get('金额'),
                        ground_truth_row_index=int(sis_item.get('row_index', -1)),
                        extracted_clause=None,
                        extracted_type=None,
                        extracted_ratio=None,
                        extracted_amount=None,
                        match_score=None,
                        is_ratio_match=None,
                        is_amount_match=None,
                        is_fully_matched=None,
                        ai_payment_type=None,
                        sis_payment_type=self._normalize_payment_type(sis_item.get('付款节点', '')),
                        ai_source=None,
                        sis_source='installation_payment'
                    ))
        
        # 步骤2: 生成摘要
        fully_matched_count = sum(1 for d in comparison_details if d.status == 'fully_matched')
        node_matched_data_mismatch_count = sum(1 for d in comparison_details if d.status == 'node_matched_data_mismatch')
        node_mismatch_count = sum(1 for d in comparison_details if d.status == 'node_mismatch')

        # 计算实际参与比较的基准数据总数
        actual_gt_count = len(equipment_payment_items) + len(installation_payment_items)
        total_ext = len(extracted_items)

        # 计算评估指标
        precision = fully_matched_count / total_ext if total_ext > 0 else 0
        recall = fully_matched_count / actual_gt_count if actual_gt_count > 0 else 0
        accuracy = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

        # 计算比例和金额匹配率
        node_matched_items = [d for d in comparison_details if d.status == 'fully_matched']
        node_matched_count = len(node_matched_items)
        ratio_matched_count = sum(1 for d in node_matched_items if d.is_ratio_match)
        ratio_match_rate = ratio_matched_count / node_matched_count if node_matched_count > 0 else 0.0
        amount_matched_count = sum(1 for d in node_matched_items if d.is_amount_match)
        amount_match_rate = amount_matched_count / node_matched_count if node_matched_count > 0 else 0.0

        summary = ComparisonSummary(
            document_id=document_id,
            total_extracted=len(extracted_items), 
            total_ground_truth=len(gt_items_for_doc),
            fully_matched_count=fully_matched_count,
            node_matched_data_mismatch_count=node_matched_data_mismatch_count,
            node_mismatch_count=node_mismatch_count,
            overall_accuracy=round(accuracy, 4), 
            ratio_match_rate=round(ratio_match_rate, 4),
            amount_match_rate=round(amount_match_rate, 4)
        )
        return ComparisonResult(summary=summary, comparison_details=comparison_details)