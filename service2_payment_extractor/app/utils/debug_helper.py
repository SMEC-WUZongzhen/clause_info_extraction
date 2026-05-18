# ===== 文件：app/utils/debug_helper.py =====
import json
import aiofiles
import aiofiles.os as aio_os
from pathlib import Path
from typing import Any
from loguru import logger

def _json_serializer(obj: Any) -> Any:
    """
    一个健壮的JSON序列化辅助函数，可以处理Pydantic模型等特殊对象。
    """
    # 如果对象有 model_dump 方法，则调用它
    if hasattr(obj, 'model_dump'):
        return obj.model_dump()
    # 否则，让JSON库用默认方式处理，如果失败则会抛出TypeError
    try:
        # 尝试让默认的JSON编码器处理
        return json.JSONEncoder().default(obj)
    except TypeError:
        # 如果还是失败，则返回其字符串表示形式
        return str(obj)

class DebugHelper:
    """一个用于在调试期间保存中间状态快照的辅助类。"""

    @staticmethod
    async def save_snapshot(
        doc_id: str,
        step_name: str,
        content: Any,
        sub_id: Any = None,
        is_debug_enabled: bool = False
    ):
        """
        将指定步骤的内容快照保存到文件。
        """
        if not is_debug_enabled:
            return  # 如果调试模式未开启，直接返回
        base_dir = Path(f"debug_output/{doc_id}")
        await aio_os.makedirs(base_dir, exist_ok=True)

        filename_parts = [step_name]
        if sub_id is not None:
            filename_parts.append(str(sub_id))
        
        filename_base = "_".join(filename_parts)
        
        if isinstance(content, str):
            filepath = base_dir / f"{filename_base}.txt"
            async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
                await f.write(content)
        else:
            filepath = base_dir / f"{filename_base}.json"
            async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
                # 【核心修复】使用一个正确的、独立的序列化函数
                json_str = json.dumps(content, indent=2, ensure_ascii=False, default=_json_serializer)
                await f.write(json_str)
        
        # 使用 logger.info 替代 print，以便更好地控制日志级别
        logger.info(f"📸 DEBUG SNAPSHOT SAVED: {filepath}")