#!/usr/bin/env python3
# coding: utf-8
"""
build_apps_json.py
==================
DBStore 远程应用商店的批量入库脚本。

数据源：apps/manifest.csv  (UTF-8, 带表头)
输出：  apps/apps.json     (主数据源)
        apps/apps_remote.json (远端副本，与主数据源一致)

CSV 列（顺序固定，列名不区分大小写）：
    id          必填，URL-safe 英文/数字，作为应用唯一标识
    name        必填，显示名
    version     选填，默认 "-"
    size        选填，文本，如 "5.87 MB"
    category    选填，默认 "工具"
    description 选填，默认 ""
    icon        选填，相对路径，如 "./icons/foo.png"；留空则用 fallback 图标
    url         必填，远程 APK 链接（http/https），或本地相对路径 "./apps/foo.apk"
    tags        选填，英文逗号分隔，如 "热门,最新,编辑推荐,必备,免费"

特性：
    * 自动去重（按 id）
    * 自动校验 URL 协议（必须是 http/https/相对路径）
    * 自动校验 icon 文件存在（相对路径时）
    * 自动补全默认字段
    * 输出格式化的 JSON（UTF-8、无 backslash-u 转义、尾部 2 空格缩进）

用法：
    python build_apps_json.py                 # 读 manifest.csv → 写 apps.json + apps_remote.json
    python build_apps_json.py --check         # 只校验，不写文件
    python build_apps_json.py --from-json     # 从现有 apps.json 导出为 manifest.csv（首次迁移用）
"""
import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

# 路径常量（脚本放在 apps/ 目录下）
HERE = Path(__file__).resolve().parent
APPS_DIR = HERE
CSV_PATH = APPS_DIR / "manifest.csv"
JSON_PATH = APPS_DIR / "apps.json"
REMOTE_JSON_PATH = APPS_DIR / "apps_remote.json"
ICONS_DIR = APPS_DIR / "icons"

# 合法 tags
VALID_TAGS = {"热门", "最新", "编辑推荐", "必备", "免费"}

# id 合法字符：英文、数字、点、下划线、连字符
ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# 合法 URL 前缀
URL_RE = re.compile(r"^(https?:|file:|\./|\../|/[^/])")


def log(level: str, msg: str) -> None:
    color = {"INFO": "\033[36m", "WARN": "\033[33m", "ERR ": "\033[31m", "OK  ": "\033[32m"}.get(level, "")
    reset = "\033[0m" if color else ""
    print(f"{color}[{level}]{reset} {msg}")


def normalize_size(size: str) -> str:
    """规范 size 字段：去多空格、统一大写单位（保留数字与单位之间恰好 1 个空格）"""
    if not size:
        return "-"
    s = size.strip()
    s = re.sub(r"\s+", " ", s)  # 折叠多空格为单空格
    # 数字与单位之间：可能没空格也可能已有空格，统一为恰好 1 个空格
    s = re.sub(r"\s*([KMGT]B)\b", r" \1", s, flags=re.IGNORECASE)
    s = re.sub(r"\s{2,}", " ", s)  # 防御性：把两次替换叠加产生的双空格压回 1
    return s.upper().strip()


