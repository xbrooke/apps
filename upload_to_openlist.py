#!/usr/bin/env python3
# coding: utf-8
"""
upload_to_openlist.py
=====================
将本地 APK 自动上传到自建 OpenList 实例（v4.0 协议），拿到可直链/中转的下载 URL，
并把记录追加/更新到 apps/manifest.csv。

OpenList v4 登录流程（两步）：
    1) POST /api/auth/login          {username, password}   →  step1_token
    2) POST /api/auth/login/hash     {username, password}   +  Header: Authorization: <step1_token>
       其中 password = SHA256("<明文密码>-https://github.com/alist-org/alist")
       →  real_token （48 小时有效）

API 路径（v4）：
    POST /api/auth/login            步骤1：明文密码登录
    POST /api/auth/login/hash       步骤2：StaticHash 密码登录
    POST /api/fs/mkdir              创建目录
    POST /api/fs/put                单文件上传（multipart/form-data）
    POST /api/fs/remove             删除文件
    POST /api/fs/get                拿下载 URL（redirect=false 走中转 /d/<path>，redirect=true 拿 raw_url）
    POST /api/fs/list               列目录

特性：
    * 单 APK 上传：python upload_to_openlist.py <apk> --id appid --name "应用名"
    * 批量扫描：python upload_to_openlist.py （扫描 _inbox/）
    * 自动走 OpenList 中转（/d/<path>），车机用户不直接走云盘限速
    * 失败时打印 OpenList 真实错误码 + 提示
    * 默认不写死密码，全部从配置文件读，配置文件不入 git

依赖：
    pip install requests
"""
from __future__ import annotations

import argparse
import csv
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
    print("缺少依赖 requests，请先: pip install requests", file=sys.stderr)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "manifest.csv"
INBOX_DIR = HERE / "_inbox"
CONFIG_PATH = HERE / "openlist.config.json"

# OpenList v4 StaticHash 硬编码盐（源码中写死，所有实例都一样）
STATIC_HASH_SALT = "https://github.com/alist-org/alist"

# ---------- 配置 ----------

DEFAULT_CONFIG = {
    "base_url": "http://appstore.cnmlynk.org",
    "username": "xudabing",
    "password": "",
    "remote_path": "/DBStore",
    "public_base": "",
    "use_proxy": True,
    "insecure_skip_2fa": True,   # 留接口，没启 2FA 时无影响
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception as e:
            log("WARN", f"读取 {CONFIG_PATH.name} 失败: {e}，回退默认")
    # 允许从环境变量覆盖
    return {
        **DEFAULT_CONFIG,
        "base_url": os.environ.get("OPENLIST_BASE", DEFAULT_CONFIG["base_url"]),
        "username": os.environ.get("OPENLIST_USER", DEFAULT_CONFIG["username"]),
        "password": os.environ.get("OPENLIST_PASS", ""),
        "remote_path": os.environ.get("OPENLIST_DIR", DEFAULT_CONFIG["remote_path"]),
    }


# ---------- 工具 ----------

def log(level: str, msg: str) -> None:
    color = {
        "INFO": "\033[36m", "WARN": "\033[33m",
        "ERR ": "\033[31m", "OK  ": "\033[32m",
    }.get(level, "")
    reset = "\033[0m" if color else ""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"{color}[{level}]{reset} {msg}", flush=True)


def static_hash(password: str) -> str:
    """OpenList v4 StaticHash: SHA256( "{password}-{salt}" )"""
    raw = f"{password}-{STATIC_HASH_SALT}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "", name)
    return (s or "app").lower()[:32]


# ---------- OpenList 客户端 ----------

