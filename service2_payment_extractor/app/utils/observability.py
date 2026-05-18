"""可选的 Prometheus / OpenTelemetry 观测接入（N7）。

设计原则：
- 依赖（`prometheus_client`、`opentelemetry-*`）均为可选；缺失时本模块**完全静默**，不影响主流程。
- 仅在 env 显式开启时才挂载 `/metrics` 与 OTLP 导出。
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def setup_observability(app: Any) -> None:
    """在 FastAPI lifespan 内调用。捕获所有异常以保证主流程不受影响。"""
    if _flag("ENABLE_PROMETHEUS"):
        try:
            from prometheus_client import CollectorRegistry, make_asgi_app, multiprocess
            registry = CollectorRegistry()
            try:
                multiprocess.MultiProcessCollector(registry)
            except Exception:  # 单进程或未配置 multiproc dir
                from prometheus_client import REGISTRY as registry  # type: ignore
            app.mount("/metrics", make_asgi_app(registry=registry))
            logger.success("[observability] Prometheus /metrics 已挂载")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[observability] Prometheus 挂载失败（已忽略）：{e}")

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if otlp_endpoint:
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            resource = Resource.create({"service.name": "service2_payment_extractor"})
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
            trace.set_tracer_provider(provider)
            FastAPIInstrumentor.instrument_app(app)
            logger.success(f"[observability] OTLP traces 已启用，endpoint={otlp_endpoint}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[observability] OTLP 启用失败（已忽略）：{e}")
