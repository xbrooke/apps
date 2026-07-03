#!/usr/bin/env python3
# coding: utf-8
"""
extract_icons.py (v2 简化版)
=============================
从 OpenList 上的 APK 里提取 launcher icon，写到 icons/<id>.png。

简化版逻辑（牺牲流量换稳定）：
  1) 读 apps.json，挑出"缺图标"的（也支持 --all 全部重提）
  2) 用新 sign 下载完整 APK 到 _inbox/<id>.apk
  3) zipfile 解 APK，找 res/mipmap-xxxhdpi/ic_launcher.png
  4) 写到 icons/<id>.png
  5) 删 _inbox/<id>.apk（节省磁盘）

只对 OpenList 自家 URL 有效。外部 url 跳过。
流量估算：每个 apk 30~80MB，30 个约 1~2 GB。
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.parse
import zipfile
from pathlib import Path
from typing import Optional

import requests

HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "apps.json"
ICONS_DIR = HERE / "icons"
INBOX_DIR = HERE / "_inbox"
STATIC_SALT = "https://github.com/alist-org/alist"

# 匹配 res/mipmap[-anydpi][-vN]/ic_launcher*.png 等
# 路径示例：res/mipmap-xxxhdpi-v4/ic_launcher.png
ICON_PATH_RE = re.compile(
    r"^res/(mipmap(-[a-z0-9]+)?(-v\d+)?|drawable(-[a-z0-9]+)?(-v\d+)?|"
    r"mipmap-anydpi-v\d+|drawable-anydpi-v\d+)/"
    r"(ic_launcher(_[a-z0-9_]+)?|ic_launcher_round|app_icon|appicon|icon)\.png$",
    re.IGNORECASE,
)
PRIORITY = ["xxxhdpi", "xxhdpi", "xhdpi", "hdpi", "mdpi", "anydpi"]


def static_hash(pwd: str) -> str:
    return hashlib.sha256(f"{pwd}-{STATIC_SALT}".encode()).hexdigest()


def load_config() -> dict:
    p = HERE / "openlist.config.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"base_url": "http://appstore.cnmlynk.org", "username": "xudabing", "password": "xb123321"}


def login(base: str, user: str, pwd: str) -> str:
    s = requests.Session()
    r1 = s.post(f"{base}/api/auth/login", json={"username": user, "password": pwd}, timeout=15).json()
    if r1.get("code") != 200:
        raise RuntimeError(f"login step1: {r1}")
    step1 = r1["data"]["token"]
    r2 = s.post(f"{base}/api/auth/login/hash",
                json={"username": user, "password": static_hash(pwd)},
                headers={"Authorization": step1}, timeout=15).json()
    if r2.get("code") != 200:
        raise RuntimeError(f"login step2: {r2}")
    return r2["data"]["token"]


def get_fresh_sign(base: str, token: str, path: str) -> Optional[str]:
    r = requests.post(f"{base}/api/fs/get",
                      json={"path": path, "password": ""},
                      headers={"Authorization": token}, timeout=15).json()
    return (r.get("data") or {}).get("sign")


def url_from_path(base: str, path: str, sign: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    encoded = "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))
    return f"{base}/d{encoded}?sign={urllib.parse.quote(sign, safe='')}"


def path_from_url(url: str) -> Optional[str]:
    """从 apps.json 里的 url 反推出 OpenList path"""
    if "/dl/" in url:
        raw = url.split("/dl/", 1)[-1].split("?")[0]
    elif "/d/" in url:
        raw = url.split("/d/", 1)[-1].split("?")[0]
    else:
        return None
    return "/" + urllib.parse.unquote(raw)


def download_apk(url: str, dest: Path, timeout: int = 120) -> bool:
    """流式下载 apk"""
    try:
        with requests.get(url, stream=True, timeout=timeout, allow_redirects=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        return dest.exists() and dest.stat().st_size > 1024
    except Exception as e:
        print(f"    download err: {e}", file=sys.stderr)
        if dest.exists():
            dest.unlink()
        return False


def extract_icon_from_apk(apk_path: Path) -> Optional[bytes]:
    """
    从 apk 里挑最佳 ic_launcher*.png，标准路径找不到就回退：
    找所有 png 中最像"应用图标"的（正方形 + 较大尺寸）
    """
    try:
        with zipfile.ZipFile(apk_path, "r") as zf:
            # === 1) 标准路径匹配 ===
            candidates = []
            for name in zf.namelist():
                if ICON_PATH_RE.match(name):
                    candidates.append(name)
            if candidates:
                def score(name: str) -> int:
                    nl = name.lower()
                    s = 0
                    for i, key in enumerate(PRIORITY):
                        if key in nl:
                            s = 100 - i * 10
                            break
                    if "round" in nl:
                        s -= 1
                    if "foreground" in nl or "background" in nl:
                        s -= 5
                    return s
                candidates.sort(key=score, reverse=True)
                best = candidates[0]
                with zf.open(best) as f:
                    return f.read()

            # === 2) 兜底：搜所有 png，优先 assets/ 或 res/ 下的 ===
            pngs = [(n, zf.getinfo(n).file_size) for n in zf.namelist()
                    if n.lower().endswith(".png") and not n.startswith("META-INF/")]
            if not pngs:
                return None
            # 过滤：太小（<1KB）、太大（>2MB）不太像图标
            # 但优先 res/ 或 assets/ 路径
            def heuristic(n_size):
                n, sz = n_size
                if sz < 1024 or sz > 2 * 1024 * 1024:
                    return -1
                nl = n.lower()
                score = 0
                if nl.startswith("res/"): score += 50
                if nl.startswith("assets/"): score += 40
                if "icon" in nl: score += 30
                if "logo" in nl: score += 25
                if "ic_" in nl: score += 20
                if "launcher" in nl: score += 20
                if "app" in nl: score += 10
                if "notification" in nl: score -= 30
                if "btn" in nl or "button" in nl: score -= 20
                if "tab" in nl: score -= 20
                if "toolbar" in nl: score -= 10
                if "background" in nl or "bg" in nl: score -= 15
                # 偏好中等大小（5-200KB）
                if 5 * 1024 <= sz <= 200 * 1024: score += 10
                return score
            pngs.sort(key=heuristic, reverse=True)
            if heuristic(pngs[0]) > 0:
                best = pngs[0][0]
                with zf.open(best) as f:
                    return f.read()
            return None
    except Exception as e:
        print(f"    zip err: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="覆盖所有（不只补缺失）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 条")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--keep-apk", action="store_true", help="下载后不删 _inbox/ 里的 apk")
    ap.add_argument("--only-id", action="append", default=[], help="只处理指定 id（可多次）")
    args = ap.parse_args()

    if not JSON_PATH.exists():
        sys.exit(f"找不到 {JSON_PATH}")
    ICONS_DIR.mkdir(exist_ok=True)
    INBOX_DIR.mkdir(exist_ok=True)

    cfg = load_config()
    base = cfg.get("base_url", "http://appstore.cnmlynk.org")
    user = cfg.get("username", "xudabing")
    pwd = cfg.get("password", "")

    print(f"登录 {base} ...", flush=True)
    token = login(base, user, pwd)
    print("  ok", flush=True)

    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    have = {p.stem for p in ICONS_DIR.glob("*.png")}
    targets = []
    for r in data:
        rid = r.get("id", "")
        url = r.get("url", "")
        if not rid:
            continue
        if "appstore.cnmlynk.org" not in url:
            continue
        if args.only_id and rid not in args.only_id:
            continue
        if not args.all and rid in have:
            continue
        path = path_from_url(url)
        if not path:
            continue
        targets.append((rid, path, r.get("name", "")))
    if args.limit:
        targets = targets[:args.limit]

    print(f"待处理 {len(targets)} 条 (concurrency={args.concurrency})")
    if not targets:
        return

    import concurrent.futures
    def work(item):
        rid, path, name = item
        # 拿新 sign
        sign = get_fresh_sign(base, token, path)
        if not sign:
            return (rid, "no_sign", 0)
        url = url_from_path(base, path, sign)
        apk_path = INBOX_DIR / f"{rid}.apk"
        if not download_apk(url, apk_path, timeout=args.timeout):
            return (rid, "download_fail", 0)
        size = apk_path.stat().st_size
        blob = extract_icon_from_apk(apk_path)
        if not blob:
            if not args.keep_apk and apk_path.exists():
                apk_path.unlink()
            return (rid, "no_icon", size)
        ICONS_DIR.joinpath(f"{rid}.png").write_bytes(blob)
        if not args.keep_apk and apk_path.exists():
            apk_path.unlink()
        return (rid, "ok", len(blob))

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for rid, status, size in ex.map(work, targets):
            print(f"  [{status:<13}] {rid}  ({size//1024} KB)")

    print()
    # 统计
    final_have = {p.stem for p in ICONS_DIR.glob("*.png")}
    print(f"完成。icons/ 现共 {len(final_have)} 个 png")


if __name__ == "__main__":
    main()
