"""payment_ratio 包（M5 修复，第一阶段）。

**当前阶段**：作为 ``app.utils.payment_ratio_extractor`` 的兼容包装层，
新代码请优先从此处导入；旧代码保持兼容。

**后续阶段**：将 2472 行的原模块按以下结构机械拆分到 ``extractor.py`` /
``summary.py`` / ``helpers.py`` / ``chains.py`` 子模块（不改任何函数实现，
仅移动 + 调整 import）：

    payment_ratio/
    ├── extractor.py      # PaymentRatioExtractor
    ├── summary.py        # PaymentSummaryRatioExtractor + Pydantic models
    ├── helpers.py        # _parse_amount_to_float / _extract_unique_total_amount 等纯函数
    └── chains.py         # 内部 LangChain Chain / Prompt 构造

包级 ``__init__`` 始终 re-export 公共 API，调用方导入路径不需改动。
"""
from app.utils.payment_ratio_extractor import (  # noqa: F401
    PaymentRatioExtractor,
    PaymentSummaryRatioExtractor,
    PaymentSummaryItem,
    PaymentSummaryOutput,
    get_summary_extractor,
    get_ratio_extractor,
)

__all__ = [
    "PaymentRatioExtractor",
    "PaymentSummaryRatioExtractor",
    "PaymentSummaryItem",
    "PaymentSummaryOutput",
    "get_summary_extractor",
    "get_ratio_extractor",
]
