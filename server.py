#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Delta Cardkey HTTP API
# 启动: python server.py --port 2233

import sys, os, time, asyncio, argparse, hashlib, threading, json
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fastapi import FastAPI, HTTPException
import uvicorn

# 可选高性能组件：有则用，没有自动降级（不影响功能）
try:
    from fastapi.responses import ORJSONResponse as _JSONResponse
except Exception:
    from fastapi.responses import JSONResponse as _JSONResponse

from main import solve_chain
from auth_client import extract_ticket

app = FastAPI(title="Delta Cardkey", default_response_class=_JSONResponse)
_executor = ThreadPoolExecutor(max_workers=10)
_lock = threading.Lock()

# ---- 持久化 key 缓存：同一 ticket 只求解一次，重复输入直接秒回 ----
_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".key_cache.json")
_key_cache = {}
_CACHE_TTL = 24 * 3600  # key 24h 内被消耗，缓存设为 24h

# 缓存落盘改为「异步合并写」：高并发命中时不再每次全量 json.dump 阻塞请求线程。
_cache_dirty = threading.Event()
_SAVE_DEBOUNCE = 2.0     # 最多每 2s 落盘一次


def _load_cache():
    global _key_cache
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE) as f:
                data = json.load(f)
            now = time.time()
            _key_cache = {h: v for h, v in data.items()
                          if isinstance(v, dict) and now - v.get("ts", 0) < _CACHE_TTL}
    except Exception:
        _key_cache = {}


def _save_cache_now():
    # 原子写：先写临时文件再 rename，避免并发/崩溃留下截断的 json
    try:
        with _lock:
            snapshot = dict(_key_cache)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, _CACHE_FILE)
    except Exception:
        pass


def _cache_writer_loop():
    # 后台合并写线程：被标记 dirty 后延迟合并落盘，多次写只产生一次 IO
    while True:
        _cache_dirty.wait()
        time.sleep(_SAVE_DEBOUNCE)
        _cache_dirty.clear()
        _save_cache_now()


def _cache_get(ticket, h=None):
    h = h or hashlib.md5(ticket.encode()).hexdigest()
    with _lock:
        rec = _key_cache.get(h)
        if rec:
            # TTL 内才算有效；过期则当未命中并清除（防 24h 后已消费 key 被误复用）
            if time.time() - rec.get("ts", 0) < _CACHE_TTL:
                return rec.get("key"), True, rec.get("solve_time", 0.0)
            _key_cache.pop(h, None)
    return None, False, 0.0


def _cache_put(ticket, key, solve_time, h=None):
    if not key:
        return
    h = h or hashlib.md5(ticket.encode()).hexdigest()
    with _lock:
        _key_cache[h] = {"key": key, "ts": time.time(), "solve_time": solve_time}
    _cache_dirty.set()      # 交给后台线程合并落盘，不阻塞当前请求


_load_cache()
threading.Thread(target=_cache_writer_loop, daemon=True).start()

MADE_BY = "Hasl_Team"
QQ_GROUP = "277707901"

# ---- 单飞行合并：同一 ticket 并发/重复请求共用一个求解，不再有 duplicate 报错 ----
_inflight = {}              # md5(ticket) -> asyncio.Future (由 _lock 保护)


def _fmt_t(secs):
    # 秒，保留1位小数
    return f"{round(float(secs or 0.0), 1)}s"


def _solo_pass(ticket):
    """在 executor 线程里跑一次完整求解链，返回 (key, err, solve_time)。"""
    t0 = time.time()
    try:
        # solve_chain 内部正常失败时不抛异常，而是返回 (None, timer)
        key, solved_timer = solve_chain(ticket, False, 3)
        err = None if key else "solve failed"
    except Exception as e:
        key, err = None, f"solve exception: {type(e).__name__}"
    if 'solved_timer' in locals() and solved_timer is not None and solved_timer.total() > 0:
        st = solved_timer.total()
    else:
        st = time.time() - t0
    return key, err, st


