#!/usr/bin/env python3
# coding: utf-8
"""
reclassify.py
=============
重新识别 apps.json 里每条记录的 category。

优先级：
  1) 描述里"原名"（短化前的原始 name，包含全部关键词）
  2) 当前 name
  3) 目录路径（apps.json 没存路径 → 退回到 url 反推）

分类（按优先级匹配，命中即停）：
  - 导航    高德/百度/腾讯/谷歌/凯立德/导航/地图/路况
  - 音乐    QQ音乐/网易云/Apple Music/汽水音乐/酷狗/酷我/咪咕/音乐/Music/Spotify
  - 视频    哔哩/爱奇艺/优酷/腾讯视频/西瓜/YouTube/Video/TV/影视
  - 直播    直播/虎牙/斗鱼/B站直播
  - 电台    喜马拉雅/蜻蜓/荔枝/电台/听书
  - 工具    文件管理/MT管理器/ES文件浏览器/应用管家/ADB/备份/解压/清理/网盘/百度网盘/阿里云盘
  - 车机    车机/小八/智控/HUD/桌面/启动器/壁纸/CarPlay/HiCar/CarLink/领克
  - 输入    输入法/手写/讯飞/搜狗/百度输入法
  - 通讯    微信/QQ/钉钉/飞书/Telegram/WhatsApp/陌陌/探探
  - 浏览器  Chrome/Edge/夸克/UC/火狐/Firefox/Brave/浏览器
  - 资讯    今日头条/网易新闻/腾讯新闻/澎湃/资讯/新闻
  - 教育    学习/作业帮/百词斩/有道/英语
  - 摄影    相机/美颜/美图/相册/拍照/摄影
  - 系统    启动器/Launcher/设置/Settings
  - 安全    杀毒/卫士/安全/360/腾讯管家
  - 购物    淘宝/京东/拼多多/唯品会/购物
  - 外卖    美团/饿了么/外卖
  - 出行    滴滴/曹操/高德打车/出行
  - 办公    WPS/Office/Word/Excel/PPT/钉钉
  - 主题    主题/壁纸/图标包/桌面美化
  - 游戏    (引擎/版) 各种游戏关键词
  - 影音    视频+音乐之外的播放器（MX Player/VLC/IJKPlayer）
  - 其他    匹配不到
"""
from __future__ import annotations
import json
import re
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
JSON_PATH = HERE / "apps.json"
REMOTE_JSON_PATH = HERE / "apps_remote.json"

# (regex, category_name) — 按列表顺序匹配，**先匹配先生效**
# 注意：中文用全角，英文用半角
CATEGORY_RULES = [
    # 导航
    (r"高德|百度.*地图|腾讯.*地图|谷歌.*地图|凯立德|导航|地图|路况|Map|Google.*Map", "导航"),

    # 音乐
    (r"汽水音乐|QQ.*音乐|Apple.*Music|网易云|酷狗|酷我|咪咕|Spotify|千千音乐|音乐|Music|Radio.*Music", "音乐"),

    # 视频/影视
    (r"哔哩哔哩|哔哩|Bili|爱奇艺|优酷|腾讯视频|西瓜视频|芒果TV|YouTube|TV|影视|MX.*Player|VLC|IJK|暴风影音|快手|抖音|皮皮虾|视频", "视频"),

    # 直播
    (r"虎牙|斗鱼|CC直播|龙珠直播|直播", "直播"),

    # 电台/听书
    (r"喜马拉雅|蜻蜓FM|荔枝|懒人听书|听书|电台|有声", "电台"),

    # 工具
    (r"MT.*管理|ES.*文件|文件管理|文件浏览器|应用管家|ADB|备份|解压|清理|网盘|百度网盘|阿里云盘|蓝奏|proxy|代理|工具|Tool|Utils", "工具"),

    # 车机（最特殊的，放中间避免被"工具"误伤）
    (r"车机|小八|智控|HUD|启动器|Launcher|桌面|CarPlay|HiCar|CarLink|领克|Lynk|Wallpaper|壁纸|OSN|仪表盘|方控|多窗|悬浮|Hicar|hicar", "车机"),

    # 输入法
    (r"输入法|手写|讯飞|搜狗|百度输入法|QQ输入法", "输入"),

    # 通讯/社交
    (r"微信|WeChat|QQ|钉钉|飞书|Telegram|WhatsApp|陌陌|探探|Soul", "通讯"),

    # 浏览器
    (r"Chrome|Edge|夸克|UC浏览器|火狐|Firefox|Brave|Via|浏览器|Browser", "浏览器"),

    # 资讯
    (r"今日头条|网易新闻|腾讯新闻|澎湃新闻|资讯|新闻|News", "资讯"),

    # 教育
    (r"作业帮|百词斩|有道|英语|学习|背单词|学霸|网课", "教育"),

    # 摄影/相机
    (r"相机|美颜|美图|相册|拍照|摄影|Camera|Photo", "摄影"),

    # 系统
    (r"系统设置|Settings|系统工具|Framework|系统更新", "系统"),

    # 安全
    (r"杀毒|卫士|安全|360|腾讯管家|LBE", "安全"),

    # 购物
    (r"淘宝|京东|拼多多|唯品会|购物|Amazon|闲鱼|转转", "购物"),

    # 外卖
    (r"美团|饿了么|外卖", "外卖"),

    # 出行
    (r"滴滴|曹操出行|高德打车|嘀嗒|T3出行|出行", "出行"),

    # 办公
    (r"WPS|Office|Word|Excel|PPT|Docs", "办公"),
]

# 兜底分类：上面都匹配不上
DEFAULT_CATEGORY = "其他"


def extract_original_name(record: dict) -> str:
    """
    从 description 里抠出"原名：xxx"，抠不到就用当前 name
    """
    desc = record.get("description", "") or ""
    for line in desc.split("\n"):
        if line.startswith("原名："):
            return line[3:].strip()
    return record.get("name", "")


def extract_path_from_url(url: str) -> str:
    if not url:
        return ""
    if "/dl/" in url:
        return urllib.parse.unquote(url.split("/dl/", 1)[-1].split("?")[0])
    if "/d/" in url:
        return urllib.parse.unquote(url.split("/d/", 1)[-1].split("?")[0])
    return ""


def classify(record: dict) -> str:
    """
    返回 category 名。
    匹配顺序：
      1) 原名 + 当前 name 合并
      2) url 反推的路径
    """
    original = extract_original_name(record)
    current = record.get("name", "")
    # 路径里"车机版"是强力提示
    path = extract_path_from_url(record.get("url", ""))

    text = f"{original} {current} {path}"
    for pat, cat in CATEGORY_RULES:
        if re.search(pat, text, flags=re.IGNORECASE):
            return cat
    return DEFAULT_CATEGORY


def main():
    if not JSON_PATH.exists():
        sys.exit(f"找不到 {JSON_PATH}")
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    print(f"读入 {len(data)} 条")

    from collections import Counter
    old_cats = Counter(r.get("category", "") for r in data)
    print("旧分类:", dict(old_cats.most_common()))

    changed = 0
    for r in data:
        new_cat = classify(r)
        if new_cat != r.get("category", ""):
            changed += 1
        r["category"] = new_cat

    new_cats = Counter(r.get("category", "") for r in data)
    print("新分类:", dict(new_cats.most_common()))
    print(f"分类变化: {changed} 条")

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
