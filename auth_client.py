#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import base64
import itertools
import time
import urllib.parse
import requests
import urllib3
from Crypto.Cipher import AES as AES
from fake_useragent import UserAgent

ua = UserAgent(platforms='mobile')
AUTH_API = "https://auth.platorelay.com/api"

# fake_useragent 的 .random 每次约 16ms（内部重新筛选数据集），在高并发下是显著开销。
# 启动时预生成一批 UA，之后 O(1) 轮转取用 —— 对外表现（UA 多样性）不变。
UA_POOL = []
UA_IDX = itertools.count()
UA_SCREEN = {}

SCREENS_IPHONE = ('390x844', '393x852', '375x812', '414x896', '430x932', '428x926', '360x780')
SCREENS_IPAD = ('820x1180', '834x1194', '768x1024', '744x1133', '1024x1366')
SCREENS_ANDROID = ('360x800', '412x915', '393x873', '384x854', '360x780', '412x892', '432x960')

FALLBACK_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'


def screens_for(platform, os_name):
    #依据 fake_useragent 的 platform/os 字段选择该机型的分辨率候选
    p = (platform or '').lower()
    o = (os_name or '').lower()
    if 'ipad' in p or 'tablet' in p:
        return SCREENS_IPAD
    if 'iphone' in p or 'ipod' in p or 'ios' in o:
        return SCREENS_IPHONE
    return SCREENS_ANDROID


def build_ua_pool(size=32):
    #预生成 UA 池
    pool = []
    for _ in range(size):
        try:
            rec = ua.getRandom
        except Exception:
            break
        if not isinstance(rec, dict):
            break
        s = rec.get('useragent')
        if not s or s in UA_SCREEN:
            continue
        cands = screens_for(rec.get('platform'), rec.get('os'))
        # 用 UA 字符串哈希取模,保证同一 UA 恒定拿到同一分辨率
        UA_SCREEN[s] = cands[hash(s) % len(cands)]
        pool.append(s)
    if not pool:
        pool = [FALLBACK_UA]
        UA_SCREEN.setdefault(FALLBACK_UA, SCREENS_IPHONE[0])
    return pool


def rand_ua():
    global UA_POOL
    if not UA_POOL:
        UA_POOL = build_ua_pool()
    return UA_POOL[next(UA_IDX) % len(UA_POOL)]


def pick_screen(user_agent):
    #返回与该 UA 绑定的屏幕分辨率;未知 UA 则按其字符串特征推断
    s = UA_SCREEN.get(user_agent)
    if s:
        return s
    low = (user_agent or '').lower()
    if 'ipad' in low:
        cands = SCREENS_IPAD
    elif 'iphone' in low or 'ipod' in low:
        cands = SCREENS_IPHONE
    else:
        cands = SCREENS_ANDROID
    return cands[hash(user_agent or '') % len(cands)]

#AES-CTR

def aes_ctr_encrypt(plaintext, key_bytes, iv_bytes):
    #对当前计数器进行加密
    key = bytearray(key_bytes) if isinstance(key_bytes, (bytes, bytearray)) else bytearray(key_bytes)
    iv = bytearray(iv_bytes) if isinstance(iv_bytes, (bytes, bytearray)) else bytearray(iv_bytes)
    data = plaintext.encode() if isinstance(plaintext, str) else plaintext
    out = bytearray()
    for i in range(0, len(data), 16):
        blk = AES.new(bytes(key), AES.MODE_ECB).encrypt(bytes(iv))
        out += bytes(a ^ b for a, b in zip(data[i:i + 16], blk))
        #递增计数器
        j = 15
        while True:
            iv[j] = (iv[j] + 1) & 0xFF
            if iv[j] != 0:
                break
            j -= 1
            if j < 0:
                break
    return bytes(out)


def build_meta_stream(ticket, now_ms=None, user_agent=None, screen=None):
    #从ticket构建AES-CTR字段
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if user_agent is None:
        user_agent = rand_ua()
    if screen is None:
        screen = pick_screen(user_agent)

    key_meta = ticket[:16]
    ctr_meta = ticket[16:32]
    key_stream = ticket[1:17]
    ctr_stream = ticket[17:33]

    meta_plain = json.dumps({
        "browserInfo": [{
            "screen": screen,
            "ua": user_agent,
            "time": now_ms
        }]
    }, separators=(',', ':'))

    stream_plain = json.dumps({
        "events": [{"event": 1, "data": {"time": now_ms}}]
    }, separators=(',', ':'))

    meta = aes_ctr_encrypt(
        meta_plain,
        [ord(c) for c in key_meta],
        [ord(c) for c in ctr_meta]
    ).hex()

    stream = aes_ctr_encrypt(
        stream_plain,
        [ord(c) for c in key_stream],
        [ord(c) for c in ctr_stream]
    ).hex()

    return meta, stream


#获取ticket

