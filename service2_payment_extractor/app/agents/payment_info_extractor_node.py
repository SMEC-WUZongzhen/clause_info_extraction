# app/agents/payment_info_extractor_node.py

import asyncio
from collections import Counter
from typing import Dict, Any, Optional, List, Tuple, Set
from loguru import logger
from langchain_core.runnables import RunnableConfig
import json
import re
from difflib import SequenceMatcher
from app.states.states import State, Paragraph, PaymentInfo, WarrantyInfo, ThinkingInfo
from app.utils.rag_retriever import retrieve_payment_type
from app.utils.payment_ratio_extractor import (
    PaymentRatioExtractor,
    PaymentSummaryRatioExtractor,
    get_summary_extractor,
    get_ratio_extractor,
)
from app.utils.node_decorator import node_with_progress
from app.config.graph_config import WORKFLOW_PROGRESS_RANGES, EXTRACTOR_STAGE_PROGRESS
from app.config.env_config import (
    get_clause_filter_keywords,
    get_dedupe_thresholds,
    normalize_clause_class,
    enforce_install_payment_type,
    get_negotiation_reject_keywords,
    get_failure_rate_threshold,
)
from app.config.business_dict import get_business_dict
from app.utils.debug_helper import DebugHelper
from app.utils.log_redact import safe_clause


# 统一日志前缀，便于 ELK 过滤；新增日志请一律使用 f"{_NODE_TAG} ..." 前缀。
_NODE_TAG = "[Service2-Extractor]"


def _calculate_similarity(text1: str, text2: str) -> float:
    """
    计算两个字符串的相似度，返回0-1之间的值
    """
    return SequenceMatcher(None, text1, text2).ratio()


# === 比例反算硬规则兜底 ===
# 业务关键词已迁移至 app/resources/business_dict/v1.yaml；此处仅保留判定函数。
# 旧的模块级常量 _AUX_FEE_KEYWORDS / _EXPLICIT_RATIO_TOKENS 已删除。


def _should_strip_ratio(payment_clause: Optional[str]) -> bool:
    """判断是否应当强制清空 ratio。

    True：条款原文包含辅助费目（如 保养费 / 指导费）且不含显式比例标记 → 反算结果不可靠，强制清空。
    False：其他情形（含显式 X% / 百分之 / 不含辅助费目）→ 保留 LLM 输出的 ratio。
    """
    if not payment_clause:
        return False
    text = str(payment_clause)
    bd = get_business_dict()
    has_explicit_ratio = any(token in text for token in bd.synonyms.percent_tokens)
    if has_explicit_ratio:
        return False
    return any(kw in text for kw in bd.aux_fee_keywords)


# === 条款有效性验证白名单兜底 ===
# 关键词与阈值已迁移至业务词典 force_valid 配置；此处仅保留判定函数。


def _should_force_valid(clause: Optional[str], clause_class: Optional[List[str]]) -> bool:
    """判断条款是否应被白名单强制保留为有效。

    用于覆盖 LLM 在"节点名 + 比例 + 非典型支付动词（垫付/退还等）"形态下的 false 输出。
    支持两种方向：
      - 关键词在前：如"提货款 95%"
      - 比例在前：如"余 5%质保金"、"5% 质量保证金"
    """
    if not clause:
        return False
    text = str(clause)
    fv = get_business_dict().force_valid
    if len(text) < fv.min_clause_len:
        return False

    # 排除关键词命中（在条款原文中匹配，clause_class 标签若可用也参与匹配）
    haystack = text
    if clause_class:
        try:
            haystack = text + " | " + " ".join(str(c) for c in clause_class)
        except Exception:
            haystack = text
    if any(kw in haystack for kw in fv.exclude_keywords):
        return False

    # 双向窗口扫描：先找出所有"百分比"出现位置，再判断是否有节点关键词在 ±gap 字符内
    pct_iter = list(re.finditer(r"\d+(\.\d+)?\s*[%％]", text))
    if not pct_iter:
        return False
    max_kw_len = max(len(kw) for kw in fv.node_keywords)
    for m in pct_iter:
        pct_start, pct_end = m.start(), m.end()
        # 前后各扩展 fv.node_pct_gap + 最长关键词长度
        win_start = max(0, pct_start - fv.node_pct_gap - max_kw_len)
        win_end = min(len(text), pct_end + fv.node_pct_gap + max_kw_len)
        window = text[win_start:win_end]
        for kw in fv.node_keywords:
            if kw in window:
                return True
    return False


# 上下文合并阈值（H1，I9 env 化）：仅在存在前后缀/子串重叠 且 相似度足够高时才合并
_DEDUPE_THRESHOLDS = get_dedupe_thresholds()
_CONTEXT_MERGE_SIM_THRESHOLD = _DEDUPE_THRESHOLDS["context_similarity"]
_CONTEXT_OVERLAP_MIN_LEN = int(_DEDUPE_THRESHOLDS["context_overlap_chars"])


# === 复核结果后处理：算术校核 + 零节点清理 ===
def _amount_appears_in_text(amount_str: Optional[str], text: Optional[str]) -> bool:
    """判断 amount 数字（如 '1572000' / '1572000.0' / '1,572,000' / '157.2万'）
    是否能在 text 原文中匹配到。匹配采用"纯数字串包含"判定，容忍：
      - 千分位逗号（含全角逗号）
      - 末尾 .0 / .00 等冗余小数
      - 单位"万元/万"换算（如 amount=1572000，原文写"157.2万元"也算命中）
    """
    if not amount_str or not text:
        return False
    amt = PaymentRatioExtractor._parse_amount_to_float(amount_str)
    if amt is None or amt <= 0:
        return False
    # 把原文中所有"数字串 + 可选(万元|万)"提取并规整为元
    nums_in_text: List[float] = []
    for m in re.finditer(r"(\d[\d,，.]*)\s*(万元|万)?", str(text)):
        raw = m.group(1).replace(",", "").replace("，", "")
        # 排除末尾仅是分隔符的情况
        if not re.search(r"\d", raw):
            continue
        try:
            v = float(raw)
        except ValueError:
            continue
        if m.group(2) in ("万元", "万"):
            v *= 10000.0
        nums_in_text.append(v)
    return any(abs(v - amt) < 0.5 for v in nums_in_text)


def _postprocess_final_payment_infos(infos: List[PaymentInfo]) -> List[PaymentInfo]:
    """对复核后的最终 PaymentInfo 列表做两件事：
      1) 算术校核：原文不含 % 且 payment_context 中存在唯一数字总价、且 amount==total → 强制 ratio=1.0；
      2) 零金额清理：amount 解析后==0（含 "0"/"0.00"/"0元"/空字符串）→ 丢弃该节点。
         适用于含比例但金额标 0 的无效条款（如延保/附件中的 5% 占位条款），
         也覆盖原 "ratio==0 且 amount==0" 的零节点场景。
    """
    if not infos:
        return infos

    cleaned: List[PaymentInfo] = []
    pct_tokens = tuple(get_business_dict().synonyms.percent_tokens)
    for info in infos:
        clause = info.payment_clause or ""
        ctx = info.payment_context or ""

        # 1) 算术校核
        if not any(t in clause for t in pct_tokens):
            unique_total = PaymentRatioExtractor._extract_unique_total_amount(ctx)
            amt = PaymentRatioExtractor._parse_amount_to_float(info.payment_amount)
            if unique_total is not None and amt is not None and abs(amt - unique_total) < 0.5:
                cur = info.payment_ratio
                if cur is None or abs(float(cur) - 1.0) > 0.01:
                    logger.warning(
                        f"{_NODE_TAG} 复核后算术校核：amt({amt})==total({unique_total})，"
                        f"强制 ratio=1.0；原值={cur}, 类型={info.payment_type}"
                    )
                    info.payment_ratio = 1.0

        # 2) 零金额清理：amount 明确为 0 时丢弃（保留 amount=None 的合法条款，例如纯比例条款）
        amt_raw = info.payment_amount
        if amt_raw is not None:
            amt_str = str(amt_raw).replace("元", "").replace(",", "").replace("，", "").strip()
            if amt_str in ("0", "0.0", "0.00", "0.000", ""):
                logger.info(
                    f"{_NODE_TAG} 删除零金额节点（amount=0）: 类型={info.payment_type}, "
                    f"ratio={info.payment_ratio}, 原amount={amt_raw}, "
                    f"原条款={safe_clause(clause, head=60)}"
                )
                continue

        cleaned.append(info)
    return cleaned


def _has_prefix_suffix_overlap(a: str, b: str, min_len: int = _CONTEXT_OVERLAP_MIN_LEN) -> bool:
    """判断两段文本是否存在前后缀重叠（用于上下文合并的硬条件）。

    若 a 的后缀与 b 的前缀、或 b 的后缀与 a 的前缀，有长度 >= min_len 的重叠，则视为可合并候选。
    """
    if not a or not b:
        return False
    upper = min(len(a), len(b))
    if upper < min_len:
        return False
    # a 的后缀 == b 的前缀
    for overlap_len in range(upper, min_len - 1, -1):
        if a[-overlap_len:] == b[:overlap_len]:
            return True
    # b 的后缀 == a 的前缀
    for overlap_len in range(upper, min_len - 1, -1):
        if b[-overlap_len:] == a[:overlap_len]:
            return True
    return False


class _DSU:
    """简易并查集（按秩合并 + 路径压缩），用于上下文合并。"""

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> bool:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return False
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1
        return True


