# app/utils/rag_retriever.py

import os
import asyncio
import random
from typing import List, Dict, Any, Optional

import httpx
import jieba
import numpy as np
import torch
from loguru import logger

from app.config.config import APP_CONFIG, get_rag_models, get_milvus_client, get_bm25_data, get_bm25_data_installation
from app.config.env_config import get_rag_thresholds


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except Exception:
        return default


_EMBED_CONCURRENCY = _env_int("EMBED_CONCURRENCY", 4)
_EMBED_MAX_RETRIES = _env_int("EMBED_MAX_RETRIES", 3)

# 模块级共享 httpx 客户端 + per-loop semaphore（参考 concurrency.py 的思路）
import weakref
_embed_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = weakref.WeakKeyDictionary()
_embed_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _get_embed_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _embed_clients.get(loop)
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        _embed_clients[loop] = client
    return client


def _get_embed_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _embed_sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_EMBED_CONCURRENCY)
        _embed_sems[loop] = sem
    return sem


async def _post_embedding_once(client: httpx.AsyncClient, url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Optional[List[List[float]]]:
    resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code == 200:
        return [item.get("embedding", []) for item in resp.json().get("data", [])]
    if resp.status_code in (429, 500, 502, 503, 504):
        return None  # 触发退避重试
    logger.error(f"远程嵌入 API 失败 status={resp.status_code} body={resp.text[:200]}")
    raise RuntimeError(f"embedding api {resp.status_code}")


async def _embed_batch(batch_texts: List[str], target_dim: int, url: str, model_name: str, api_key: Optional[str]) -> np.ndarray:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model_name, "input": batch_texts}

    client = _get_embed_client()
    sem = _get_embed_semaphore()

    embeddings: Optional[List[List[float]]] = None
    last_exc: Optional[BaseException] = None
    async with sem:
        for attempt in range(_EMBED_MAX_RETRIES):
            try:
                embeddings = await _post_embedding_once(client, url, headers, payload)
                if embeddings is not None:
                    break
            except Exception as e:  # 网络/序列化等
                last_exc = e
                logger.warning(f"远程嵌入请求异常（attempt={attempt + 1}）: {e}")
            backoff = 0.5 * (2 ** attempt) + random.uniform(0, 0.25)
            await asyncio.sleep(backoff)

    if embeddings is None:
        if last_exc is not None:
            logger.error(f"远程嵌入最终失败，使用零向量降级: {last_exc}")
        return np.zeros((len(batch_texts), target_dim), dtype=np.float32)

    remote_dim = len(embeddings[0]) if embeddings and embeddings[0] else target_dim
    if len(embeddings) != len(batch_texts) or any(len(e) != remote_dim for e in embeddings):
        logger.warning("远程嵌入返回数量或维度异常，使用零向量填充。")
        arr = np.array([e if len(e) == remote_dim else [0.0] * remote_dim for e in embeddings], dtype=np.float32)
        if arr.shape[0] < len(batch_texts):
            pad = np.zeros((len(batch_texts) - arr.shape[0], remote_dim), dtype=np.float32)
            arr = np.vstack([arr, pad])
    else:
        arr = np.array(embeddings, dtype=np.float32)

    # L2 归一化
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    arr = arr / norms

    # 调整维度
    if arr.shape[1] != target_dim:
        if arr.shape[1] > target_dim:
            arr = arr[:, :target_dim]
        else:
            pad = np.zeros((arr.shape[0], target_dim - arr.shape[1]), dtype=np.float32)
            arr = np.hstack([arr, pad])
    return arr


async def _get_embedding_async(texts: List[str]) -> np.ndarray:
    """异步获取文本嵌入向量（httpx + 退避 + 并发）。"""
    config = APP_CONFIG.text_processing.rag
    url = config.remote_embedding_api_url
    model_name = config.remote_embedding_model_name
    api_key = config.remote_embedding_api_key
    target_dim = config.embedding_dimension
    logger.debug("嵌入模型模型名为: {}", model_name)

    if not url or not model_name:
        logger.error("远程嵌入 API 的 URL 或模型名称未配置，无法生成向量。")
        return np.zeros((len(texts), target_dim), dtype=np.float32)

    if not texts:
        return np.zeros((0, target_dim), dtype=np.float32)

    batch_size = 16
    batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]
    arrs = await asyncio.gather(
        *[_embed_batch(b, target_dim, url, model_name, api_key) for b in batches]
    )
    return np.concatenate(arrs, axis=0) if arrs else np.zeros((0, target_dim), dtype=np.float32)

