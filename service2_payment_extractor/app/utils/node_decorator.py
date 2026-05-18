# app/utils/node_decorator.py

import functools
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from langchain_core.runnables import RunnableConfig
from loguru import logger as global_logger

try:
    from langgraph.config import get_stream_writer
except ImportError:
    try:
        from langgraph.utils import get_stream_writer
    except ImportError:
        def get_stream_writer():
            return None

from app.states.states import State

def get_logger(name: str):
    return global_logger.bind(name=name)

def _emit_event(
    event_type: str,
    data: Dict[str, Any],
    config: Optional[RunnableConfig] = None,
):
    """一个统一的事件发送辅助函数"""
    try:
        writer = get_stream_writer(config)
        if writer:
            writer.put({event_type: data})
            get_logger("event_emitter").trace(f"Emitted event '{event_type}': {data}")
    except Exception as e:
        get_logger("event_emitter").trace(f"Could not get stream writer: {e}")

def node_with_progress(
    node_name: str,
    display_name: Optional[str] = None,
    track_state_keys: Optional[List[str]] = None,
    progress_range: Optional[tuple[int, int]] = None,
):
    """
    【演进版】装饰器，为节点添加统一的进度跟踪、日志和错误处理。
    """
    def decorator(func: Callable) -> Callable:
        
        @functools.wraps(func)
        async def wrapper(state: State, config: RunnableConfig, **kwargs) -> Dict[str, Any]:
            logger = get_logger("workflow")
            display = display_name or node_name
            
            node_execution_id = f"node_exec_{uuid.uuid4().hex[:8]}"
            wrapper._current_config = config

            start_progress_val = progress_range[0] if progress_range else None
            _emit_event(
                "progress",
                {
                    "node": node_name, "display_name": display, "status": "start", 
                    "message": f"🚀 开始: {display}", "timestamp": time.time(),
                    "data": {"progress": start_progress_val},
                    "run_id": node_execution_id,
                },
                config
            )

            try:
                result = await func(state, config, **kwargs)

                if not isinstance(result, dict):
                    logger.warning(f"Node '{display}' did not return a dictionary. Cannot attach progress.")
                    return result

                done_progress_val = progress_range[1] if progress_range else None
                completion_data = result.get("metadata", {})
                if done_progress_val is not None:
                    completion_data["progress"] = done_progress_val
                
                _emit_event(
                    "progress",
                    {
                        "node": node_name, "display_name": display, "status": "done",
                        "message": f"✅ 完成: {display}", "timestamp": time.time(),
                        "data": completion_data,
                        "run_id": node_execution_id,
                    },
                    config
                )
                
                if "progress" in result:
                    del result["progress"]

                return result

            except Exception as e:
                error_message = f"❌ 错误: {display} - {str(e)}"
                logger.opt(exception=True).error("Node '{name}' execution failed: {err}", name=display, err=str(e))
                
                _emit_event(
                    "progress",
                    {
                        "node": node_name, "display_name": display, "status": "error",
                        "message": error_message, "timestamp": time.time(),
                        "data": {"error": str(e), "error_type": type(e).__name__},
                        "run_id": node_execution_id,
                    },
                    config
                )
                
                # 重新抛出异常，让 LangGraph 引擎来处理
                raise e

        def emit_running(message: str, config: Optional[RunnableConfig] = None, progress: Optional[int] = None, data: Optional[Dict] = None):
            run_config = config or getattr(wrapper, "_current_config", None)
            display = display_name or node_name
            
            running_data = data or {}
            if progress is not None and progress_range:
                start, end = progress_range
                final_progress = start + int((progress / 100.0) * (end - start))
                running_data["progress"] = final_progress
            
            _emit_event(
                "progress",
                {
                    "node": node_name, "display_name": display, "status": "running",
                    "message": f"⚙️ {message}", "timestamp": time.time(),
                    "data": running_data,
                    "run_id": f"event_{uuid.uuid4().hex[:8]}",
                },
                run_config
            )

        wrapper.emit_running = emit_running
        return wrapper

    return decorator