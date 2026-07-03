#!/usr/bin/env python3
# coding: utf-8
"""
sync_openlist_to_apps_json.py
=============================
读取 OpenList 根目录（递归子目录）下的所有 .apk/.zip，
为每个文件生成一条记录。

URL 模式（--url-mode / openlist.config.json: url_mode）：
  proxy    → Netlify Function（默认。车机点 /dl/<path>，Function 实时签发 sign）
  openlist → OpenList /d/<path>?sign=<fresh>（仅作为临时方案：sign 会过期）

合并策略：
  * 读取 OpenList 全部文件
  * 按文件名（去后缀）做 id，提取版本号
  * 如果 manifest.csv 存在，保留 csv 里的 description/category/icon/tags，
    仅用 OpenList 列表更新 url/size/version
  * OpenList 没有但 csv 独有的应用 → 保留（带 WARN 提示）
  * 同步写入 apps.json 和 apps_remote.json

依赖：pip install requests
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
CONFIG_PATH = HERE / "openlist.config.json"
JSON_PATH = HERE / "apps.json"
REMOTE_JSON_PATH = HERE / "apps_remote.json"
CSV_PATH = HERE / "manifest.csv"

STATIC_HASH_SALT = "https://github.com/alist-org/alist"

DEFAULT_CONFIG = {
    "base_url": "http://appstore.cnmlynk.org",
    "username": "xudabing",
    "password": "",
    "public_base": "http://appstore.cnmlynk.org",
    "use_proxy": True,
    "max_depth": 5,
    "default_category": "工具",
    # 部署 Netlify Function 后，把 netlify_base 改成你的 Netlify 域名
    "netlify_base": "https://dbstore.netlify.app",
    "url_mode": "proxy",  # proxy | openlist
}


# ---------- utils ----------

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


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return {
        **DEFAULT_CONFIG,
        "base_url": os.environ.get("OPENLIST_BASE", DEFAULT_CONFIG["base_url"]),
        "username": os.environ.get("OPENLIST_USER", DEFAULT_CONFIG["username"]),
        "password": os.environ.get("OPENLIST_PASS", ""),
    }


def static_hash(pwd: str) -> str:
    return hashlib.sha256(f"{pwd}-{STATIC_HASH_SALT}".encode()).hexdigest()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def encode_path(remote_path: str) -> str:
    """把 /DBStore/中文目录/文件.apk → /DBStore/%E4%B8%AD%E6%96%87/...apk"""
    p = remote_path if remote_path.startswith("/") else "/" + remote_path
    return "/".join(urllib.parse.quote(seg, safe="") for seg in p.split("/"))


def build_url(mode: str, cfg: dict, remote_path: str, sign: str = "") -> str:
    encoded = encode_path(remote_path)
    if mode == "proxy":
        base = (cfg.get("netlify_base") or "https://dbstore.netlify.app").rstrip("/")
        return f"{base}/dl{encoded}"
    base = (cfg.get("public_base") or cfg["base_url"]).rstrip("/")
    if sign:
        return f"{base}/d{encoded}?sign={urllib.parse.quote(sign, safe='')}"
    return f"{base}/d{encoded}"


# ---------- OpenList ----------

class OpenList:
    def __init__(self, base: str, user: str, pwd: str):
        self.base = base.rstrip("/")
        self.user = user
        self.pwd = pwd
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "DBStore-sync/1.0"

    def _post(self, path: str, body: dict, token: Optional[str] = None) -> dict:
        h = {}
        if token:
            h["Authorization"] = token
        r = self.s.post(self.base + path, json=body, headers=h, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(
                f"{path} 失败: code={data.get('code')} msg={data.get('message')}"
            )
        return data.get("data") or {}

    def login(self) -> str:
        d1 = self._post("/api/auth/login", {"username": self.user, "password": self.pwd})
        step1 = d1.get("token")
        d2 = self._post(
            "/api/auth/login/hash",
            {"username": self.user, "password": static_hash(self.pwd)},
            token=step1,
        )
        return d2.get("token")

    def list(self, token: str, path: str, refresh: bool = False) -> list[dict]:
        d = self._post("/api/fs/list", {"path": path, "refresh": refresh}, token=token)
        return d.get("content") or []

    def get_sign(self, token: str, path: str) -> str:
        d = self._post("/api/fs/get", {"path": path, "password": ""}, token=token)
        return d.get("sign") or ""


# ---------- 文件名解析 ----------

VERSION_RE = re.compile(r"[_-]?v?(\d+(?:\.\d+){1,3})", flags=re.IGNORECASE)

CATEGORY_RULES = [
    (r"高德|百度|腾讯.{0,4}地图|地图", "导航"),
    (r"汽水|QQ.{0,4}音乐|Apple.{0,4}Music|网易云|酷狗|酷我|音乐|music", "音乐"),
    (r"哔哩|bili|youku|爱奇艺|tencent.{0,4}video|视频", "视频"),
    (r"二刺|二次元", "娱乐"),
    (r"车机|carplay|hicar|hyapp|领克|lynk|lynktool|小八|智控|壁纸|wallpaper|启动器|车机助手|mt\s*管|es\s*文件|应用管家|水果互联|互传", "工具"),
]


def guess_category(filename: str) -> str:
    n = filename.lower()
    for pat, cat in CATEGORY_RULES:
        if re.search(pat, n, flags=re.IGNORECASE):
            return cat
    return "工具"


def parse_filename(filename: str) -> tuple[str, str]:
    stem = Path(filename).stem
    m = VERSION_RE.search(stem)
    if m:
        version = "v" + m.group(1)
        name = stem[:m.start()].rstrip("_- ")
        if not name:
            name = stem
    else:
        version = "-"
        name = stem
    return name.strip() or stem, version


def slugify_id(name: str) -> str:
    ascii_part = re.sub(r"[^A-Za-z0-9._-]+", "", name).strip("._-")
    if ascii_part:
        return ascii_part.lower()[:32]
    h = hashlib.md5(name.encode("utf-8")).hexdigest()[:6]
    return f"app_{h}"


# ---------- 主流程 ----------

def walk_apks(client: OpenList, token: str, root: str, max_depth: int) -> list[dict]:
    apks = []

    def dfs(path: str, depth: int):
        if depth > max_depth:
            return
        try:
            items = client.list(token, path)
        except Exception as e:
            log("WARN", f"  list {path} 失败: {e}")
            return
        for it in items:
            if it.get("is_dir"):
                dfs(f"{path.rstrip('/')}/{it['name']}", depth + 1)
            else:
                name = it.get("name", "")
                if name.lower().endswith(".apk"):  # 只拉 .apk，过滤掉 .zip/.apks/.xapk
                    apks.append({
                        "name": name,
                        "size": it.get("size", 0),
                        "path": f"{path.rstrip('/')}/{name}",
                        "parent": path,
                    })

    log("INFO", f"扫描 {root} (max_depth={max_depth}) …")
    dfs(root, 0)
    return apks


def build_records(apks: list[dict], cfg: dict, mode: str,
                  client: Optional[OpenList], token: Optional[str]) -> list[dict]:
    seen = set()
    records = []
    fetched_sign = 0
    for a in apks:
        display_name, version = parse_filename(a["name"])
        id_ = slugify_id(display_name)
        if id_ in seen:
            base_id = id_
            n = 2
            while f"{base_id}{n}" in seen:
                n += 1
            id_ = f"{base_id}{n}"
            log("WARN", f"  重名 id '{base_id}' → '{id_}' ({a['path']})")
        seen.add(id_)

        sign = ""
        if mode == "openlist" and client and token:
            try:
                sign = client.get_sign(token, a["path"])
                fetched_sign += 1
            except Exception as e:
                log("WARN", f"  get_sign 失败 {a['path']}: {e}")

        url = build_url(mode, cfg, a["path"], sign)
        records.append({
            "id": id_,
            "name": display_name,
            "version": version,
            "size": human_size(a["size"]),
            "category": guess_category(display_name),
            "description": f"同步自 OpenList: {a['path']}",
            "icon": f"./icons/{id_}.png",
            "url": url,
            "tags": [],
        })
    if mode == "openlist":
        log("OK", f"已为 {fetched_sign}/{len(apks)} 条取到 sign（注意：sign 几小时内过期）")
    return records


def merge_with_csv(records: list[dict]) -> list[dict]:
    if not CSV_PATH.exists():
        return records
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        csv_rows = list(csv.DictReader(f))
    by_id_csv = {r.get("id"): r for r in csv_rows if r.get("id")}

    merged = []
    seen_ids = set()
    for rec in records:
        rid = rec["id"]
        old = by_id_csv.get(rid)
        if old:
            rec = {
                **rec,
                "name": old.get("name") or rec["name"],
                "category": old.get("category") or rec["category"],
                "description": old.get("description") or rec["description"],
                "icon": old.get("icon") or rec["icon"],
                "tags": [t for t in (old.get("tags") or "").split(",") if t] or [],
                "version": rec["version"] if rec["version"] != "-" else old.get("version", "-"),
                "size": rec["size"] if rec["size"] else old.get("size", ""),
            }
        merged.append(rec)
        seen_ids.add(rid)

    for cid, old in by_id_csv.items():
        if cid not in seen_ids:
            keep = {
                "id": cid,
                "name": old.get("name", cid),
                "version": old.get("version", "-"),
                "size": old.get("size", ""),
                "category": old.get("category", "工具"),
                "description": old.get("description", ""),
                "icon": old.get("icon", f"./icons/{cid}.png"),
                "url": old.get("url", ""),
                "tags": [t for t in (old.get("tags") or "").split(",") if t],
            }
            merged.append(keep)
            log("WARN", f"  OpenList 未找到 '{cid}'，保留 manifest.csv 原记录")

    return merged


def write_json(records: list[dict], path: Path) -> None:
    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log("OK  ", f"已写 {path.name}  ({len(records)} 条, {path.stat().st_size/1024:.1f} KB)")


def main():
    ap = argparse.ArgumentParser(description="把 OpenList 文件列表同步到 apps.json（不下载 APK）")
    ap.add_argument("--root", default="/", help="扫描根目录，默认 /")
    ap.add_argument("--max-depth", type=int, default=None, help="递归深度（默认 5）")
    ap.add_argument("--no-merge", action="store_true", help="不合并 manifest.csv，直接用 OpenList 覆盖")
    ap.add_argument("--url-mode", choices=["proxy", "openlist"], default=None,
                    help="proxy=Netlify Function 永久地址（默认）；openlist=OpenList 直签（临时）")
    ap.add_argument("--dry-run", action="store_true", help="只打印预览，不写文件")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.get("password"):
        log("ERR ", "未配置密码。请在 openlist.config.json 配 password 或设置 OPENLIST_PASS")
        sys.exit(1)
    if args.max_depth is not None:
        cfg["max_depth"] = args.max_depth
    mode = args.url_mode or cfg.get("url_mode", "proxy")

    log("INFO", f"登录 {cfg['base_url']} …")
    client = OpenList(cfg["base_url"], cfg["username"], cfg["password"])
    try:
        token = client.login()
    except Exception as e:
        log("ERR ", f"登录失败: {e}")
        sys.exit(2)
    log("OK  ", "登录成功")

    apks = walk_apks(client, token, args.root, cfg.get("max_depth", 5))
    if not apks:
        log("WARN", f"{args.root} 下没有任何 APK/ZIP")
        return
    log("OK  ", f"发现 {len(apks)} 个文件")

    records = build_records(apks, cfg, mode, client, token)
    if not args.no_merge:
        records = merge_with_csv(records)

    print()
    log("INFO", f"url_mode={mode}，共 {len(records)} 条，预览前 10 条:")
    for r in records[:10]:
        size = r.get("size", "") or "-"
        ver = r.get("version", "-")
        print(f"  [{r.get('category','-'):>4}] {r.get('name',''):<28} {ver:<10} {size:<10} {r.get('url','')[:90]}")
    if len(records) > 10:
        print(f"  ... 还有 {len(records)-10} 条")

    if args.dry_run:
        log("INFO", "dry-run，不写文件")
        return

    write_json(records, JSON_PATH)
    write_json(records, REMOTE_JSON_PATH)
    if mode == "proxy":
        log("OK  ", f"完成。链接走 Netlify Function: {cfg.get('netlify_base')}/dl/<path>")
    else:
        log("WARN", f"完成。但 url_mode=openlist，sign 几小时后会过期，建议改用 --url-mode proxy")
    log("OK  ", "下一步: python fetch_icons.py   # 补图标")


if __name__ == "__main__":
    main()
