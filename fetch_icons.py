#!/usr/bin/env python3
# coding: utf-8
"""
fetch_icons.py
==============
根据 apps/manifest.csv 里的 name 字段，从 iTunes Search API 检索图标，
下载到 apps/icons/<id>.png，自动校验/转换格式。

原理：
    icon.yukonga.top (HQ-ICON) 本质上是 iTunes Search API 的一个前端壳子：
        GET https://itunes.apple.com/search?term={name}&country={cn}&entity=software&limit={n}
    返回 JSON 里 artworkUrl512 是 512x512 原始图标链接，把其中
    512x512bb.{jpg,png} 替换成 1024x1024bb.png 就能拿到高清水印图。

特性：
    * 按 id 命名落盘，与 manifest.csv / apps.json 里的 icon 路径保持一致
    * 仅当本地缺失或 --force 时才下载（默认 --only-missing）
    * 智能匹配：搜索 term 自动尝试多个候选（name 原名 / 去后缀 / 英文候选）
    * 下载后用 Pillow 校验/转码/裁切为正方形（不强制依赖 Pillow，无则跳过转码）
    * 失败时打印清晰的诊断信息，不中断整个流程
    * 完成后输出"建议回填到 manifest.csv 的 icon 路径"

用法：
    python fetch_icons.py                    # 仅补缺失的
    python fetch_icons.py --all              # 强制重新下载全部
    python fetch_icons.py --only-missing     # 等价于不带参数
    python fetch_icons.py --country us       # 切到美区
    python fetch_icons.py --size 1024        # 下载 1024x1024 高清水印图
    python fetch_icons.py --id qishui         # 只处理指定 id
    python fetch_icons.py --dry-run          # 只打印计划，不真下载
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "manifest.csv"
JSON_PATH = HERE / "apps.json"
ICONS_DIR = HERE / "icons"
ICONS_DIR.mkdir(exist_ok=True)

ITUNES_SEARCH = "https://itunes.apple.com/search"

# ---------- 工具 ----------

def log(level: str, msg: str) -> None:
    color = {
        "INFO": "\033[36m", "WARN": "\033[33m",
        "ERR ": "\033[31m", "OK  ": "\033[32m",
    }.get(level, "")
    reset = "\033[0m" if color else ""
    # Windows 终端默认 GBK，强制 UTF-8 输出避免中文/特殊符号报错
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"{color}[{level}]{reset} {msg}", flush=True)


def http_get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (DBStore-fetch_icons/1.0)"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_download(url: str, dst: Path, timeout: int = 30) -> int:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (DBStore-fetch_icons/1.0)"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    dst.write_bytes(data)
    return len(data)


# ---------- 图标 URL 转换 ----------

def bump_artwork_url(url: str, size: int) -> str:
    """
    把 artworkUrl512 里的 512x512bb.{jpg,png}
    替换成 {size}x{size}bb.png，拿到更高清水印图。

    输入示例:
        https://is1-ssl.mzstatic.com/image/thumb/.../512x512bb.jpg
        https://is1-ssl.mzstatic.com/image/thumb/.../512x512bb.png
    输出示例(size=1024):
        https://is1-ssl.mzstatic.com/image/thumb/.../1024x1024bb.png
    """
    # 同时处理 jpg / png 后缀
    new = re.sub(r"/\d+x\d+bb\.(?:jpg|png)$", f"/{size}x{size}bb.png", url, flags=re.IGNORECASE)
    return new


# ---------- 搜索 term 候选生成 ----------

# 常见的车机版 / 魔改版 / 第三方变体关键词，可按需扩
KEYWORD_DROPS = [
    "车机版", "车机", "车载版", "车载", "魔改版", "魔改",
    "免会员版", "破解版", "VIP版", "Pro版", "Pro",
    "去广告版", "增强版", "定制版",
    "TV版", "Pad版",
    "小八推荐", "小八智控", "小八", "智控",
    "LynkCo", "Lynk", "领克",
    "吉利", "OSN", "OSN车机",
]


def gen_search_terms(name: str) -> list[str]:
    """
    给出 name 后，产出多个搜索 term 候选（按优先级）。
    iTunes Search 是模糊匹配 + 排序算法，不一定完全等于名字就能命中第一，
    试试简化后的版本常常更稳。
    """
    candidates = []
    seen = set()

    def add(t: str):
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            candidates.append(t)

    add(name)  # 原名

    # 去掉关键词后缀
    stripped = name
    for kw in KEYWORD_DROPS:
        stripped = stripped.replace(kw, "")
    stripped = stripped.strip()
    if stripped and stripped != name:
        add(stripped)

    # 去掉括号内容：例如 "MT管理器(v2.14.5)" → "MT管理器"
    no_paren = re.sub(r"[（(].*?[)）]", "", name).strip()
    if no_paren and no_paren != name:
        add(no_paren)

    # 去掉【xxx】前缀（车友圈推荐前缀）
    no_bracket = re.sub(r"^【[^】]*】", "", name).strip()
    if no_bracket and no_bracket != name:
        add(no_bracket)

    # 去掉 - 后面的人名/作者/后缀
    if "-" in no_bracket:
        left = no_bracket.split("-", 1)[0].strip()
        if left and left != no_bracket:
            add(left)

    return candidates


# ---------- 匹配 + 下载 ----------

def search_icon(term: str, country: str, limit: int = 5) -> str | None:
    """返回该 term 命中的最佳 artworkUrl（或 None）"""
    params = {"term": term, "country": country, "entity": "software", "limit": limit}
    url = f"{ITUNES_SEARCH}?{urllib.parse.urlencode(params)}"
    try:
        data = http_get_json(url)
    except Exception as e:
        log("WARN", f"  搜索失败: {term!r} -> {e}")
        return None

    results = data.get("results", []) or []
    if not results:
        return None

    # 取首个有 artworkUrl512 的
    for r in results:
        art = r.get("artworkUrl512")
        if art:
            return art
    return None


def fetch_one(app: dict, *, country: str, size: int, force: bool, dry_run: bool) -> str:
    """
    处理单个应用：返回最终图标相对路径（写进 manifest 的 icon 字段）。
    """
    aid = app["id"]
    name = app["name"]
    target = ICONS_DIR / f"{aid}.png"
    relpath = f"./icons/{aid}.png"

    if target.exists() and not force:
        log("INFO", f"  ↳ 跳过（已存在）: {relpath}")
        return relpath

    if dry_run:
        log("INFO", f"  ↳ [DRY] 将处理 {name!r}")
        return relpath

    # 多个候选 term 依次尝试
    art = None
    used_term = None
    for term in gen_search_terms(name):
        log("INFO", f"  ↳ 搜索 {term!r} (country={country}) …")
        art = search_icon(term, country=country)
        if art:
            used_term = term
            break
        time.sleep(0.3)  # 礼貌限速

    if not art:
        log("WARN", f"  ↳ 未找到 {name!r} 的图标，请手动到 https://icon.yukonga.top/?name={urllib.parse.quote(name)} 选一个")
        return ""

    high_res = bump_artwork_url(art, size=size)
    log("INFO", f"  ↳ 命中 term={used_term!r}, artworkUrl = {high_res}")

    try:
        size_bytes = http_download(high_res, target)
    except Exception as e:
        log("ERR ", f"  ↳ 下载失败: {e}")
        return ""

    log("OK  ", f"  ↳ 已保存 {relpath} ({size_bytes/1024:.1f} KB)")

    # 尝试用 Pillow 校验/转码为 PNG（保证后缀统一、文件可用）
    try:
        from PIL import Image
        with Image.open(target) as im:
            im.load()
            # 已是 PNG 且 size=目标 size 就不动；否则规范化
            if im.format != "PNG" or im.size[0] != size or im.size[1] != size:
                # 居中裁切为正方形
                w, h = im.size
                side = min(w, h)
                left = (w - side) // 2
                top = (h - side) // 2
                im = im.crop((left, top, left + side, top + side))
                im = im.resize((size, size), Image.LANCZOS)
                im.save(target, format="PNG", optimize=True)
                log("INFO", f"    已规范化: {size}x{size} PNG")
    except ImportError:
        log("WARN", "    未安装 Pillow，跳过格式校验（Pillow 是可选依赖）")
    except Exception as e:
        log("WARN", f"    Pillow 处理失败（仍保留下载原图）: {e}")

    return relpath


# ---------- 主流程 ----------

def load_apps() -> list[dict]:
    """
    优先读 apps.json（包含 OpenList 同步来的所有应用），
    读不到再回退 manifest.csv。
    """
    if JSON_PATH.exists():
        try:
            apps = json.loads(JSON_PATH.read_text(encoding="utf-8"))
            log("INFO", f"读 {JSON_PATH.name}，{len(apps)} 条")
            return apps
        except Exception as e:
            log("WARN", f"读 {JSON_PATH.name} 失败: {e}，回退 csv")

    if not CSV_PATH.exists():
        log("ERR ", f"找不到 {JSON_PATH.name} 也不到 {CSV_PATH.name}")
        sys.exit(1)
    apps = []
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            apps.append({(k or "").strip().lower(): (v or "").strip()
                         for k, v in row.items() if k})
    log("INFO", f"读 {CSV_PATH.name}，{len(apps)} 条")
    return apps


def main():
    p = argparse.ArgumentParser(description="DBStore 图标自动下载工具")
    p.add_argument("--all", action="store_true", help="强制重新下载全部（覆盖已有）")
    p.add_argument("--only-missing", action="store_true", help="只补缺失的（默认行为）")
    p.add_argument("--id", action="append", default=[], help="只处理指定 id（可多次传）")
    p.add_argument("--country", default="cn", help="iTunes 地区码，默认 cn")
    p.add_argument("--size", type=int, default=512, choices=[256, 512, 1024],
                   help="下载尺寸，默认 512")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不下载")
    args = p.parse_args()

    force = args.all

    apps = load_apps()
    if not apps:
        log("WARN", "没有任何应用记录")
        return

    if args.id:
        apps = [a for a in apps if a["id"] in args.id]
        if not apps:
            log("WARN", f"--id 过滤后无匹配: {args.id}")
            return

    log("INFO", f"共 {len(apps)} 个应用待处理 (country={args.country}, size={args.size}, force={force})")
    ICONS_DIR.mkdir(exist_ok=True)

    results = []
    for a in apps:
        log("INFO", f"• {a['id']}  {a['name']}")
        rel = fetch_one(a, country=args.country, size=args.size,
                        force=force, dry_run=args.dry_run)
        results.append((a["id"], rel))

    ok = sum(1 for _, r in results if r)
    fail = len(results) - ok
    log("OK  ", f"完成: {ok} 成功, {fail} 失败")

    if fail:
        log("INFO", "失败的应用，可在浏览器手动取图标：")
        for aid, rel in results:
            if not rel:
                a = next(x for x in apps if x["id"] == aid)
                log("INFO", f"  https://icon.yukonga.top/?name={urllib.parse.quote(a['name'])}&country={args.country}")

    log("INFO", "完成后请执行: python build_apps_json.py 重新生成 apps.json")


if __name__ == "__main__":
    main()
