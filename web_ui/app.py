"""app.py — Flask Web 入口
==============================
- GET  /                 首页 (index.html)
- POST /api/upload       上传文件 + 合同类型 → 返回 session_id
- GET  /api/process      SSE 流式推送 step1/step2/error/done
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from typing import Dict, Any

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

import config, pipeline


app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
)
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH

# 会话临时存储：session_id → {"md_bytes": bytes, "contract_type": str, "filename": str, "created_at": float}
SESSIONS: Dict[str, Dict[str, Any]] = {}
_SESSION_LOCK = threading.Lock()
_SESSION_TTL = 300  # 5 分钟

ALLOWED_CONTRACT_TYPES = set(config.PAYMENT_CLASS_MAP.keys())

# 前端可配置参数的合法范围
LINES_PER_CHUNK_RANGE = (50, 5000)
MAX_CHARS_RANGE = (50, 5000)


def _parse_int_field(raw: str, field_name: str, default: int,
                     valid_range: tuple) -> int:
    """解析并校验整数表单字段；空字符串则用默认值。"""
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"{field_name} 必须为整数")
    lo, hi = valid_range
    if not (lo <= v <= hi):
        raise ValueError(f"{field_name} 需在 [{lo}, {hi}] 范围内")
    return v


def _cleanup_sessions():
    now = time.time()
    with _SESSION_LOCK:
        stale = [sid for sid, s in SESSIONS.items() if now - s["created_at"] > _SESSION_TTL]
        for sid in stale:
            SESSIONS.pop(sid, None)


def _sse(event: str, payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


@app.get("/")
def index():
    return render_template(
        "index.html",
        service2_mode=config.SERVICE2_MODE,
        service2_base_url=config.SERVICE2_CONFIG.get("base_url", ""),
    )


@app.get("/api/runtime-info")
def runtime_info():
    """返回当前 Service 2 连接模式（供前端显示）。"""
    return jsonify({
        "service2_mode": config.SERVICE2_MODE,
        "service2_base_url": config.SERVICE2_CONFIG.get("base_url", ""),
        "service1_base_url": config.SERVICE1_CONFIG.get("base_url", ""),
    })


@app.post("/api/upload")
def upload():
    _cleanup_sessions()

    file = request.files.get("file")
    contract_type = (request.form.get("contract_type") or "").strip()

    if not file or not file.filename:
        return jsonify({"error": "未上传文件"}), 400
    filename = secure_filename(file.filename) or file.filename
    if not filename.lower().endswith(".md"):
        return jsonify({"error": "仅支持 .md 文件"}), 400
    if contract_type not in ALLOWED_CONTRACT_TYPES:
        return jsonify({
            "error": f"contract_type 非法（允许: {sorted(ALLOWED_CONTRACT_TYPES)}）"
        }), 400

    # 可选参数：每份行数阈值、上下文最大字符数、是否跳过 Service 2
    try:
        lines_per_chunk = _parse_int_field(
            request.form.get("lines_per_chunk"),
            "lines_per_chunk",
            config.LINES_PER_CHUNK,
            LINES_PER_CHUNK_RANGE,
        )
        max_chars = _parse_int_field(
            request.form.get("max_chars"),
            "max_chars",
            config.MAX_CONTEXT_CHARS,
            MAX_CHARS_RANGE,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    skip_service2 = (request.form.get("skip_service2") or "").strip().lower() in ("1", "true", "on", "yes")

    # 可选参数：任务 ID（留空则在 /api/process 中默认生成 webui-{时间戳}）
    task_id_raw = (request.form.get("task_id") or "").strip()
    if len(task_id_raw) > 128:
        return jsonify({"error": "task_id 长度不能超过 128"}), 400

    md_bytes = file.read()
    if not md_bytes:
        return jsonify({"error": "文件为空"}), 400
    try:
        md_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"error": "文件非 UTF-8 编码"}), 400

    session_id = uuid.uuid4().hex
    with _SESSION_LOCK:
        SESSIONS[session_id] = {
            "md_bytes": md_bytes,
            "contract_type": contract_type,
            "filename": filename,
            "lines_per_chunk": lines_per_chunk,
            "max_chars": max_chars,
            "skip_service2": skip_service2,
            "task_id": task_id_raw,
            "created_at": time.time(),
        }

    return jsonify({
        "session_id": session_id,
        "filename": filename,
        "size": len(md_bytes),
        "contract_type": contract_type,
        "lines_per_chunk": lines_per_chunk,
        "max_chars": max_chars,
        "skip_service2": skip_service2,
        "task_id": task_id_raw,
    })


@app.get("/api/process")
def process():
    session_id = request.args.get("session_id", "").strip()
    with _SESSION_LOCK:
        session = SESSIONS.pop(session_id, None)

    if not session:
        def _err():
            yield _sse("error", {"stage": "init", "message": "session 无效或已过期"})
            yield _sse("done", {})
        return Response(_err(), mimetype="text/event-stream")

    md_bytes = session["md_bytes"]
    contract_type = session["contract_type"]
    filename = session["filename"]
    lines_per_chunk = session.get("lines_per_chunk") or config.LINES_PER_CHUNK
    max_chars = session.get("max_chars") or config.MAX_CONTEXT_CHARS
    skip_service2 = bool(session.get("skip_service2"))
    # 优先使用前端指定的 task_id；未指定则默认生成 webui-{时间戳}
    task_id = (session.get("task_id") or "").strip() or f"webui-{int(time.time())}"

    def generate():
        yield _sse("status", {
            "stage": "step1_running",
            "message": f"Service 1 处理中... (lines={lines_per_chunk}, max_chars={max_chars})",
        })
        # 通过队列在 worker 线程与 SSE 生成器之间转发进度
        progress_q: "queue.Queue[Any]" = queue.Queue()
        SENTINEL = object()
        result_holder: Dict[str, Any] = {}

        def _on_progress(done: int, total: int):
            progress_q.put(("progress", done, total))

        def _worker():
            try:
                result_holder["result"] = pipeline.run_step1(
                    md_bytes, task_id,
                    lines_per_chunk=lines_per_chunk,
                    max_chars=max_chars,
                    on_progress=_on_progress,
                )
            except BaseException as e:
                result_holder["error"] = e
            finally:
                progress_q.put(SENTINEL)

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()

        # 实时推送进度事件，直到 worker 完成（通过 SENTINEL 通知）
        while True:
            msg = progress_q.get()
            if msg is SENTINEL:
                break
            _, done, total = msg
            percent = round(done * 100.0 / total, 1) if total else 0.0
            yield _sse("step1_progress", {
                "done": done,
                "total": total,
                "percent": percent,
            })

        worker.join()

        err = result_holder.get("error")
        if isinstance(err, pipeline.PipelineError):
            yield _sse("error", {"stage": err.stage, "message": err.message, "detail": err.detail})
            yield _sse("done", {})
            return
        if err is not None:
            yield _sse("error", {"stage": "step1", "message": f"未预期异常: {err}"})
            yield _sse("done", {})
            return

        try:
            step1_result = result_holder["result"]
            paragraphs_raw = step1_result.get("paragraphs", [])
            all_clauses = step1_result.get("all_clauses", [])
            paragraphs = pipeline.apply_contract_type(paragraphs_raw, contract_type)
        except pipeline.PipelineError as e:
            yield _sse("error", {"stage": e.stage, "message": e.message, "detail": e.detail})
            yield _sse("done", {})
            return
        except Exception as e:
            yield _sse("error", {"stage": "step1", "message": f"未预期异常: {e}"})
            yield _sse("done", {})
            return

        yield _sse("step1", {
            "paragraphs": paragraphs,
            "count": len(paragraphs),
            "all_clauses": all_clauses,
            "all_clauses_count": len(all_clauses),
            "contract_type": contract_type,
            "filename": filename,
        })

        if skip_service2:
            yield _sse("status", {"stage": "step2_skipped",
                                   "message": "用户选择仅执行 Service 1，已跳过 Service 2"})
            yield _sse("step2", {"extraction_result": [], "message": "skip: user requested"})
            yield _sse("done", {})
            return

        if not paragraphs:
            yield _sse("status", {"stage": "step2_skipped",
                                   "message": "Step 1 未筛选到付款/质保期条款，跳过 Service 2"})
            yield _sse("step2", {"extraction_result": [], "message": "skip: no paragraphs"})
            yield _sse("done", {})
            return

        yield _sse("status", {"stage": "step2_running", "message": "Service 2 处理中..."})
        try:
            step2 = pipeline.run_step2(paragraphs, task_id)
        except pipeline.PipelineError as e:
            yield _sse("error", {"stage": e.stage, "message": e.message, "detail": e.detail})
            yield _sse("done", {})
            return
        except Exception as e:
            yield _sse("error", {"stage": "step2", "message": f"未预期异常: {e}"})
            yield _sse("done", {})
            return

        yield _sse("step2", step2)
        yield _sse("done", {})

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
    }
    return Response(generate(), mimetype="text/event-stream", headers=headers)


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": f"文件过大（> {config.MAX_CONTENT_LENGTH} 字节）"}), 413


def _parse_cli():
    import argparse
    parser = argparse.ArgumentParser(
        description="合同付款信息提取 - 可视化前端 Web 服务",
    )
    parser.add_argument(
        "--mode", "--service2-mode",
        dest="service2_mode",
        choices=["local", "remote"],
        default= "remote",
        help="Service 2 连接模式：local(localhost:8001) / remote(百舸平台)；"
             "默认读取环境变量 SERVICE2_MODE，未设置则为 remote",
    )
    parser.add_argument("--host", default=None, help=f"监听地址（默认 {config.HOST}）")
    parser.add_argument("--port", type=int, default=None, help=f"监听端口（默认 {config.PORT}）")
    return parser.parse_args()


if __name__ == "__main__":
    _args = _parse_cli()

    if _args.service2_mode:
        active = config.set_service2_mode(_args.service2_mode)
    else:
        active = config.SERVICE2_CONFIG

    host = _args.host or config.HOST
    port = _args.port or config.PORT

    print("=" * 60)
    print(f"  Service 1 URL : {config.SERVICE1_CONFIG['base_url']}")
    print(f"  Service 2 MODE: {config.SERVICE2_MODE}")
    print(f"  Service 2 URL : {active['base_url']}{active['endpoint']}")
    print(f"  Web listening : http://{host}:{port}")
    print("=" * 60)

    app.run(host=host, port=port, debug=False, threaded=True)