def _deduplicate_and_merge_contexts(
    clauses_with_contexts: List[Tuple[str, int]]
) -> Tuple[List[str], List[int]]:
    """
    对条款上下文进行去重和合并（基于并查集，复杂度 O(n^2 * L)）。

    策略：
    1. 完全相同的上下文 → 先按文本聚合为一组；
    2. 仅当存在前后缀/子串重叠，且 SequenceMatcher 相似度 >= 0.85 时才判为可合并（H1）；
    3. 用并查集将可合并组合并为连通分量（H2），结果顺序稳定（按每组最小原始下标）。

    Args:
        clauses_with_contexts: [(clause_context, original_index), ...] 列表

    Returns:
        merged_contexts: 去重合并后的上下文列表（不含序号标记）
        context_refs: 与输入等长的整数列表，每个元素是去重后上下文列表中的索引（0-based）
    """
    if not clauses_with_contexts:
        return [], []

    # ---- Step 1: 按文本聚合为初始组 ----
    text_to_group: Dict[str, int] = {}
    group_members: List[List[int]] = []  # group_id -> [original indices]
    group_rep_text: List[str] = []        # group_id -> 代表文本（初始为该文本）

    for orig_idx, (ctx, _) in enumerate(clauses_with_contexts):
        gid = text_to_group.get(ctx)
        if gid is None:
            gid = len(group_members)
            text_to_group[ctx] = gid
            group_members.append([orig_idx])
            group_rep_text.append(ctx)
        else:
            group_members[gid].append(orig_idx)

    n_groups = len(group_members)

    # ---- Step 2: 并查集合并重叠/高相似度组 ----
    dsu = _DSU(n_groups)
    for i in range(n_groups):
        ctx_i = group_rep_text[i]
        for j in range(i + 1, n_groups):
            if dsu.find(i) == dsu.find(j):
                continue
            ctx_j = group_rep_text[j]
            if ctx_i in ctx_j or ctx_j in ctx_i:
                dsu.union(i, j)
                continue
            if not _has_prefix_suffix_overlap(ctx_i, ctx_j):
                continue
            if SequenceMatcher(None, ctx_i, ctx_j).ratio() >= _CONTEXT_MERGE_SIM_THRESHOLD:
                dsu.union(i, j)

    # ---- Step 3: 按根聚合，按组内最小原始下标排序输出 ----
    root_to_gids: Dict[int, List[int]] = {}
    for gid in range(n_groups):
        root_to_gids.setdefault(dsu.find(gid), []).append(gid)

    def _group_min_idx(gids: List[int]) -> int:
        return min(min(group_members[g]) for g in gids)

    sorted_components = sorted(root_to_gids.values(), key=_group_min_idx)

    merged_contexts: List[str] = []
    index_to_ref: List[int] = [-1] * len(clauses_with_contexts)

    for ref_idx, gids in enumerate(sorted_components):
        # 依序合并组内代表文本
        sorted_gids = sorted(gids, key=lambda g: min(group_members[g]))
        rep = group_rep_text[sorted_gids[0]]
        for g in sorted_gids[1:]:
            merged_rep = _merge_overlapping_strings(rep, group_rep_text[g])
            if merged_rep is not None:
                rep = merged_rep
            # 否则跳过拼接（无重叠），保留较长的代表文本
            elif len(group_rep_text[g]) > len(rep):
                rep = group_rep_text[g]
        merged_contexts.append(rep)
        for g in sorted_gids:
            for orig_idx in group_members[g]:
                index_to_ref[orig_idx] = ref_idx

    # 统计日志（M8：使用 Counter 代替 list.count）
    saved = len(clauses_with_contexts) - len(merged_contexts)
    logger.info(
        f"{_NODE_TAG} 上下文去重合并完成：输入 {len(clauses_with_contexts)} 条 → "
        f"去重后 {len(merged_contexts)} 条（节省 {saved} 条）"
    )
    ref_counter = Counter(index_to_ref)
    for ri, mc in enumerate(merged_contexts):
        logger.debug(f"  上下文[{ri + 1}]（被 {ref_counter[ri]} 条条款引用）: {safe_clause(mc, head=100)}")

    return merged_contexts, index_to_ref


def _merge_overlapping_strings(a: str, b: str) -> Optional[str]:
    """
    将两个可能有重叠的字符串合并为一个超集字符串。
    策略：找到 a 的后缀和 b 的前缀的最大重叠，将 b 追加到 a 后面。
    如果完全不重叠，返回 None（由调用方决定如何处理），避免产生语义不相关的超长拼接。
    """
    if not a:
        return b
    if not b:
        return a

    # 包含关系：较长者即是超集
    if a in b:
        return b
    if b in a:
        return a

    # a 的后缀 == b 的前缀
    for overlap_len in range(min(len(a), len(b)), 0, -1):
        if a[-overlap_len:] == b[:overlap_len]:
            return a + b[overlap_len:]

    # b 的后缀 == a 的前缀
    for overlap_len in range(min(len(a), len(b)), 0, -1):
        if b[-overlap_len:] == a[:overlap_len]:
            return b + a[overlap_len:]

    # 完全不重叠：不强行拼接
    return None


def _remove_duplicate_payment_items(payment_items: List[Dict[str, Any]], similarity_threshold: Optional[float] = None) -> List[Dict[str, Any]]:
    """
    去除字符相似度大于阈值的重复支付条款，保留较长的条款
    
    Args:
        payment_items: 待去重的列表
        similarity_threshold: 相似度阈值；None 时使用 env 默认值（DEDUPE_ITEM_SIM_LOOSE）
    
    Returns:
        去重后的支付条款列表
    """
    if not payment_items or len(payment_items) <= 1:
        return payment_items

    if similarity_threshold is None:
        similarity_threshold = _DEDUPE_THRESHOLDS["item_similarity_loose"]

    logger.info(f"开始对初步提取结果进行去重处理，原始条款数量: {len(payment_items)}，阈值: {similarity_threshold}")
    
    # 创建条款副本，避免修改原始数据
    items = list(payment_items)
    to_remove = set()
    
    # 两两比较条款
    for i in range(len(items)):
        if i in to_remove:
            continue
            
        item1 = items[i]
        text1 = item1.get('payment_clause', '').strip()
        
        for j in range(i + 1, len(items)):
            if j in to_remove:
                continue

            item2 = items[j]

            # 不同 clause_class 的条目不进行去重（混签展开产生的设备/安装副本需各自保留）
            cc1 = item1.get('clause_class', [])
            cc2 = item2.get('clause_class', [])
            if cc1 != cc2:
                continue

            # 不同 payment_type 的条目不进行去重（同一条款拆分出的多个付款节点需各自保留）
            pt1 = item1.get('payment_type', '')
            pt2 = item2.get('payment_type', '')
            if pt1 and pt2 and pt1 != pt2:
                continue

            text2 = item2.get('payment_clause', '').strip()
            
            # 检查包含关系
            is_duplicate = False
            duplicate_reason = ""
            
            # 0. 同 payment_type + 同 amount → 视为重复（来自不同段落的同一笔付款）
            amt1 = str(item1.get('payment_amount', '') or '').replace('元', '').replace(',', '').strip()
            amt2 = str(item2.get('payment_amount', '') or '').replace('元', '').replace(',', '').strip()
            if pt1 and pt1 == pt2 and amt1 and amt2 and amt1 == amt2:
                is_duplicate = True
                duplicate_reason = f"同payment_type='{pt1}' + 同amount='{amt1}'"
            
            # 1. 检查完全包含关系
            if not is_duplicate and text1 in text2 and len(text1) < len(text2):
                is_duplicate = True
                duplicate_reason = "条款1完全包含在条款2中"
            elif not is_duplicate and text2 in text1 and len(text2) < len(text1):
                is_duplicate = True
                duplicate_reason = "条款2完全包含在条款1中"
            
            # 2. 检查相似度
            if not is_duplicate:
                similarity = _calculate_similarity(text1, text2)
                if similarity >= similarity_threshold:
                    is_duplicate = True
                    duplicate_reason = f"字符相似度: {similarity:.2f}"
            
            if is_duplicate:
                # 优先保留条款原文中实际包含 amount/ratio 的那条（避免幻觉节点胜出）
                amt_str = amt1 or amt2  # 去重匹配时确认的 amount 值
                text1_has_evidence = bool(amt_str and amt_str in text1) or bool(re.search(r'\d+%', text1))
                text2_has_evidence = bool(amt_str and amt_str in text2) or bool(re.search(r'\d+%', text2))

                if text1_has_evidence and not text2_has_evidence:
                    # text1 有证据，保留 text1
                    keep_i = True
                elif text2_has_evidence and not text1_has_evidence:
                    # text2 有证据，保留 text2
                    keep_i = False
                else:
                    # 都有或都无证据时，保留较长的
                    keep_i = len(text1) >= len(text2)

                if keep_i:
                    to_remove.add(j)
                    logger.info(f"发现重复条款，原因: {duplicate_reason}")
                    logger.info(f"  保留: IDX={item1.get('sub_clause_index', '')}, 长度={len(text1)}")
                    logger.info(f"  移除: IDX={item2.get('sub_clause_index', '')}, 长度={len(text2)}")
                    logger.info(f"  条款1: {safe_clause(text1, head=150)}")
                    logger.info(f"  条款2: {safe_clause(text2, head=150)}")
                else:
                    to_remove.add(i)
                    logger.info(f"发现重复条款，原因: {duplicate_reason}")
                    logger.info(f"  保留: IDX={item2.get('sub_clause_index', '')}, 长度={len(text2)}")
                    logger.info(f"  移除: IDX={item1.get('sub_clause_index', '')}, 长度={len(text1)}")
                    logger.info(f"  条款1: {safe_clause(text1, head=150)}")
                    logger.info(f"  条款2: {safe_clause(text2, head=150)}")
                    break  # 当前条款被移除，跳出内层循环
    
    # 移除标记的条款
    result = [item for i, item in enumerate(items) if i not in to_remove]
    
    logger.success(f"初步提取结果去重完成，移除 {len(to_remove)} 个重复条款，剩余 {len(result)} 个条款")
    return result


def _enforce_unique_payment_type(items: List[PaymentInfo]) -> List[PaymentInfo]:
    """
    代码级强制去重：确保同一 clause_category 下每个 payment_type 只保留一个条款。
    当存在重复时，按以下优先级选择最优条款：
    1. 条款文本包含具体支付动作（支付/付款）且包含金额/比例
    2. 条款文本更长（信息更完整）
    """
    # 按 (clause_category, payment_type) 分组
    groups: Dict[tuple, List[Any]] = {}
    non_grouped = []  # 没有 clause_category 或 payment_type 的条款直接保留

    for item in items:
        category = getattr(item, 'clause_category', None)
        ptype = getattr(item, 'payment_type', None)
        if category and ptype:
            key = (category, ptype)
            groups.setdefault(key, []).append(item)
        else:
            # 缺字段的条款记录日志后仍保留，交由下游发现问题（H4 完整修复在中长期重构中）
            logger.warning(
                f"{_NODE_TAG} 代码级去重跳过条款：缺少 clause_category 或 payment_type，"
                f"id={getattr(item, 'id', '?')}, category={category}, payment_type={ptype}"
            )
            non_grouped.append(item)

    result = list(non_grouped)
    for key, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
        else:
            # 需要去重：按规则打分选最优
            best = _pick_best_clause(group)
            removed_ids = [getattr(it, 'id', '?') for it in group if it is not best]
            logger.warning(
                f"代码级强制去重: {key[0]}+{key[1]} 有 {len(group)} 个条款，"
                f"保留 {getattr(best, 'id', '?')}，移除 {removed_ids}"
            )
            result.append(best)
    
    return result


