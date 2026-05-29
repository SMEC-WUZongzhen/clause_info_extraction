# =============================================================================
# Dockerfile - service2_payment_extractor
# =============================================================================
# Multi-stage build targeting linux/amd64 (Intel/AMD) and linux/arm64 (Apple Silicon).
#   Build example (Intel/AMD):
#     docker build -t payment-extractor:latest --platform linux/amd64 .
#   Build example (Apple Silicon):
#     docker build -t payment-extractor:latest --platform linux/arm64 .
#
# IMPORTANT: 本 Dockerfile 依赖 .dockerignore 文件来过滤 .git/.venv/logs/
#            debug_output/outputs/app/tests 等不需要进入镜像的目录。
#            构建前请确认 .dockerignore 存在，否则镜像会被脏文件撑大。
# =============================================================================

# --- Stage 1: Builder ---
# Use the specific version to ensure reproducible builds.
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

# 1. Install Poetry (only used in builder stage; not copied to runtime).
RUN pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple "poetry==1.8.5"

# 2. Copy project definition files.
#    poetry.lock is optional; if it exists Poetry uses it for reproducible installs,
#    otherwise it resolves dependencies directly from pyproject.toml.
COPY pyproject.toml ./
COPY poetry.lock* ./

# 3. Pre-install CPU-only torch BEFORE poetry resolves dependencies.
#    避免 poetry install 拉到 GPU 版 torch（~2GB）后再被强制重装。
#    版本约束需与 pyproject.toml 一致（当前 ">=2.0.0,<2.6.0"），
#    否则 poetry 解析时会判定不满足约束并把 torch 降级到主源 GPU 版。
RUN pip install --no-cache-dir \
        "torch>=2.0.0,<2.6.0" \
        --index-url https://download.pytorch.org/whl/cpu

# 4. Install all other Python dependencies via Poetry.
#    不再在构建期执行 `poetry lock --no-update`：构建期改 lock 文件
#    会破坏可复现构建语义。若 pyproject.toml 与 poetry.lock 不一致，
#    应在开发机修复并提交，让构建在此处直接失败比静默改 lock 更安全。
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --no-root --only main \
        --no-cache

# 5. 兜底强制 CPU 版 torch：
#    清华镜像（pyproject.toml 中的 primary 源）同步自 PyPI，发布的是带
#    CUDA 元数据的标准 wheel。其文件名/hash 与 pytorch.org/whl/cpu 不一致，
#    poetry install 按 lock 校验时可能把第 3 步预装的 CPU 版 torch 覆盖
#    成 GPU 版，导致镜像膨胀 ~1GB 并引入无谓 CUDA 依赖。
#    --force-reinstall 保证一定回退到 CPU wheel；
#    --no-deps 确保只动 torch 自身，不影响 poetry 已装好的依赖树。
RUN pip install --no-cache-dir --force-reinstall --no-deps \
        "torch>=2.0.0,<2.6.0" \
        --index-url https://download.pytorch.org/whl/cpu


# --- Stage 2: Runtime ---
# slim-bookworm is more stable than slim alone.
FROM python:3.12-slim-bookworm

# OCI image labels (便于制品库管理 / docker inspect).
LABEL org.opencontainers.image.title="service2_payment_extractor" \
      org.opencontainers.image.description="Service 2 - 付款信息提取微服务" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.vendor="SMEC"

# Create a dedicated non-root user for security.
# uid/gid default to 1000; override at build time with
#   --build-arg USER_UID=... --build-arg USER_GID=...
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid ${USER_GID} appgroup \
    && useradd --uid ${USER_UID} --gid ${USER_GID} --shell /bin/bash --create-home appuser

# Define build-time arguments with defaults.
ARG APP_CONFIG_DIR=/app_config
ARG LOG_DIR=/app/logs
ARG APP_PORT=8000

# Default ENV values.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOKENIZERS_PARALLELISM=false \
    CONFIG_DIR=${APP_CONFIG_DIR} \
    LOG_DIR=${LOG_DIR} \
    APP_PORT=${APP_PORT}

WORKDIR /app

# Create writable directories for config and logs (owned by appuser).
RUN mkdir -p ${APP_CONFIG_DIR} ${LOG_DIR} \
    && chown -R appuser:appgroup ${APP_CONFIG_DIR} ${LOG_DIR}

# Copy pre-installed Python packages from builder.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 移除运行时不需要的 poetry / 构建工具，降低镜像体积与攻击面。
RUN pip uninstall -y poetry poetry-core poetry-plugin-export 2>/dev/null || true \
    && rm -rf /root/.cache /tmp/* ~/.cache

# Copy application source (chown 已在 COPY 时完成，不再二次 chown -R 整棵树以避免镜像层膨胀).
COPY --chown=appuser:appgroup . .

# 仅 chown /app 目录本身（不递归），便于 appuser 在 /app 下做必要的写入
# (例如 load_dotenv() 兜底、prompts 热下载等)。
RUN chown appuser:appgroup /app

# Switch to non-root user.
USER appuser

# Expose the port the application listens on.
EXPOSE ${APP_PORT}

# Declare volumes for configuration and logs so they persist outside the container.
VOLUME ["${APP_CONFIG_DIR}", "${LOG_DIR}"]

# Health check — verifies the HTTP endpoint is responding.
# 首启动会从 BOS 下载 BM25 pickle (~MB) 与 bge-reranker-large (~1GB)，外加
# tiktoken / Milvus / Embedding 预热，冷启动 3~5 分钟较常见，故 start-period=300s。
HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
    CMD python -c "\
import urllib.request; \
urllib.request.urlopen('http://localhost:${APP_PORT}/health', timeout=5); \
print('OK')" || exit 1

# Run via main.py so that all CLI flags (--download-*-from-bos, --bos-*,
# --config-dir, LOG_ENABLE_FILE, etc.) are actually honoured.
# 使用 exec form (JSON) 接收 SIGTERM 实现优雅停机；
# main.py 内部用 argparse 取 --port 默认值 8000，与 EXPOSE 一致。
# 如需通过环境变量改端口，请在 docker run 时覆盖 CMD：
#   docker run ... img python main.py --port ${APP_PORT}
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "8000"]
