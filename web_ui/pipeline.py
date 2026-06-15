"""pipeline.py — Service 1 / Service 2 调用编排
=================================================
职责：
- 复用 `clause_classify_client.py` 的 `ClauseClassifierClient` 与 `read_and_split_md`
- 对 Service 1 做分块并发 extract + filter + get_context，聚合出 paragraphs
- 按用户选择的合同类型覆盖 paragraphs 的 clause_class
- 调用远程 Service 2 /extract_payment_info，返回结构化结果

由于 Service 1 脚本所在目录含中文「service1相关脚本」，采用 importlib 按文件路径加载，
避免 package 命名限制。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

import requests

import config

logger = logging.getLogger(__name__)


class CancelledError(Exception):
    """流水线被客户端取消时抛出的内部信号。"""


# ===== 动态加载 clause_classify_client 模块 =====
def _load_classifier_module():
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.normpath(
        os.path.join(here, "..", "service1相关脚本", "clause_classify_client.py")
    )
    if not os.path.isfile(candidate):
        raise FileNotFoundError(f"未找到 clause_classify_client.py: {candidate}")

    spec = importlib.util.spec_from_file_location(
        "web_ui_clause_classify_client", candidate
    )
    module = importlib.util.module_from_spec(spec)
    # 注册到 sys.modules，避免重复加载产生多实例
    sys.modules["web_ui_clause_classify_client"] = module
    spec.loader.exec_module(module)
    return module


_classifier_mod = _load_classifier_module()
ClauseClassifierClient = _classifier_mod.ClauseClassifierClient
read_and_split_md = _classifier_mod.read_and_split_md


class PipelineError(Exception):
    """Pipeline 阶段失败的统一异常"""

    def __init__(self, stage: str, message: str, detail: Optional[Any] = None):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message
        self.detail = detail


# ============================================================
# Step 1: Service 1 分类 + 上下文
# ============================================================
def _fetch_context_for_items(client: ClauseClassifierClient,
                              items: List[Dict[str, Any]],
                              full_doc_text: str,
                              max_chars: int,
                              cancelled: Optional[threading.Event] = None) -> None:
    """为单个 chunk 的 filtered items 并发获取上下文，就地修改。

    注意：传入的是整份文档的全文（full_doc_text），而非当前 chunk，
    以便 Service 1 在更完整的语境中定位条款上下文。
    """
    if not items:
        return

    def _one(item):
        if cancelled is not None and cancelled.is_set():
            return item
        text = item.get("text", "")
        try:
            ctx = client.get_context(text, full_doc_text, max_chars=max_chars)
            item["context"] = {
                "context_before": ctx.get("context_before", ""),
                "context_after": ctx.get("context_after", ""),
                "full_context": ctx.get("full_context", ""),
            }
        except Exception as e:
            item["context"] = {"error": str(e), "full_context": text}
        return item

    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        list(ex.map(_one, items))


def _process_single_chunk(client: ClauseClassifierClient,
                           chunk_text: str,
                           full_doc_text: str,
                           chunk_idx: int,
                           total_chunks: int,
                           task_id: str,
                           max_chars: int,
                           cancelled: Optional[threading.Event] = None) -> Dict[str, Any]:
    """单个 chunk：extract → filter → 并发 get_context（上下文基于整份文档）"""
    if cancelled is not None and cancelled.is_set():
        raise CancelledError()

    raw = client.extract(chunk_text, task_id=f"{task_id}-chunk{chunk_idx}",
                         llm_timeout=config.LLM_TIMEOUT)
    if raw.get("status") != "success":
        err = raw.get("error", {})
        return {
            "chunk_index": chunk_idx,
            "total_chunks": total_chunks,
            "status": raw.get("status", "error"),
            "error": err,
            "items_with_context": [],
        }

    filtered = client.filter_categories(raw)
    grouped = filtered.get("grouped_result", {})

    items: List[Dict[str, Any]] = []
    for category in ("混签付款条款", "质保期条款"):
        for it in grouped.get(category, []):
            text = (it.get("text") or "").strip()
            if not text or text in ("...", "    ..."):
                continue
            items.append(it)

    # 收集合同总价条款（raw class = contract_price）
    contract_price_items: List[Dict[str, Any]] = []
    seen_price_text: set = set()
    for line in raw.get("lines", []):
        classes = line.get("clause_class") or []
        if config.CONTRACT_PRICE_RAW_CLASS not in classes:
            continue
        text = (line.get("text") or "").strip()
        if not text or text in ("...", "    ...") or text in seen_price_text:
            continue
        seen_price_text.add(text)
        contract_price_items.append({
            "text": text,
            "original_classes": list(classes),
            "confidence": (line.get("metadata") or {}).get("confidence", 0.0),
            "beacon_ids": line.get("beacon_ids") or [],
        })

    if cancelled is not None and cancelled.is_set():
        raise CancelledError()

    _fetch_context_for_items(client, items, full_doc_text, max_chars, cancelled=cancelled)
    _fetch_context_for_items(client, contract_price_items, full_doc_text, max_chars, cancelled=cancelled)

    # 保留该 chunk 所有原始条款（含非目标类别），用于前端"全部展开"
    raw_lines: List[Dict[str, Any]] = []
    for line in raw.get("lines", []):
        text = (line.get("text") or "").strip()
        if not text or text in ("...", "    ..."):
            continue
        raw_lines.append({
            "text": text,
            "clause_class": line.get("clause_class") or [],
            "confidence": (line.get("metadata") or {}).get("confidence", 0.0),
            "beacon_ids": line.get("beacon_ids") or [],
            "chunk_index": chunk_idx,
        })

    return {
        "chunk_index": chunk_idx,
        "total_chunks": total_chunks,
        "status": "success",
        "items_with_context": items,
        "contract_price_items": contract_price_items,
        "raw_lines": raw_lines,
    }


def run_step1(md_bytes: bytes, task_id: str,
              lines_per_chunk: Optional[int] = None,
              max_chars: Optional[int] = None,
              on_progress: Optional[Callable[[int, int], None]] = None,
              cancelled: Optional[threading.Event] = None) -> Dict[str, Any]:
    """
    执行 Service 1 完整链路，返回：
        {
          "paragraphs": [ ... ],   # 仅付款/质保期（供 Service 2）
          "all_clauses": [ ... ],  # 全部分类结果（供前端展开查看）
        }

    每个 paragraph: {clause_class, original_classes, clause_context, clause, confidence}
    每个 all_clause: {text, clause_class, confidence, chunk_index}

    流程：临时落盘 → read_and_split_md → 并发 _process_single_chunk → 聚合去重

    Args:
        lines_per_chunk: 每份行数阈值；None 则使用 config.LINES_PER_CHUNK
        max_chars: 上下文最大字符数；None 则使用 config.MAX_CONTEXT_CHARS
        cancelled: 客户端断开时由调用方 set()，pipeline 会尽快抛出 CancelledError
    """
    effective_lines = int(lines_per_chunk) if lines_per_chunk else config.LINES_PER_CHUNK
    effective_max_chars = int(max_chars) if max_chars else config.MAX_CONTEXT_CHARS

    # 1) 临时落盘（read_and_split_md 需要文件路径）
    tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".md", delete=False)
    try:
        tmp.write(md_bytes)
        tmp.flush()
        tmp.close()
        try:
            chunks = read_and_split_md(tmp.name, effective_lines)
        except Exception as e:
            raise PipelineError("step1", f"分割 MD 失败: {e}")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    total_chunks = len(chunks)
    # 报告初始进度（含总分块数）
    if on_progress:
        try:
            on_progress(0, total_chunks)
        except Exception:
            pass

    if not chunks:
        return {"paragraphs": [], "all_clauses": [], "contract_price": None}

    # 整份文档文本：用于 Service 1 上下文查询，避免被分块截断
    # 使用 utf-8-sig 以兼容可能存在的 BOM；解析失败时回退为各 chunk 拼接
    try:
        full_doc_text = md_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        full_doc_text = "\n".join(chunks)

    client = ClauseClassifierClient(
        base_url=config.SERVICE1_CONFIG["base_url"],
        timeout=config.SERVICE1_CONFIG["timeout"],
    )

    # 2) 并发处理各 chunk
    chunk_results: List[Dict[str, Any]] = []
    errors: List[str] = []
    done_count = 0
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        future_map = {
            ex.submit(_process_single_chunk, client, chunk, full_doc_text, idx,
                      total_chunks, task_id, effective_max_chars, cancelled): idx
            for idx, chunk in enumerate(chunks, 1)
        }
        for fut in as_completed(future_map):
            idx = future_map[fut]
            try:
                chunk_results.append(fut.result())
            except CancelledError:
                # 客户端已断开，停止收集后续结果
                logger.info("run_step1 cancelled by client at chunk%s", idx)
                break
            except Exception as e:
                errors.append(f"chunk{idx}: {e}")
            done_count += 1
            if on_progress:
                try:
                    on_progress(done_count, total_chunks)
                except Exception:
                    pass

    if cancelled is not None and cancelled.is_set():
        raise CancelledError()

    if errors and not chunk_results:
        raise PipelineError("step1", "所有分块均处理失败", detail=errors)

    # 3) 聚合 + 去重（以 clause 文本为 key）
    paragraphs: List[Dict[str, Any]] = []
    seen_para: set = set()
    all_clauses: List[Dict[str, Any]] = []
    seen_all: set = set()
    contract_price_items_all: List[Dict[str, Any]] = []
    seen_price: set = set()

    # 保持按 chunk_index 排序便于阅读
    chunk_results.sort(key=lambda r: r.get("chunk_index", 0))
    for cr in chunk_results:
        # 3.1 过滤后的 paragraphs（供 Service 2 + 前端 Step 1 主表）
        for item in cr.get("items_with_context", []):
            text = (item.get("text") or "").strip()
            if not text or text in seen_para:
                continue
            seen_para.add(text)
            mapped = item.get("mapped_class") or ""
            original = item.get("original_classes") or []
            ctx = item.get("context", {}) or {}
            full_ctx = ctx.get("full_context") or text
            paragraphs.append({
                "clause_class": [mapped] if mapped else [],
                "original_classes": list(original),
                "clause_context": full_ctx,
                "clause": text,
                "confidence": item.get("confidence", 0.0),
            })

        # 3.2 全部分类结果（供前端展开）
        for line in cr.get("raw_lines", []):
            text = line.get("text", "")
            if not text or text in seen_all:
                continue
            seen_all.add(text)
            all_clauses.append(line)

        # 3.3 合同总价条款聚合
        for item in cr.get("contract_price_items", []):
            text = (item.get("text") or "").strip()
            if not text or text in seen_price:
                continue
            seen_price.add(text)
            ctx = item.get("context", {}) or {}
            full_ctx = ctx.get("full_context") or text
            contract_price_items_all.append({
                "clause": text,
                "context": full_ctx,
                "original_classes": list(item.get("original_classes") or []),
                "confidence": item.get("confidence", 0.0),
            })

    contract_price: Optional[Dict[str, Any]] = None
    if contract_price_items_all:
        # 多条按出现顺序合并为单段（用 \n 连接）
        clauses = [it["clause"] for it in contract_price_items_all]
        contexts: List[str] = []
        seen_ctx: set = set()
        for it in contract_price_items_all:
            c = it.get("context") or ""
            if c and c not in seen_ctx:
                seen_ctx.add(c)
                contexts.append(c)
        contract_price = {
            "clause": "\n".join(clauses),
            "context": "\n".join(contexts) if contexts else None,
            "items": contract_price_items_all,
        }

    return {
        "paragraphs": paragraphs,
        "all_clauses": all_clauses,
        "contract_price": contract_price,
    }


# ============================================================
# 合同类型覆盖
# ============================================================
def apply_contract_type(paragraphs: List[Dict[str, Any]],
                        contract_type: str) -> List[Dict[str, Any]]:
    """按用户选择覆盖付款类 clause_class；质保期保留。"""
    target = config.PAYMENT_CLASS_MAP.get(contract_type)
    if not target:
        raise PipelineError(
            "override",
            f"未知合同类型: {contract_type}（允许: {list(config.PAYMENT_CLASS_MAP)}）",
        )

    for p in paragraphs:
        cls = p.get("clause_class") or []
        # 质保期条款不覆盖
        if any(c in config.WARRANTY_MAPPED_ALIASES for c in cls):
            continue
        p["clause_class"] = [target]
    return paragraphs


# ============================================================
# Step 2: Service 2 远程调用
# ============================================================
def run_step2(paragraphs: List[Dict[str, Any]], task_id: str,
              operation_type: str = "extract",
              sis_payment_stages: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """调用远程 Service 2 /extract_payment_info，返回解析后的 JSON。

    Args:
        paragraphs: 合同条款段落列表
        task_id: 任务 ID
        operation_type: 操作类型，"extract"(仅提取) 或 "analyze"(提取并比对)
        sis_payment_stages: 标准答案列表，仅 operation_type="analyze" 时需要
    """
    cfg = config.SERVICE2_CONFIG
    url = f"{cfg['base_url'].rstrip('/')}{cfg['endpoint']}"

    payload = {
        "id": task_id,
        "paragraphs": paragraphs,
        "operation_type": operation_type,
    }
    if operation_type == "analyze" and sis_payment_stages:
        payload["sis_payment_stages"] = sis_payment_stages
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    start = time.time()
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=cfg["timeout"])
    except requests.exceptions.Timeout:
        raise PipelineError("step2", f"Service 2 请求超时 ({cfg['timeout']}s)")
    except requests.exceptions.ConnectionError as e:
        raise PipelineError("step2", f"Service 2 连接失败: {e}")
    except Exception as e:
        raise PipelineError("step2", f"Service 2 请求异常: {e}")

    elapsed = round(time.time() - start, 2)

    if resp.status_code != 200:
        raise PipelineError(
            "step2",
            f"Service 2 HTTP {resp.status_code}",
            detail=resp.text[:500],
        )

    try:
        result = resp.json()
    except Exception as e:
        raise PipelineError("step2", f"Service 2 响应 JSON 解析失败: {e}",
                            detail=resp.text[:500])

    result["_elapsed_seconds"] = elapsed
    return result


# ============================================================
# 合同总金额抽取与比对（Service 2 /compare_contract_price）
# ============================================================
def run_compare_contract_price(clause: str,
                               context: Optional[str],
                               task_id: str,
                               sis_contract_price: Optional[float] = None
                               ) -> Dict[str, Any]:
    """调用 Service 2 /compare_contract_price 接口。

    Args:
        clause: 合同总价条款原文（多条已合并为单段）
        context: 上下文文本，可为 None 或空串
        task_id: 数据标识，需匹配 [A-Za-z0-9_-]{1,64}
        sis_contract_price: SIS 合同金额；None 时仅抽取不比对
    """
    cfg = config.SERVICE2_CONFIG
    url = f"{cfg['base_url'].rstrip('/')}{config.SERVICE2_PRICE_ENDPOINT}"

    payload: Dict[str, Any] = {
        "id": task_id,
        "contract_price_clause": clause,
        "contract_price_clause_context": context if context else None,
    }
    if sis_contract_price is not None:
        payload["sis_contract_price"] = float(sis_contract_price)

    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    start = time.time()
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=cfg["timeout"])
    except requests.exceptions.Timeout:
        raise PipelineError("contract_price",
                            f"Service 2 /compare_contract_price 请求超时 ({cfg['timeout']}s)")
    except requests.exceptions.ConnectionError as e:
        raise PipelineError("contract_price",
                            f"Service 2 /compare_contract_price 连接失败: {e}")
    except Exception as e:
        raise PipelineError("contract_price",
                            f"Service 2 /compare_contract_price 请求异常: {e}")

    elapsed = round(time.time() - start, 2)

    if resp.status_code != 200:
        raise PipelineError(
            "contract_price",
            f"Service 2 /compare_contract_price HTTP {resp.status_code}",
            detail=resp.text[:500],
        )

    try:
        result = resp.json()
    except Exception as e:
        raise PipelineError("contract_price",
                            f"Service 2 响应 JSON 解析失败: {e}",
                            detail=resp.text[:500])

    result["_elapsed_seconds"] = elapsed
    return result
