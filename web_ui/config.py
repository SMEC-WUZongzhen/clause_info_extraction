"""web_ui 配置模块
=====================
集中管理 Service 1 / Service 2 的地址、超时、并发、分块等参数。
可通过环境变量覆盖（便于部署切换）。
"""
import os


def _env(name: str, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    # 自动按 default 类型转换
    if isinstance(default, int):
        try:
            return int(v)
        except ValueError:
            return default
    return v


# ===== Service 1（条款分类）=====
SERVICE1_CONFIG = {
    "base_url": _env("SERVICE1_BASE_URL", "http://10.204.2.21:2251"),
    "timeout": _env("SERVICE1_TIMEOUT", 1200),
}

# ===== Service 2（付款信息提取）=====
# 两套预设：本地开发 / 百舸远程生产
SERVICE2_LOCAL_CONFIG = {
    "base_url": _env("SERVICE2_LOCAL_BASE_URL", "http://localhost:8001"),
    "endpoint": _env("SERVICE2_ENDPOINT", "/extract_payment_info"),
    "api_key": "",  # 本地无需认证
    "timeout": _env("SERVICE2_TIMEOUT", 600),
}

SERVICE2_REMOTE_CONFIG = {
    "base_url": _env("SERVICE2_REMOTE_BASE_URL", "http://106.13.172.186/s-r644699c4b7c/8000"),
    "endpoint": _env("SERVICE2_ENDPOINT", "/extract_payment_info"),
    "api_key": _env("SERVICE2_API_KEY", "7d9b2e17-2290d95b9773-2e862b5cee2c"),
    "timeout": _env("SERVICE2_TIMEOUT", 600),
}

# 启动模式：local / remote，可通过环境变量 SERVICE2_MODE 或 app.py --mode 覆盖
SERVICE2_MODE = _env("SERVICE2_MODE", "remote").lower()

# 当前生效配置（启动时可由 app.py 根据 CLI 参数重新赋值）
SERVICE2_CONFIG = (
    SERVICE2_LOCAL_CONFIG if SERVICE2_MODE == "local" else SERVICE2_REMOTE_CONFIG
)


def set_service2_mode(mode: str) -> dict:
    """切换 Service 2 模式，返回生效后的配置。供 app.py 启动时调用。"""
    global SERVICE2_CONFIG, SERVICE2_MODE
    mode = (mode or "").lower()
    if mode not in ("local", "remote"):
        raise ValueError(f"SERVICE2_MODE 必须为 local/remote，得到: {mode!r}")
    SERVICE2_MODE = mode
    SERVICE2_CONFIG = SERVICE2_LOCAL_CONFIG if mode == "local" else SERVICE2_REMOTE_CONFIG
    return SERVICE2_CONFIG

# ===== Pipeline 参数 =====
LINES_PER_CHUNK = _env("LINES_PER_CHUNK", 300)
LLM_TIMEOUT = _env("LLM_TIMEOUT", 300)
MAX_CONTEXT_CHARS = _env("MAX_CONTEXT_CHARS", 700)
MAX_WORKERS = _env("PIPELINE_MAX_WORKERS", 4)

# ===== Web 参数 =====
MAX_CONTENT_LENGTH = _env("MAX_CONTENT_LENGTH", 20 * 1024 * 1024)  # 20 MB
HOST = _env("WEB_HOST", "0.0.0.0")
PORT = _env("WEB_PORT", 5001)

# 合同类型 -> 付款条款 mapped_class 覆盖值
PAYMENT_CLASS_MAP = {
    "installation": "安装付款条款",
    "equipment": "设备付款条款",
    "mixed": "混签付款条款",
}

# 需要覆盖的「付款类」判定关键字（对应 Service 1 内 mapped_class）
PAYMENT_MAPPED_ALIASES = {"混签付款条款", "安装付款条款", "设备付款条款"}
WARRANTY_MAPPED_ALIASES = {"质保期条款"}
