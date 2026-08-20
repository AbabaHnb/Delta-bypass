# Delta Cardkey Solver

自动完成 Platoboost Delta Key System的captcha验证→获取key的全流程求解器

提供 CLI 与 HTTP API 两种用法。

---

## 快速开始

要求 Python 3.10+。

```bash
# 1. 安装依赖
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# 2. 求解一条链接
./venv/bin/python main.py "https://auth.platorelay.com/a?d=<ticket>"

# 3. 部署 API 服务
./venv/bin/python server.py --port 2233 --workers 16
```

---

## 使用方法

### 直接求解已有 ticket

```bash
# 从 auth 链接
python main.py "https://auth.platorelay.com/a?d=<ticket>"

# 从原始 ticket 字符串
python main.py "<ticket>"

# 从文件读取（一行一个 ticket，批量）
python main.py tickets.txt
```

### 生成测试链接并求解(测试链接只能用来测试求解器 无任何其他用途)

```bash
# 生成 5 条测试链接并求解
python main.py --generate 5

# 生成 3 条，静默模式
python main.py --generate 3 --quiet

# 只生成测试链接，不求解
python main.py --generate 5 --no-auto
```

### 命令行参数

| 参数 | 说明 |
|------|------|
| `target` | auth URL / ticket 字符串 / 文件路径 |
| `--generate N` / `-g N` | 通过 Platoboost API 生成 N 条链接 |
| `--quiet` / `-q` | 静默模式，只输出结果 |
| `--max-rounds N` | 最大求解轮数（默认 3） |
| `--no-auto` | 只生成链接，不求解 |

### 作为库使用

```python
from captcha_solver import solve, session
import requests

# 获取 captcha 挑战
s = session()
ch = s.get('https://captcha.platorelay.com/api/challenge').json()

# 下载图片
img = requests.get(
    'https://captcha.platorelay.com' + ch['image'],
    headers={'Referer': 'https://captcha.platorelay.com/'}
).content

# 求解
x, y = solve(img, ch['type'])
print(f'答案: ({x}, {y})')

# 提交答案
r = s.post('https://captcha.platorelay.com/api/answer', json={
    'challenge_id': ch['challenge_id'],
    'x': x, 'y': y
}).json()
print(f'成功: {r["success"]}, token: {r.get("token")}')
```

```python
from auth_client import extract_ticket, do_step, create_session
from captcha_solver import solve, session as captcha_session

# 提取 ticket
ticket = extract_ticket("https://auth.platorelay.com/a?d=...")

# 创建会话
s = create_session()

# 获取 captcha 并求解
cs = captcha_session()
ch = cs.get('https://captcha.platorelay.com/api/challenge').json()
img = cs.get('https://captcha.platorelay.com' + ch['image'],
             headers={'Referer': 'https://captcha.platorelay.com/'}).content
x, y = solve(img, ch['type'])
r = cs.post('https://captcha.platorelay.com/api/answer', json={
    'challenge_id': ch['challenge_id'], 'x': x, 'y': y
}).json()
token = r['token']

# 推进 checkpoint
result = do_step(ticket, token, service=3)
print(result)
```

---

## HTTP API

服务只有一个接口：`GET /delta`。

### `GET /delta?url=<auth_url>`

求解一条 auth 链接并返回 key。

```bash
curl "http://127.0.0.1:2233/delta?url=https://auth.platorelay.com/a?d=<ticket>"
```

```json
{
  "key": "FREE_8d4d157d3a1dae339ba822a61840e7d5",
  "cached": false,
  "error": null,
  "made_by": "Hasl_Team",
  "qq_group": "277707901",
  "times": "8.2s"
}
```

| 字段 | 说明 |
|------|------|
| `key` | 求解成功的 key；失败为 `null` |
| `cached` | `true` = 命中 24h 缓存，直接返回，未重新求解 |
| `error` | 失败原因，成功为 `null` |
| `times` | **真实求解耗时**，缓存命中时返回当初求解的耗时 |

可能的 `error` 值：

| error | 含义 |
|-------|------|
| `invalid url` | URL 解析失败 |
| `invalid url (no ticket)` | URL 中没有 `d=` 参数 |
| `solve failed` | 两次求解均未拿到 key（已含一次自动重试） |
| `solve exception: XxxError` | 求解过程抛出异常 |

