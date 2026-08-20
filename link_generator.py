#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import hashlib
import random
import time
import sys
import requests

API = "https://api.platoboost.net"
UA = "Platoboost Python Client/1.0"
SERVICES = {"android": 6, "ios": 8}


def get_auth_link(service=6, hwid=None):
    if hwid is None:
        hwid = hashlib.sha256(
            str(random.randint(0, 2 ** 32)).encode()
        ).hexdigest()

    r = requests.post(f"{API}/public/start", json={
        "service": service,
        "identifier": hwid
    }, timeout=15, headers={
        "User-Agent": UA,
        "Content-Type": "application/json"
    })

    data = r.json()
    if data.get("success"):
        return data["data"]["url"]
    raise RuntimeError(f"API error: {data.get('message', 'unknown')}")


def batch_links(count=5, service=6, interval=0.3):
    urls = []
    for i in range(count):
        try:
            url = get_auth_link(service)
            urls.append(url)
            print(f"[{i + 1}/{count}] {url[:64]}...", flush=True)
        except Exception as e:
            print(f"[{i + 1}/{count}] ERROR: {e}", file=sys.stderr, flush=True)
        if i < count - 1:
            time.sleep(interval)
    return urls


if __name__ == '__main__':
    service = 6
    count = 1
    if len(sys.argv) >= 2:
        arg = sys.argv[1].lower()
        if arg in SERVICES:
            service = SERVICES[arg]
        else:
            try:
                count = int(arg)
            except ValueError:
                print(f"用法: python {sys.argv[0]} [android|ios] [数量]", file=sys.stderr)
                sys.exit(1)
    if len(sys.argv) >= 3:
        try:
            count = int(sys.argv[2])
        except ValueError:
            pass
    for url in batch_links(count, service):
        print(url)