#!/usr/bin/env python3
# coding: utf-8
"""
dedup.py
========
去除 apps.json 里的重复项。规则分两步：

第一步：同 (name, version) 视为完全重复 → 只保留 1 条
  * 保留优先级（从高到低）：
      1) url 是 OpenList 自家（appstore.cnmlynk.org）
      2) size 信息完整（数字部分 > 0）
      3) description 最长
  * 把被删条目的路径信息合并到保留条的 description 末尾

第二步：同 name 但 version 不同 → 只保留版本号最高的那一条
  * 版本号按 "." / 数字串分段，每段按整数逐段比较（v 前缀忽略）
  * 段数不同的用"短者补 0"到同长度再比（v1.23 vs v1.8.6 → v1.8.6 更新）
  * 当组内同时存在有版本/无版本条目时，无版本条目会被删除
  * 被删的低版本条目：其 path 合并到保留条 description 末尾，并标注被删的版本号
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "apps.json"
REMOTE_JSON_PATH = HERE / "apps_remote.json"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

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


def url_to_path(u: str) -> str:
    """从 url 中抠出 OpenList 上的源路径（/dl/ 或 /d/ 之后到 ? 之前）"""
    if not u:
        return ""
    if "/dl/" in u:
        raw = u.split("/dl/", 1)[-1].split("?")[0]
    elif "/d/" in u:
        raw = u.split("/d/", 1)[-1].split("?")[0]
    else:
        return ""
    return unquote(raw)


_VERSION_RE = re.compile(r"(\d+)")


def parse_version(v: str) -> list[int] | None:
    """解析版本号 → 整数段列表。无法解析返回 None（如 '-' 或纯中文）。"""
    if not v:
        return None
    v = v.strip()
    if v in ("-", "—", "·", ""):
        return None
    # 去掉前缀 v/V 和后缀 (xxx) 注释
    v = re.sub(r"^[vV]", "", v)
    v = v.split("(")[0].split("（")[0].strip()
    if not v:
        return None
    parts = _VERSION_RE.findall(v)
    if not parts:
        return None
    return [int(p) for p in parts]


def cmp_version(a: list[int] | None, b: list[int] | None) -> int:
    """比较两个版本号列表。

    规则：
      1) None 视为最低
      2) 段数不同 → 段数多者更高（高德 v16.02.0.2045 vs v6.1.7；厂商主版本号只会变长）
      3) 段数相同 → 逐段按整数比（高位优先）

    注：曾尝试"短者补 0"再逐段比，但会导致 v1.23 vs v1.8.6 误判为 v1.23 更新。
    实际 OpenList 数据里版本号段数变化几乎都对应"新版主版本号"，方向稳定。
    """
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    if len(a) != len(b):
        return (len(a) > len(b)) - (len(a) < len(b))
    for x, y in zip(a, b):
        if x != y:
            return (x > y) - (x < y)
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def step1_dedup_same_version(items: list[dict]) -> list[dict]:
    """同 (name, version) 完全相同 → 只留 1 条。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in items:
        key = (r.get("name", ""), r.get("version", "-"))
        groups.setdefault(key, []).append(r)

    kept: list[dict] = []
    removed_total = 0
    for (name, ver), group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        group.sort(key=lambda r: (
            is_openlist_url(r.get("url", "")),
            size_to_bytes(r.get("size", "")),
            len(r.get("description", "")),
        ), reverse=True)
        winner = group[0]
        losers = group[1:]

        # 把 losers 的路径拼到 winner.description 末尾
        if losers:
            extra = []
            for l in losers:
                p = url_to_path(l.get("url", ""))
                if p:
                    extra.append(p)
            if extra:
                desc = winner.get("description", "") or ""
                tag = "（另有重复版本：" + " | ".join(extra[:3])
                if len(extra) > 3:
                    tag += f" 等 {len(extra)} 个"
                tag += "）"
                winner["description"] = (desc + ("\n" if desc else "") + tag).strip()

        kept.append(winner)
        removed_total += len(losers)
        if removed_total <= 12 or len(group) >= 3:
            print(f"  [同版本去重] {name} {ver:<12} 保留 1 条，删 {len(losers)} 条")

    print(f"[步骤 1] 同版本去重：删 {removed_total} 条，剩 {len(kept)} 条")
    return kept


def step2_dedup_lower_version(items: list[dict]) -> list[dict]:
    """同 name 不同 version → 只保留版本最高的那一条。

    算法（DSU 模式）：
      1) 找全组中 cmp_version 最大的那个 version（max_v）
      2) 在 max_v 同分的候选里，按次要键（openlist 优先、size 大、desc 长）挑 winner
      3) 其余作为 losers，被删条目的 path 拼到 winner.description 末尾
    """
    groups: dict[str, list[dict]] = {}
    for r in items:
        groups.setdefault(r.get("name", ""), []).append(r)

    kept: list[dict] = []
    removed_total = 0
    for name, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue

        parsed = [parse_version(r.get("version", "-")) for r in group]

        # 找 max_v
        max_v: list[int] | None = None
        for v in parsed:
            if v is None:
                continue
            if max_v is None or cmp_version(v, max_v) > 0:
                max_v = v

        # 决定 winner
        if max_v is not None:
            # 在 max_v 同分组内按次要键挑一条
            candidates = [(r, v) for r, v in zip(group, parsed) if v is not None and cmp_version(v, max_v) == 0]
            candidates.sort(key=lambda t: (
                is_openlist_url(t[0].get("url", "")),
                size_to_bytes(t[0].get("size", "")),
                len(t[0].get("description", "")),
            ), reverse=True)
            winner = candidates[0][0]
        else:
            # 全组都是 None 版本 → 按次要键挑一条
            group_sorted = sorted(
                group,
                key=lambda r: (
                    is_openlist_url(r.get("url", "")),
                    size_to_bytes(r.get("size", "")),
                    len(r.get("description", "")),
                ),
                reverse=True,
            )
            winner = group_sorted[0]

        losers = [r for r in group if r is not winner]

        # 拼被删低版本路径到 description
        low_paths: list[tuple[str, str]] = []
        for l in losers:
            p = url_to_path(l.get("url", ""))
            if p:
                low_paths.append((l.get("version", "-"), p))

        if low_paths:
            desc = winner.get("description", "") or ""
            tag_lines = ["（已移除的低版本："]
            for vstr, p in low_paths:
                tag_lines.append(f"  · {vstr} → {p}")
            tag_lines.append("）")
            winner["description"] = (desc + ("\n" if desc else "") + "\n".join(tag_lines)).strip()

        kept.append(winner)
        removed_total += len(losers)
        win_ver_str = winner.get("version", "-")
        if removed_total <= 12 or len(group) >= 3:
            print(f"  [低版本去重] {name:<20} 保留 {win_ver_str}，删 {len(losers)} 条低版本")

    print(f"[步骤 2] 低版本去重：删 {removed_total} 条，剩 {len(kept)} 条")
    return kept


def main():
    if not JSON_PATH.exists():
        sys.exit(f"找不到 {JSON_PATH}")
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    print(f"读入 {len(data)} 条")

    after_step1 = step1_dedup_same_version(data)
    after_step2 = step2_dedup_lower_version(after_step1)

    print(f"最终：{len(data)} → {len(after_step2)} 条（删 {len(data) - len(after_step2)} 条）")

    JSON_PATH.write_text(
        json.dumps(after_step2, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    REMOTE_JSON_PATH.write_text(
        json.dumps(after_step2, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已写 {JSON_PATH.name} + {REMOTE_JSON_PATH.name}")


if __name__ == "__main__":
    main()
