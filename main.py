"""FastAPI 入口：配置 / 事件 / 回放 / 时间轴 / 内置演示目标。"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import database as db
import dispatcher


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Webhook 事件回放实验室",
    description="导入 JSON 事件，按顺序签名投递，自动指数退避重试，查看完整时间轴。",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------- 请求模型 ----------

class ConfigPayload(BaseModel):
    target_url: str = Field(min_length=1, description="本地目标 URL")
    hmac_secret: str = Field(min_length=1, description="HMAC-SHA256 密钥")
    max_retries: int = Field(ge=0, le=10, description="最大重试次数（不含首次）")
    timeout: float = Field(gt=0, le=60, description="单次请求超时秒数")
    backoff_base: float = Field(gt=0, le=30, description="指数退避基数（秒）")

    @field_validator("target_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("target_url 必须以 http:// 或 https:// 开头")
        return value


class ReplayPayload(BaseModel):
    event_ids: list[str] | None = Field(
        default=None, description="指定要回放的事件 ID；为空则按状态筛选"
    )
    only_failed: bool = Field(default=False, description="仅回放失败事件")


# ---------- 配置 ----------

@app.get("/api/config", tags=["config"], summary="读取投递配置")
def read_config():
    return db.get_config()


@app.put("/api/config", tags=["config"], summary="更新投递配置")
def write_config(payload: ConfigPayload):
    return db.update_config(payload.model_dump())


# ---------- 事件 ----------

@app.post("/api/events", tags=["events"], summary="导入 JSON 事件（对象或数组）")
async def create_events(request: Request):
    raw = await request.body()
    if not raw.strip():
        raise HTTPException(status_code=400, detail="请求体为空")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败: {exc}") from exc

    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list) and all(isinstance(x, dict) for x in data):
        items = data
    else:
        raise HTTPException(
            status_code=400, detail="事件必须是 JSON 对象，或 JSON 对象组成的数组"
        )
    if not items:
        raise HTTPException(status_code=400, detail="事件数组不能为空")

    count = db.insert_events(items)
    return {"imported": count}


@app.get("/api/events", tags=["events"], summary="按顺序列出全部事件")
def read_events():
    return db.list_events()


@app.get("/api/events/{event_id}", tags=["events"], summary="查看事件原始载荷与投递时间轴")
def read_event(event_id: str):
    event = db.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    event["deliveries"] = db.get_deliveries(event_id)
    return event


@app.delete("/api/events", tags=["events"], summary="清理全部历史记录")
def delete_all_events():
    if dispatcher.is_busy():
        raise HTTPException(status_code=409, detail="回放进行中，暂时无法清理")
    db.clear_events()
    return {"cleared": True}


# ---------- 回放控制 ----------

@app.post("/api/replay", tags=["replay"], summary="按事件顺序回放（失败时指数退避重试）")
def replay(payload: ReplayPayload):
    if dispatcher.is_busy():
        raise HTTPException(status_code=409, detail="已有回放任务正在运行")

    event_ids = payload.event_ids
    if event_ids:
        missing = [eid for eid in event_ids if db.get_event(eid) is None]
        if missing:
            raise HTTPException(status_code=404, detail=f"事件不存在: {missing}")

    started = dispatcher.start(event_ids=event_ids, only_failed=payload.only_failed)
    if not started:
        raise HTTPException(status_code=409, detail="已有回放任务正在运行")
    return {"started": True}


@app.post("/api/events/{event_id}/replay", tags=["replay"], summary="手动重放单条事件")
def replay_one(event_id: str):
    if db.get_event(event_id) is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    if dispatcher.is_busy():
        raise HTTPException(status_code=409, detail="已有回放任务正在运行")
    started = dispatcher.start(event_ids=[event_id])
    if not started:
        raise HTTPException(status_code=409, detail="已有回放任务正在运行")
    return {"started": True, "event_id": event_id}


@app.get("/api/replay/status", tags=["replay"], summary="回放任务状态与事件计数")
def replay_status():
    return dispatcher.status()


# ---------- 内置演示目标 ----------

@app.post("/demo-target", tags=["demo"], summary="内置测试目标：支持 ?status=500 / ?delay=2 / ?fail_times=2")
async def demo_target(request: Request):
    """方便开箱试验：

    - 把目标 URL 配成 http://127.0.0.1:8000/demo-target?fail_times=2 可观察两次 500 后成功
    - ?status=400 模拟不可重试的 4xx
    - ?delay=8 模拟超时（当 timeout 小于 delay 时）
    """
    signature = request.headers.get("x-webhook-signature")
    idempotency_key = request.headers.get("x-idempotency-key")
    attempt = request.headers.get("x-webhook-attempt", "1")
    body = await request.body()

    cfg = db.get_config()
    expected = dispatcher.compute_signature(cfg["hmac_secret"], body)
    signature_valid = bool(signature) and signature == expected

    params = request.query_params
    forced_status = int(params.get("status", "200"))
    delay = float(params.get("delay", "0"))
    fail_times = int(params.get("fail_times", "0"))

    if delay > 0:
        import asyncio
        await asyncio.sleep(delay)

    if fail_times > 0 and int(attempt) <= fail_times:
        return JSONResponse(
            status_code=500,
            content={"error": f"模拟第 {attempt} 次失败", "attempt": int(attempt)},
        )
    if forced_status != 200:
        return JSONResponse(
            status_code=forced_status,
            content={"error": f"模拟返回 HTTP {forced_status}"},
        )

    return {
        "received": True,
        "attempt": int(attempt),
        "idempotency_key": idempotency_key,
        "signature_valid": signature_valid,
    }


# ---------- 健康检查与前端 ----------

@app.get("/health", tags=["meta"], summary="健康检查")
def health():
    return {"status": "ok"}


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
