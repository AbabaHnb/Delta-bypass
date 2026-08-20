#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import base64
import itertools
import time
import urllib.parse
import requests
import urllib3
from Crypto.Cipher import AES as _AES
from fake_useragent import UserAgent

_ua = UserAgent(platforms='mobile')
AUTH_API = "https://auth.platorelay.com/api"

# fake_useragent 的 .random 每次约 16ms（内部重新筛选数据集），在高并发下是显著开销。
# 启动时预生成一批 UA，之后 O(1) 轮转取用 —— 对外表现（UA 多样性）不变。
_UA_POOL = []
_UA_IDX = itertools.count()


def _rand_ua():
    global _UA_POOL
    if not _UA_POOL:
        seen = []
        for _ in range(32):
            try:
                seen.append(_ua.random)
            except Exception:
                break
        _UA_POOL = list(dict.fromkeys(seen)) or ['Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)']
    return _UA_POOL[next(_UA_IDX) % len(_UA_POOL)]

#AES-CTR

def _aes_ctr_encrypt(plaintext, key_bytes, iv_bytes):
    #对当前计数器进行加密
    key = bytearray(key_bytes) if isinstance(key_bytes, (bytes, bytearray)) else bytearray(key_bytes)
    iv = bytearray(iv_bytes) if isinstance(iv_bytes, (bytes, bytearray)) else bytearray(iv_bytes)
    data = plaintext.encode() if isinstance(plaintext, str) else plaintext
    out = bytearray()
    for i in range(0, len(data), 16):
        blk = _AES.new(bytes(key), _AES.MODE_ECB).encrypt(bytes(iv))
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


def build_meta_stream(ticket, now_ms=None):
    #从ticket构建AES-CTR字段
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    key_meta = ticket[:16]
    ctr_meta = ticket[16:32]
    key_stream = ticket[1:17]
    ctr_stream = ticket[17:33]

    meta_plain = json.dumps({
        "browserInfo": [{
            "screen": "390x844",
            "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)",
            "time": now_ms
        }]
    }, separators=(',', ':'))

    stream_plain = json.dumps({
        "events": [{"event": 1, "data": {"time": now_ms}}]
    }, separators=(',', ':'))

    meta = _aes_ctr_encrypt(
        meta_plain,
        [ord(c) for c in key_meta],
        [ord(c) for c in ctr_meta]
    ).hex()

    stream = _aes_ctr_encrypt(
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
_step_pool = urllib3.PoolManager(
    num_pools=8, maxsize=64, block=False, retries=False,
    timeout=urllib3.Timeout(connect=3.0, read=8.0),
)

def create_session():
    #构建requests
    s = requests.Session()
    s.headers.update({
        'User-Agent': _rand_ua(),
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
    meta, stream = build_meta_stream(ticket, now_ms)
    url = f"{AUTH_API}/session/step?ticket={urllib.parse.quote(ticket)}&service={service}"

    body = json.dumps({
        "captcha": token,
        "meta": meta,
        "stream": stream,
        "resolved": True
    }).encode()

    try:
        r = _step_pool.request('PUT', url, body=body, headers={
            'User-Agent': _rand_ua(),
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/plain, */*'
        })
        return json.loads(r.data)
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_session_status(ticket, session=None):
    # GET /api/session/status 
    try:
        r = _step_pool.request('GET',
            f"{AUTH_API}/session/status?ticket={urllib.parse.quote(ticket)}",
            headers={'User-Agent': _rand_ua(), 'Accept': 'application/json'})
        return json.loads(r.data)
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_session_metadata(ticket, session=None):
    # GET /api/session/metadata 
    try:
        r = _step_pool.request('GET',
            f"{AUTH_API}/session/metadata?ticket={urllib.parse.quote(ticket)}",
            headers={'User-Agent': _rand_ua(), 'Accept': 'application/json'})
        return json.loads(r.data)
    except Exception as e:
        return {"success": False, "error": str(e)}
