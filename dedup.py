#!/usr/bin/env python3
# coding: utf-8
"""
dedup.py
========
去除 apps.json 里的重复项。规则：
  * 重复键 = (name, version)
  * 同 name + 同 version → 只保留 1 条
  * 保留优先级（从高到低）：
      1) url 是 OpenList 自家（appstore.cnmlynk.org）
      2) size 信息完整（数字部分 > 0）
      3) description 最长
  * 把被删条目的路径信息合并到保留条的 description 末尾
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "apps.json"
REMOTE_JSON_PATH = HERE / "apps_remote.json"


def size_to_bytes(s: str) -> int:
    """把 '15.14 MB' 转成 15144960。无单位则按字节。"""
    if not s:
        return 0
    m = re.match(r"([\d.]+)\s*([KMGT]?B)?", s.strip(), flags=re.I)
    if not m:
        return 0
    n = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}.get(unit, 1)
    return int(n * mult)


def is_openlist_url(u: str) -> bool:
    return "appstore.cnmlynk.org" in (u or "")


def main():
    if not JSON_PATH.exists():
        sys.exit(f"找不到 {JSON_PATH}")
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    print(f"读入 {len(data)} 条")

    # 按 (name, version) 分组
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in data:
        key = (r.get("name", ""), r.get("version", "-"))
        groups.setdefault(key, []).append(r)

    # 选每组最优保留
    kept = []
    removed_total = 0
    for key, items in groups.items():
        if len(items) == 1:
            kept.append(items[0])
            continue
        # 排序：openlist > size > desc 长度
        items.sort(key=lambda r: (
            is_openlist_url(r.get("url", "")),
            size_to_bytes(r.get("size", "")),
            len(r.get("description", "")),
        ), reverse=True)
        winner = items[0]
        losers = items[1:]
        # 把 loser's url 路径拼到 winner.description 末尾
        if losers:
            extra_paths = []
            for l in losers:
                u = l.get("url", "")
                if "/dl/" in u:
                    extra_paths.append(urllib_unquote(u.split("/dl/", 1)[-1].split("?")[0]))
                elif "/d/" in u:
                    extra_paths.append(urllib_unquote(u.split("/d/", 1)[-1].split("?")[0]))
            if extra_paths:
                desc = winner.get("description", "") or ""
                tag = "（另有重复版本：" + " | ".join(extra_paths[:3])
                if len(extra_paths) > 3:
                    tag += f" 等 {len(extra_paths)} 个"
                tag += "）"
                winner["description"] = (desc + ("\n" if desc else "") + tag).strip()
        kept.append(winner)
        removed_total += len(losers)
        if removed_total <= 12 or len(items) >= 3:
            print(f"  [去重] {key[0]} {key[1]:<8} 保留 1 条，删 {len(losers)} 条")

    print(f"去重: 删 {removed_total} 条，剩 {len(kept)} 条")

    JSON_PATH.write_text(
        json.dumps(kept, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    REMOTE_JSON_PATH.write_text(
        json.dumps(kept, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已写 {JSON_PATH.name} + {REMOTE_JSON_PATH.name}")


def urllib_unquote(s: str) -> str:
    from urllib.parse import unquote
    return unquote(s)


if __name__ == "__main__":
    main()
