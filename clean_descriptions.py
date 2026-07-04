#!/usr/bin/env python3
# coding: utf-8
"""
clean_descriptions.py
=====================
清理 apps.json / apps_remote.json 里 description 中的「同步自 OpenList: /CarMax/分类/子分类/」前缀。

只处理首行（最常见的同步导入格式），保留后续的"（已移除的低版本：...）"等备注。
其他非匹配行原样保留。
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "apps.json"
REMOTE_JSON_PATH = HERE / "apps_remote.json"

# 匹配 "同步自 OpenList: /CarMax/<分类>[/子分类]/<文件名>.apk"
# - 前缀：同步自 OpenList: /CarMax/<非斜线>[/<非斜线>]/
# - 尾部：捕获到行尾的整段（文件名可含空格/中文/标点，但不含换行/斜线）
# - 允许 1 段或 2 段目录深度
PATTERN = re.compile(
    r"^同步自 OpenList:\s*/CarMax/[^/]+(?:/[^/]+)?/(?P<fname>[^\n/]+)$"
)


def clean_desc(desc: str) -> tuple[str, bool]:
    """如果首行匹配，去掉前缀只保留 .apk 文件名；返回 (新 desc, 是否改动)。"""
    if not desc:
        return desc, False
    # 拆首行和剩余
    if "\n" in desc:
        first, rest = desc.split("\n", 1)
        rest = "\n" + rest
    else:
        first, rest = desc, ""
    m = PATTERN.match(first.strip())
    if not m:
        return desc, False
    new_first = m.group("fname")
    return (new_first + rest).strip(), True


def main():
    if not JSON_PATH.exists():
        sys.exit(f"找不到 {JSON_PATH}")
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    print(f"读入 {len(data)} 条")

    changed = 0
    for r in data:
        new_desc, ok = clean_desc(r.get("description", ""))
        if ok:
            r["description"] = new_desc
            changed += 1
    print(f"清理 {changed} 条 description")

    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    JSON_PATH.write_text(payload, encoding="utf-8")
    REMOTE_JSON_PATH.write_text(payload, encoding="utf-8")
    print(f"已写 {JSON_PATH.name} + {REMOTE_JSON_PATH.name}")


if __name__ == "__main__":
    main()
