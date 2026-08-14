#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 apps.json 中的应用名统一为"英文名 + 版本号"，
并把服务器 /opt/dbdns/static/downloads/ 下的 APK 文件名也同步重命名为英文+版本号。
"""
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path, PurePosixPath

import paramiko
from pypinyin import lazy_pinyin

BASE = Path(__file__).parent
APPS_JSON = BASE / "apps.json"

SERVER_HOST = "apps.sosun.cc"
SERVER_USER = "root"
SERVER_PASSWORD = os.environ.get("ROOT_PASSWORD", "")
SERVER_DOWNLOAD_DIR = "/opt/dbdns/static/downloads"
SERVER_BASE_URL = f"http://{SERVER_HOST}/static/downloads"

NOISE_SUFFIXES = [
    r"官方车机版",
    r"车机版",
    r"车机助手",
    r"适配版",
    r"修复版",
    r"正式版",
    r"专区",
    r"小八推荐",
    r"\(.*?\)",
    r"（.*?）",
    r"_.*",
    r"-.*",
    r"v\d+(\.\d+)*",
    r"V\d+",
    r"\d+款\d+.*",
]

NOISE_PREFIXES = [
    r"推荐_?",
    r"\[.*?\]",
]


def safe_print(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "ignore").decode("ascii", "ignore"))


def clean_name(name: str) -> str:
    text = name.strip()
    for p in NOISE_PREFIXES:
        text = re.sub(p, "", text)
    for s in NOISE_SUFFIXES:
        text = re.sub(s, "", text)
    return text.strip()


def is_english_name(name: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", name)
    return len(letters) >= 2 and re.match(r"^[A-Za-z0-9 _\-\(\)\.]+$", name)


def to_english_name(name: str) -> str:
    if is_english_name(name):
        return name.strip()

    cleaned = clean_name(name)
    cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9 ]+", " ", cleaned)

    if re.match(r"^[A-Za-z0-9 ]+$", cleaned.strip()):
        return cleaned.strip()

    pinyins = lazy_pinyin(cleaned)
    return "".join(p.capitalize() for p in pinyins if p)


def normalize_version(version: str) -> str:
    v = (version or "").strip()
    if not v or v == "-":
        return ""
    v = re.sub(r"^[vV]+", "", v)
    return f"v{v}"


def build_english_name(raw_name: str, version: str) -> str:
    raw_name = raw_name.strip()
    version = normalize_version(version)

    # 去掉原名称里的版本号
    name_no_ver = re.sub(r"\s*[vV]\d+.*$", "", raw_name).strip()
    english = to_english_name(name_no_ver)

    if len(english) > 28:
        abbr = "".join(re.findall(r"[A-Z]", english))
        if len(abbr) >= 2:
            english = abbr

    if version:
        return f"{english} {version}"
    return english


def make_safe_filename(name_part: str, version: str, ext: str) -> str:
    base = name_part.replace(" ", "")
    if version:
        base = f"{base}_{version}"
    base = re.sub(r"[^A-Za-z0-9_\-]", "", base)
    return f"{base}{ext}"


def main():
    if not SERVER_PASSWORD:
        safe_print("错误：请先设置 ROOT_PASSWORD 环境变量")
        sys.exit(1)

    with open(APPS_JSON, "r", encoding="utf-8") as f:
        apps = json.load(f)

    # 更新 name 和 version
    for app in apps:
        app["name"] = build_english_name(app.get("name", ""), app.get("version", ""))
        app["version"] = normalize_version(app.get("version", ""))

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(SERVER_HOST, username=SERVER_USER, password=SERVER_PASSWORD, timeout=20)
    sftp = ssh.open_sftp()

    renamed = 0
    updated = 0

    for app in apps:
        url = app.get("url", "")
        if not url.startswith(SERVER_BASE_URL):
            continue

        old_filename = urllib.parse.unquote(PurePosixPath(url).name)
        ext = Path(old_filename).suffix or ".apk"

        name_part = app["name"].split(" v")[0].split(" V")[0]
        version = app.get("version", "")
        new_filename = make_safe_filename(name_part, version, ext)

        # 避免重名，如果冲突则追加编号
        counter = 1
        final_filename = new_filename
        while final_filename != old_filename:
            try:
                sftp.stat(f"{SERVER_DOWNLOAD_DIR}/{final_filename}")
                # 文件已存在且不是当前文件
                stem = Path(new_filename).stem
                ext = Path(new_filename).suffix
                final_filename = f"{stem}_{counter}{ext}"
                counter += 1
            except FileNotFoundError:
                break

        if old_filename == final_filename:
            safe_print(f"跳过 {old_filename}")
            continue

        old_path = f"{SERVER_DOWNLOAD_DIR}/{old_filename}"
        new_path = f"{SERVER_DOWNLOAD_DIR}/{final_filename}"

        try:
            sftp.rename(old_path, new_path)
            safe_print(f"重命名: {old_filename} -> {final_filename}")
            renamed += 1
        except FileNotFoundError:
            safe_print(f"服务器上不存在: {old_path}")
            continue
        except Exception as e:
            safe_print(f"重命名失败 {old_filename}: {e}")
            continue

        app["url"] = f"{SERVER_BASE_URL}/{final_filename}"
        updated += 1

    sftp.close()
    ssh.close()

    with open(APPS_JSON, "w", encoding="utf-8") as f:
        json.dump(apps, f, ensure_ascii=False, indent=2)

    safe_print(f"\n完成：重命名 {renamed} 个文件，更新 {updated} 条 URL，共 {len(apps)} 个应用")


if __name__ == "__main__":
    main()
