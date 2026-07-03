#!/usr/bin/env python3
# coding: utf-8
"""
check_urls.py
=============
批量自检 apps.json 里的 url 是否有效：
  1) HEAD / Range: bytes=0-0  探测（不下载整个 APK）
  2) 失效的（4xx/5xx/超时）→ 重新登录 OpenList，调 /api/fs/get 拿最新 sign
  3) 重写 url 并写回 apps.json / apps_remote.json
  4) 仍修不好的 → 标记到 desc 末尾 "[无效]" 让车机前端展示

只针对 OpenList 域（http://appstore.cnmlynk.org）的 url 做处理；
外链（lz0.qaiu.top 等）只做检查、不自动修复。
"""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("缺少 requests: pip install requests", file=sys.stderr)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "apps.json"
REMOTE_JSON_PATH = HERE / "apps_remote.json"
CONFIG_PATH = HERE / "openlist.config.json"
STATIC_HASH_SALT = "https://github.com/alist-org/alist"

OPENLIST_HOST = "appstore.cnmlynk.org"


def static_hash(pwd: str) -> str:
    return hashlib.sha256(f"{pwd}-{STATIC_HASH_SALT}".encode()).hexdigest()


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def login(base: str, user: str, pwd: str) -> Optional[str]:
    """v4 两步登录，返回真 token"""
    s = requests.Session()
    s.headers["User-Agent"] = "DBStore-check/1.0"
    r1 = s.post(f"{base}/api/auth/login", json={"username": user, "password": pwd}, timeout=15)
    if r1.status_code != 200:
        return None
    d1 = r1.json()
    if d1.get("code") != 200:
        return None
    step1 = d1["data"]["token"]
    r2 = s.post(f"{base}/api/auth/login/hash",
                json={"username": user, "password": static_hash(pwd)},
                headers={"Authorization": step1}, timeout=15)
    if r2.status_code != 200:
        return None
    d2 = r2.json()
    return d2.get("data", {}).get("token")


def get_fresh_sign(base: str, token: str, path: str) -> Optional[str]:
    try:
        r = requests.post(f"{base}/api/fs/get",
                          json={"path": path, "password": ""},
                          headers={"Authorization": token}, timeout=15)
        if r.status_code != 200:
            return None
        d = r.json()
        return (d.get("data") or {}).get("sign")
    except Exception:
        return None


def url_alive(url: str, timeout: int = 12) -> bool:
    """HEAD + Range 探测。任一成功即视为有效。"""
    headers = {"User-Agent": "DBStore-check/1.0", "Range": "bytes=0-0"}
    try:
        # 先 HEAD
        r = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        if r.status_code in (200, 206, 302):
            return True
        # HEAD 失败再 GET 0 字节
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
        ok = r.status_code in (200, 206, 302)
        r.close()
        return ok
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="最多检查 N 条（0=全部）")
    args = ap.parse_args()

    if not JSON_PATH.exists():
        sys.exit(f"找不到 {JSON_PATH}")
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    cfg = load_config()
    base = cfg.get("base_url", "http://appstore.cnmlynk.org")
    user = cfg.get("username", "xudabing")
    pwd = cfg.get("password", "")
    if not pwd:
        sys.exit("openlist.config.json 里没配 password")

    print(f"[1/4] 登录 {base} ...", flush=True)
    token = login(base, user, pwd)
    if not token:
        sys.exit("登录失败")
    print(f"  ok")

    # 待检查列表
    targets = []
    for idx, r in enumerate(data):
        u = r.get("url", "")
        if not u:
            continue
        targets.append((idx, r, u))
    if args.limit:
        targets = targets[:args.limit]
    print(f"[2/4] 共 {len(targets)} 条 url 待检查，并发={args.concurrency}")

    def check_one(item):
        idx, r, u = item
        alive = url_alive(u, timeout=args.timeout)
        return idx, r, u, alive

    alive_map: dict[int, bool] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for idx, r, u, alive in ex.map(check_one, targets):
            alive_map[idx] = alive
    bad = [i for i, a in alive_map.items() if not a]
    print(f"  ok={len(alive_map) - len(bad)} bad={len(bad)}")

    if not bad:
        print("[3/4] 全部 url 有效，无需刷新 sign")
        return

    print(f"[3/4] 重刷 {len(bad)} 条失效 url 的 sign ...", flush=True)
    fixed = 0
    for idx in bad:
        r = data[idx]
        u = r["url"]
        # 从 url 反推 path
        path = None
        m = re.search(r"/d/([^?]+)", u)
        if m:
            from urllib.parse import unquote
            path = unquote(m.group(1))
        if not path:
            print(f"  skip (no path): {u[:80]}")
            continue
        sign = get_fresh_sign(base, token, path)
        if not sign:
            print(f"  refresh failed: {path}")
            r["description"] = (r.get("description") or "") + "\n[无效] " + u
            continue
        sep = "&" if "?" in u else "?"
        new_u = f"{u.split('?')[0]}{sep}sign={urllib.parse.quote(sign, safe='')}"
        r["url"] = new_u
        fixed += 1
    print(f"  fixed={fixed}, still_bad={len(bad)-fixed}")

    if args.dry_run:
        print("[4/4] dry-run, 不写文件")
        return

    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REMOTE_JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[4/4] 已写 {JSON_PATH.name} + {REMOTE_JSON_PATH.name}")


if __name__ == "__main__":
    main()