def load_csv() -> list[dict]:
    """读取 manifest.csv，返回 list[dict]"""
    if not CSV_PATH.exists():
        log("ERR ", f"找不到 {CSV_PATH}")
        sys.exit(1)

    apps = []
    seen_ids = set()
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        # 列名归一化（小写、去空格）
        rows = []
        for row in reader:
            norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}
            rows.append(norm)

        if not rows:
            log("WARN", "manifest.csv 是空的，将生成空 apps.json")
            return []

        # 校验必填列
        required = {"id", "name", "url"}
        first_keys = set(rows[0].keys())
        missing = required - first_keys
        if missing:
            log("ERR ", f"manifest.csv 缺少必填列：{sorted(missing)}")
            sys.exit(1)

        for i, row in enumerate(rows, start=2):  # start=2 算上表头
            app_id = row.get("id", "")
            name = row.get("name", "")
            url = row.get("url", "")

            # 必填校验
            if not app_id:
                log("WARN", f"第 {i} 行：id 为空，跳过")
                continue
            if not ID_RE.match(app_id):
                log("WARN", f"第 {i} 行：id='{app_id}' 含非法字符（仅允许英文/数字/_-.)，跳过")
                continue
            if not name:
                log("WARN", f"第 {i} 行：name 为空，跳过")
                continue
            if not url:
                log("WARN", f"第 {i} 行：url 为空，跳过")
                continue
            if not URL_RE.match(url):
                log("WARN", f"第 {i} 行：url='{url}' 协议非法（仅允许 http/https/相对路径），跳过")
                continue

            # 去重
            if app_id in seen_ids:
                log("WARN", f"第 {i} 行：id='{app_id}' 重复，跳过")
                continue
            seen_ids.add(app_id)

            # 解析 tags
            tags_raw = row.get("tags", "")
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
            invalid_tags = set(tags) - VALID_TAGS
            if invalid_tags:
                log("WARN", f"第 {i} 行：id='{app_id}' 含未识别的 tag {invalid_tags}（已过滤）")
                tags = [t for t in tags if t in VALID_TAGS]

            # icon 校验
            icon = row.get("icon", "").strip()
            if icon and icon.startswith("./"):
                icon_path = (APPS_DIR / icon[2:]).resolve()
                if not icon_path.exists():
                    log("WARN", f"第 {i} 行：id='{app_id}' 的 icon='{icon}' 文件不存在，已置空")
                    icon = ""

            apps.append({
                "id": app_id,
                "name": name,
                "url": url,
                "size": normalize_size(row.get("size", "")),
                "icon": icon,
                "version": row.get("version", "").strip() or "-",
                "category": row.get("category", "").strip() or "工具",
                "description": row.get("description", "").strip(),
                "tags": tags,
            })

    return apps


def write_json(apps: list[dict], path: Path) -> None:
    """写格式化 JSON（UTF-8、ensure_ascii=False、缩进 2）"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(apps, f, ensure_ascii=False, indent=2)
        f.write("\n")
    size_kb = path.stat().st_size / 1024
    log("OK  ", f"已写入 {path.name}  ({len(apps)} 个应用, {size_kb:.1f} KB)")


def export_from_json() -> None:
    """从现有 apps.json 导出为 manifest.csv（首次迁移用）"""
    if not JSON_PATH.exists():
        log("ERR ", f"找不到 {JSON_PATH}，无法导出")
        sys.exit(1)
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        apps = json.load(f)

    fieldnames = ["id", "name", "version", "size", "category", "description", "icon", "url", "tags"]
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for a in apps:
            writer.writerow({
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "version": a.get("version", "-"),
                "size": a.get("size", ""),
                "category": a.get("category", "工具"),
                "description": a.get("description", ""),
                "icon": a.get("icon", ""),
                "url": a.get("url", ""),
                "tags": ",".join(a.get("tags", []) or []),
            })
    log("OK  ", f"已导出 {len(apps)} 个应用到 {CSV_PATH.name}")


def main():
    parser = argparse.ArgumentParser(description="DBStore 批量入库工具")
    parser.add_argument("--check", action="store_true", help="只校验 manifest.csv，不写文件")
    parser.add_argument("--from-json", action="store_true", help="从 apps.json 导出 manifest.csv（首次迁移用）")
    args = parser.parse_args()

    if args.from_json:
        export_from_json()
        return

    apps = load_csv()

    # 统计
    cats = {}
    for a in apps:
        cats[a["category"]] = cats.get(a["category"], 0) + 1
    log("INFO", f"读取 {len(apps)} 个应用")
    for c, n in sorted(cats.items(), key=lambda x: -x[1]):
        log("INFO", f"  · {c}: {n}")

    if args.check:
        log("OK  ", "校验通过，未写文件")
        return

    write_json(apps, JSON_PATH)
    write_json(apps, REMOTE_JSON_PATH)


if __name__ == "__main__":
    main()