def _pick_best_clause(candidates: List[PaymentInfo]) -> Optional[PaymentInfo]:
    """从多个同 payment_type 的条款中选出最优的一个。"""

    def _score(item) -> tuple:
        clause = str(getattr(item, 'payment_clause', '') or '')
        # 维度1: 是否包含支付动作 + 金额/比例
        has_action = bool(re.search(r'支付|付款|汇入|付清', clause))
        has_amount = bool(re.search(r'\d+(\.\d+)?%|百分之|万元|元整|\d+元', clause))
        score_action = 2 if (has_action and has_amount) else (1 if has_action else 0)
        # 维度2: 条款文本长度（信息丰富度）
        score_len = len(clause)
        return (score_action, score_len)

    return max(candidates, key=_score)


async def _validate_extraction_results(
    summary_result: List[Any],
    thinking_info: Optional[Any],
    llm_config: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Any], Optional[Any], Set[str]]:
    """
    对提取结果进行校验的辅助函数。
    使用与extract_summary相同的LLM实现进行二次校验。
    返回: (校验后结果, 思考信息, 被去重移除的ID集合)
    """
    try:
        logger.info("开始执行结果校验...")
        
        # 如果原始结果为空，直接返回
        if not summary_result:
            logger.warning("原始提取结果为空，跳过校验")
            return summary_result, thinking_info, set()
        
        # 1. 先通过硬编码比较找出重复节点
        logger.info("开始硬编码比较，识别重复节点...")
        
        # 按clause_category分组
        equipment_items = []
        installation_items = []
        
        for item in summary_result:
            if hasattr(item, 'clause_category'):
                if item.clause_category == 'equipment_payment':
                    equipment_items.append(item)
                elif item.clause_category == 'installation_payment':
                    installation_items.append(item)
        
        # 识别设备付款条款中的重复节点
        equipment_duplicates = []
        equipment_payment_types = {}
        for item in equipment_items:
            payment_type = getattr(item, 'payment_type', '')
            if payment_type in equipment_payment_types:
                # 发现重复，将两个都加入重复列表
                if equipment_payment_types[payment_type] not in equipment_duplicates:
                    equipment_duplicates.append(equipment_payment_types[payment_type])
                equipment_duplicates.append(item)
            else:
                equipment_payment_types[payment_type] = item
        
        # 识别安装付款条款中的重复节点
        installation_duplicates = []
        installation_payment_types = {}
        for item in installation_items:
            payment_type = getattr(item, 'payment_type', '')
            if payment_type in installation_payment_types:
                # 发现重复，将两个都加入重复列表
                if installation_payment_types[payment_type] not in installation_duplicates:
                    installation_duplicates.append(installation_payment_types[payment_type])
                installation_duplicates.append(item)
            else:
                installation_payment_types[payment_type] = item
        
        # 记录重复节点信息
        if equipment_duplicates:
            logger.warning(f"设备付款条款中发现 {len(equipment_duplicates)} 个重复节点")
            for item in equipment_duplicates:
                logger.info(f"  - 重复节点: {getattr(item, 'payment_type', '')} (ID: {getattr(item, 'id', '')})")
        
        if installation_duplicates:
            logger.warning(f"安装付款条款中发现 {len(installation_duplicates)} 个重复节点")
            for item in installation_duplicates:
                logger.info(f"  - 重复节点: {getattr(item, 'payment_type', '')} (ID: {getattr(item, 'id', '')})")
        
        # 如果没有重复节点，直接返回原始结果
        if not equipment_duplicates and not installation_duplicates:
            logger.success("未发现重复节点，校验通过")
            return summary_result, thinking_info, set()

        summary_extractor = await get_summary_extractor(llm_config)
        all_duplicates = equipment_duplicates + installation_duplicates

        # 3. 按 (clause_category, payment_type) 分组重复节点
        dup_groups: Dict[tuple, List[Any]] = {}
        for item in all_duplicates:
            group_key = (getattr(item, 'clause_category', ''), getattr(item, 'payment_type', ''))
            dup_groups.setdefault(group_key, []).append(item)

        logger.info(f"准备按组校验：共 {len(dup_groups)} 个重复组（设备重复 {len(equipment_duplicates)} 条 + 安装重复 {len(installation_duplicates)} 条）")

        # 4. 每组独立并发调用 LLM（Qwen2.5-9B 友好：单任务单输出，候选仅 2-3 条，避免跨组漏选/幻觉）
        group_keys_ordered = list(dup_groups.keys())
        group_tasks = []
        for gk in group_keys_ordered:
            group_items = dup_groups[gk]
            payload = [
                {
                    "id": getattr(it, 'id', ''),
                    "payment_clause": getattr(it, 'payment_clause', ''),
                    "payment_type": getattr(it, 'payment_type', ''),
                    "final_ratio": getattr(it, 'final_ratio', ''),
                    "final_amount": getattr(it, 'final_amount', ''),
                    "clause_category": getattr(it, 'clause_category', ''),
                }
                for it in group_items
            ]
            group_tasks.append(summary_extractor.verify_single_group_single(payload))

        group_results = await asyncio.gather(*group_tasks, return_exceptions=True)

        # 5. 汇总每组的 selected_id，并累计 reason 作为 thinking
        final_result = list(summary_result)
        dedup_removed_ids: set = set()
        thinking_lines: List[str] = []

        for gk, gr in zip(group_keys_ordered, group_results):
            group_items = dup_groups[gk]
            group_ids = {getattr(it, 'id', '') for it in group_items}

            if isinstance(gr, Exception):
                logger.warning(f"去重组 {gk} LLM 调用异常: {gr}，交给代码级兜底")
                continue

            selected_id = (gr or {}).get("select_clause_id", "")
            reason = (gr or {}).get("reason", "")
            if selected_id not in group_ids:
                # verify_single_group_single 内部已做兜底，这里一般不会进入；保险再兜一次
                logger.warning(f"去重组 {gk} 选出 ID={selected_id} 不在组内 {group_ids}，交给代码级兜底")
                continue

            thinking_lines.append(f"[{gk[0]}|{gk[1]}] 保留 {selected_id}：{reason}")
            for item in group_items:
                item_id = getattr(item, 'id', '')
                if item_id != selected_id and item in final_result:
                    final_result.remove(item)
                    dedup_removed_ids.add(item_id)
                    logger.info(f"去重移除条款 ID={item_id}（组={gk}，保留 {selected_id}）")

        # 6. 组装 thinking_info（兼容原返回结构）
        validated_thinking_info = thinking_info
        if thinking_lines:
            try:
                validated_thinking_info = ThinkingInfo.model_validate({"thinking_output": "\n".join(thinking_lines)})
            except Exception as e:
                logger.warning(f"按组去重的 thinking_info 组装失败: {e}")

        # 7. 代码级强制去重兜底：确保同一 clause_category 下每个 payment_type 只保留一个
        before_enforce = set(getattr(it, 'id', '') for it in final_result)
        final_result = _enforce_unique_payment_type(final_result)
        after_enforce = set(getattr(it, 'id', '') for it in final_result)
        dedup_removed_ids |= (before_enforce - after_enforce)

        logger.success(f"校验完成，最终保留 {len(final_result)} 个条款，去重移除ID: {dedup_removed_ids or '无'}")
        return final_result, validated_thinking_info, dedup_removed_ids
        
    except Exception as e:
        logger.error(f"结果校验过程中发生错误: {e}", exc_info=True)
        logger.warning("校验失败，对原始结果执行代码级强制去重后返回")
        return _enforce_unique_payment_type(summary_result), thinking_info, set()