async def retrieve_payment_type(text: str, clause_class: list) -> Dict[str, Any]:
    """【重构版】异步RAG检索，获取支付类型。"""
    config = APP_CONFIG.text_processing.rag
    thresholds = get_rag_thresholds()
    bm25_min = thresholds["bm25_min_score"]
    vector_min = thresholds["vector_min_distance"]
    results = {"bm25_results": [], "vector_results": [], "rerank_results": [], "final_results": []}

    if 'installation_payment' in clause_class:
        collection_name = config.collection_name_installation
        output_fields = config.output_fields_installation
    else:
        collection_name = config.collection_name
        output_fields = config.output_fields
    # --- 并发执行 BM25 和 Vector 召回 ---
    async def _bm25_recall():
        if 'installation_payment' in clause_class:
            bm25, _, entities = await get_bm25_data_installation()
        else:
            bm25, _, entities = await get_bm25_data()
        if bm25 is None or entities is None:
            return []
        tokenized_query = await asyncio.to_thread(list, jieba.cut(text))
        scores = await asyncio.to_thread(bm25.get_scores, tokenized_query)
        top_indices = np.argsort(scores)[::-1][:config.top_k]

        recalled = []
        for idx in top_indices:
            try:
                if scores[idx] >= bm25_min:
                    item = entities[idx]
                    normalized = {
                        "payment_type": item.get("payment_type") or item.get("label"),
                        "clause_text": item.get("clause_text") or item.get("text"),
                        "payment_ratio": item.get("payment_ratio"),
                        # "clause_context": item.get("clause_context") or item.get("context"),
                        # "bm25_score": float(scores[idx]),
                        # "source": "bm25",
                    }
                    normalized["label"] = normalized.get("payment_type")
                    normalized["text"] = normalized.get("clause_text")
                    recalled.append(normalized)
            except Exception as e:
                logger.debug(f"BM25召回项处理失败: {e}")
                continue
        return recalled

    async def _vector_recall():
        client = await get_milvus_client()
        if client is None:
            logger.warning("Milvus客户端不可用，跳过向量检索。")
            return []
        
        # 确保 Collection 已加载到内存
        def _ensure_collection_loaded():
            try:
                client.load_collection(collection_name=collection_name)
                logger.debug(f"Milvus Collection '{collection_name}' 已加载")
            except Exception as load_err:
                # Collection 可能已经加载或不需要显式加载，忽略错误
                logger.debug(f"Collection 加载检查（可忽略）: {load_err}")
        
        try:
            await asyncio.to_thread(_ensure_collection_loaded)
        except Exception as e:
            logger.warning(f"尝试加载 Collection 时出现异常: {e}，继续尝试搜索...")
        
        query_vector = await _get_embedding_async([text])
        query_list = [query_vector[0].tolist()] if isinstance(query_vector, np.ndarray) and query_vector.ndim == 2 else [query_vector]
        search_params = {"metric_type": config.search_metric_type} if getattr(config, "search_metric_type", None) else None
        search_kwargs: Dict[str, Any] = {
            "collection_name": collection_name,
            "data": query_list,
            "limit": config.top_k,
            "output_fields": output_fields,
        }
        if search_params:
            search_kwargs["search_params"] = search_params
        try:
            search_res = await asyncio.to_thread(client.search, **search_kwargs)
        except Exception as e:
            logger.error(f"Milvus search 调用失败: {e}")
            return []

        recalled = []
        for hit in search_res[0]:
            try:
                distance_val = hit.get("distance", 0.0)
                if isinstance(distance_val, (int, float)) and distance_val >= vector_min:
                    entity = hit.get("entity", {})
                    normalized = {
                        "payment_type": entity.get("payment_type") or entity.get("label"),
                        "clause_text": entity.get("clause_text") or entity.get("text"),
                        "payment_ratio": entity.get("payment_ratio"),
                        "payment_amount": entity.get("payment_amount"),
                        # "clause_context": entity.get("clause_context") or entity.get("context"),
                        # "vector_distance": float(distance_val),
                        # "source": "vector",
                    }
                    normalized["label"] = normalized.get("payment_type")
                    normalized["text"] = normalized.get("clause_text")
                    recalled.append(normalized)
            except Exception as e:
                logger.debug(f"向量召回项处理失败: {e}")
                continue
        return recalled

    bm25_res, vector_res = await asyncio.gather(_bm25_recall(), _vector_recall())
    results["bm25_results"] = bm25_res
    results["vector_results"] = vector_res

    # --- 融合与Rerank ---
    candidates = []
    seen = set()
    for item in vector_res:
        text_val = item.get("text") or item.get("clause_text") or ""
        if text_val and text_val not in seen:
            candidates.append(item)
            seen.add(text_val)
    for item in bm25_res:
        text_val = item.get("text") or item.get("clause_text") or ""
        if text_val and text_val not in seen:
            candidates.append(item)
            seen.add(text_val)

    if config.use_rerank and len(candidates) > 1:
        _, _, rerank_model, rerank_tokenizer = await get_rag_models()
        if rerank_model is None or rerank_tokenizer is None:
            logger.warning("Rerank 模型不可用，跳过 rerank 阶段。")
            results["final_results"] = candidates[: config.top_k]
            return results
        pairs_a = [text] * len(candidates)
        pairs_b = [c.get("text") or c.get("clause_text", "") for c in candidates]

        def _rerank():
            with torch.no_grad():
                inputs = rerank_tokenizer(pairs_a, pairs_b, padding=True, truncation=True, return_tensors="pt")
                scores = rerank_model(**inputs).logits.squeeze(-1).cpu().numpy()
            return scores

        rerank_scores = await asyncio.to_thread(_rerank)
        sorted_indices = np.argsort(rerank_scores)[::-1]

        reranked_results = []
        for i in sorted_indices:
            candidates[i]["rerank_score"] = float(rerank_scores[i])
            reranked_results.append(candidates[i])

        results["rerank_results"] = reranked_results
        results["final_results"] = reranked_results[:config.top_k]
    else:
        results["final_results"] = candidates[:config.top_k]

    return results