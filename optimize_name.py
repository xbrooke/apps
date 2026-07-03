#!/usr/bin/env python3
# coding: utf-8
"""
optimize_name.py
================
清理 apps.json 里过长的 name：
  * 剥垃圾字符（【】、✅、[]、「」、...）
  * 剥修饰括号片段（(HUD+方控)、By.Cheter.Chao、...
  * 剥"牛逼版/定制版/最新版"等修饰
  * 长内容存到 description（保留原 name 作 desc 末尾）
  * name 截到 MAX_LEN 以内（默认 18）

策略：
  * name 短而干净（≤18 字符）→ 适合车机列表展示
  * description 含原长 name + 路径 + 简短说明 → 车机详情页可看
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "apps.json"
REMOTE_JSON_PATH = HERE / "apps_remote.json"

MAX_NAME_LEN = 18

MODIFIER_PATTERNS = [
    r"【[^】]*】",
    r"☐[^☐]*☐",
    r"〔[^〕]*〕",
    r"「[^」]*」",
    r"『[^』]*』",
    r"\[[^\]]{1,50}\]",
    r"✅+",
    r"✓+",
    r"^_+|_+$",
    r"^-+|-+$",
    r"^\.+|\.+$",
]

TRIM_SUFFIX_PATTERNS = [
    r"[（(]HUD[+＋][^）)]*[）)]",
    r"[（(]适配OSN[）)]",
    r"[（(]适配[^）)]*[）)]",
    r"[（(]车友定制[^）)]*[）)]",
    r"[（(]知阳推荐[）)]",
    r"[（(]作者定制[^）)]*[）)]",
    r"_?By\.?[A-Za-z][A-Za-z0-9._-]*-?",
    r"-?By\.?[A-Za-z][A-Za-z0-9._-]*",
    r"_\d{8,}$",  # 末尾的 _20240615
]

NORMALIZE_PATTERNS = [
    (re.compile(r"V(\d+(?:\.\d+)+)第[一二三四五六七八九十0-9]+版", re.I), r"V\1"),
    (re.compile(r"牛逼版", re.I), ""),
    (re.compile(r"定制版", re.I), ""),
    (re.compile(r"最新版", re.I), ""),
    (re.compile(r"[\s_]+V(\d)", re.I), r" V\1"),
    (re.compile(r"_(\d+\.\d+)"), r" V\1"),
]


def clean_name(raw: str) -> tuple[str, list[str]]:
    """返回 (cleaned_name, removed_tokens)"""
    s = raw
    removed = []
    for pat in MODIFIER_PATTERNS:
        prev = None
        while s != prev:
            prev = s
            m = re.search(pat, s)
            if m:
                removed.append(m.group(0).strip(" _-·•·"))
                s = s[:m.start()] + " " + s[m.end():]
    for pat in TRIM_SUFFIX_PATTERNS:
        m = re.search(pat, s)
        if m:
            removed.append(m.group(0).strip(" _-·•·"))
            s = s[:m.start()] + s[m.end():]
    for pat, rep in NORMALIZE_PATTERNS:
        s = pat.sub(rep, s)
    s = re.sub(r"\s+", " ", s).strip(" _-·•·,，")
    if len(s) > MAX_NAME_LEN:
        truncated = s[:MAX_NAME_LEN]
        sp = truncated.rfind(" ")
        if sp >= MAX_NAME_LEN * 0.6:
            truncated = truncated[:sp]
        s = truncated.rstrip(" _-·•·,，") + "…"
    return s, removed


def build_description(old_desc: str, old_name: str, removed: list[str], path: str) -> str:
    """
    把剥掉的修饰语整理成 description。格式：
      <原 name>
      <剥掉的修饰片段，逗号分隔>
      路径：<path>
    """
    parts = []
    if old_name and old_name.strip():
        parts.append(f"原名：{old_name.strip()}")
    useful_removed = [t for t in removed if t and t not in ("…", "[TRUNC]", "TRUNC") and len(t) >= 2]
    if useful_removed:
        parts.append("特征：" + "、".join(useful_removed[:6]))
    if path and path not in (old_desc or ""):
        parts.append(f"路径：{path}")
    new_desc = "\n".join(parts)
    # 如果原 desc 已有内容且新内容不同，附加在后面
    if old_desc and old_desc != new_desc and not old_desc.startswith("同步自 OpenList:"):
        new_desc = new_desc + "\n\n" + old_desc
    return new_desc or old_desc


def main():
    if not JSON_PATH.exists():
        print(f"找不到 {JSON_PATH}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    print(f"读入 {len(data)} 条")
    changed = 0
    for r in data:
        old = r.get("name", "")
        new, removed = clean_name(old)
        if not new:
            continue
        if new != old:
            # 推断原 path（从 url 里反推）
            url = r.get("url", "")
            path = ""
            if "/dl/" in url:
                from urllib.parse import unquote
                path = unquote(url.split("/dl/", 1)[-1].split("?")[0])
            elif "/d/" in url:
                from urllib.parse import unquote
                path = unquote(url.split("/d/", 1)[-1].split("?")[0])
            r["name"] = new
            r["description"] = build_description(r.get("description", ""), old, removed, path)
            changed += 1
    print(f"清理 {changed} 条")

    JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    REMOTE_JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已写 {JSON_PATH.name} + {REMOTE_JSON_PATH.name}")


if __name__ == "__main__":
    main()
