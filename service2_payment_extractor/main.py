# main.py - service2_payment_extractor

import os
import sys
import uvicorn
import argparse
from pathlib import Path
from dotenv import load_dotenv

def resolve_env_path(env_path: str) -> Path:
    """解析 .env 文件路径，支持绝对路径和相对路径。"""
    p = Path(env_path)
    if p.is_absolute():
        return p
    # 相对路径基于 main.py 所在目录解析
    return (Path(__file__).parent / p).resolve()

def main():
    """
    启动付款信息提取服务 (Service 2)。
    """
    parser = argparse.ArgumentParser(description="Run the Payment Extractor Service (Service 2).")
    parser.add_argument("--config-dir", type=str, default=None,
                        help="Directory used as CONFIG_DIR. If set, the service will prefer loading '.env' from this directory.")
    parser.add_argument("--env-path", type=str, default=None,
                        help="Path to the .env file. Defaults to auto-detection.")
    # --- BOS bootstrap (for downloading .env and enabling other downloads) ---
    parser.add_argument("--bos-endpoint", type=str, default=None, help="Override BOS_ENDPOINT at startup.")
    parser.add_argument("--bos-bucket-name", type=str, default=None, help="Override BOS_BUCKET_NAME at startup.")
    parser.add_argument("--bos-access-key", type=str, default=None, help="Override BOS_ACCESS_KEY at startup.")
    parser.add_argument("--bos-secret-key", type=str, default=None, help="Override BOS_SECRET_KEY at startup.")
    parser.add_argument("--download-env-from-bos", action="store_true",
                        help="If set, download .env from BOS on every startup into CONFIG_DIR and then load it.")
    parser.add_argument("--env-bos-key", type=str, default=None,
                        help="BOS object key for the .env file (remote path/key inside the bucket).")
    parser.add_argument("--env-bos-bucket", type=str, default=None,
                        help="Bucket name for the .env file download (defaults to --bos-bucket-name / BOS_BUCKET_NAME).")
    parser.add_argument("--download-prompts-from-bos", action="store_true",
                        help="If set, download app/config/prompts.py from BOS on every startup and overwrite the local file.")
    parser.add_argument("--prompts-bos-key", type=str, default=None,
                        help="BOS object key for prompts.py (remote path/key inside the bucket).")
    parser.add_argument("--prompts-bos-bucket", type=str, default=None,
                        help="Bucket name for prompts.py download (defaults to BOS_BUCKET_NAME).")
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    # ----- 并发调优（可被环境变量覆盖，默认值适合中小规模部署）-----
    parser.add_argument("--workers", type=int, default=int(os.getenv("UVICORN_WORKERS", "1")),
                        help="Uvicorn worker 进程数。CPU 密集（rerank/jieba）和 LLM 高并发建议 2-4。reload 模式下被强制为 1。")
    parser.add_argument("--backlog", type=int, default=int(os.getenv("UVICORN_BACKLOG", "2048")),
                        help="TCP 等待队列长度。")
    parser.add_argument("--limit-concurrency", type=int,
                        default=(int(os.environ["UVICORN_LIMIT_CONCURRENCY"])
                                 if os.getenv("UVICORN_LIMIT_CONCURRENCY") else None),
                        help="单 worker 最大并发连接数，超出立刻 503。默认不限制。")
    parser.add_argument("--timeout-keep-alive", type=int,
                        default=int(os.getenv("UVICORN_TIMEOUT_KEEP_ALIVE", "30")),
                        help="HTTP keep-alive 空闲超时秒数。")
    args = parser.parse_args()

    # 0) Bootstrap CONFIG_DIR and BOS env overrides as early as possible
    if args.config_dir:
        os.environ["CONFIG_DIR"] = args.config_dir

    if args.bos_endpoint:
        os.environ["BOS_ENDPOINT"] = args.bos_endpoint
    if args.bos_bucket_name:
        os.environ["BOS_BUCKET_NAME"] = args.bos_bucket_name
    if args.bos_access_key:
        os.environ["BOS_ACCESS_KEY"] = args.bos_access_key
    if args.bos_secret_key:
        os.environ["BOS_SECRET_KEY"] = args.bos_secret_key

    # 1) Optional: always download .env from BOS on startup
    if args.download_env_from_bos:
        config_dir = Path(os.getenv("CONFIG_DIR", "")).resolve() if os.getenv("CONFIG_DIR") else None
        if not config_dir:
            print("[main] ERROR: --download-env-from-bos requires --config-dir (CONFIG_DIR).", file=sys.stderr)
            sys.exit(1)
        if not args.env_bos_key:
            print("[main] ERROR: --download-env-from-bos requires --env-bos-key.", file=sys.stderr)
            sys.exit(1)

        env_bucket = args.env_bos_bucket or os.getenv("BOS_BUCKET_NAME")
        access_key = os.getenv("BOS_ACCESS_KEY")
        secret_key = os.getenv("BOS_SECRET_KEY")
        endpoint = os.getenv("BOS_ENDPOINT")

        missing = [k for k, v in {
            "BOS_ACCESS_KEY": access_key,
            "BOS_SECRET_KEY": secret_key,
            "BOS_ENDPOINT": endpoint,
            "BOS_BUCKET_NAME/--env-bos-bucket": env_bucket,
        }.items() if not v]
        if missing:
            print(f"[main] ERROR: Missing BOS settings for env download: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)

        try:
            from app.utils.bos_helper import BosHandler
        except Exception as e:
            print(f"[main] ERROR: Failed to import BOS helper: {e}", file=sys.stderr)
            sys.exit(1)

        local_env_path = (config_dir / ".env").resolve()
        try:
            handler = BosHandler({
                "access_key": access_key,
                "secret_key": secret_key,
                "endpoint": endpoint,
                "bucket_name": env_bucket,
            })
            ok = handler.download_file(
                remote_file=args.env_bos_key,
                local_file=local_env_path,
                bucket_name=env_bucket,
            )
            if not ok:
                print(f"[main] ERROR: Failed to download .env from bos://{env_bucket}/{args.env_bos_key}", file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            print(f"[main] ERROR: Downloading .env from BOS failed: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"[main] Downloaded .env to: {local_env_path}")
        load_dotenv(dotenv_path=str(local_env_path), override=False)
    # 2) Otherwise: use local .env loading behavior
    if args.env_path:
        resolved = resolve_env_path(args.env_path)
        if not resolved.is_file():
            print(f"[main] ERROR: --env-path file not found: {resolved}", file=sys.stderr)
            sys.exit(1)
        print(f"[main] Loading environment from: {resolved}")
        load_dotenv(dotenv_path=str(resolved), override=False)
    else:
        load_dotenv()  # 自动查找 .env 文件

    # 3) Optional: always download prompts.py from BOS on startup (after env is loaded)
    if args.download_prompts_from_bos:
        if not args.prompts_bos_key:
            print("[main] ERROR: --download-prompts-from-bos requires --prompts-bos-key.", file=sys.stderr)
            sys.exit(1)

        prompts_bucket = args.prompts_bos_bucket or os.getenv("BOS_BUCKET_NAME")
        access_key = os.getenv("BOS_ACCESS_KEY")
        secret_key = os.getenv("BOS_SECRET_KEY")
        endpoint = os.getenv("BOS_ENDPOINT")

        missing = [k for k, v in {
            "BOS_ACCESS_KEY": access_key,
            "BOS_SECRET_KEY": secret_key,
            "BOS_ENDPOINT": endpoint,
            "BOS_BUCKET_NAME/--prompts-bos-bucket": prompts_bucket,
        }.items() if not v]
        if missing:
            print(f"[main] ERROR: Missing BOS settings for prompts download: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)

        try:
            from app.utils.bos_helper import BosHandler
        except Exception as e:
            print(f"[main] ERROR: Failed to import BOS helper: {e}", file=sys.stderr)
            sys.exit(1)

        service_root = Path(__file__).parent.resolve()
        local_prompts_path = (service_root / "app" / "config" / "prompts.py").resolve()
        try:
            handler = BosHandler({
                "access_key": access_key,
                "secret_key": secret_key,
                "endpoint": endpoint,
                "bucket_name": prompts_bucket,
            })
            ok = handler.download_file(
                remote_file=args.prompts_bos_key,
                local_file=local_prompts_path,
                bucket_name=prompts_bucket,
            )
            if not ok:
                print(f"[main] ERROR: Failed to download prompts.py from bos://{prompts_bucket}/{args.prompts_bos_key}", file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            print(f"[main] ERROR: Downloading prompts.py from BOS failed: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"[main] Downloaded prompts.py to: {local_prompts_path}")

    # reload 模式下 uvicorn 强制单进程；其它情况允许多 worker
    effective_workers = 1 if args.reload else max(1, args.workers)
    run_kwargs = dict(
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=effective_workers,
        backlog=args.backlog,
        timeout_keep_alive=args.timeout_keep_alive,
    )
    if args.limit_concurrency is not None:
        run_kwargs["limit_concurrency"] = args.limit_concurrency

    print(f"[main] uvicorn 启动参数: workers={effective_workers}, backlog={args.backlog}, "
          f"limit_concurrency={args.limit_concurrency}, timeout_keep_alive={args.timeout_keep_alive}")

    uvicorn.run("app.api:app", **run_kwargs)

if __name__ == "__main__":
    main()