def extract_ticket(arg):
    #从auth URL原始ticket字符串或文件路径获取ticket
    t = arg.strip()
    if t.startswith('http'):
        parsed = urllib.parse.urlparse(t)
        qs = urllib.parse.parse_qs(parsed.query)
        if 'd' in qs:
            return qs['d'][0]
        return t
    if t.endswith('.txt') or '/' in t or '\\' in t:
        try:
            with open(t) as f:
                content = f.read().strip()
                if content:
                    return extract_ticket(content)
        except (IOError, OSError):
            pass
    return t


def decode_callback_url(loot_url):
    # 从loot URL的r=参数解码回调URL
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(loot_url).query)
    r_param = qs.get('r', [''])[0]
    if not r_param:
        return None
    b64 = r_param.replace('-', '+').replace('_', '/')
    padding = (4 - len(b64) % 4) % 4
    try:
        dec = base64.b64decode(b64 + '=' * padding).decode('utf-8')
        if dec.startswith('http'):
            return dec
    except Exception:
        pass
    return None


def extract_ticket_from_callback(callback_url):
    # 从回调URL获取下一个ticket
    if not callback_url:
        return None
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    return qs.get('d', [None])[0]


#步骤

#urllib3连接池
# 高并发：单主机连接池要够大，否则请求排队等连接（原 maxsize=1 是并发瓶颈）
step_pool = urllib3.PoolManager(
    num_pools=8, maxsize=64, block=False, retries=False,
    timeout=urllib3.Timeout(connect=3.0, read=8.0),
)

def create_session():
    #构建requests
    s = requests.Session()
    s.headers.update({
        'User-Agent': rand_ua(),
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/plain, */*',
    })
    #无重试
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=8, pool_maxsize=32, max_retries=0)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    return s


def do_step(ticket, token, service=3, session=None, now_ms=None):
    # PUT /api/session/step
    step_ua = rand_ua()
    meta, stream = build_meta_stream(ticket, now_ms, user_agent=step_ua)
    url = f"{AUTH_API}/session/step?ticket={urllib.parse.quote(ticket)}&service={service}"

    body = json.dumps({
        "captcha": token,
        "meta": meta,
        "stream": stream,
        "resolved": True
    }).encode()

    STEP_HTTP_RETRIES = 3        
    STEP_RETRY_SLEEP = 0.25      
    last_err = None
    for attempt in range(STEP_HTTP_RETRIES + 1):
        try:
            r = step_pool.request('PUT', url, body=body, redirect=False, headers={
                'User-Agent': step_ua,
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*'
            })
            if r.status != 200:
                last_err = f"http {r.status}"
                if attempt < STEP_HTTP_RETRIES:
                    time.sleep(STEP_RETRY_SLEEP)
                    continue
                return {"success": False, "error": last_err}
            try:
                return json.loads(r.data)
            except Exception:
                # 200 但响应体不是 JSON
                last_err = "non-json response"
                if attempt < STEP_HTTP_RETRIES:
                    time.sleep(STEP_RETRY_SLEEP)
                    continue
                return {"success": False, "error": last_err}
        except Exception as e:
            last_err = str(e)
            if attempt < STEP_HTTP_RETRIES:
                time.sleep(STEP_RETRY_SLEEP)
                continue
            return {"success": False, "error": last_err}
    return {"success": False, "error": last_err or "step failed"}


def get_json(path_qs, retries=3, sleep=0.25):
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = step_pool.request('GET', f"{AUTH_API}/{path_qs}",
                redirect=False,
                headers={'User-Agent': rand_ua(), 'Accept': 'application/json'})
            if r.status != 200:
                last_err = f"http {r.status}"
                if attempt < retries:
                    time.sleep(sleep)
                    continue
                return {"success": False, "error": last_err, "transient": True}
            try:
                return json.loads(r.data)
            except Exception:
                last_err = "non-json response"
                if attempt < retries:
                    time.sleep(sleep)
                    continue
                return {"success": False, "error": last_err, "transient": True}
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(sleep)
                continue
            return {"success": False, "error": last_err, "transient": True}
    return {"success": False, "error": last_err or "get failed", "transient": True}


def get_session_status(ticket, session=None):
    # GET /api/session/status
    return get_json(f"session/status?ticket={urllib.parse.quote(ticket)}")


def get_session_metadata(ticket, session=None):
    # GET /api/session/metadata
    return get_json(f"session/metadata?ticket={urllib.parse.quote(ticket)}")

INVALID_MARKERS = ('invalid payload', 'expired', 'not found', 'invalid session',
                     'invalid ticket', 'does not exist')


def check_ticket_valid(ticket, session=None):
    try:
        meta = get_session_metadata(ticket, session=session)
    except Exception:
        return True, None
    if not isinstance(meta, dict):
        return True, None
    if meta.get('success') is True:
        return True, None
    if meta.get('transient'):
        return True, None
    if meta.get('success') is False:
        msg = str(meta.get('message') or meta.get('error') or '').lower()
        if any(m in msg for m in INVALID_MARKERS):
            return False, str(meta.get('message') or meta.get('error') or 'invalid link')
        # 其它未知失败
        return True, None
    return True, None