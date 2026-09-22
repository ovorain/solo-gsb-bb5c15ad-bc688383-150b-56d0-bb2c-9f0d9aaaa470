# Webhook 事件回放实验室

一个可 **Docker 一键启动** 的 Webhook 事件回放实验室：粘贴/导入 JSON 事件，配置本地目标
URL、HMAC 密钥与最大重试次数；系统**按事件顺序**向目标发送请求，自动附加 **HMAC-SHA256
签名**与**幂等键**；遇到超时或 5xx 时按**指数退避自动重试**，并在时间轴中展示每次请求的
HTTP 状态、响应摘要与最终结果。支持查看事件原始载荷、手动重放单条失败事件、清理历史记录。

技术栈：**FastAPI + SQLite（标准库）+ httpx + 原生单文件前端**。

## 一键启动

```bash
docker compose up --build
```

启动后访问：

| 入口 | 地址 |
| --- | --- |
| 前端页面 | http://localhost:8000 |
| 交互式 API 文档 (Swagger) | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |

- 监听端口：**8000**（在 `docker-compose.yml` 的 `ports` 中修改宿主端口）。
- SQLite 数据文件持久化在 Docker 卷 `webhook-data`（容器内 `/data/webhook.db`），重启不丢数据。

不想用 Compose 也可以：

```bash
docker build -t webhook-replay-lab .
docker run --rm -p 8000:8000 -v webhook-data:/data webhook-replay-lab
```

## 60 秒上手（用内置演示目标）

容器自带一个演示接收端 `POST /demo-target`，无需自己起服务即可体验完整流程：

1. 打开 http://localhost:8000，左侧「投递配置」默认目标为
   `http://127.0.0.1:8000/demo-target`，先把 URL 改成可观察重试的形式：

   ```
   http://127.0.0.1:8000/demo-target?fail_times=2
   ```

   点击 **保存配置**。
2. 在「导入 JSON 事件」中点击 **填入示例 → 导入**。
3. 点击 **回放全部待处理**：每条事件前两次收到 500，自动按 `1s → 2s` 指数退避重试，
   第三次成功；右侧时间轴可展开每次尝试的状态码、耗时、响应体、签名与幂等键。

演示目标支持的查询参数：

| 参数 | 作用 |
| --- | --- |
| `fail_times=2` | 前 N 次尝试返回 500，之后返回 200（模拟临时故障恢复） |
| `status=400` | 始终返回指定状态码（4xx 不会重试） |
| `delay=8` | 延迟 N 秒响应，配合较小 timeout 模拟超时 |
| 组合 | `?delay=0&fail_times=1` |

> 指向宿主机上的本地服务时，容器内请使用 `http://host.docker.internal:端口/路径`
> （Linux 上需 Docker 20.10+；或使用 `--add-host=host.docker.internal:host-gateway`）。

## 示例请求（curl）

```bash
# 1. 查看 / 修改投递配置
curl -s http://localhost:8000/api/config
curl -s -X PUT http://localhost:8000/api/config \
  -H 'Content-Type: application/json' \
  -d '{"target_url":"http://127.0.0.1:8000/demo-target?fail_times=2",
       "hmac_secret":"change-me","max_retries":3,
       "timeout":3.0,"backoff_base":1.0}'

# 2. 导入事件（JSON 对象或对象数组均可，按导入顺序投递）
curl -s -X POST http://localhost:8000/api/events \
  -H 'Content-Type: application/json' \
  -d '[{"type":"order.created","id":"ord-1001","amount":99},
       {"type":"order.paid","id":"ord-1002","amount":99}]'

# 3. 按顺序回放所有未成功事件（后台执行，立即返回）
curl -s -X POST http://localhost:8000/api/replay \
  -H 'Content-Type: application/json' -d '{}'

# 3b. 只回放失败的事件
curl -s -X POST http://localhost:8000/api/replay \
  -H 'Content-Type: application/json' -d '{"only_failed":true}'

# 4. 查看回放进度与计数
curl -s http://localhost:8000/api/replay/status

# 5. 列出全部事件
curl -s http://localhost:8000/api/events

# 6. 查看单条事件：原始载荷 + 每次投递的完整时间轴
curl -s http://localhost:8000/api/events/<EVENT_ID>

# 7. 手动重放单条事件（成功/失败均可重放）
curl -s -X POST http://localhost:8000/api/events/<EVENT_ID>/replay

# 8. 清理全部历史记录（回放进行中会返回 409）
curl -s -X DELETE http://localhost:8000/api/events
```

导入 `.json` 文件：

```bash
curl -s -X POST http://localhost:8000/api/events \
  -H 'Content-Type: application/json' \
  --data-binary @events.json
```

## 发送给目标的请求

对每条事件，系统发送 `POST <target_url>`，请求体为事件原始 JSON，并附加以下请求头：

| 请求头 | 内容 |
| --- | --- |
| `Content-Type` | `application/json` |
| `X-Webhook-Signature` | `sha256=<HMAC-SHA256(secret, 原始请求体)>` |
| `X-Idempotency-Key` | 事件 ID（UUID），同一事件的所有重试保持不变 |
| `X-Webhook-Id` / `X-Webhook-Event` | 事件 ID |
| `X-Webhook-Attempt` | 当前尝试序号，从 `1` 开始 |
| `X-Webhook-Timestamp` | 发送时间（UTC ISO 8601） |

目标端可用如下方式校验签名（Python 示例）：

```python
import hmac, hashlib

expected = "sha256=" + hmac.new(
    b"change-me", raw_body, hashlib.sha256
).hexdigest()
hmac.compare_digest(expected, request.headers["X-Webhook-Signature"])
```

## 重试与退避策略

- **投递顺序**：严格按事件导入顺序（`seq`）串行发送，前一条处理完才发送下一条。
- **成功**：HTTP 2xx。
- **自动重试**：HTTP 5xx，或超时 / 连接被拒 / DNS 失败等网络错误。
  等待时间为指数退避 `backoff_base × 2^(n-1)`（默认 `1s, 2s, 4s…`，上限 30s），
  重试次数由「最大重试」配置控制（不含首次请求，默认 3 次，即最多 4 次尝试）。
- **不重试**：HTTP 4xx 等其余状态码，直接标记为失败并记录原因。
- **幂等性**：每次重试携带相同的 `X-Idempotency-Key`，目标端可据此去重。
- 回放期间修改配置（如缩短超时）会对后续事件即时生效。

## 时间轴中记录的内容

每次尝试都会在 `GET /api/events/{id}` 与前端详情页中记录：

- 尝试序号、HTTP 状态码（网络错误时为空）、耗时毫秒数、发送时间；
- 响应体前 2000 字符摘要、响应 Content-Type；
- 错误类型与信息（超时/连接错误）、是否还会退避重试；
- 本次使用的幂等键与完整签名；
- 事件最终状态：`pending` / `running` / `success` / `failed`，以及尝试总次数。

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
export WEBHOOK_DB=./webhook.db        # 可选，默认 /data/webhook.db
uvicorn main:app --reload --port 8000
```

## 项目结构

```
.
├── main.py              # FastAPI 路由：配置/事件/回放/演示目标/静态页
├── database.py          # SQLite 表结构与数据访问
├── dispatcher.py        # 签名、投递、指数退避重试、后台顺序回放线程
├── static/index.html    # 单文件前端（无构建步骤）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