def _run_solves(ticket):
    """执行真实求解，失败时带 1 次重试，返回 (key, error, solve_time)。"""
    key, err, st = _solo_pass(ticket)
    if key or err.startswith("solve exception"):
        return key, err, st
    # 首次失败 → 短暂停顿后重试一次，提高命中率
    time.sleep(0.3)
    return _solo_pass(ticket)


@app.get("/delta")
async def delta(url: str):
    t0 = time.time()

    try:
        ticket = extract_ticket(url)
    except Exception:
        return {"key": None, "error": "invalid url", "cached": False,
                "made_by": MADE_BY, "qq_group": QQ_GROUP,
                "times": _fmt_t(time.time() - t0)}
    if not ticket:
        return {"key": None, "error": "invalid url (no ticket)", "cached": False,
                "made_by": MADE_BY, "qq_group": QQ_GROUP,
                "times": _fmt_t(time.time() - t0)}

    h = hashlib.md5(ticket.encode()).hexdigest()

    # 1) 已缓存过 key → 直接返回缓存 key + 当时求解耗时(真正拿key的时间)
    #    这条路径是纯内存查表，无锁竞争外的任何 IO，可承载瞬时大量请求
    cached, found, cached_solve_time = _cache_get(ticket, h)
    if found:
        return {"key": cached, "cached": True, "error": None,
                "made_by": MADE_BY, "qq_group": QQ_GROUP,
                "times": _fmt_t(cached_solve_time)}

    # 2) 单飞行：同一 ticket 只发起一次真实求解，其它请求共用其结果 (不再 duplicate)
    #    用同步锁保护 dict（避免 async 锁在高并发下的额外调度开销）
    loop = asyncio.get_running_loop()
    with _lock:
        fut = _inflight.get(h)
        if fut is None:
            fut = loop.create_future()
            _inflight[h] = fut
            spawn = True
        else:
            spawn = False
    if spawn:
        loop.create_task(_drive(h, fut, ticket))

    try:
        key, err, solve_time = await asyncio.shield(fut)
    except Exception as e:
        key, err, solve_time = None, f"internal: {type(e).__name__}", time.time() - t0

    return {"key": key, "cached": False,
            "error": err,
            "made_by": MADE_BY, "qq_group": QQ_GROUP,
            "times": _fmt_t(solve_time)}


async def _drive(h, fut, ticket):
    """在后台线程执行求解(带重试)，结果写入缓存并唤醒所有等待方。"""
    try:
        key, err, st = await asyncio.get_running_loop().run_in_executor(
            _executor, _run_solves, ticket)
    except Exception as e:
        key, err, st = None, f"solve exception: {type(e).__name__}", 0.0
    if key:
        _cache_put(ticket, key, st, h)
    with _lock:
        _inflight.pop(h, None)
    if not fut.done():
        fut.set_result((key, err, st))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", "-p", type=int, default=2233)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--workers", "-w", type=int, default=10)
    args = ap.parse_args()

    # 高并发需要足够的文件描述符（每个连接一个 fd）。默认 1024 时
    # 上千并发会直接连接失败，这里在允许范围内自动提到硬上限。
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = min(hard, 65535)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            print(f"[server] fd limit {soft} -> {want}")
    except Exception:
        pass

    _executor = ThreadPoolExecutor(max_workers=args.workers,
                                   thread_name_prefix="solve")

    # uvloop + httptools 显著降低事件循环/解析开销；缺失则自动回退
    kw = {}
    try:
        import uvloop  # noqa: F401
        kw['loop'] = 'uvloop'
    except Exception:
        pass
    try:
        import httptools  # noqa: F401
        kw['http'] = 'httptools'
    except Exception:
        pass

    # backlog 调大以吸收瞬时突发连接；缓存命中路径为纯内存，可高速返回
    uvicorn.run(app, host=args.host, port=args.port,
                backlog=4096, access_log=False, **kw)