async def _process_single_payment_paragraph(
    para: Paragraph,
    state: State,
    current_clauses_chunk: str,
    para_global_idx: int = 0,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    对单个段落（= 一个原子子条款）进行 RAG+LLM 处理。
    支持从单个条款中提取多个付款节点（如分期付款条款包含多个独立付款动作）。
    返回: (初步提取信息字典列表, 设备RAG示例列表, 安装RAG示例列表)
    """
    node_name = "payment_info_extractor"  # 兼容历史，新代码请直接用 _NODE_TAG
    results = []
    equipment_rag_examples = []
    installation_rag_examples = []

    clause_text = (para.clause or "").strip()
    if not clause_text:
        logger.warning(f"{_NODE_TAG} 段落(global_idx:{para_global_idx}) 无有效条款文本，跳过。")
        return results, equipment_rag_examples, installation_rag_examples

    # 从 clause_class 推导英文类型（M7：使用集中化的 normalize_clause_class）
    type_list: List[str] = []
    for cc in para.clause_class:
        canonical = normalize_clause_class(cc)
        if canonical == "installation_payment":
            type_list = ["installation_payment"]
            break
        if canonical == "equipment_payment":
            type_list = ["equipment_payment"]
            break
    if not type_list:
        type_list = ["equipment_payment"]

    logger.debug(f"{_NODE_TAG} 开始处理条款 (global_idx:{para_global_idx})...")

    # M4：错误分层，便于定位哪一步失败
    # --- RAG 阶段 ---
    try:
        rag_result = await retrieve_payment_type(clause_text, type_list)
    except Exception as e:
        logger.error(
            f"{_NODE_TAG}[RAG失败] 条款(global_idx:{para_global_idx}) RAG 检索异常: {e}",
            exc_info=True,
        )
        # H2 修复：把失败信息上报到 state，便于节点末端做失败率裁决
        state.setdefault("_extraction_errors", []).append({
            "stage": "rag",
            "paragraph_idx": para_global_idx,
            "error_type": type(e).__name__,
            "error": str(e)[:200],
        })
        return results, equipment_rag_examples, installation_rag_examples

    final_rag_results = rag_result.get("final_results", [])
    if not final_rag_results:
        logger.warning(f"{_NODE_TAG} 条款(global_idx:{para_global_idx}) RAG未召回，跳过。")
        return results, equipment_rag_examples, installation_rag_examples

    # RAG投票获取参考类型（仅作为LLM输入的参考，不再作为最终类型）
    label_counts: Dict[str, int] = {}
    for r in final_rag_results:
        label = r.get("label") or r.get("payment_type", "未知类型")
        if label and label != "未知类型":
            label_counts[label] = label_counts.get(label, 0) + 1

    if label_counts:
        rag_payment_type = max(label_counts.items(), key=lambda x: x[1])[0]
        logger.debug(f"{_NODE_TAG} RAG类别统计: {label_counts}, 参考类型: {rag_payment_type}")
    else:
        rag_payment_type = "未知类型"
        logger.warning(f"{_NODE_TAG} RAG 结果无有效类别标签，使用默认值")

    # --- LLM 提取阶段 ---
    try:
        # 进程级单例：避免每条段落重新构造 ChatOpenAI + chain；
        # llm_config 由 state 注入，单例内部按需重新初始化（首次或配置变化时）。
        extractor = await get_ratio_extractor(state.get("payment_ratio_llm_config") or state.get("llm_config"))
        clause_class_str = type_list[0] if type_list else "equipment_payment"
        payment_nodes = await extractor.extract_payment_info(
            payment_clause=clause_text,
            payment_type=rag_payment_type,
            rag_results=final_rag_results,
            current_clauses_chunk=current_clauses_chunk,
            state=state,
            clause_class=clause_class_str,
        )
    except Exception as e:
        logger.error(
            f"{_NODE_TAG}[LLM失败] 条款(global_idx:{para_global_idx}) 付款信息提取异常: {e}",
            exc_info=True,
        )
        # H2 修复：上报 LLM 阶段失败
        state.setdefault("_extraction_errors", []).append({
            "stage": "llm",
            "paragraph_idx": para_global_idx,
            "error_type": type(e).__name__,
            "error": str(e)[:200],
        })
        return results, equipment_rag_examples, installation_rag_examples

    if not payment_nodes:
        logger.warning(f"{_NODE_TAG} 条款(global_idx:{para_global_idx}) LLM未提取到有效付款节点。")
        return results, equipment_rag_examples, installation_rag_examples

    logger.info(
        f"{_NODE_TAG} 条款(global_idx:{para_global_idx}) 识别到 {len(payment_nodes)} 个付款节点"
    )

    # --- 解析/构造阶段（M2：sub_clause_index 统一为 "i.s" 字符串） ---
    for sub_idx, node in enumerate(payment_nodes):
        try:
            compound_index = f"{para_global_idx}.{sub_idx}"
            node_payment_type = node.get("payment_type") or rag_payment_type
            node_ratio = node.get("ratio")
            node_amount = node.get("amount")
            # 硬规则兜底：辅助费目（保养费/指导费等）+ 无显式比例 → 强制清空 ratio
            if node_ratio is not None and _should_strip_ratio(clause_text):
                logger.info(
                    f"{_NODE_TAG} 条款(global_idx:{para_global_idx}, sub:{sub_idx}) "
                    f"命中辅助费目硬规则，强制清空 ratio（原值={node_ratio}）"
                )
                node_ratio = None
            result = {
                "sub_clause_index": compound_index,
                "payment_clause": clause_text,
                "clause_context": current_clauses_chunk,
                "payment_type": node_payment_type,
                "payment_amount": node_amount,
                "payment_ratio": node_ratio,
                "clause_class": type_list,
                "metadata": {"source": "rag", "created_at": state.get("created_at")},
            }
            logger.success(
                f"{_NODE_TAG} 条款(global_idx:{para_global_idx}, sub:{sub_idx}) 初步提取完成，"
                f"类型: {node_payment_type}, 比例: {node_ratio}, 金额: {node_amount}"
            )
            results.append(result)
        except Exception as e:
            logger.error(
                f"{_NODE_TAG}[解析失败] 条款(global_idx:{para_global_idx}, sub:{sub_idx}) 构造结果异常: {e}",
                exc_info=True,
            )
            continue

    rag_example = final_rag_results[0] if final_rag_results else None
    if rag_example:
        if "equipment_payment" in type_list:
            equipment_rag_examples.append(rag_example)
        elif "installation_payment" in type_list:
            installation_rag_examples.append(rag_example)

    return results, equipment_rag_examples, installation_rag_examples

@node_with_progress(node_name="payment_info_extractor", display_name="支付信息深度提取", track_state_keys=["paragraphs"], progress_range=WORKFLOW_PROGRESS_RANGES.get("payment_info_extractor"))
async def payment_info_extractor_node(state: State, config: Optional[RunnableConfig] = None) -> Dict[str, Any]:
    logger.info("=" * 50)
    logger.info("PAYMENT_INFO_EXTRACTOR_NODE 开始执行...")
    logger.info("=" * 50)
    # =========================================================================
    # H1 修复（阶段标注，第一阶段）
    # -------------------------------------------------------------------------
    # 本节点函数较长（870+ 行），按时间线划分为以下 12 个阶段。
    # 后续 P1 重构将把每个 `=== STAGE: xxx ===` 段抽成命名 `_stage_*` 私有协程，
    # 主节点只负责编排 + 错误聚合（参见 doc.md / tasks.md 规划）。
    # 当前阶段：仅在阶段起点插入显式标注，便于阅读与精准 diff，不改动业务行为。
    #
    # 阶段索引（与 EXTRACTOR_STAGE_PROGRESS 字典对齐）：
    #   1. prefilter            - 预过滤 + 协商被拒过滤
    #   2. validate             - 条款有效性 LLM 校验（progress=5）
    #   3. resolve_mixed        - 混签条款归属判定（progress=8）
    #   4. warranty             - 质保期单独抽取
    #   5. concurrent_extract   - 并发 RAG + LLM 抽取（progress=20）
    #   6. strict_dedupe        - 字符串严格去重
    #   7. merge_contexts       - 上下文 DSU 去重合并
    #   8. summary_review       - 批量复核（progress=60）
    #   9. dedupe_review        - 单组去重 LLM
    #  10. result_verify        - _validate_extraction_results（progress=70）
    #  11. recovery_fallback    - 兜底恢复（白名单/硬规则）
    #  12. postprocess          - 算术校核 + 零金额清理
    # =========================================================================

    configurable_config = config.get("configurable", {}) if config else {}
    is_debug_mode = configurable_config.get("debug_mode", False)

    original_paragraphs = state.get("paragraphs", [])
    if not original_paragraphs:
        logger.warning(f"{_NODE_TAG} 没有找到段落，跳过支付信息提取")
        return {"current_step": "payment_extract_skipped"}

    # L4：使用 Pydantic 的 model_copy(deep=True) 替代通用 deepcopy
    all_paragraphs = [p.model_copy(deep=True) for p in original_paragraphs]

    # 混签付款条款：延迟到步骤1（有效性验证）之后再展开/归属判定，
    # 由 LLM 同步返回 category 决定路由，避免对设备小节条款走安装流水线产生幻觉。
    mixed_count = sum(1 for p in all_paragraphs if "混签付款条款" in p.clause_class)
    if mixed_count:
        logger.info(f"{_NODE_TAG} 发现 {mixed_count} 个混签付款条款，将在有效性验证阶段同步执行归属判定")

    #######存储stage0的paragraphs
    await DebugHelper.save_snapshot(
        doc_id=state['document_id'], step_name="stage0",
        content={"payment_paragraphs": all_paragraphs},
        is_debug_enabled = is_debug_mode
    )
    
    payment_paragraphs = [
        p for p in all_paragraphs
        if "设备付款条款" in p.clause_class
           or "安装付款条款" in p.clause_class
           or "混签付款条款" in p.clause_class
    ]
    warranty_paragraphs = [
        p for p in all_paragraphs
        if "质保期条款" in p.clause_class
    ]
    logger.info(
        f"{_NODE_TAG} 接收到 {len(all_paragraphs)} 个段落，"
        f"其中 payment={len(payment_paragraphs)}, warranty={len(warranty_paragraphs)}"
    )

    # ---- 关键词预过滤（直接过滤 payment_paragraphs，不可被后续异常绕过） ----
    _INVALID_KEYWORDS = get_clause_filter_keywords()
    _pre_filtered_paras = []
    _pre_invalid_count = 0
    for para in payment_paragraphs:
        clause_text = (para.clause or "").strip()
        matched_kw = next((kw for kw in _INVALID_KEYWORDS if kw in clause_text), None)
        if matched_kw:
            _pre_invalid_count += 1
            logger.info(f"{_NODE_TAG} 硬编码预过滤: 命中关键词=\"{matched_kw}\", 条款={safe_clause(clause_text, head=80)}")
        else:
            _pre_filtered_paras.append(para)
    if _pre_invalid_count:
        logger.success(f"硬编码预过滤完成：过滤 {_pre_invalid_count} 条，剩余 {len(_pre_filtered_paras)} 条")
    payment_paragraphs = _pre_filtered_paras

    # ---- Fix-1：协商被拒上下文过滤 ----
    # 当条款的 clause_context 中命中"不予调整"等否决回复关键词时，整条条款被丢弃。
    # 仅匹配 clause_context（不匹配 clause 本身），避免误杀。
    _NEG_REJECT_KWS = get_negotiation_reject_keywords()
    if _NEG_REJECT_KWS:
        _neg_filtered_paras = []
        _neg_invalid_count = 0
        for para in payment_paragraphs:
            ctx_text = (getattr(para, "clause_context", "") or "")
            matched_kw = next((kw for kw in _NEG_REJECT_KWS if kw in ctx_text), None)
            if matched_kw:
                _neg_invalid_count += 1
                clause_preview = safe_clause((para.clause or "").strip(), head=80)
                logger.info(
                    f"{_NODE_TAG} 协商被拒过滤: context 命中=\"{matched_kw}\", "
                    f"条款={clause_preview}"
                )
            else:
                _neg_filtered_paras.append(para)
        if _neg_invalid_count:
            logger.success(
                f"{_NODE_TAG} 协商被拒过滤完成：过滤 {_neg_invalid_count} 条，"
                f"剩余 {len(_neg_filtered_paras)} 条"
            )
        payment_paragraphs = _neg_filtered_paras

    logger.info(
        f"{_NODE_TAG} 预过滤后分类统计："
        f"设备={len([p for p in payment_paragraphs if '设备付款条款' in p.clause_class])}, "
        f"安装={len([p for p in payment_paragraphs if '安装付款条款' in p.clause_class])}, "
        f"质保={len(warranty_paragraphs)}（含混签展开）"
    )

    payment_info_extractor_node.emit_running(
        f"正在进行付款条款有效性验证（过滤非付款条款）...",
        config,
        progress=EXTRACTOR_STAGE_PROGRESS["validate"]
    )

    # ========== 步骤1: 条款有效性验证（LLM校验，过滤非付款条款） ==========

    def _is_table_or_non_clause(text: str) -> bool:
        """代码预过滤：识别表格分隔线、纯碎片行、备注说明等非自然语言条款，无需送入LLM。

        注意：表格行本身**不**应被一刀切过滤——很多合同把"付款条件/比例/金额"放在表格列里。
        仅对以下情形判定为非自然语言：
        1. 纯分隔线（| --- | --- |）；
        2. 几乎全空的碎片续行（仅 1 个非空 cell，其余皆 ''）；
        3. 全为分隔符 / 空 cell 的兜底行；
        4. 注释/备注开头 且 不含付款关键词。
        其余多列、含金额/比例/付款语义的真实数据行 → 保留。
        """
        stripped = text.strip()
        if not stripped:
            return True
        # 表格分隔线：| --- | --- | 或 |---|---|
        if re.match(r'^\|[\s\-|]+\|$', stripped):
            return True
        # 表格行细化判定（以 | 开头/结尾且至少含若干 | 分隔符）
        if stripped.startswith('|') and stripped.count('|') >= 3:
            inner = stripped.strip('|')
            cells = [c.strip() for c in inner.split('|')]
            non_empty = [c for c in cells if c]
            # 全部 cell 为空或都是 ---
            if not non_empty or all(set(c) <= {'-'} for c in non_empty):
                return True
            # 几乎全空的碎片续行：仅 1 个非空 cell（剩余皆 ''）
            if len(non_empty) <= 1 and len(cells) >= 3:
                return True
            # 注释/价格组成说明行：首个非空 cell 以"注"开头 且 不含明确支付动作
            if non_empty[0].lstrip().startswith('注') and not re.search(
                r'支付|付款|请款', non_empty[0]
            ):
                return True
            # 其他真实数据行（含付款条件/比例/金额）→ 保留
            return False
        # 备注/说明行——但如果包含付款关键词（付款/支付/比例/%/请款），保留送入LLM
        if re.match(r'^备注\d*[：:]', stripped):
            if not re.search(r'付款|支付|请款|比例|%|％|全额', stripped):
                return True
        return False

    # 第一步：物理剔除被代码预过滤的非自然语言条款（表格碎片/分隔线/纯备注等），
    # 避免它们流入后续抽取阶段（之前的实现仅跳过 LLM 校验调用，但条款仍残留在 payment_paragraphs 中被 LLM 抽取）。
    _kept_paragraphs: List = []
    prefilter_count = 0
    for i, para in enumerate(payment_paragraphs):
        sc_text = (para.clause or "").strip()
        if not sc_text:
            prefilter_count += 1
            logger.info(f"预过滤跳过空条款 ID={i}")
            continue
        if _is_table_or_non_clause(sc_text):
            prefilter_count += 1
            logger.info(f"{_NODE_TAG} 预过滤跳过非自然语言条款 ID={i}: {safe_clause(sc_text, head=80)}")
            continue
        _kept_paragraphs.append(para)
    if prefilter_count > 0:
        logger.info(f"代码预过滤：跳过 {prefilter_count} 条非自然语言/空条款（表格行/分隔线/备注等）")
    payment_paragraphs = _kept_paragraphs

    # 第二步：构造待校验列表（id 与 payment_paragraphs 的下标一一对应）
    clauses_to_validate = []
    for i, para in enumerate(payment_paragraphs):
        clauses_to_validate.append({
            "id": str(i),
            "clause": (para.clause or "").strip(),
            "clause_class": para.clause_class,
            "clause_context": para.clause_context,
        })

    validated_paragraphs = []  # 验证通过的有效条款（此时混签条款仍保留原 clause_class，待步骤1.5路由）
    invalid_count = 0

    def _expand_mixed_dual(para):
        """混签兜底：展开为设备+安装两条副本。"""
        eq = para.model_copy(deep=True)
        inst = para.model_copy(deep=True)
        eq.clause_class = ["设备付款条款"]
        inst.clause_class = ["安装付款条款"]
        return [eq, inst]

    if clauses_to_validate:
        try:
            summary_extractor = await get_summary_extractor(state.get("llm_config"))

            # 并发调用，每条条款独立验证
            logger.info(f"条款有效性验证：共 {len(clauses_to_validate)} 条，并发逐条验证")
            tasks = [
                summary_extractor.validate_payment_clause_single(clause)
                for clause in clauses_to_validate
            ]
            validated_results = await asyncio.gather(*tasks, return_exceptions=True)

            # 根据验证结果过滤条款
            for clause_input, result in zip(clauses_to_validate, validated_results):
                clause_id = clause_input["id"]

                # 如果并发任务抛出异常，默认保留该条款
                if isinstance(result, Exception):
                    logger.warning(f"条款 ID={clause_id} 验证异常: {result}，默认保留")
                    try:
                        validated_paragraphs.append(payment_paragraphs[int(clause_id)])
                    except (ValueError, IndexError):
                        pass
                    continue

                # I3: fail-closed —— 仅当 LLM 显式返回 True 才视为有效
                is_valid = result.get("is_valid") is True

                # 白名单兜底：原文出现"节点关键词 + 紧邻百分比"形态时，强制保留
                # 用于救回"提货款 95% + 垫付/退还"这类支付动词非典型的有效条款
                if not is_valid:
                    try:
                        force_valid = _should_force_valid(
                            clause_input.get("clause"),
                            clause_input.get("clause_class"),
                        )
                    except Exception as _e:
                        logger.opt(exception=True).warning(
                            f"白名单兜底判定异常 ID={clause_id}: {_e}"
                        )
                        force_valid = False
                    if force_valid:
                        original_reason = result.get("reason", "")
                        logger.info(
                            f"条款验证白名单强制保留: ID={clause_id}, "
                            f"LLM原因='{original_reason}', "
                            f"覆盖原因='原文含 节点+比例，强制保留'"
                        )
                        is_valid = True

                # 找到对应的原始 paragraph
                try:
                    idx = int(clause_id)
                    original_para = payment_paragraphs[idx]
                except (ValueError, IndexError):
                    logger.warning(f"无法找到ID为 {clause_id} 的原始条款，跳过")
                    continue

                if is_valid:
                    validated_paragraphs.append(original_para)
                else:
                    invalid_count += 1
                    reason = result.get("reason", "未知原因")
                    logger.info(f"条款验证不通过，已过滤: ID={clause_id}, 原因={reason}")
                    logger.debug(f"  过滤条款原文: {safe_clause(original_para.clause, head=100)}")

            logger.success(f"条款有效性验证完成：共 {len(clauses_to_validate)} 条，验证通过 {len(validated_paragraphs)} 条，过滤 {invalid_count} 条")

            # 使用验证后的条款列表进行后续处理
            payment_paragraphs = validated_paragraphs

        except Exception as e:
            logger.opt(exception=True).error("条款有效性验证阶段发生错误: {err}", err=str(e))
            logger.warning("验证失败，不进行过滤，继续使用原始条款列表")
    else:
        logger.info("没有需要验证的支付条款")

    # ========== 步骤 1.5: 混签付款条款归属判定（独立 LLM 调用） ==========
    # 仅对 is_valid 通过且原 clause_class 含"混签付款条款"的条款触发；
    # 设计动机：对基模 Qwen2.5-9B 这类 9B 量级模型，将分类任务与有效性任务解耦，
    # 每个子 prompt 单任务单输出，显著提升 JSON 遵循率与枚举稳定性。
    mixed_paragraphs_to_classify = [p for p in payment_paragraphs if "混签付款条款" in p.clause_class]

    if mixed_paragraphs_to_classify:
        payment_info_extractor_node.emit_running(
            f"正在对 {len(mixed_paragraphs_to_classify)} 条混签付款条款执行归属判定...",
            config,
            progress=EXTRACTOR_STAGE_PROGRESS["resolve_mixed"],
        )
        mixed_route_stats = {"equipment_payment": 0, "installation_payment": 0, "both": 0}
        logger.info(f"{_NODE_TAG} 步骤1.5: 混签归属判定，共 {len(mixed_paragraphs_to_classify)} 条")

        try:
            _category_extractor = await get_summary_extractor(state.get("llm_config"))
            _cat_inputs = [
                {
                    "id": str(i),
                    "clause": (p.clause or "").strip(),
                    "clause_context": (p.clause_context or "") or "",
                }
                for i, p in enumerate(mixed_paragraphs_to_classify)
            ]
            _cat_tasks = [
                _category_extractor.classify_mixed_category_single(c) for c in _cat_inputs
            ]
            _cat_results = await asyncio.gather(*_cat_tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"{_NODE_TAG} 混签归属判定批次异常: {e}，全部兜底为设备", exc_info=True)
            _cat_results = [Exception("batch_failed")] * len(mixed_paragraphs_to_classify)

        routed: List = []
        for para, res in zip(mixed_paragraphs_to_classify, _cat_results):
            if isinstance(res, Exception):
                logger.warning(f"{_NODE_TAG} 混签归属判定失败，兜底→equipment：{res}")
                cat = "equipment_payment"
                reason = "classifier_exception"
            else:
                cat = res.get("category") or "equipment_payment"
                if cat not in ("equipment_payment", "installation_payment", "both"):
                    cat = "equipment_payment"
                reason = res.get("reason", "")

            mixed_route_stats[cat] = mixed_route_stats.get(cat, 0) + 1
            if cat == "both":
                routed.extend(_expand_mixed_dual(para))
                logger.info(f"{_NODE_TAG} 混签归属=both 展开为双轨。reason={reason}")
            elif cat == "installation_payment":
                inst = para.model_copy(deep=True)
                inst.clause_class = ["安装付款条款"]
                routed.append(inst)
                logger.info(f"{_NODE_TAG} 混签归属=installation。reason={reason}")
            else:  # equipment_payment
                eq = para.model_copy(deep=True)
                eq.clause_class = ["设备付款条款"]
                routed.append(eq)
                logger.info(f"{_NODE_TAG} 混签归属=equipment。reason={reason}")

        _non_mixed = [p for p in payment_paragraphs if "混签付款条款" not in p.clause_class]
        payment_paragraphs = _non_mixed + routed
        logger.success(
            f"{_NODE_TAG} 混签归属判定完成: {mixed_route_stats}；"
            f"混签路由后总条款数 {len(payment_paragraphs)}"
        )

    # 安全兜底：任何原因残留的混签条款统一双轨展开，保证下游不见混签
    _remaining_mixed = [p for p in payment_paragraphs if "混签付款条款" in p.clause_class]
    if _remaining_mixed:
        logger.warning(f"{_NODE_TAG} 仍残留 {len(_remaining_mixed)} 个混签条款，兜底展开为双轨")
        _non_mixed = [p for p in payment_paragraphs if "混签付款条款" not in p.clause_class]
        _expanded = []
        for p in _remaining_mixed:
            _expanded.extend(_expand_mixed_dual(p))
        payment_paragraphs = _non_mixed + _expanded

    # 取出所有质保期相关条款 —— 每个 para 就是一个条款，直接使用 clause 字段
    warranty_text_items = [
        para.clause.strip()
        for para in warranty_paragraphs
        if para.clause.strip()
    ]

    # 无论是否存在支付段落，都先单独调用质保抽取（使用专门接口）
    # H8：质保结果只通过返回值上报，不再直接 mutate state
    final_warranty_info: Optional[WarrantyInfo] = None
    warranty_thinking: Optional[ThinkingInfo] = None
    if warranty_text_items:
        try:
            summary_extractor = await get_summary_extractor(state.get("llm_config"))
            _, warranty_only_result, warranty_thinking = await summary_extractor.extract_summary_warranty(warranty_text_items)
            if warranty_only_result:
                await DebugHelper.save_snapshot(
                    doc_id=state['document_id'], step_name="warranty_info",
                    content={
                        "warranty": warranty_only_result.warranty,
                        "warranty_clause": warranty_text_items,
                        "effective_conditions": warranty_only_result.effective_conditions,
                        "closed_end_conditions": warranty_only_result.closed_end_conditions,
                        "warranty_thinking": warranty_thinking.thinking_output if warranty_thinking else None},
                    is_debug_enabled=is_debug_mode
                )
                final_warranty_info = warranty_only_result
                logger.success(
                    f"{_NODE_TAG} 质保期提取成功：warranty={warranty_only_result.warranty}, "
                    f"effective_conditions={warranty_only_result.effective_conditions}, "
                    f"closed_end_conditions={warranty_only_result.closed_end_conditions}"
                )
                for para in all_paragraphs:
                    if "质保期条款" in para.clause_class:
                        if "warranty_details" not in para.metadata:
                            para.metadata["warranty_details"] = []
                        para.metadata["warranty_details"].append({
                            "warranty_period": warranty_only_result.warranty,
                            "warranty_clause": warranty_only_result.warranty_clause,
                            "source": "llm_warranty_only"
                        })
            else:
                logger.warning(f"{_NODE_TAG} 未能单独提取到质保期信息")
        except Exception as inner_e:
            logger.error(f"{_NODE_TAG} 调用 extract_summary_warranty 时发生错误: {inner_e}", exc_info=True)

    # 如果没有支付段落，直接返回（保留原始返回格式）
    if not payment_paragraphs:
        logger.info(f"{_NODE_TAG} 未找到支付相关段落，已单独提取质保期信息，跳过支付信息提取。")
        return {
            "payment_infos": [],
            "warranty_info": final_warranty_info or state.get("warranty_info"),
            "thinking_info": warranty_thinking or state.get("thinking_info"),
            "current_step": "payment_extract_skipped",
            "paragraphs": all_paragraphs
        }
    logger.info(f"{_NODE_TAG} 找到 {len(payment_paragraphs)} 个支付段落，准备并发提取信息...")
    payment_info_extractor_node.emit_running(
        f"正在对 {len(payment_paragraphs)} 个支付条款进行RAG+LLM分析...",
        config,
        progress=EXTRACTOR_STAGE_PROGRESS["concurrent_extract"]
    )

    # 每个 para 是一个原子子条款，用全局枚举 index 作为唯一标识
    tasks = []
    for i, para in enumerate(payment_paragraphs):
        tasks.append(asyncio.create_task(
            _process_single_payment_paragraph(para, state, para.clause_context, para_global_idx=i)
        ))

    task_results = await asyncio.gather(*tasks)

    # H2 修复：基于 _process_single_payment_paragraph 上报的错误做失败率裁决
    _errors = state.get("_extraction_errors", []) or []
    _total = len(payment_paragraphs)
    _failed_paras = len({err.get("paragraph_idx") for err in _errors if err.get("paragraph_idx") is not None})
    if _total > 0:
        _fail_rate = _failed_paras / _total
        _threshold = get_failure_rate_threshold()
        if _fail_rate > _threshold:
            state["extraction_partial"] = True
            state["extraction_failure_rate"] = round(_fail_rate, 4)
            logger.error(
                f"{_NODE_TAG} 抽取失败率过高: {_failed_paras}/{_total}={_fail_rate:.2%} > "
                f"阈值 {_threshold:.2%}，已标记 extraction_partial=true"
            )
        else:
            state["extraction_partial"] = False
    else:
        state["extraction_partial"] = False
    # 把内部错误列表升级为公开字段（API 层取用）
    state["extraction_errors"] = list(_errors)

    # 解包每个任务的结果
    processed_results = []
    all_equipment_rag_examples = []
    all_installation_rag_examples = []
    for results, equipment_rag_examples, installation_rag_examples in task_results:
        processed_results.append(results)
        all_equipment_rag_examples.extend(equipment_rag_examples)
        all_installation_rag_examples.extend(installation_rag_examples)

    initial_payment_items = [item for para_result in processed_results for item in para_result]

    logger.info(f"[去重前] initial_payment_items 共 {len(initial_payment_items)} 个")
    for _i, item in enumerate(initial_payment_items):
        logger.info(f"  去重前[{_i}]: sub_clause_index={item.get('sub_clause_index')}, clause_class={item.get('clause_class')}, payment_type={item.get('payment_type')}, amount={item.get('payment_amount')}")

    # 对初步提取结果进行字符相似度去重
    logger.info("开始对初步提取结果进行字符相似度去重...")
    initial_payment_items = _remove_duplicate_payment_items(
        initial_payment_items,
        similarity_threshold=_DEDUPE_THRESHOLDS["item_similarity_strict"],
    )

    logger.info(f"[去重后] initial_payment_items 共 {len(initial_payment_items)} 个")
    for _i, item in enumerate(initial_payment_items):
        logger.info(f"  去重后[{_i}]: sub_clause_index={item.get('sub_clause_index')}, clause_class={item.get('clause_class')}, payment_type={item.get('payment_type')}, amount={item.get('payment_amount')}")

    # ---- 上下文去重合并：按 clause_class 分组后统一处理 ----
    equipment_items_for_context = []   # [(clause_context, sub_clause_index), ...]
    install_items_for_context = []
    for item in initial_payment_items:
        ctx = item.get("clause_context", "")
        sci = item["sub_clause_index"]
        if 'equipment_payment' in item.get('clause_class', []):
            equipment_items_for_context.append((ctx, sci))
        elif 'installation_payment' in item.get('clause_class', []):
            install_items_for_context.append((ctx, sci))

    # 设备组去重合并
    equip_merged_contexts, equip_context_refs = _deduplicate_and_merge_contexts(equipment_items_for_context)
    # 安装组去重合并
    install_merged_contexts, install_context_refs = _deduplicate_and_merge_contexts(install_items_for_context)

    # 构建去重后的 chunk_text_map（每条上下文带序号）
    equip_chunks_text = ""
    for idx, ctx in enumerate(equip_merged_contexts, 1):
        equip_chunks_text += f"{idx}. {ctx}\n\n"

    install_chunks_text = ""
    for idx, ctx in enumerate(install_merged_contexts, 1):
        install_chunks_text += f"{idx}. {ctx}\n\n"

    chunk_text_map = {
        "equipment_chunks": equip_chunks_text,
        "installation_chunks": install_chunks_text
    }

    # 构建 sub_clause_index -> 去重后上下文引用 的映射（直接 zip，一次遍历）
    equip_sci_to_ref = {
        sci: ref
        for (_, sci), ref in zip(equipment_items_for_context, equip_context_refs)
    }
    install_sci_to_ref = {
        sci: ref
        for (_, sci), ref in zip(install_items_for_context, install_context_refs)
    }

    logger.info(f"上下文去重合并统计:")
    logger.info(f"  设备组：{len(equipment_items_for_context)} 条上下文 → {len(equip_merged_contexts)} 条唯一上下文（节省 {len(equipment_items_for_context) - len(equip_merged_contexts)} 条重复）")
    logger.info(f"  安装组：{len(install_items_for_context)} 条上下文 → {len(install_merged_contexts)} 条唯一上下文（节省 {len(install_items_for_context) - len(install_merged_contexts)} 条重复）")
    # M8：使用 Counter 替换 list.count，避免循环内 O(n) 调用
    equip_ref_counter = Counter(equip_context_refs)
    install_ref_counter = Counter(install_context_refs)
    for ref_idx, mc in enumerate(equip_merged_contexts, 1):
        logger.debug(f"    设备上下文[{ref_idx}]（被 {equip_ref_counter[ref_idx - 1]} 条条款引用）: {safe_clause(mc, head=100)}")
    for ref_idx, mc in enumerate(install_merged_contexts, 1):
        logger.debug(f"    安装上下文[{ref_idx}]（被 {install_ref_counter[ref_idx - 1]} 条条款引用）: {safe_clause(mc, head=100)}")

    items_for_summary = []
    install_items_for_summary = []
    summary_key_to_initial_item: Dict[str, Dict] = {}
    eq_seq = 0   # 设备类别内连续编号
    in_seq = 0   # 安装类别内连续编号
    for item in initial_payment_items:
        if 'equipment_payment' in item.get('clause_class', []):
            prefixed_key = f"eq_{eq_seq}"   # 分类内连续编号，避免9B模型重编号
            eq_seq += 1
            summary_key_to_initial_item[prefixed_key] = item
            logger.info(f"  路由[sub_clause_index={item['sub_clause_index']}] → equipment, prefixed_key={prefixed_key}, payment_type={item.get('payment_type')}, clause_class={item.get('clause_class')}")
            sci = item["sub_clause_index"]
            context_ref = equip_sci_to_ref.get(sci, 0) + 1  # 转为1-based，与chunk_text_map中的编号对齐
            items_for_summary.append({
                "id": prefixed_key,
                "payment_clause": item["payment_clause"],
                "context_ref": context_ref,  # 1-based，指向 chunk_text_map 中的上下文编号
                "payment_type": item["payment_type"],
                'init_amount': item['payment_amount'],
                "init_ratio": f"{round(item['payment_ratio']*100, 2)}%" if item["payment_ratio"] is not None else None
            })
        elif 'installation_payment' in item.get('clause_class', []):
            prefixed_key = f"in_{in_seq}"   # 分类内连续编号，避免9B模型重编号
            in_seq += 1
            summary_key_to_initial_item[prefixed_key] = item
            logger.info(f"  路由[sub_clause_index={item['sub_clause_index']}] → installation, prefixed_key={prefixed_key}, payment_type={item.get('payment_type')}, clause_class={item.get('clause_class')}")
            sci = item["sub_clause_index"]
            context_ref = install_sci_to_ref.get(sci, 0) + 1  # 转为1-based，与chunk_text_map中的编号对齐
            install_items_for_summary.append({
                "id": prefixed_key,
                "payment_clause": item["payment_clause"],
                "context_ref": context_ref,  # 1-based，指向 chunk_text_map 中的上下文编号
                "payment_type": item["payment_type"],
                'init_amount': item['payment_amount'],
                "init_ratio": f"{round(item['payment_ratio']*100, 2)}%" if item["payment_ratio"] is not None else None
            })
        else:
            logger.warning(
                f"{_NODE_TAG} 路由[sub_clause_index={item.get('sub_clause_index')}] 未匹配分类，"
                f"clause_class={item.get('clause_class')}，已丢弃"
            )
    
    if not items_for_summary and not install_items_for_summary:
        logger.warning("初步提取后没有可供复核的支付条款。")
        return {"payment_infos": [], "current_step": "payment_info_success", "paragraphs": all_paragraphs}

    payment_info_extractor_node.emit_running(
        f"初步提取完成，准备对 {len(items_for_summary)+len(install_items_for_summary)} 条款进行批量复核...", 
        config, 
        progress=EXTRACTOR_STAGE_PROGRESS["summary_review"]
    )
 
    # 组织复核上下文（质保已在上方单独抽取），准备批量复核
    summary_extractor = await get_summary_extractor(state.get("llm_config"))
    batch_payment_items_str = json.dumps(items_for_summary, ensure_ascii=False)
    batch_install_payment_items_str = json.dumps(install_items_for_summary, ensure_ascii=False)

    await DebugHelper.save_snapshot(
        doc_id=state['document_id'], step_name="rag-llm_stage1",
        content={
            "equipment_stage1": items_for_summary,
            "install_stage1": install_items_for_summary,
            "merged_equipment_contexts": equip_merged_contexts,
            "merged_install_contexts": install_merged_contexts,
            "equip_context_refs": equip_context_refs,
            "install_context_refs": install_context_refs,
        },
        is_debug_enabled = is_debug_mode
    )
    summary_result, thinking_info= await summary_extractor.extract_summary(batch_payment_items_str, batch_install_payment_items_str, chunk_text_map, all_equipment_rag_examples, all_installation_rag_examples)
    # 添加结果校验步骤--
    logger.info("开始执行结果校验步骤...")
    payment_info_extractor_node.emit_running(
        f"正在对提取结果进行校验...", 
        config, 
        progress=EXTRACTOR_STAGE_PROGRESS["result_verify"]
    )
    # 调用校验函数
    validated_summary_result, validated_thinking_info, dedup_removed_ids = await _validate_extraction_results(
        summary_result,
        thinking_info,
        llm_config=state.get("llm_config"),
    )
    summary_result = validated_summary_result
    # S3：校验阶段若产出了新的 thinking，覆盖前一阶段；否则保留 extract_summary 的 thinking
    if validated_thinking_info is not None:
        thinking_info = validated_thinking_info

    logger.debug(f"{_NODE_TAG} 批量复核LLM输出条数: {len(summary_result) if summary_result else 0}")
    logger.debug(f"{_NODE_TAG} 思考过程长度: {len(thinking_info.thinking_output) if thinking_info else 0}")

    final_payment_infos: List[PaymentInfo] = []
    final_thinking_info: Optional[ThinkingInfo] = thinking_info

    # H3 修复：构建 prefix-30 hash 桶，回退匹配先在同桶内查找，
    # 避免 LLM 改写 id 时全量 N×M·L² 的相似度回退。
    _fallback_buckets: Dict[str, List[str]] = {}
    for _bk_key, _bk_item in summary_key_to_initial_item.items():
        _bk_clause = str(_bk_item.get('payment_clause', '') or '').strip()
        if not _bk_clause:
            continue
        _bk_id = _bk_clause[:30]
        _fallback_buckets.setdefault(_bk_id, []).append(_bk_key)
    _fallback_total_keys = list(summary_key_to_initial_item.keys())
    _fallback_hits = 0
    _fallback_misses = 0

    # 【核心修改】在最终循环创建 PaymentInfo 对象时
    processed_ids: set = set()  # 防止同一 id 被重复处理（如 LLM 将一个 id 拆成多个节点）
    for idx, reviewed_item in enumerate(summary_result):
        item_id = getattr(reviewed_item, 'id', None)
        logger.debug(
            f"{_NODE_TAG} 回溯 idx={idx}, item_id={repr(item_id)}, "
            f"in_key={item_id in summary_key_to_initial_item if item_id else 'N/A'}, "
            f"processed={len(processed_ids)}"
        )

        if item_id and item_id in summary_key_to_initial_item:
            if item_id in processed_ids:
                logger.debug(f"{_NODE_TAG} id={item_id} 已处理过，跳过重复条目（LLM拆分产生）")
                continue
            processed_ids.add(item_id)
            initial_item = summary_key_to_initial_item[item_id]
        elif item_id:
            # ID 不匹配（常见于9B模型将输入ID重新顺序编号）
            # H6：更严格的回退策略 —— 子串包含 > 前缀精确匹配(长度相近) > 高相似度(>=0.85 且 长度相近)
            reviewed_clause_text = str(getattr(reviewed_item, 'payment_clause', '') or '').strip()
            fallback_key = None
            fallback_item = None
            best_sim = 0.0
            if reviewed_clause_text:
                reviewed_prefix = reviewed_clause_text[:30]
                reviewed_len = len(reviewed_clause_text)
                # H3 修复：先在 hash 桶内尝试匹配；命中则跳过全量遍历
                _bucket_keys = _fallback_buckets.get(reviewed_prefix, [])
                _candidate_keys = _bucket_keys if _bucket_keys else _fallback_total_keys
                if _bucket_keys:
                    _fallback_hits += 1
                else:
                    _fallback_misses += 1
                for map_key in _candidate_keys:
                    if map_key in processed_ids:
                        continue
                    map_item = summary_key_to_initial_item.get(map_key)
                    if not map_item:
                        continue
                    map_clause = str(map_item.get('payment_clause', '') or '').strip()
                    if not map_clause:
                        continue
                    # 优先级1：完整子串包含（精确匹配，直接采用）
                    if map_clause in reviewed_clause_text or reviewed_clause_text in map_clause:
                        fallback_key = map_key
                        fallback_item = map_item
                        best_sim = 1.0
                        break
                    # 优先级2：前缀匹配（前30字符相同 且 长度差异 < 40%）
                    map_prefix = map_clause[:30]
                    map_len = len(map_clause)
                    length_ratio = min(reviewed_len, map_len) / max(reviewed_len, map_len, 1)
                    if (
                        len(reviewed_prefix) >= 10
                        and reviewed_prefix == map_prefix
                        and length_ratio >= 0.6
                    ):
                        fallback_key = map_key
                        fallback_item = map_item
                        best_sim = 0.95
                        break
                    # 优先级3：相似度计算（阈值提升至 0.85 且 长度差异 < 30%）
                    if length_ratio < 0.7:
                        continue
                    sim = _calculate_similarity(reviewed_clause_text, map_clause)
                    if sim > best_sim and sim >= 0.85:
                        best_sim = sim
                        fallback_key = map_key
                        fallback_item = map_item
            if fallback_item:
                logger.warning(
                    f"复核结果 id={item_id} 不在映射表中，按文本回退匹配到 key={fallback_key}（相似度={best_sim:.2f}），继续处理"
                )
                processed_ids.add(fallback_key)
                item_id = fallback_key
                initial_item = fallback_item
            else:
                logger.warning(
                    f"复核结果中有无法匹配的项（ID和文本均未命中）: id={item_id}, "
                    f"category={getattr(reviewed_item, 'clause_category', '')}, "
                    f"clause={safe_clause(reviewed_clause_text, head=80)}"
                )
                continue
        else:
            logger.warning(f"复核结果中有无id字段的项: category={getattr(reviewed_item, 'clause_category', '')}, clause={safe_clause(getattr(reviewed_item, 'payment_clause', ''), head=80)}")
            continue
            
        final_ratio_str = getattr(reviewed_item, 'final_ratio', None)
        final_payment_type = getattr(reviewed_item, 'payment_type', initial_item['payment_type'])
        final_amount_from_summary = getattr(reviewed_item, 'final_amount', None)

        # 获取 sub_clause_index（用于标识原始条款）
        sub_clause_index = initial_item.get('sub_clause_index')

        # 当 summary LLM 返回 ratio=null 时，回退使用初始提取的 ratio
        if not final_ratio_str or str(final_ratio_str).strip().lower() in ('none', 'null', ''):
            initial_ratio = initial_item.get('payment_ratio')
            if initial_ratio is not None:
                final_ratio_str = str(initial_ratio) if not str(initial_ratio).endswith('%') else str(initial_ratio)
                # 如果 initial_ratio 已经是 0.5 这种小数形式，转为百分比字符串
                try:
                    _r = float(initial_ratio)
                    if 0 < _r <= 1:
                        final_ratio_str = f"{_r * 100}%"
                    else:
                        final_ratio_str = f"{_r}%"
                except (ValueError, TypeError):
                    final_ratio_str = str(initial_ratio)

        if final_payment_type:
            try:
                # ratio 可为空（仅金额无比例的节点也合法，如"38100元 进场前首付"）
                ratio_str = str(final_ratio_str).strip() if final_ratio_str else ""
                if ratio_str and ratio_str.lower() not in ('none', 'null'):
                    ratio_val = float(ratio_str.replace('%', '')) / 100.0
                else:
                    ratio_val = None

                # M7：集中化归一化——将中文分类映射到英文Literal值
                clause_class_list = initial_item.get('clause_class', [])
                raw_category = clause_class_list[0] if clause_class_list else None
                canonical = normalize_clause_class(raw_category) if raw_category else None
                clause_category = "installation_payment" if canonical == "installation_payment" else "equipment_payment"

                # 安装侧 payment_type 白名单兜底：LLM 可能透传设备节点（如"预付款"），强制映射为安装合法节点
                if clause_category == "installation_payment":
                    _normalized_pt, _pt_action = enforce_install_payment_type(final_payment_type)
                    if _pt_action == "mapped":
                        logger.info(
                            f"{_NODE_TAG} 安装侧 payment_type 强制映射: "
                            f"{final_payment_type} → {_normalized_pt} (item_id={item_id})"
                        )
                        final_payment_type = _normalized_pt
                    elif _pt_action == "dropped":
                        logger.warning(
                            f"{_NODE_TAG} 安装侧 payment_type 非法已丢弃: "
                            f"'{final_payment_type}' (item_id={item_id})"
                        )
                        continue
                    elif _pt_action == "missing":
                        # H4 修复：payment_type 字段缺失但条款本体有效；保留节点不丢弃
                        logger.warning(
                            f"{_NODE_TAG} 安装侧 payment_type 字段缺失，保留节点 "
                            f"(item_id={item_id}, clause={safe_clause(initial_item.get('payment_clause'))})"
                        )

                # 硬规则兜底：辅助费目（保养费/指导费等）+ 无显式比例 → 强制清空 ratio
                final_ratio_val: Optional[float] = round(ratio_val, 4) if ratio_val is not None else None
                if _should_strip_ratio(initial_item.get('payment_clause', '')):
                    logger.info(
                        f"{_NODE_TAG} 复核阶段命中辅助费目硬规则，强制清空 ratio "
                        f"（item_id={item_id}, 原值={final_ratio_val}）"
                    )
                    final_ratio_val = None

                # 金额优先级：summary LLM 的 final_amount（已合并/归并） > 初步抽取的 payment_amount
                # 例如：per-clause 仅识别到 "调验费5000元"，summary 综合识别到 "5000+3000=8000"。
                # 但 summary LLM 可能从上下文借金额造成幻觉（例如把合同主体的 1,572,000 元
                # 回填到原文未写金额的"质保金/竣工款"等节点）。因此覆盖前必须校验：
                # final_amount 的数字串必须能在 payment_clause 原文中匹配到，否则保持 initial 值。
                _initial_amount = initial_item.get('payment_amount')
                _summary_amount_str = (
                    str(final_amount_from_summary).strip()
                    if final_amount_from_summary is not None
                    else ""
                )
                if _summary_amount_str and _summary_amount_str.lower() not in ("null", "none", "0", "0.0"):
                    _clause_text_for_amount = str(initial_item.get('payment_clause', '') or '')
                    if _amount_appears_in_text(_summary_amount_str, _clause_text_for_amount):
                        final_amount_val = _summary_amount_str
                        if str(_initial_amount or "").strip() != _summary_amount_str:
                            logger.info(
                                f"{_NODE_TAG} 复核阶段以 summary final_amount 覆盖 initial payment_amount "
                                f"(item_id={item_id}, initial={_initial_amount} → summary={_summary_amount_str})"
                            )
                    else:
                        # summary 给出的 amount 在原文中找不到对应数字，视为幻觉，拒绝覆盖
                        logger.warning(
                            f"{_NODE_TAG} 复核阶段拒绝覆盖：summary final_amount={_summary_amount_str} "
                            f"未在条款原文出现，保持 initial={_initial_amount} "
                            f"(item_id={item_id}, clause={safe_clause(_clause_text_for_amount, head=50)})"
                        )
                        final_amount_val = _initial_amount
                else:
                    final_amount_val = _initial_amount

                info = PaymentInfo(
                    clause_category=clause_category,
                    payment_clause=initial_item['payment_clause'],
                    payment_context=initial_item.get('clause_context', ''),
                    payment_type=final_payment_type,
                    payment_ratio=final_ratio_val,
                    payment_amount=final_amount_val,
                    image_url=None,
                    first_page_image_url=None,
                )
                final_payment_infos.append(info)
                logger.success(f"成功创建复核后的PaymentInfo: 分类='{info.clause_category}', 类型='{info.payment_type}', 比例={info.payment_ratio}, 金额={info.payment_amount}")
            except (ValueError, TypeError) as e:
                logger.error(f"解析复核后的比例失败: '{final_ratio_str}' for item_id '{item_id}'. Error: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"创建PaymentInfo对象时发生错误: {e} for item_id '{item_id}'. initial_item keys: {list(initial_item.keys()) if isinstance(initial_item, dict) else 'N/A'}", exc_info=True)
        else:
            logger.warning(f"复核结果中缺少必要信息: payment_type='{final_payment_type}', final_ratio='{final_ratio_str}' for item_id '{item_id}'")

    # 方向1：未匹配条款恢复——检查映射表中未被 processed_ids 处理的条款，用原始数据兜底
    #        排除被去重有意移除的 ID，防止去重结果被恢复逻辑撤销
    unprocessed_keys = set(summary_key_to_initial_item.keys()) - processed_ids - dedup_removed_ids
    if unprocessed_keys:
        logger.warning(f"发现 {len(unprocessed_keys)} 个条款未被 LLM 复核结果匹配到，尝试用初步提取结果恢复: {unprocessed_keys}")
        for key in unprocessed_keys:
            item = summary_key_to_initial_item[key]
            orig_ratio = item.get('payment_ratio')
            orig_type = item.get('payment_type')
            orig_clause = item.get('payment_clause', '')
            
            if not orig_type or orig_ratio is None:
                logger.warning(f"  跳过恢复 key={key}：原始数据缺少 payment_type 或 payment_ratio")
                continue
            
            # 跳过 0 元条款（LLM 有意丢弃的无效条款）
            orig_amount_str = str(item.get('payment_amount', '') or '')
            orig_amount_clean = orig_amount_str.replace('元', '').replace(',', '').strip()
            is_zero_amount = orig_amount_clean in ('0', '0.0', '0.00', '')
            is_zero_in_clause = any(z in orig_clause for z in ('零元', 'RMB0', 'RMB 0', '0元'))
            if is_zero_amount and is_zero_in_clause:
                logger.info(f"  跳过恢复 key={key}：0 元条款，LLM 已有意丢弃")
                continue
            
            # M7：集中化归一化——从 clause_class 推导 clause_category
            clause_class_list = item.get('clause_class', [])
            raw_cat = clause_class_list[0] if clause_class_list else ''
            canonical = normalize_clause_class(raw_cat) if raw_cat else None
            clause_category = "installation_payment" if canonical == "installation_payment" else "equipment_payment"

            # 安装侧 payment_type 白名单兜底（与批量复核回溯保持一致）
            if clause_category == "installation_payment":
                _normalized_pt, _pt_action = enforce_install_payment_type(orig_type)
                if _pt_action == "mapped":
                    logger.info(
                        f"{_NODE_TAG} 恢复分支安装侧 payment_type 强制映射: "
                        f"{orig_type} → {_normalized_pt} (key={key})"
                    )
                    orig_type = _normalized_pt
                elif _pt_action == "dropped":
                    logger.warning(
                        f"{_NODE_TAG} 跳过恢复 key={key}：安装侧 payment_type 非法 '{orig_type}'"
                    )
                    continue
                elif _pt_action == "missing":
                    # H4 修复：恢复路径同样区分 missing 与 dropped；缺字段不丢节点
                    logger.warning(
                        f"{_NODE_TAG} 恢复分支安装侧 payment_type 缺失，保留节点 (key={key})"
                    )

            # 跳过同 category 下已存在相同 payment_type 的条款（LLM 已有意去重）
            existing_types = {(info.clause_category, info.payment_type) for info in final_payment_infos}
            if (clause_category, orig_type) in existing_types:
                logger.info(f"  跳过恢复 key={key}：同类别下已存在 payment_type='{orig_type}'，LLM 已有意去重")
                continue

            # I7：仅当"同类别 + 同 payment_type + 相似条款文本"三者同时满足时才跳过恢复。
            # 混签场景下不同子节点会共用整段 payment_clause（相似度 ≈ 1.0），
            # 必须叠加 payment_type 维度才能避免误杀。LLM 主动改写场景通常保留原 payment_type，
            # 故此判定仍可挡住"原节点被改写后再被恢复分支塞回"的回流。
            already_present = False
            for info in final_payment_infos:
                if info.clause_category != clause_category:
                    continue
                if (info.payment_type or "") != (orig_type or ""):
                    continue
                existing_clause = info.payment_clause or ""
                if not existing_clause or not orig_clause:
                    continue
                if existing_clause == orig_clause:
                    already_present = True
                    break
                if existing_clause in orig_clause or orig_clause in existing_clause:
                    already_present = True
                    break
                if _calculate_similarity(existing_clause, orig_clause) >= 0.85:
                    already_present = True
                    break
            if already_present:
                logger.info(
                    f"  跳过恢复 key={key}：同类别 + 同 payment_type='{orig_type}' + 相似条款文本，LLM 已主动剔除/改写"
                )
                continue
            
            try:
                info = PaymentInfo(
                    clause_category=clause_category,
                    payment_clause=orig_clause,
                    payment_context=item.get('clause_context', ''),
                    payment_type=orig_type,
                    payment_ratio=round(float(orig_ratio), 4) if orig_ratio is not None else None,
                    payment_amount=item.get('payment_amount'),
                    image_url=None,
                    first_page_image_url=None,
                )
                final_payment_infos.append(info)
                logger.warning(
                    f"  恢复条款 key={key}: 类型='{orig_type}', 比例={orig_ratio}, "
                    f"条款={safe_clause(orig_clause, head=60)}"
                )
            except Exception as e:
                logger.error(f"  恢复条款 key={key} 时创建 PaymentInfo 失败: {e}")

    # 复核结果后处理：算术校核（amount == 上下文唯一总价 → 100%）+ 零节点清理（ratio==0 且 amount==0 → 删除）
    final_payment_infos = _postprocess_final_payment_infos(final_payment_infos)

    # H8：思考过程仅通过返回值透传，不再直接 mutate state
    if thinking_info:
        logger.success(f"{_NODE_TAG} 成功提取思考过程信息: {len(thinking_info.thinking_output)} 字符")
    else:
        logger.warning(f"{_NODE_TAG} 未能提取到思考过程信息")

    logger.success(f"{_NODE_TAG} 支付信息提取完成，共提取 {len(final_payment_infos)} 个支付条款")

    # H3 修复：记录 hash 桶命中率，便于评估优化效果（命中率高 → 全量遍历调用次数显著下降）
    _fb_total = _fallback_hits + _fallback_misses
    if _fb_total > 0:
        logger.debug(
            f"{_NODE_TAG} 复核回填 hash 桶命中率: {_fallback_hits}/{_fb_total} "
            f"({_fallback_hits / _fb_total:.1%})，桶 miss 时退化为全量遍历"
        )

    await DebugHelper.save_snapshot(
        doc_id=state['document_id'], step_name="summary-llm_stage2",
        content={"payment_infos": final_payment_infos, "warranty_info": final_warranty_info,"thinking_info": final_thinking_info},
        is_debug_enabled = is_debug_mode
    )
    return {
        "payment_infos": final_payment_infos,
        "warranty_info": final_warranty_info,
        "thinking_info": final_thinking_info,
        "current_step": "payment_info_success",
        "paragraphs": all_paragraphs
    }