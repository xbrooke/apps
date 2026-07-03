#!/usr/bin/env python3
# coding: utf-8
"""
test_openlist.py
================
DBStore + OpenList 健康检查（只读，不上传任何文件）。

诊断项：
    1. OpenList 站点可达性（HTTP HEAD）
    2. /api/public/settings 版本号
    3. /api/auth/login 第一步登录（明文密码）
    4. /api/auth/login/hash 第二步（StaticHash 密码）
    5. /api/me 用真 token 取用户信息
    6. /api/fs/list 列根目录
    7. /api/fs/mkdir 测试建一个临时目录，再 /api/fs/remove 删掉（验证写权限）
    8. 给出最终诊断结论 + 修复建议

用法：
    python test_openlist.py                        # 用默认配置
    OPENLIST_PASS=xxx python test_openlist.py      # 临时给密码
    python test_openlist.py --base http://localhost:5244 --user admin --password xxx

退出码：
    0  全部通过
    1  连接 / 版本问题
    2  认证失败
    3  权限不足
    4  其他错误
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先: pip install requests", file=sys.stderr)
    sys.exit(1)

STATIC_HASH_SALT = "https://github.com/alist-org/alist"


def log(level: str, msg: str) -> None:
    color = {
        "INFO": "\033[36m", "WARN": "\033[33m",
        "ERR ": "\033[31m", "OK  ": "\033[32m",
        "STEP": "\033[35m",
    }.get(level, "")
    reset = "\033[0m" if color else ""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"{color}[{level}]{reset} {msg}", flush=True)


def static_hash(pwd: str) -> str:
    return hashlib.sha256(f"{pwd}-{STATIC_HASH_SALT}".encode()).hexdigest()


class OpenList:
    def __init__(self, base: str, user: str, pwd: str):
        self.base = base.rstrip("/")
        self.user = user
        self.pwd = pwd
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "DBStore-test/1.0"

    def post(self, path, body=None, token=None, timeout=15):
        h = {}
        if token:
            h["Authorization"] = token
        r = self.s.post(self.base + path, json=body or {}, headers=h, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def get(self, path, params=None, token=None, timeout=10):
        h = {}
        if token:
            h["Authorization"] = token
        r = self.s.get(self.base + path, params=params or {}, headers=h, timeout=timeout)
        r.raise_for_status()
        return r


def main():
    HERE = Path(__file__).resolve().parent
    cfg_path = HERE / "openlist.config.json"

    # 配置优先级：CLI > 环境变量 > openlist.config.json > 默认
    defaults_from_cfg = {}
    if cfg_path.exists():
        try:
            defaults_from_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("OPENLIST_BASE", defaults_from_cfg.get("base_url", "http://appstore.cnmlynk.org")))
    ap.add_argument("--user", default=os.environ.get("OPENLIST_USER", defaults_from_cfg.get("username", "xudabing")))
    ap.add_argument("--password", default=os.environ.get("OPENLIST_PASS", defaults_from_cfg.get("password", "")))
    ap.add_argument("--no-write", action="store_true", help="不测写权限")
    args = ap.parse_args()

    if not args.password:
        log("ERR ", "未提供密码。请传 --password / 设置 OPENLIST_PASS / 在 openlist.config.json 配 password")
        sys.exit(4)

    cli = OpenList(args.base, args.user, args.password)
    fail = 0
    token = None
    test_dir = f"/__dbstore_healthcheck_{int(time.time())}"

    def step(name):
        log("STEP", f"━━ {name}")

    # 1) 可达性
    step("1/8 HTTP 探活")
    try:
        r = cli.get("/", timeout=10)
        ok = r.status_code == 200 and "OpenList" in r.text
        log("OK  " if ok else "ERR ", f"GET /  →  HTTP {r.status_code}, contains 'OpenList': {'OpenList' in r.text}")
        if not ok:
            fail = 1
    except Exception as e:
        log("ERR ", f"GET / 失败: {e}")
        sys.exit(1)

    # 2) 版本
    step("2/8 读 /api/public/settings")
    try:
        d = cli.get("/api/public/settings", timeout=10).json()
        ver = (d.get("data") or {}).get("version", "未知")
        title = (d.get("data") or {}).get("site_title", "未知")
        log("OK  ", f"版本: {ver}  站点: {title}")
    except Exception as e:
        log("ERR ", f"settings 失败: {e}")
        fail = 1

    # 3) step1 login
    step("3/8 POST /api/auth/login  (明文密码)")
    try:
        d = cli.post("/api/auth/login", {"username": args.user, "password": args.password})
        step1 = (d.get("data") or {}).get("token")
        if d.get("code") == 200 and step1:
            log("OK  ", f"step1 token: {step1[:32]}...  (len={len(step1)})")
        else:
            log("ERR ", f"step1 失败: {json.dumps(d, ensure_ascii=False)[:300]}")
            sys.exit(2)
    except Exception as e:
        log("ERR ", f"step1 异常: {e}")
        sys.exit(2)

    # 4) step2 StaticHash
    step("4/8 POST /api/auth/login/hash  (StaticHash 密码)")
    try:
        d = cli.post(
            "/api/auth/login/hash",
            {"username": args.user, "password": static_hash(args.password)},
            token=step1,
        )
        token = (d.get("data") or {}).get("token")
        if d.get("code") == 200 and token:
            log("OK  ", f"real token: {token[:32]}...  (len={len(token)})")
        else:
            log("ERR ", f"step2 失败: {json.dumps(d, ensure_ascii=False)[:300]}")
            sys.exit(2)
    except Exception as e:
        log("ERR ", f"step2 异常: {e}")
        sys.exit(2)

    # 5) /api/me  (v4 是 GET，Authorization header 带 token)
    step("5/8 GET /api/me  (验证 token)")
    try:
        d = cli.get("/api/me", token=token).json()
        me = d.get("data") or {}
        log("OK  ", f"用户: id={me.get('id')}  user={me.get('username')}  role={me.get('role')}  perm={me.get('permission')}")
        if me.get("disabled"):
            log("ERR ", "用户已被禁用")
            sys.exit(2)
    except Exception as e:
        log("ERR ", f"/api/me 失败: {e}")
        sys.exit(2)

    # 6) list /
    step("6/8 POST /api/fs/list  {path: '/'}")
    try:
        d = cli.post("/api/fs/list", {"path": "/", "refresh": False}, token=token)
        items = (d.get("data") or {}).get("content") or []
        log("OK  ", f"根目录 {len(items)} 个条目:")
        for it in items[:20]:
            marker = "📁" if it.get("is_dir") else "📄"
            print(f"      {marker} {it.get('name')}  size={it.get('size', 0)}")
    except Exception as e:
        log("ERR ", f"list 失败: {e}")
        fail = 3

    # 7) mkdir + remove (验证写权限)
    if not args.no_write:
        step(f"7/8 POST /api/fs/mkdir  (建临时目录 {test_dir})")
        try:
            d = cli.post("/api/fs/mkdir", {"path": test_dir}, token=token)
            log("OK  ", f"已建: {test_dir}")
        except Exception as e:
            log("ERR ", f"mkdir 失败: {e}")
            log("WARN", "写权限不足，跳过清理步骤")
            fail = 3
        else:
            step("8/8 POST /api/fs/remove  (清理临时目录)")
            try:
                d = cli.post("/api/fs/remove", {"dir": "/", "names": [test_dir.lstrip("/")]}, token=token)
                log("OK  ", f"已清理: {test_dir}")
            except Exception as e:
                log("WARN", f"remove 失败（可手动清理）: {e}")

    # 总结
    print()
    if fail == 0:
        log("OK  ", "✨ 全部检查通过！可以执行 python upload_to_openlist.py 上传 APK 了")
    else:
        log("ERR ", f"发现 {fail} 类问题，请按上面的提示修复")


if __name__ == "__main__":
    main()
