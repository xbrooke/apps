#!/usr/bin/env python3
# coding: utf-8
"""
check_icons.py
==============
校验 apps.json 里 icon 字段与 icons/ 目录的一致性。

检查项：
  1) 缺图：app 引用了 ./icons/<id>.png 但文件不存在
  2) 多余：icons/ 里有 png 但没有任何 app 引用
  3) 路径错：icon 字段不是 ./icons/ 开头或格式异常
  4) id 异常：app 的 id 为空 / 含奇怪字符
  5) 占位检查：是否所有 app 都有 id + name + url + icon

输出格式：清晰的 4 段（缺图 / 多余 / 路径错 / 其它）
退出码：0 表示无问题，1 表示有缺图或路径错
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "apps.json"
ICONS_DIR = HERE / "icons"

# icon 字段期望格式（id 可以含中文）
ICON_PATTERN = re.compile(r"^\./icons/([A-Za-z0-9._\-\u4e00-\u9fff]+)\.png$")


def main():
    if not JSON_PATH.exists():
        sys.exit(f"找不到 {JSON_PATH}")
    if not ICONS_DIR.exists():
        sys.exit(f"找不到 {ICONS_DIR}/")

    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    all_icons = {p.stem for p in ICONS_DIR.glob("*.png")}

    # 1) 收集 app 引用的 id
    referenced: dict[str, list[str]] = {}  # id -> [app name]
    bad_path: list[tuple[str, str]] = []   # (name, icon 字段)
    incomplete: list[tuple[str, list[str]]] = []  # (name, 缺哪些字段)

    for r in data:
        name = r.get("name", "")
        rid = r.get("id", "")
        icon = r.get("icon", "")

        # 字段完整性
        missing_fields = []
        if not rid: missing_fields.append("id")
        if not name: missing_fields.append("name")
        if not r.get("url"): missing_fields.append("url")
        if not icon: missing_fields.append("icon")
        if missing_fields:
            incomplete.append((name or "(无 name)", missing_fields))
            continue

        m = ICON_PATTERN.match(icon)
        if not m:
            bad_path.append((name, icon))
            continue

        icon_id = m.group(1)
        referenced.setdefault(icon_id, []).append(name)

    # 2) 缺图：引用了但 png 不存在
    missing = [(iid, names) for iid, names in referenced.items() if iid not in all_icons]
    # 3) 多余：png 有但无引用
    orphan = sorted(all_icons - set(referenced.keys()))

    # 输出
    print("=" * 60)
    print(f" apps.json: {len(data)} 条应用")
    print(f" icons/: {len(all_icons)} 个 png")
    print("=" * 60)

    print()
    print(f"[1] 缺图: {len(missing)} 个 id 被引用但 png 不存在")
    if missing:
        for iid, names in sorted(missing):
            print(f"    - {iid}.png  (被 {len(names)} 个应用引用，如: {names[0]})")
    else:
        print("    ✓ 全部齐全")

    print()
    print(f"[2] 多余: {len(orphan)} 个 png 没被任何应用引用")
    if orphan:
        for iid in orphan[:20]:
            print(f"    - {iid}.png")
        if len(orphan) > 20:
            print(f"    ... 还有 {len(orphan)-20} 个")
    else:
        print("    ✓ 没有冗余")

    print()
    print(f"[3] 路径错: {len(bad_path)} 个 icon 字段不是 ./icons/<id>.png 格式")
    if bad_path:
        for name, icon in bad_path[:10]:
            print(f"    - {name!r}: {icon!r}")
        if len(bad_path) > 10:
            print(f"    ... 还有 {len(bad_path)-10} 个")
    else:
        print("    ✓ 全部标准格式")

    print()
    print(f"[4] 字段缺失: {len(incomplete)} 条应用缺字段")
    if incomplete:
        for name, fields in incomplete[:10]:
            print(f"    - {name}: 缺 {fields}")
    else:
        print("    ✓ 全部完整")

    # 覆盖统计
    have = sum(1 for iid in referenced if iid in all_icons)
    total = len(referenced)
    pct = (have / total * 100) if total else 0
    print()
    print("=" * 60)
    print(f" 覆盖率: {have}/{total} = {pct:.1f}%")
    print("=" * 60)

    if missing or bad_path or incomplete:
        sys.exit(1)


if __name__ == "__main__":
    main()