---

## 行为说明

**Key 缓存（24 小时）**
同一 ticket 求解成功后，key 写入 `.key_cache.json`。后续请求同一链接直接命中缓存秒回（`cached: true`），不重复消耗。TTL 设为 24h，与 key 自身的有效期一致；过期条目自动淘汰，不会返回已失效的 key。

**同链接并发合并**
同一链接的并发请求只会触发**一次**真实求解，其余请求等待并共享同一结果。实测 500 并发请求同一未缓存 ticket → 真实求解 1 次，全部返回同一 key。因此不存在"重复请求"报错，失败后也可立即重试。

**限流规避**
服务端对同一链路的相邻 checkpoint 提交有最小间隔要求（实测 ~5s，低于此值返回 `finishing checkpoints too fast`）。求解器按上次提交的真实时间差主动补齐间隔——间隔已足够则不等待，不足才精确补齐。

---

## 求解流程

```
ticket → 获取 metadata → 求解 captcha → 推进 step → 解码回调 → 下一张 ticket → ... → 获取 key
```

每轮自动从 metadata 获取 service 参数，captcha 与 metadata 并行获取以减少耗时；相邻 checkpoint 主动补齐最小间隔以规避服务端限流。

---

## 部署

### 鉴权

> **`/delta` 没有任何认证。** 任何能访问该端口的人都能消耗你的求解能力。
> 生产环境不要直接暴露到公网，至少加上这一项：

仅监听本地、由上层服务转发：

```bash
python server.py --host 127.0.0.1 --port 2233
```

### 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--host` | `0.0.0.0` | 监听地址。仅本机访问用 `127.0.0.1` |
| `--port` | `2233` | 端口 |
| `--workers` | `10` | 求解线程数。单次求解约 8s 且多为等待，可设为预期并发求解数 |

启动时自动将进程 fd 上限提到 65535（受 hard limit 约束）。systemd 部署建议同时设 `LimitNOFILE`。

---

## 性能基线

8 核 Debian 实测：

| 项 | 数值 |
|---|---|
| 单条求解 | ~8s（其中约 5s 是服务端强制的 checkpoint 间隔） |
| 验证码识别 | ~290ms/张 |
| 识别命中率 | 100%（520 张真实样本，服务端判定） |
| 缓存命中响应 | 4.4ms（单请求） |
| 缓存命中吞吐 | 3000 并发全部成功，峰值 ~900 req/s |
| 多票并发 | 3.5× 吞吐（限流 per-ticket，等待可重叠） |

单条求解的 8s 中约 5s 是服务端硬性限制，无法压缩；余下为验证码计算与网络往返。吞吐提升靠并发。

---

## 依赖

- Python 3.10+
- numpy, scipy, Pillow — 图片处理
- pycryptodome — AES-CTR
- requests, urllib3 — 网络请求
- numba — 验证码加速
- fastapi, uvicorn — HTTP API
- uvloop, httptools, orjson — 可选，高并发加速（缺失自动降级）

---

## 文件

| 文件 | 作用 |
|------|------|
| `server.py` | HTTP API：缓存、同链接合并|
| `main.py` | 求解链路：验证码 → step → 回调解码 → 轮询 key |
| `captcha_solver.py` | 验证码识别 |
| `auth_client.py` | auth 服务客户端：AES-CTR、连接池、UA 池 |
| `link_generator.py` | 生成测试链接 |
| `requirements.txt` | Python 依赖 |

运行时生成：`.key_cache.json`（key 缓存，可安全删除）。

---

## 故障排查

| 现象 | 排查方向 |
|------|----------|
| `solve failed` | 看 CLI verbose 输出的结束行 `未获取到 key (原因: ...)`，原因为 `captcha-failed` / `step-failed` / `poll-timeout` 等 |
| 大量 `finishing checkpoints too fast` | 检查 `main.py` 的 `MIN_STEP_GAP`（默认 5.0），服务端策略变更时需上调 |
| 高并发下连接失败 | fd 上限不足，确认 systemd `LimitNOFILE=65535` |
| 返回已失效的 key | 确认 `.key_cache.json` 的 TTL 逻辑生效；必要时删除该文件重建 |

CLI 默认 verbose，排查时直接用 `main.py` 跑单条链接观察每轮日志。

---

## License

MIT
