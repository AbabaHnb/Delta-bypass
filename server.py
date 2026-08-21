#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Delta Cardkey HTTP API
# 启动: python server.py --port 2233

import sys, os, time, asyncio, argparse, hashlib, threading, json
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fastapi import FastAPI, HTTPException
import uvicorn
try:
    from fastapi.responses import ORJSONResponse as JSONResponse
except Exception:
    from fastapi.responses import JSONResponse

from main import solve_chain
from auth_client import extract_ticket

app = FastAPI(title="Delta Cardkey", default_response_class=JSONResponse)
executor = ThreadPoolExecutor(max_workers=10)
lock = threading.Lock()

# 缓存：同一 ticket 只求解一次，重复输入直接秒回
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".key_cache.json")
key_cache = {}
CACHE_TTL = 24 * 3600  #key 24h 内被消耗 缓存设为 24h
cache_dirty = threading.Event()
SAVE_DEBOUNCE = 2.0     # 最多每 2s 落盘一次


def load_cache():
    global key_cache
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                data = json.load(f)
            now = time.time()
            key_cache = {h: v for h, v in data.items()
                          if isinstance(v, dict) and now - v.get("ts", 0) < CACHE_TTL}
    except Exception:
        key_cache = {}


def save_cache_now():
    try:
        with lock:
            snapshot = dict(key_cache)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def cache_writer_loop():
    while True:
        cache_dirty.wait()
        time.sleep(SAVE_DEBOUNCE)
        cache_dirty.clear()
        save_cache_now()


def cache_get(ticket, h=None):
    h = h or hashlib.md5(ticket.encode()).hexdigest()
    with lock:
        rec = key_cache.get(h)
        if rec:
            if time.time() - rec.get("ts", 0) < CACHE_TTL:
                return rec.get("key"), True, rec.get("solve_time", 0.0)
            key_cache.pop(h, None)
    return None, False, 0.0


def cache_put(ticket, key, solve_time, h=None):
    if not key:
        return
    h = h or hashlib.md5(ticket.encode()).hexdigest()
    with lock:
        key_cache[h] = {"key": key, "ts": time.time(), "solve_time": solve_time}
    cache_dirty.set()      # 交给后台线程合并 不阻塞当前请求


load_cache()
threading.Thread(target=cache_writer_loop, daemon=True).start()

MADE_BY = "Hasl_Team"
QQ_GROUP = "277707901"
inflight = {}              

def fmt_t(secs):
    return f"{round(float(secs or 0.0), 12)}s"


def solo_pass(ticket):
    t0 = time.time()
    solved_timer = None
    try:
        key, solved_timer = solve_chain(ticket, False, 3)
        if key:
            err = None
        else:
            reason = getattr(solved_timer, 'invalid_reason', None)
            err = f"无效链接: {reason}" if reason else "solve failed"
    except Exception as e:
        key, err = None, f"solve exception: {type(e).__name__}"
    if solved_timer is not None and solved_timer.total() > 0:
        st = solved_timer.total()
    else:
        st = time.time() - t0
    return key, err, st


def run_solves(ticket):
    key, err, st = solo_pass(ticket)
    if key or err.startswith("solve exception"):
        return key, err, st
    if err.startswith("无效链接"):
        return key, err, st
    time.sleep(0.3)
    return solo_pass(ticket)


@app.get("/delta")
async def delta(url: str):
    t0 = time.time()

    try:
        ticket = extract_ticket(url)
    except Exception:
        return {"key": None, "error": "invalid url", "cached": False,
                "made_by": MADE_BY, "qq_group": QQ_GROUP,
                "times": fmt_t(time.time() - t0)}
    if not ticket:
        return {"key": None, "error": "invalid url (no ticket)", "cached": False,
                "made_by": MADE_BY, "qq_group": QQ_GROUP,
                "times": fmt_t(time.time() - t0)}

    h = hashlib.md5(ticket.encode()).hexdigest()
    cached, found, cached_solve_time = cache_get(ticket, h)
    if found:
        return {"key": cached, "cached": True, "error": None,
                "made_by": MADE_BY, "qq_group": QQ_GROUP,
                "times": fmt_t(cached_solve_time)}

    loop = asyncio.get_running_loop()
    with lock:
        fut = inflight.get(h)
        if fut is None:
            fut = loop.create_future()
            inflight[h] = fut
            spawn = True
        else:
            spawn = False
    if spawn:
        loop.create_task(drive(h, fut, ticket))

    try:
        key, err, solve_time = await asyncio.shield(fut)
    except Exception as e:
        key, err, solve_time = None, f"internal: {type(e).__name__}", time.time() - t0

    return {"key": key, "cached": False,
            "error": err,
            "made_by": MADE_BY, "qq_group": QQ_GROUP,
            "times": fmt_t(solve_time)}


async def drive(h, fut, ticket):
    try:
        key, err, st = await asyncio.get_running_loop().run_in_executor(
            executor, run_solves, ticket)
    except Exception as e:
        key, err, st = None, f"solve exception: {type(e).__name__}", 0.0
    if key:
        cache_put(ticket, key, st, h)
    with lock:
        inflight.pop(h, None)
    if not fut.done():
        fut.set_result((key, err, st))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", "-p", type=int, default=2233)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--workers", "-w", type=int, default=10)
    args = ap.parse_args()
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = min(hard, 65535)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            print(f"[server] fd limit {soft} -> {want}")
    except Exception:
        pass

    executor = ThreadPoolExecutor(max_workers=args.workers,
                                   thread_name_prefix="solve")
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
    uvicorn.run(app, host=args.host, port=args.port,
                backlog=4096, access_log=False, **kw)