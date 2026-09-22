"""事件投递：HMAC 签名、幂等键、指数退避重试，后台线程顺序执行。"""
import hashlib
import hmac
import threading
import time

import httpx

import database as db

# 可重试的 HTTP 状态码判定：超时 / 连接错误 / 5xx
MAX_BACKOFF_SECONDS = 30.0

_state = {
    "running": False,
    "last_started": None,
    "last_finished": None,
    "last_message": "尚未运行",
}
_lock = threading.Lock()


def status() -> dict:
    info = dict(_state)
    info["counts"] = db.counts()
    return info


def is_busy() -> bool:
    return _state["running"]


def compute_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _record(event_id: str, attempt: int, key: str, signature: str,
            http_status: int | None, ok: bool, will_retry: bool,
            response_excerpt: str | None, content_type: str | None,
            error: str | None, duration_ms: int) -> None:
    db.insert_delivery(
        event_id=event_id,
        attempt=attempt,
        idempotency_key=key,
        signature=signature,
        http_status=http_status,
        ok=ok,
        will_retry=will_retry,
        response_excerpt=response_excerpt,
        content_type=content_type,
        error=error,
        duration_ms=duration_ms,
    )


def _deliver_one(event: dict, cfg: dict) -> None:
    """对单条事件执行「发送 + 重试」完整流程。"""
    event_id = event["id"]
    body = event["payload"].encode("utf-8")
    total_attempts = int(cfg["max_retries"]) + 1
    db.mark_event(event_id, "running", attempts=0)

    with httpx.Client(timeout=httpx.Timeout(float(cfg["timeout"]))) as client:
        for attempt in range(1, total_attempts + 1):
            signature = compute_signature(cfg["hmac_secret"], body)
            headers = {
                "Content-Type": "application/json",
                "X-Webhook-Event": event_id,
                "X-Webhook-Id": event_id,
                "X-Idempotency-Key": event_id,
                "X-Webhook-Signature": signature,
                "X-Webhook-Attempt": str(attempt),
                "X-Webhook-Timestamp": db.now(),
            }
            db.mark_event(event_id, "running", attempts=attempt)
            started = time.monotonic()
            try:
                resp = client.post(cfg["target_url"], content=body, headers=headers)
                duration_ms = int((time.monotonic() - started) * 1000)
                ok = 200 <= resp.status_code < 300
                retryable = resp.status_code >= 500
                will_retry = (not ok) and retryable and attempt < total_attempts
                excerpt = resp.text[:2000] if resp.text else ""
                _record(
                    event_id, attempt, event_id, signature,
                    http_status=resp.status_code,
                    ok=ok,
                    will_retry=will_retry,
                    response_excerpt=excerpt,
                    content_type=resp.headers.get("content-type"),
                    error=None,
                    duration_ms=duration_ms,
                )
                if ok:
                    db.mark_event(event_id, "success", attempts=attempt)
                    return
                if not will_retry:
                    db.mark_event(
                        event_id, "failed", attempts=attempt,
                        last_error=f"收到不可重试的状态码 HTTP {resp.status_code}",
                    )
                    return
            except httpx.HTTPError as exc:
                # TimeoutException / 连接被拒 / DNS 失败等均视为可重试
                duration_ms = int((time.monotonic() - started) * 1000)
                will_retry = attempt < total_attempts
                _record(
                    event_id, attempt, event_id, signature,
                    http_status=None,
                    ok=False,
                    will_retry=will_retry,
                    response_excerpt=None,
                    content_type=None,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=duration_ms,
                )
                if not will_retry:
                    db.mark_event(
                        event_id, "failed", attempts=attempt,
                        last_error=f"{type(exc).__name__}: {exc}",
                    )
                    return

            # 指数退避：base * 2^(n-1)，如 1s, 2s, 4s
            backoff = min(
                float(cfg["backoff_base"]) * (2 ** (attempt - 1)),
                MAX_BACKOFF_SECONDS,
            )
            time.sleep(backoff)


def _run_job(event_ids: list[str] | None, only_failed: bool) -> None:
    events = db.fetch_due(event_ids, only_failed)
    total = len(events)
    _state["last_message"] = f"准备回放 {total} 条事件"
    for index, event in enumerate(events, start=1):
        # 每条事件都重新读取配置，回放过程中修改配置可即时生效
        cfg = db.get_config()
        _state["last_message"] = f"正在发送第 {index}/{total} 条事件"
        _deliver_one(event, cfg)
    _state["last_message"] = f"回放结束，共处理 {total} 条事件"


def start(event_ids: list[str] | None = None, only_failed: bool = False) -> bool:
    """启动后台回放；已有任务运行时返回 False。"""
    if not _lock.acquire(blocking=False):
        return False

    def worker() -> None:
        _state["running"] = True
        _state["last_started"] = db.now()
        _state["last_finished"] = None
        try:
            _run_job(event_ids, only_failed)
        except Exception as exc:  # noqa: BLE001 - 后台任务不能静默崩溃
            _state["last_message"] = f"回放异常终止: {type(exc).__name__}: {exc}"
        finally:
            _state["running"] = False
            _state["last_finished"] = db.now()
            _lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return True