class OpenList:
    def __init__(self, cfg: dict):
        self.base = cfg["base_url"].rstrip("/")
        self.user = cfg["username"]
        self.pwd = cfg["password"]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "DBStore-uploader/1.0"})

    def _post(self, path: str, body: dict, token: Optional[str] = None) -> dict:
        headers = {}
        if token:
            headers["Authorization"] = token  # v4 不加 Bearer
        r = self.session.post(f"{self.base}{path}", json=body, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(
                f"OpenList {path} 失败 code={data.get('code')} msg={data.get('message')!r}\n"
                f"  完整响应: {json.dumps(data, ensure_ascii=False)[:400]}"
            )
        return data.get("data") or {}

    def login(self) -> str:
        """v4 两步登录：明文 → StaticHash，返回 real_token"""
        # step 1
        d1 = self._post("/api/auth/login", {"username": self.user, "password": self.pwd})
        step1_token = d1.get("token")
        if not step1_token:
            raise RuntimeError(f"step1 未返回 token: {d1}")
        # step 2
        d2 = self._post(
            "/api/auth/login/hash",
            {"username": self.user, "password": static_hash(self.pwd)},
            token=step1_token,
        )
        real = d2.get("token")
        if not real:
            raise RuntimeError(f"step2 未返回 token: {d2}")
        return real

    def mkdir(self, token: str, path: str) -> None:
        try:
            self._post("/api/fs/mkdir", {"path": path}, token=token)
            log("INFO", f"  已建目录: {path}")
        except RuntimeError as e:
            if "exist" in str(e).lower() or "already" in str(e).lower():
                log("INFO", f"  目录已存在: {path}")
            else:
                raise

    def remove(self, token: str, dir_path: str, name: str) -> None:
        try:
            self._post("/api/fs/remove", {"dir": dir_path, "names": [name]}, token=token)
        except Exception:
            pass  # 文件不存在时容错

    def upload(self, token: str, local: Path, remote_dir: str) -> dict:
        """v4 /api/fs/put 单文件上传，返回元信息"""
        remote_path = f"{remote_dir.rstrip('/')}/{local.name}"
        # 先尝试删旧
        self.remove(token, remote_dir, local.name)

        with open(local, "rb") as f:
            files = {"file": (local.name, f, "application/octet-stream")}
            data = {"path": remote_dir}
            r = self.session.post(
                f"{self.base}/api/fs/put",
                files=files,
                data=data,
                headers={"Authorization": token},
                timeout=1800,  # 30 分钟，大 APK 慢慢传
            )
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 200:
            raise RuntimeError(f"上传失败: {body}")
        return {
            "path": remote_path,
            "name": local.name,
            "size": local.stat().st_size,
            "raw": body.get("data"),
        }

    def list(self, token: str, path: str = "/", refresh: bool = False) -> list[dict]:
        """列目录，返回 content 列表"""
        d = self._post("/api/fs/list", {"path": path, "refresh": refresh}, token=token)
        return d.get("content") or []

    def list_storages(self, token: str) -> list[dict]:
        """列已挂载的存储（v4 用 /api/admin/storage/list 或 /api/fs/list '/'）"""
        try:
            d = self._post("/api/admin/storage/list", {}, token=token)
            return d.get("content") or d.get("data") or []
        except Exception:
            # 回退：根目录条目里找带 driver 的就是 storage
            return []

    def get_url(self, token: str, remote_path: str, use_proxy: bool, public_base: str) -> str:
        """拿可下载 URL：use_proxy=True 走 OpenList 中转（/d/<path>），False 取 raw_url"""
        data = self._post("/api/fs/get", {"path": remote_path}, token=token)
        if use_proxy:
            base = (public_base or self.base).rstrip("/")
            # OpenList 中转下载：/d{path}  (path 必须以 / 开头)
            p = remote_path if remote_path.startswith("/") else "/" + remote_path
            # /d 路由对空格、中文不需要 url-encode，保留原样
            return f"{base}/d{p}"
        # 不走代理：要求 OpenList 给 raw_url（v4 默认不返回 raw，需要 admin 配 sign/直链）
        raw = data.get("raw_url") or data.get("url")
        if not raw:
            log("WARN", "  OpenList 未返回 raw_url，自动回退到中转 /d/ 路径")
            base = (public_base or self.base).rstrip("/")
            p = remote_path if remote_path.startswith("/") else "/" + remote_path
            return f"{base}/d{p}"
        return raw


# ---------- manifest 读写 ----------

def read_csv_rows() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_rows(rows: list[dict]) -> None:
    if not rows:
        return
    fieldnames = ["id", "name", "version", "size", "category", "description", "icon", "url", "tags"]
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def upsert_row(rows: list[dict], new_row: dict) -> bool:
    for i, r in enumerate(rows):
        if r.get("id") == new_row["id"]:
            rows[i].update(new_row)
            return False
    rows.append(new_row)
    return True


# ---------- 主流程 ----------

def resolve_remote_path(client: OpenList, token: str, wanted: str) -> str:
    """
    如果 wanted 路径在 OpenList 里是 storage（即 /{name} 是已挂载存储根），直接用；
    否则尝试列出根目录，挑第一个 storage 兜底。
    """
    try:
        client._post("/api/fs/mkdir", {"path": wanted}, token=token)
        return wanted
    except RuntimeError as e:
        msg = str(e)
        if "storage not found" in msg:
            # 兜底：列根目录
            log("WARN", f"  {wanted} 不是已挂载的 storage，自动选根目录第一个目录")
            content = client.list(token, "/")
            for it in content:
                if it.get("is_dir"):
                    fallback = "/" + it["name"]
                    log("INFO", f"  改用: {fallback}")
                    return fallback
        raise


def process_one(client: OpenList, token: str, cfg: dict, local: Path, *,
                id_: str, name: str, version: str, category: str, description: str) -> None:
    log("INFO", f"• {local.name}  →  id={id_}")
    target_dir = resolve_remote_path(client, token, cfg["remote_path"])
    info = client.upload(token, local, target_dir)
    url = client.get_url(token, info["path"], cfg["use_proxy"], cfg["public_base"])
    size_str = human_size(info["size"])
    log("OK  ", f"  上传完成: {info['path']} ({size_str})")
    log("OK  ", f"  下载 URL: {url}")

    rows = read_csv_rows()
    new_row = {
        "id": id_,
        "name": name,
        "version": version or "-",
        "size": size_str,
        "category": category or "工具",
        "description": description or "",
        "icon": f"./icons/{id_}.png",
        "url": url,
        "tags": "",
    }
    is_new = upsert_row(rows, new_row)
    write_csv_rows(rows)
    log("OK  ", f"  manifest.csv 已{'新增' if is_new else '更新'}: {id_}")


def process_inbox(client: OpenList, token: str, cfg: dict) -> None:
    if not INBOX_DIR.exists():
        INBOX_DIR.mkdir(exist_ok=True)
        log("WARN", f"已创建 {INBOX_DIR.name}/，把待上传的 APK 丢进去再跑")
        return
    apks = sorted(INBOX_DIR.glob("*.apk")) + sorted(INBOX_DIR.glob("*.zip"))
    if not apks:
        log("WARN", f"{INBOX_DIR.name}/ 是空的")
        return
    for apk in apks:
        stem = apk.stem
        id_ = re.sub(r"[^a-zA-Z0-9._-]", "", stem.lower())[:32] or "app"
        m = re.search(r"[_-]v?(\d+(?:\.\d+)+)", stem, flags=re.IGNORECASE)
        version = f"v{m.group(1)}" if m else "-"
        process_one(client, token, cfg, apk, id_=id_, name=stem,
                    version=version, category="工具", description="")


def main():
    p = argparse.ArgumentParser(description="DBStore 自动上传到 OpenList v4 并写回 manifest.csv")
    p.add_argument("apk", nargs="?", help="单个 APK 路径；不传则扫描 _inbox/")
    p.add_argument("--id", help="app id（英文/数字/_-.)")
    p.add_argument("--name", help="显示名")
    p.add_argument("--version", default="-", help="版本号")
    p.add_argument("--category", default="工具", help="分类")
    p.add_argument("--description", default="", help="描述")
    p.add_argument("--no-proxy", action="store_true", help="不走 OpenList 中转，拿 139 直链")
    p.add_argument("--save-config", action="store_true", help="把当前 CLI 参数写进 openlist.config.json")
    args = p.parse_args()

    cfg = load_config()
    if not cfg.get("password"):
        log("ERR ", "未配置密码。\n"
                    "  方式 A: 创建 openlist.config.json（推荐，已加 .gitignore）\n"
                    "  方式 B: 设置环境变量 OPENLIST_PASS\n"
                    "  方式 C: --password <明文>（不入文件）")
        sys.exit(1)
    if args.no_proxy:
        cfg["use_proxy"] = False

    log("INFO", f"登录 {cfg['base_url']} …")
    client = OpenList(cfg)
    try:
        token = client.login()
    except Exception as e:
        log("ERR ", f"登录失败: {e}")
        sys.exit(2)
    log("OK  ", "登录成功")

    if args.save_config:
        # 不保存密码明文
        safe = {k: v for k, v in cfg.items() if k != "password"}
        CONFIG_PATH.write_text(json.dumps(safe, indent=2, ensure_ascii=False), encoding="utf-8")
        log("OK  ", f"已写 {CONFIG_PATH.name}（不含 password，请用环境变量）")

    if args.apk:
        local = Path(args.apk).resolve()
        if not local.exists():
            log("ERR ", f"找不到文件: {local}")
            sys.exit(1)
        id_ = args.id or slugify(local.stem)
        name = args.name or local.stem
        process_one(client, token, cfg, local, id_=id_, name=name,
                    version=args.version, category=args.category, description=args.description)
    else:
        process_inbox(client, token, cfg)

    log("INFO", "下一步: python fetch_icons.py   # 补图标")
    log("INFO", "然后:   python build_apps_json.py  # 重新生成 apps.json")


if __name__ == "__main__":
    main()
