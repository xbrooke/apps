#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DBStore 后台管理应用
功能：
1. 管理本地 apps.json（增删改查、搜索、排序）
2. 上传 APK 到 /opt/dbdns/static/downloads/
3. 同步 OpenList 应用到本地服务器
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import paramiko
import requests
from functools import wraps
from flask import Flask, jsonify, render_template_string, request, send_from_directory, session, redirect, url_for

# ---------- 配置 ----------
APP_ROOT = Path(__file__).parent
APPS_JSON = APP_ROOT / "apps.json"
ICONS_DIR = APP_ROOT / "icons"
OPENLIST_CONFIG = APP_ROOT / "openlist.config.json"
GITHUB_CONFIG = APP_ROOT / "github.config.json"

SERVER_HOST = "39.108.105.65"
SERVER_USER = "root"
SERVER_PASSWORD = os.environ.get("ROOT_PASSWORD", "")
SERVER_DOWNLOAD_DIR = "/opt/dbdns/static/downloads"
SERVER_BASE_URL = f"http://{SERVER_HOST}/static/downloads"

STATIC_HASH_SALT = "https://github.com/alist-org/alist"


def load_openlist_config():
    default = {
        "base_url": "http://appstore.cnmlynk.org",
        "username": "",
        "password": "",
        "remote_path": "/DBStore",
        "public_base": "http://appstore.cnmlynk.org",
        "use_proxy": True,
    }
    if not OPENLIST_CONFIG.exists():
        return default
    try:
        with open(OPENLIST_CONFIG, "r", encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except Exception as e:
        safe_print(f"读取 openlist.config.json 失败: {e}")
        return default


OPENLIST_CONF = load_openlist_config()
OPENLIST_BASE = OPENLIST_CONF.get("base_url", "http://appstore.cnmlynk.org").rstrip("/")
OPENLIST_USER = OPENLIST_CONF.get("username", "")
OPENLIST_PASS = OPENLIST_CONF.get("password", "")


def load_github_config():
    default = {
        "owner": "xbrooke",
        "repo": "apps",
        "branch": "main",
        "token": os.environ.get("GH_TOKEN", ""),
    }
    if not GITHUB_CONFIG.exists():
        return default
    try:
        with open(GITHUB_CONFIG, "r", encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except Exception as e:
        safe_print(f"读取 github.config.json 失败: {e}")
        return default


GITHUB_CONF = load_github_config()
GH_OWNER = GITHUB_CONF.get("owner", "xbrooke")
GH_REPO = GITHUB_CONF.get("repo", "apps")
GH_BRANCH = GITHUB_CONF.get("branch", "main")
GH_TOKEN = GITHUB_CONF.get("token", "")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "dbstore2026")
app = Flask(__name__)
app.json.ensure_ascii = False
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dbstore-admin-secret-key-change-in-production")


def require_login(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get("logged_in"):
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": "未登录"}), 401
        return redirect(url_for("login_page"))
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "密码错误"
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout_page():
    session.pop("logged_in", None)
    return redirect(url_for("login_page"))


# ---------- 工具函数 ----------
def safe_print(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "ignore").decode("utf-8", "ignore"))


def load_apps():
    if not APPS_JSON.exists():
        return []
    with open(APPS_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def save_apps(apps):
    with open(APPS_JSON, "w", encoding="utf-8") as f:
        json.dump(apps, f, ensure_ascii=False, indent=2)


def static_hash(pwd):
    raw = f"{pwd}-{STATIC_HASH_SALT}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def login_openlist():
    r1 = requests.post(f"{OPENLIST_BASE}/api/auth/login", json={"username": OPENLIST_USER, "password": OPENLIST_PASS}, timeout=30)
    r1.raise_for_status()
    step1 = r1.json()["data"]["token"]
    r2 = requests.post(
        f"{OPENLIST_BASE}/api/auth/login/hash",
        json={"username": OPENLIST_USER, "password": static_hash(OPENLIST_PASS)},
        headers={"Authorization": step1},
        timeout=30,
    )
    r2.raise_for_status()
    return r2.json()["data"]["token"]


def get_openlist_path(url):
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    if path.startswith("/d/"):
        path = path[3:]
    return "/" + urllib.parse.unquote(path)


def safe_filename(name):
    p = Path(name)
    stem = p.stem
    suffix = p.suffix
    stem = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9._-]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_.-")
    if not stem:
        stem = "app"
    stem = stem[:80]
    return f"{stem}{suffix}"


def extract_version(name):
    m = re.search(r"[vV]?(\d+(?:\.\d+)+)", name)
    if m:
        return f"v{m.group(1)}"
    m = re.search(r"_V(\d+)", name)
    if m:
        return f"V{m.group(1)}"
    return "-"


def local_apk_name_for_app(app, original_name):
    """
    优先复用 apps.json 里已有的服务器文件名（英文 URL），避免 OpenList
    同步把已改好的英文 URL 覆盖回中文文件名。若还没有本地 URL，则使用
    app.id + version 生成英文文件名。
    """
    url = app.get("url") or ""
    if url.startswith(SERVER_BASE_URL):
        existing = urllib.parse.unquote(url.split("/")[-1])
        if existing:
            return existing

    app_id = app.get("id", "")
    version = app.get("version") or extract_version(original_name) or "v1"
    if app_id:
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", app_id).strip("_")
        if stem:
            safe = f"{stem}_v{version}.apk".replace("__", "_")
            return safe

    return safe_filename(original_name)


def format_size(size):
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size/1024:.2f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size/1024/1024:.2f} MB"
    else:
        return f"{size/1024/1024/1024:.2f} GB"


def ensure_default_icon():
    default_path = ICONS_DIR / "default.png"
    if default_path.exists():
        return
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (128, 128), (240, 240, 240, 255))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([16, 16, 112, 112], radius=20, fill=(26, 115, 232, 255))
        draw.ellipse([48, 36, 80, 68], fill=(255, 255, 255, 255))
        draw.rounded_rectangle([44, 72, 84, 96], radius=8, fill=(255, 255, 255, 255))
        default_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(default_path)
        safe_print(f"已生成默认图标: {default_path}")
    except ImportError:
        default_path.parent.mkdir(parents=True, exist_ok=True)
        default_path.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
    except Exception as e:
        safe_print(f"生成默认图标失败: {e}")


def ssh_client():
    if not SERVER_PASSWORD:
        raise ValueError("未设置 ROOT_PASSWORD 环境变量")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SERVER_HOST, username=SERVER_USER, password=SERVER_PASSWORD, timeout=30)
    return client


def upload_to_server(local_path, remote_name):
    client = ssh_client()
    try:
        sftp = client.open_sftp()
        remote_path = f"{SERVER_DOWNLOAD_DIR}/{remote_name}"
        sftp.put(str(local_path), remote_path)
        sftp.close()
        return remote_path
    finally:
        client.close()


def get_openlist_download_url(token, path):
    r = requests.post(
        f"{OPENLIST_BASE}/api/fs/get",
        json={"path": path},
        headers={"Authorization": token},
        timeout=30,
    )
    d = r.json()
    if d.get("code") != 200:
        return None, 0
    data = d.get("data") or {}
    url = data.get("raw_url") or data.get("url")
    return url, data.get("size", 0)


LOGIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DBStore 后台登录</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
        .login-box { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); width: 360px; }
        .login-box h1 { font-size: 22px; margin-bottom: 8px; color: #1a73e8; }
        .login-box p { color: #666; font-size: 14px; margin-bottom: 24px; }
        .login-box input { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; margin-bottom: 16px; }
        .login-box button { width: 100%; padding: 12px; background: #1a73e8; color: white; border: none; border-radius: 6px; font-size: 15px; cursor: pointer; }
        .login-box button:hover { background: #1557b0; }
        .error { color: #ea4335; font-size: 13px; margin-bottom: 12px; }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>DBStore 后台登录</h1>
        <p>请输入管理密码</p>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <input type="password" name="password" placeholder="管理密码" required autofocus>
            <button type="submit">登录</button>
        </form>
    </div>
</body>
</html>
"""


# ---------- 页面 ----------
HTML_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DBStore 后台管理</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; }
        .header { background: #1a73e8; color: white; padding: 16px 24px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .header h1 { font-size: 20px; font-weight: 600; }
        .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
        .toolbar { background: white; padding: 16px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
        .toolbar input, .toolbar select { padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        .toolbar input { min-width: 200px; }
        .btn { padding: 8px 16px; border: none; border-radius: 4px; font-size: 14px; cursor: pointer; transition: background 0.2s; }
        .btn-primary { background: #1a73e8; color: white; }
        .btn-primary:hover { background: #1557b0; }
        .btn-success { background: #34a853; color: white; }
        .btn-success:hover { background: #2d8e47; }
        .btn-danger { background: #ea4335; color: white; }
        .btn-danger:hover { background: #c53929; }
        .btn-warning { background: #fbbc05; color: #333; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 16px; }
        .stat-card { background: white; padding: 16px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .stat-card .number { font-size: 28px; font-weight: 700; color: #1a73e8; }
        .stat-card .label { color: #666; font-size: 14px; margin-top: 4px; }
        table { width: 100%; background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 14px; }
        th { background: #fafafa; font-weight: 600; color: #555; }
        tr:hover { background: #f8f9fa; }
        .app-icon { width: 40px; height: 40px; border-radius: 8px; object-fit: cover; }
        .tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; background: #e8f0fe; color: #1a73e8; }
        .tag.openlist { background: #fce8e6; color: #c53929; }
        .tag.local { background: #e6f4ea; color: #2d8e47; }
        .url-cell { max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .sortable { cursor: pointer; user-select: none; }
        .sortable:hover { background: #e9ecef; }
        .sortable span { font-size: 12px; color: #999; margin-left: 4px; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 1000; justify-content: center; align-items: center; }
        .modal.active { display: flex; }
        .modal-content { background: white; padding: 24px; border-radius: 8px; width: 90%; max-width: 600px; max-height: 90vh; overflow-y: auto; }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; margin-bottom: 6px; font-weight: 500; font-size: 14px; }
        .form-group input, .form-group select, .form-group textarea { width: 100%; padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        .form-group textarea { min-height: 80px; resize: vertical; }
        .modal-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 20px; }
        .sync-log { background: #1e1e1e; color: #d4d4d4; padding: 12px; border-radius: 4px; font-family: monospace; font-size: 12px; max-height: 400px; overflow-y: auto; white-space: pre-wrap; }
        .toast { position: fixed; bottom: 24px; right: 24px; padding: 12px 20px; border-radius: 4px; color: white; font-size: 14px; z-index: 2000; opacity: 0; transition: opacity 0.3s; }
        .toast.show { opacity: 1; }
        .toast.success { background: #34a853; }
        .toast.error { background: #ea4335; }
        .loading { display: inline-block; width: 16px; height: 16px; border: 2px solid #fff; border-top-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 8px; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
    <div class="header">
        <h1>DBStore 后台管理</h1>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card">
                <div class="number" id="totalCount">0</div>
                <div class="label">应用总数</div>
            </div>
            <div class="stat-card">
                <div class="number" id="openlistCount">0</div>
                <div class="label">OpenList 来源</div>
            </div>
            <div class="stat-card">
                <div class="number" id="localCount">0</div>
                <div class="label">本地服务器</div>
            </div>
            <div class="stat-card">
                <div class="number" id="categoryCount">0</div>
                <div class="label">分类数</div>
            </div>
            <div class="stat-card">
                <div class="number" id="totalSize">0 MB</div>
                <div class="label">总大小</div>
            </div>
        </div>

        <div id="serverStatus" style="display:none; background:#fff3cd; color:#856404; padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:14px; border:1px solid #ffeeba;">
            未设置 ROOT_PASSWORD 环境变量，APK 上传、服务器文件管理和 OpenList 同步功能不可用。请在启动前设置环境变量：<code>ROOT_PASSWORD=你的密码</code>（PowerShell: <code>$env:ROOT_PASSWORD="你的密码"</code>）
        </div>
        <div class="toolbar">
            <input type="text" id="searchInput" placeholder="搜索应用名称/ID/分类..." oninput="renderApps()">
            <select id="categoryFilter" onchange="renderApps()">
                <option value="">全部分类</option>
            </select>
            <select id="sourceFilter" onchange="renderApps()">
                <option value="">全部来源</option>
                <option value="openlist">OpenList</option>
                <option value="local">本地服务器</option>
            </select>
            <button class="btn btn-primary" type="button" onclick="openEditModal()">+ 新增应用</button>
            <button class="btn btn-success" id="topSyncBtn" onclick="openSyncModal()">同步 OpenList</button>
            <button class="btn btn-warning" type="button" onclick="openServerFilesModal()">服务器文件</button>
            <button class="btn" style="background:#17a2b8;color:white;" type="button" onclick="openSystemStatusModal()">系统状态</button>
            <button class="btn" style="background:#6f42c1;color:white;" type="button" onclick="openOpenlistConfigModal()">OpenList 配置</button>
            <button class="btn" style="background:#24292e;color:white;" type="button" onclick="openGithubConfigModal()">GitHub 配置</button>
            <button class="btn" style="background:#2ea44f;color:white;" type="button" id="pushGithubBtn" onclick="pushToGithub()">推送到 GitHub</button>
            <button class="btn" style="background:#6c757d;color:white;" type="button" onclick="openToolsModal()">更多</button>
        </div>

        <table>
            <thead>
                <tr>
                    <th>图标</th>
                    <th class="sortable" onclick="sortApps('id')">ID <span id="sort-id"></span></th>
                    <th class="sortable" onclick="sortApps('name')">名称 <span id="sort-name"></span></th>
                    <th class="sortable" onclick="sortApps('category')">分类 <span id="sort-category"></span></th>
                    <th class="sortable" onclick="sortApps('version')">版本 <span id="sort-version"></span></th>
                    <th class="sortable" onclick="sortApps('size')">大小 <span id="sort-size"></span></th>
                    <th>来源</th>
                    <th>下载链接</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody id="appTableBody"></tbody>
        </table>
    </div>

    <div class="modal" id="editModal">
        <div class="modal-content">
            <h3 id="modalTitle">新增应用</h3>
            <form id="appForm" onsubmit="saveApp(event)">
                <input type="hidden" id="editIndex" value="-1">
                <div class="form-group">
                    <label>应用ID</label>
                    <input type="text" id="appId" required placeholder="例如：app_tool_001">
                </div>
                <div class="form-group">
                    <label>名称</label>
                    <input type="text" id="appName" required>
                </div>
                <div class="form-group">
                    <label>分类</label>
                    <input type="text" id="appCategory" list="categoryList" required placeholder="例如：实用工具">
                    <datalist id="categoryList"></datalist>
                </div>
                <div class="form-group">
                    <label>版本</label>
                    <input type="text" id="appVersion" placeholder="例如：v1.0.0">
                </div>
                <div class="form-group">
                    <label>大小</label>
                    <input type="text" id="appSize" placeholder="例如：12.5 MB">
                </div>
                <div class="form-group">
                    <label>图标路径</label>
                    <input type="text" id="appIcon" placeholder="./icons/xxx.png">
                    <div style="margin-top:8px;">
                        <input type="file" id="iconFile" accept=".png,.jpg,.jpeg,.gif,.webp" onchange="onIconSelected()" style="display:none;">
                        <button type="button" class="btn" style="background:#6c757d;color:white;padding:6px 12px;font-size:13px;" onclick="document.getElementById('iconFile').click()">上传图标</button>
                        <span id="iconInfo" style="font-size:12px;color:#666;margin-left:8px;"></span>
                    </div>
                </div>
                <div class="form-group">
                    <label>下载链接</label>
                    <input type="text" id="appUrl" placeholder="http://...">
                </div>
                <div class="form-group">
                    <label>描述</label>
                    <textarea id="appDesc" placeholder="应用描述..."></textarea>
                </div>
                <div class="form-group">
                    <label>上传 APK（可选，上传后会自动生成下载链接）</label>
                    <input type="file" id="apkFile" accept=".apk" onchange="onApkSelected()">
                    <div id="apkInfo" style="margin-top:8px;font-size:12px;color:#666;"></div>
                </div>
                <div class="modal-actions">
                    <button type="button" class="btn" onclick="closeModal()" style="background:#eee;color:#333;">取消</button>
                    <button type="submit" class="btn btn-primary">保存</button>
                </div>
            </form>
        </div>
    </div>

    <div class="modal" id="syncModal">
        <div class="modal-content" style="max-width: 900px;">
            <h3>同步 OpenList 应用</h3>
            <p style="margin: 12px 0; color: #666; font-size: 14px;">点击下方按钮开始同步。此操作会把 OpenList 上新增/更新的 APK 下载到你的服务器，并更新 apps.json。</p>
            <button class="btn btn-success" onclick="startSync()" id="syncStartBtn" type="button">开始同步</button>
            <div class="sync-log" id="syncLog" style="margin-top: 16px; display: none;"></div>
            <div class="modal-actions">
                <button type="button" class="btn" onclick="closeSyncModal()" style="background:#eee;color:#333;">关闭</button>
            </div>
        </div>
    </div>

    <div class="modal" id="serverFilesModal">
        <div class="modal-content" style="max-width: 900px;">
            <h3>服务器 APK 文件管理</h3>
            <p style="margin: 12px 0; color: #666; font-size: 14px;">列出 /opt/dbdns/static/downloads/ 下的 APK 文件。绿色表示被 apps.json 引用，红色表示孤儿文件可清理。</p>
            <button class="btn btn-primary" type="button" onclick="loadServerFiles()">刷新</button>
            <div id="serverFilesList" style="margin-top: 16px; max-height: 400px; overflow-y: auto;"></div>
            <div class="modal-actions">
                <button type="button" class="btn" onclick="closeServerFilesModal()" style="background:#eee;color:#333;">关闭</button>
            </div>
        </div>
    </div>

    <div class="modal" id="toolsModal">
        <div class="modal-content" style="max-width: 500px;">
            <h3>工具箱</h3>
            <div style="display:flex;flex-direction:column;gap:12px;margin-top:16px;">
                <button class="btn btn-primary" type="button" onclick="exportApps()">导出 apps.json</button>
                <button class="btn" type="button" onclick="document.getElementById('importFile').click()" style="background:#6c757d;color:white;">导入 apps.json</button>
                <input type="file" id="importFile" accept=".json" onchange="importApps()" style="display:none;">
                <button class="btn btn-warning" type="button" onclick="startHealthCheck()">URL 健康检查</button>
            </div>
            <div id="healthResult" style="margin-top:16px;display:none;">
                <h4>检查结果</h4>
                <div id="healthStats" style="margin-bottom:8px;font-size:14px;color:#666;"></div>
                <div id="healthList" style="max-height:250px;overflow-y:auto;font-size:13px;"></div>
            </div>
            <div class="modal-actions">
                <button type="button" class="btn" onclick="closeToolsModal()" style="background:#eee;color:#333;">关闭</button>
            </div>
        </div>
    </div>

    <div class="modal" id="systemStatusModal">
        <div class="modal-content" style="max-width: 700px;">
            <h3>服务器系统状态</h3>
            <pre id="systemStatusOutput" style="background:#1e1e1e;color:#d4d4d4;padding:12px;border-radius:4px;font-family:monospace;font-size:12px;max-height:400px;overflow-y:auto;white-space:pre-wrap;margin-top:16px;">加载中...</pre>
            <div class="modal-actions">
                <button type="button" class="btn btn-primary" onclick="loadSystemStatus()">刷新</button>
                <button type="button" class="btn" onclick="closeSystemStatusModal()" style="background:#eee;color:#333;">关闭</button>
            </div>
        </div>
    </div>

    <div class="modal" id="openlistConfigModal">
        <div class="modal-content" style="max-width: 500px;">
            <h3>OpenList 配置</h3>
            <div class="form-group" style="margin-top:16px;">
                <label>Base URL</label>
                <input type="text" id="olBaseUrl" placeholder="http://appstore.cnmlynk.org">
            </div>
            <div class="form-group">
                <label>用户名</label>
                <input type="text" id="olUsername" placeholder="OpenList 用户名">
            </div>
            <div class="form-group">
                <label>密码</label>
                <input type="password" id="olPassword" placeholder="OpenList 密码">
            </div>
            <div class="form-group">
                <label>URL 模式</label>
                <input type="text" id="olUrlMode" placeholder="openlist">
            </div>
            <div id="openlistConfigMsg" style="font-size:14px;color:#34a853;min-height:20px;"></div>
            <div class="modal-actions">
                <button type="button" class="btn btn-primary" onclick="saveOpenlistConfig()">保存</button>
                <button type="button" class="btn" onclick="closeOpenlistConfigModal()" style="background:#eee;color:#333;">关闭</button>
            </div>
        </div>
    </div>

    <div class="modal" id="githubConfigModal">
        <div class="modal-content" style="max-width: 500px;">
            <h3>GitHub 配置</h3>
            <p style="font-size:13px;color:#666;margin-top:8px;">用于把服务器上的 apps.json 直接推送到 GitHub，触发 Netlify 自动部署。</p>
            <div class="form-group" style="margin-top:16px;">
                <label>Owner</label>
                <input type="text" id="ghOwner" placeholder="xbrooke">
            </div>
            <div class="form-group">
                <label>Repo</label>
                <input type="text" id="ghRepo" placeholder="apps">
            </div>
            <div class="form-group">
                <label>Branch</label>
                <input type="text" id="ghBranch" placeholder="main">
            </div>
            <div class="form-group">
                <label>GitHub Token (PAT)</label>
                <input type="password" id="ghToken" placeholder="ghp_...">
            </div>
            <div id="githubConfigMsg" style="font-size:14px;color:#34a853;min-height:20px;"></div>
            <div class="modal-actions">
                <button type="button" class="btn btn-primary" onclick="saveGithubConfig()">保存</button>
                <button type="button" class="btn" onclick="closeGithubConfigModal()" style="background:#eee;color:#333;">关闭</button>
            </div>
        </div>
    </div>

    <div class="modal" id="githubPushModal">
        <div class="modal-content" style="max-width: 600px;">
            <h3>推送到 GitHub</h3>
            <pre id="githubPushOutput" style="background:#1e1e1e;color:#d4d4d4;padding:12px;border-radius:4px;font-family:monospace;font-size:12px;max-height:300px;overflow-y:auto;white-space:pre-wrap;margin-top:16px;">准备推送 apps.json 到 GitHub...</pre>
            <div class="modal-actions">
                <button type="button" class="btn" onclick="closeGithubPushModal()" style="background:#eee;color:#333;">关闭</button>
            </div>
        </div>
    </div>

    <div class="toast" id="toast"></div>

    <script>
        let apps = [];
        let categories = [];
        let sortState = { column: null, direction: 'asc' };

        async function apiFetch(url, options = {}) {
            const res = await fetch(url, options);
            if (res.status === 401) {
                window.location.href = '/login';
                return null;
            }
            return res;
        }

        async function loadApps() {
            const res = await apiFetch('/api/apps');
            if (!res) return;
            apps = await res.json();
            categories = [...new Set(apps.map(a => a.category).filter(Boolean))].sort();
            updateCategoryFilter();
            updateStats();
            renderApps();
            updateServerStatus();
        }

        async function updateServerStatus() {
            try {
                const res = await apiFetch('/api/status');
                if (!res) return;
                const status = await res.json();
                if (!status.server_ready) {
                    const el = document.getElementById('serverStatus');
                    el.style.display = 'block';
                    document.getElementById('topSyncBtn').disabled = true;
                    document.getElementById('topSyncBtn').title = '未设置 ROOT_PASSWORD，无法同步';
                }
            } catch (e) {
                console.warn('获取服务器状态失败:', e);
            }
        }

        function updateCategoryFilter() {
            const select = document.getElementById('categoryFilter');
            select.innerHTML = '<option value="">全部分类</option>' + categories.map(c => `<option value="${c}">${c}</option>`).join('');
            const datalist = document.getElementById('categoryList');
            datalist.innerHTML = categories.map(c => `<option value="${c}">`).join('');
        }

        function updateStats() {
            document.getElementById('totalCount').textContent = apps.length;
            document.getElementById('openlistCount').textContent = apps.filter(a => getSource(a) === 'openlist').length;
            document.getElementById('localCount').textContent = apps.filter(a => getSource(a) === 'local').length;
            document.getElementById('categoryCount').textContent = categories.length;

            const totalMB = apps.reduce((sum, a) => {
                if (!a.size) return sum;
                const m = a.size.match(/([\\d.]+)\\s*(MB|GB|KB)/i);
                if (!m) return sum;
                const v = parseFloat(m[1]);
                const unit = m[2].toUpperCase();
                if (unit === 'GB') return sum + v * 1024;
                if (unit === 'MB') return sum + v;
                if (unit === 'KB') return sum + v / 1024;
                return sum;
            }, 0);
            document.getElementById('totalSize').textContent = totalMB >= 1024
                ? (totalMB / 1024).toFixed(2) + ' GB'
                : totalMB.toFixed(2) + ' MB';
        }

        function getSource(app) {
            if (app.url && app.url.includes('appstore.cnmlynk.org')) return 'openlist';
            if (app.url && app.url.includes('39.108.105.65')) return 'local';
            return 'other';
        }

        function parseSize(size) {
            if (!size) return 0;
            const m = size.match(/([\\d.]+)\\s*(MB|GB|KB)/i);
            if (!m) return 0;
            const v = parseFloat(m[1]);
            const unit = m[2].toUpperCase();
            if (unit === 'GB') return v * 1024;
            if (unit === 'MB') return v;
            if (unit === 'KB') return v / 1024;
            return 0;
        }

        function compareApp(a, b, column, direction) {
            let va, vb;
            if (column === 'size') {
                va = parseSize(a.size);
                vb = parseSize(b.size);
            } else {
                va = (a[column] || '').toLowerCase();
                vb = (b[column] || '').toLowerCase();
            }
            if (va < vb) return direction === 'asc' ? -1 : 1;
            if (va > vb) return direction === 'asc' ? 1 : -1;
            return 0;
        }

        function sortApps(column) {
            if (sortState.column === column) {
                sortState.direction = sortState.direction === 'asc' ? 'desc' : 'asc';
            } else {
                sortState.column = column;
                sortState.direction = 'asc';
            }
            ['id','name','category','version','size'].forEach(c => {
                document.getElementById('sort-' + c).textContent = sortState.column === c ? (sortState.direction === 'asc' ? '▲' : '▼') : '';
            });
            renderApps();
        }

        async function copyUrl(url) {
            try {
                await navigator.clipboard.writeText(url);
                showToast('下载链接已复制', 'success');
            } catch (e) {
                showToast('复制失败', 'error');
            }
        }

        function renderApps() {
            const search = document.getElementById('searchInput').value.toLowerCase();
            const category = document.getElementById('categoryFilter').value;
            const source = document.getElementById('sourceFilter').value;

            let filtered = apps.filter(a => {
                if (search && !(`${a.id} ${a.name} ${a.category} ${a.version}`.toLowerCase().includes(search))) return false;
                if (category && a.category !== category) return false;
                if (source && getSource(a) !== source) return false;
                return true;
            });

            if (sortState.column) {
                filtered = filtered.slice().sort((a, b) => compareApp(a, b, sortState.column, sortState.direction));
            }

            const tbody = document.getElementById('appTableBody');
            tbody.innerHTML = filtered.map((a) => {
                const src = getSource(a);
                const tagClass = src === 'openlist' ? 'openlist' : (src === 'local' ? 'local' : '');
                const tagText = src === 'openlist' ? 'OpenList' : (src === 'local' ? '本地' : '其他');
                const realIdx = apps.indexOf(a);
                return `<tr>
                    <td><img src="${a.icon || './icons/default.png'}" class="app-icon" onerror="this.src='./icons/default.png'"></td>
                    <td>${a.id}</td>
                    <td><strong>${a.name}</strong></td>
                    <td>${a.category || '-'}</td>
                    <td>${a.version || '-'}</td>
                    <td>${a.size || '-'}</td>
                    <td><span class="tag ${tagClass}">${tagText}</span></td>
                    <td class="url-cell" title="${a.url || ''}">
                        <a href="${a.url || '#'}" target="_blank">${a.url ? '打开' : '-'}</a>
                        ${a.url ? `<button class="btn" style="padding:2px 8px;font-size:11px;margin-left:6px;background:#6c757d;color:white;" onclick="event.stopPropagation();copyUrl('${a.url.replace(/'/g, "\\'")}')">复制</button>` : ''}
                    </td>
                    <td>
                        <button class="btn btn-primary" style="padding:4px 10px;font-size:12px;" onclick="editApp(${realIdx})">编辑</button>
                        ${src === 'openlist' ? `<button class="btn btn-success" style="padding:4px 10px;font-size:12px;" onclick="resyncApp(${realIdx})">重同步</button>` : ''}
                        <button class="btn btn-danger" style="padding:4px 10px;font-size:12px;" onclick="deleteApp(${realIdx})">删除</button>
                    </td>
                </tr>`;
            }).join('');
        }

        function openEditModal() {
            document.getElementById('modalTitle').textContent = '新增应用';
            document.getElementById('appForm').reset();
            document.getElementById('editIndex').value = -1;
            document.getElementById('apkInfo').textContent = '';
            document.getElementById('editModal').classList.add('active');
        }

        function closeModal() {
            document.getElementById('editModal').classList.remove('active');
        }

        function editApp(index) {
            const a = apps[index];
            document.getElementById('modalTitle').textContent = '编辑应用';
            document.getElementById('editIndex').value = index;
            document.getElementById('appId').value = a.id || '';
            document.getElementById('appName').value = a.name || '';
            document.getElementById('appCategory').value = a.category || '';
            document.getElementById('appVersion').value = a.version || '';
            document.getElementById('appSize').value = a.size || '';
            document.getElementById('appIcon').value = a.icon || '';
            document.getElementById('appUrl').value = a.url || '';
            document.getElementById('appDesc').value = a.description || '';
            document.getElementById('apkInfo').textContent = '';
            document.getElementById('editModal').classList.add('active');
        }

        async function deleteApp(index) {
            if (!confirm('确定要删除这个应用吗？')) return;
            const a = apps[index];
            const res = await apiFetch('/api/apps/' + encodeURIComponent(a.id), { method: 'DELETE' });
            if (!res) return;
            const result = await res.json();
            if (result.success) {
                showToast('删除成功', 'success');
                loadApps();
            } else {
                showToast(result.error || '删除失败', 'error');
            }
        }

        function onApkSelected() {
            const file = document.getElementById('apkFile').files[0];
            if (file) {
                document.getElementById('apkInfo').textContent = `已选择: ${file.name} (${(file.size/1024/1024).toFixed(2)} MB)`;
            }
        }

        function openSyncModal() {
            document.getElementById('syncModal').classList.add('active');
            document.getElementById('syncLog').style.display = 'none';
            document.getElementById('syncLog').textContent = '';
            const btn = document.getElementById('syncStartBtn');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '开始同步';
            }
        }

        function closeSyncModal() {
            document.getElementById('syncModal').classList.remove('active');
        }

        function appendLog(msg) {
            const logEl = document.getElementById('syncLog');
            logEl.textContent += msg + '\\n';
            logEl.scrollTop = logEl.scrollHeight;
        }

        async function startSync() {
            const logEl = document.getElementById('syncLog');
            const btn = document.getElementById('syncStartBtn');
            logEl.style.display = 'block';
            logEl.textContent = '';
            if (btn) {
                btn.disabled = true;
                btn.innerHTML = '<span class="loading"></span>同步中...';
            }

            try {
                const res = await apiFetch('/api/sync-openlist', { method: 'POST' });
                if (!res) return;
                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    const text = decoder.decode(value, { stream: true });
                    appendLog(text);
                }
                showToast('同步完成', 'success');
                loadApps();
            } catch (err) {
                appendLog('错误: ' + err.message);
                showToast('同步失败', 'error');
            } finally {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = '开始同步';
                }
            }
        }

        function onIconSelected() {
            const file = document.getElementById('iconFile').files[0];
            if (file) {
                document.getElementById('iconInfo').textContent = `已选择: ${file.name}`;
            }
        }

        async function uploadIconIfNeeded() {
            const file = document.getElementById('iconFile').files[0];
            if (!file) return true;
            const statusRes = await apiFetch('/api/status');
            if (!statusRes) return false;
            const status = await statusRes.json();
            if (!status.server_ready) {
                showToast('未设置 ROOT_PASSWORD，无法上传图标', 'error');
                return false;
            }
            const formData = new FormData();
            formData.append('icon', file);
            const res = await apiFetch('/api/upload-icon', { method: 'POST', body: formData });
            if (!res) return false;
            const data = await res.json();
            if (!data.success) {
                showToast(data.error || '图标上传失败', 'error');
                return false;
            }
            document.getElementById('appIcon').value = data.icon;
            document.getElementById('iconInfo').textContent = '';
            document.getElementById('iconFile').value = '';
            return true;
        }

        async function saveApp(e) {
            e.preventDefault();
            const index = parseInt(document.getElementById('editIndex').value);
            const apkFile = document.getElementById('apkFile').files[0];

            if (!await uploadIconIfNeeded()) return;

            const data = {
                id: document.getElementById('appId').value.trim(),
                name: document.getElementById('appName').value.trim(),
                category: document.getElementById('appCategory').value.trim(),
                version: document.getElementById('appVersion').value.trim(),
                size: document.getElementById('appSize').value.trim(),
                icon: document.getElementById('appIcon').value.trim(),
                url: document.getElementById('appUrl').value.trim(),
                description: document.getElementById('appDesc').value.trim(),
            };

            if (apkFile) {
                const statusRes = await apiFetch('/api/status');
                if (!statusRes) return;
                const status = await statusRes.json();
                if (!status.server_ready) {
                    showToast('未设置 ROOT_PASSWORD，无法上传 APK。请关闭程序后重新用 $env:ROOT_PASSWORD="密码" 启动。', 'error');
                    return;
                }
                showToast('正在上传 APK...', 'success');
                const formData = new FormData();
                formData.append('apk', apkFile);
                const res = await apiFetch('/api/upload', { method: 'POST', body: formData });
                if (!res) return;
                const uploadInfo = await res.json();
                if (!uploadInfo.success) {
                    showToast(uploadInfo.error || '上传失败', 'error');
                    return;
                }
                data.url = uploadInfo.url;
                data.size = uploadInfo.size;
                if (!data.version || data.version === '-') {
                    data.version = uploadInfo.version;
                }
            }

            const url = index >= 0 ? `/api/apps/${encodeURIComponent(apps[index].id)}` : '/api/apps';
            const method = index >= 0 ? 'PUT' : 'POST';
            const res = await apiFetch(url, {
                method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
            if (!res) return;
            const result = await res.json();
            if (result.success) {
                showToast('保存成功', 'success');
                closeModal();
                loadApps();
            } else {
                showToast(result.error || '保存失败', 'error');
            }
        }

        async function resyncApp(index) {
            const a = apps[index];
            if (!confirm(`确定要重新同步 ${a.name} 吗？`)) return;
            openSyncModal();
            const logEl = document.getElementById('syncLog');
            const btn = document.getElementById('syncStartBtn');
            logEl.style.display = 'block';
            logEl.textContent = '';
            if (btn) {
                btn.disabled = true;
                btn.innerHTML = '<span class="loading"></span>同步中...';
            }
            try {
                const res = await apiFetch(`/api/resync-openlist/${encodeURIComponent(a.id)}`, { method: 'POST' });
                if (!res) return;
                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    const text = decoder.decode(value, { stream: true });
                    appendLog(text);
                }
                showToast('重新同步完成', 'success');
                loadApps();
            } catch (err) {
                appendLog('错误: ' + err.message);
                showToast('重新同步失败', 'error');
            } finally {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = '开始同步';
                }
            }
        }

        function openServerFilesModal() {
            document.getElementById('serverFilesModal').classList.add('active');
            loadServerFiles();
        }

        function closeServerFilesModal() {
            document.getElementById('serverFilesModal').classList.remove('active');
        }

        async function loadServerFiles() {
            const listEl = document.getElementById('serverFilesList');
            listEl.innerHTML = '<p>加载中...</p>';
            const res = await apiFetch('/api/server-files');
            if (!res) return;
            const data = await res.json();
            if (!data.success) {
                listEl.innerHTML = `<p style="color:#ea4335">加载失败: ${data.error || '未知错误'}</p>`;
                return;
            }
            const files = data.files || [];
            if (files.length === 0) {
                listEl.innerHTML = '<p>服务器上没有 APK 文件</p>';
                return;
            }
            let html = '<table style="width:100%;font-size:13px;"><thead><tr><th>文件名</th><th>大小</th><th>状态</th><th>操作</th></tr></thead><tbody>';
            files.forEach(f => {
                const status = f.used ? '<span style="color:#34a853">已引用</span>' : '<span style="color:#ea4335">未引用</span>';
                const delBtn = `<button class="btn btn-danger" style="padding:2px 8px;font-size:12px;" onclick="deleteServerFile('${encodeURIComponent(f.name)}')">删除</button>`;
                html += `<tr><td title="${f.name}">${f.name.length > 40 ? f.name.slice(0,40)+'...' : f.name}</td><td>${f.size}</td><td>${status}</td><td>${delBtn}</td></tr>`;
            });
            html += '</tbody></table>';
            listEl.innerHTML = html;
        }

        async function deleteServerFile(name) {
            if (!confirm(`确定要删除服务器上的 ${decodeURIComponent(name)} 吗？此操作不可恢复。`)) return;
            const res = await apiFetch('/api/server-files/' + name, { method: 'DELETE' });
            if (!res) return;
            const result = await res.json();
            if (result.success) {
                showToast('删除成功', 'success');
                loadServerFiles();
            } else {
                showToast(result.error || '删除失败', 'error');
            }
        }

        function openToolsModal() {
            document.getElementById('toolsModal').classList.add('active');
        }

        function closeToolsModal() {
            document.getElementById('toolsModal').classList.remove('active');
        }

        function openSystemStatusModal() {
            document.getElementById('systemStatusModal').classList.add('active');
            loadSystemStatus();
        }

        function closeSystemStatusModal() {
            document.getElementById('systemStatusModal').classList.remove('active');
        }

        async function loadSystemStatus() {
            const out = document.getElementById('systemStatusOutput');
            out.textContent = '加载中...';
            const res = await apiFetch('/api/system-status');
            if (!res) return;
            const data = await res.json();
            if (data.success) {
                out.textContent = `服务状态: ${data.service_active || '未知'}\n\n磁盘:\n${data.disk}\n\n内存:\n${data.memory}`;
            } else {
                out.textContent = '加载失败: ' + (data.error || '未知错误');
            }
        }

        function openOpenlistConfigModal() {
            document.getElementById('openlistConfigModal').classList.add('active');
            loadOpenlistConfig();
        }

        function closeOpenlistConfigModal() {
            document.getElementById('openlistConfigModal').classList.remove('active');
            document.getElementById('openlistConfigMsg').textContent = '';
        }

        async function loadOpenlistConfig() {
            const res = await apiFetch('/api/openlist-config');
            if (!res) return;
            const cfg = await res.json();
            document.getElementById('olBaseUrl').value = cfg.base_url || '';
            document.getElementById('olUsername').value = cfg.username || '';
            document.getElementById('olPassword').value = cfg.password || '';
            document.getElementById('olUrlMode').value = cfg.url_mode || 'openlist';
        }

        async function saveOpenlistConfig() {
            const cfg = {
                base_url: document.getElementById('olBaseUrl').value.trim(),
                username: document.getElementById('olUsername').value.trim(),
                password: document.getElementById('olPassword').value.trim(),
                url_mode: document.getElementById('olUrlMode').value.trim(),
            };
            const res = await apiFetch('/api/openlist-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(cfg),
            });
            if (!res) return;
            const data = await res.json();
            const msg = document.getElementById('openlistConfigMsg');
            if (data.success) {
                msg.textContent = data.message;
                msg.style.color = '#34a853';
                showToast('OpenList 配置已保存', 'success');
            } else {
                msg.textContent = data.error || '保存失败';
                msg.style.color = '#ea4335';
                showToast(data.error || '保存失败', 'error');
            }
        }

        function openGithubConfigModal() {
            document.getElementById('githubConfigModal').classList.add('active');
            loadGithubConfig();
        }

        function closeGithubConfigModal() {
            document.getElementById('githubConfigModal').classList.remove('active');
            document.getElementById('githubConfigMsg').textContent = '';
        }

        async function loadGithubConfig() {
            const res = await apiFetch('/api/github-config');
            if (!res) return;
            const cfg = await res.json();
            document.getElementById('ghOwner').value = cfg.owner || '';
            document.getElementById('ghRepo').value = cfg.repo || '';
            document.getElementById('ghBranch').value = cfg.branch || '';
            document.getElementById('ghToken').value = '';
            const msg = document.getElementById('githubConfigMsg');
            msg.textContent = cfg.token_set ? '已配置 GitHub Token' : '未配置 GitHub Token';
            msg.style.color = cfg.token_set ? '#34a853' : '#ea4335';
        }

        async function saveGithubConfig() {
            const cfg = {
                owner: document.getElementById('ghOwner').value.trim(),
                repo: document.getElementById('ghRepo').value.trim(),
                branch: document.getElementById('ghBranch').value.trim(),
                token: document.getElementById('ghToken').value.trim(),
            };
            const res = await apiFetch('/api/github-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(cfg),
            });
            if (!res) return;
            const data = await res.json();
            const msg = document.getElementById('githubConfigMsg');
            if (data.success) {
                msg.textContent = data.message;
                msg.style.color = '#34a853';
                showToast('GitHub 配置已保存', 'success');
            } else {
                msg.textContent = data.error || '保存失败';
                msg.style.color = '#ea4335';
                showToast(data.error || '保存失败', 'error');
            }
        }

        async function pushToGithub() {
            const btn = document.getElementById('pushGithubBtn');
            if (btn.disabled) return;
            btn.disabled = true;
            btn.innerHTML = '<span class="loading"></span>推送中...';
            const out = document.getElementById('githubPushOutput');
            out.textContent = '正在推送到 GitHub...';
            document.getElementById('githubPushModal').classList.add('active');
            try {
                const res = await apiFetch('/api/push-to-github', { method: 'POST' });
                if (!res) {
                    out.textContent = '请求失败，未返回响应';
                    return;
                }
                const data = await res.json();
                if (data.success) {
                    out.textContent = `推送成功！\n提交: ${data.commit || '未知'}\n仓库: xbrooke/apps (main)`;
                    showToast('已推送到 GitHub', 'success');
                } else {
                    out.textContent = '推送失败: ' + (data.error || '未知错误');
                    showToast(data.error || '推送失败', 'error');
                }
            } finally {
                btn.disabled = false;
                btn.textContent = '推送到 GitHub';
            }
        }

        function closeGithubPushModal() {
            document.getElementById('githubPushModal').classList.remove('active');
        }

        function exportApps() {
            window.location.href = '/api/export';
        }

        async function importApps() {
            const file = document.getElementById('importFile').files[0];
            if (!file) return;
            const formData = new FormData();
            formData.append('file', file);
            const res = await apiFetch('/api/import', { method: 'POST', body: formData });
            if (!res) return;
            const result = await res.json();
            if (result.success) {
                showToast(`导入成功，共 ${result.count} 个应用`, 'success');
                loadApps();
                closeToolsModal();
            } else {
                showToast(result.error || '导入失败', 'error');
            }
            document.getElementById('importFile').value = '';
        }

        async function startHealthCheck() {
            const resultEl = document.getElementById('healthResult');
            const statsEl = document.getElementById('healthStats');
            const listEl = document.getElementById('healthList');
            resultEl.style.display = 'block';
            statsEl.textContent = '检查中...';
            listEl.innerHTML = '';
            const res = await apiFetch('/api/health-check', { method: 'POST' });
            if (!res) return;
            const data = await res.json();
            if (!data.success) {
                statsEl.textContent = data.error || '检查失败';
                return;
            }
            statsEl.innerHTML = `已检查 ${data.checked} 个，异常 ${data.bad} 个`;
            let html = '<table style="width:100%;font-size:12px;"><thead><tr><th>名称</th><th>状态</th></tr></thead><tbody>';
            (data.results || []).forEach(r => {
                const color = r.status === 'ok' ? '#34a853' : '#ea4335';
                html += `<tr><td>${r.name}</td><td style="color:${color}">${r.status}</td></tr>`;
            });
            html += '</tbody></table>';
            listEl.innerHTML = html;
        }

        function showToast(msg, type) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.className = `toast ${type} show`;
            setTimeout(() => toast.classList.remove('show'), 3000);
        }

        loadApps();
    </script>
</body>
</html>
"""


# ---------- API 路由 ----------
@app.route("/")
@require_login
def index():
    return render_template_string(HTML_PAGE)


@app.route("/icons/<path:filename>")
def serve_icon(filename):
    return send_from_directory(str(ICONS_DIR), filename)


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "server_ready": bool(SERVER_PASSWORD),
        "server_host": SERVER_HOST,
        "server_dir": SERVER_DOWNLOAD_DIR,
        "openlist_configured": bool(OPENLIST_USER and OPENLIST_PASS),
    })


@app.route("/api/openlist-config", methods=["GET"])
@require_login
def api_get_openlist_config():
    try:
        cfg = load_openlist_config()
    except Exception:
        cfg = {"base_url": "http://appstore.cnmlynk.org", "username": "", "password": "", "url_mode": "openlist"}
    return jsonify(cfg)


@app.route("/api/openlist-config", methods=["POST"])
@require_login
def api_set_openlist_config():
    data = request.get_json() or {}
    cfg = {
        "base_url": data.get("base_url", "http://appstore.cnmlynk.org").strip(),
        "username": data.get("username", "").strip(),
        "password": data.get("password", "").strip(),
        "url_mode": data.get("url_mode", "openlist").strip(),
    }
    if not cfg["username"] or not cfg["password"]:
        return jsonify({"success": False, "error": "用户名和密码不能为空"})
    OPENLIST_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"success": True, "message": "OpenList 配置已保存"})


@app.route("/api/system-status", methods=["GET"])
@require_login
def api_system_status():
    if not SERVER_PASSWORD:
        return jsonify({"success": False, "error": "未设置 ROOT_PASSWORD"})
    try:
        client = ssh_client()
        sftp = client.open_sftp()

        stdin, stdout, stderr = client.exec_command(f"df -h {SERVER_DOWNLOAD_DIR}")
        df_output = stdout.read().decode("utf-8", errors="replace").strip()

        stdin, stdout, stderr = client.exec_command("free -h")
        free_output = stdout.read().decode("utf-8", errors="replace").strip()

        stdin, stdout, stderr = client.exec_command("systemctl is-active dbstore-admin")
        service_active = stdout.read().decode().strip()

        sftp.close()
        client.close()

        return jsonify({
            "success": True,
            "disk": df_output,
            "memory": free_output,
            "service_active": service_active,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/github-config", methods=["GET"])
@require_login
def api_get_github_config():
    cfg = load_github_config()
    return jsonify({
        "owner": cfg.get("owner", "xbrooke"),
        "repo": cfg.get("repo", "apps"),
        "branch": cfg.get("branch", "main"),
        "token_set": bool(cfg.get("token", "")),
    })


@app.route("/api/github-config", methods=["POST"])
@require_login
def api_set_github_config():
    data = request.get_json() or {}
    cfg = {
        "owner": data.get("owner", "xbrooke").strip(),
        "repo": data.get("repo", "apps").strip(),
        "branch": data.get("branch", "main").strip(),
        "token": data.get("token", "").strip(),
    }
    if not cfg["owner"] or not cfg["repo"]:
        return jsonify({"success": False, "error": "owner 和 repo 不能为空"})
    GITHUB_CONFIG.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"success": True, "message": "GitHub 配置已保存"})


@app.route("/api/push-to-github", methods=["POST"])
@require_login
def api_push_to_github():
    cfg = load_github_config()
    token = cfg.get("token") or os.environ.get("GH_TOKEN", "")
    owner = cfg.get("owner", "xbrooke")
    repo = cfg.get("repo", "apps")
    branch = cfg.get("branch", "main")
    if not token:
        return jsonify({"success": False, "error": "未设置 GitHub Token（GH_TOKEN 环境变量或 GitHub 配置）"})

    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/apps.json"

        get_resp = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=30)
        sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None

        content = APPS_JSON.read_bytes()
        encoded = base64.b64encode(content).decode()

        payload = {
            "message": "Update apps.json from DBStore admin",
            "content": encoded,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        put_resp = requests.put(api_url, headers=headers, json=payload, timeout=30)
        if put_resp.status_code in (200, 201):
            return jsonify({"success": True, "message": "已推送到 GitHub", "commit": put_resp.json().get("commit", {}).get("sha", "")})
        return jsonify({"success": False, "error": f"GitHub API 返回 {put_resp.status_code}: {put_resp.text}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/apps", methods=["GET"])
@require_login
def api_get_apps():
    return jsonify(load_apps())


@app.route("/api/apps", methods=["POST"])
@require_login
def api_create_app():
    data = request.json
    apps = load_apps()

    if not data.get("id") or not data.get("name"):
        return jsonify({"success": False, "error": "ID 和名称不能为空"})

    if any(a["id"] == data["id"] for a in apps):
        return jsonify({"success": False, "error": "应用 ID 已存在"})

    new_app = {
        "id": data["id"],
        "name": data["name"],
        "category": data.get("category", "工具"),
        "version": data.get("version", "-"),
        "size": data.get("size", "-"),
        "description": data.get("description", ""),
        "icon": data.get("icon", f"./icons/{data['id']}.png"),
        "url": data.get("url", ""),
        "tags": [],
    }
    apps.append(new_app)
    save_apps(apps)
    return jsonify({"success": True, "data": new_app})


@app.route("/api/apps/<app_id>", methods=["PUT"])
@require_login
def api_update_app(app_id):
    data = request.json
    apps = load_apps()

    for app in apps:
        if app["id"] == app_id:
            app["name"] = data.get("name", app.get("name", ""))
            app["category"] = data.get("category", app.get("category", "工具"))
            app["version"] = data.get("version", app.get("version", "-"))
            app["size"] = data.get("size", app.get("size", "-"))
            app["description"] = data.get("description", app.get("description", ""))
            app["icon"] = data.get("icon", app.get("icon", ""))
            if "url" in data:
                app["url"] = data["url"]
            save_apps(apps)
            return jsonify({"success": True, "data": app})

    return jsonify({"success": False, "error": "应用不存在"})


@app.route("/api/apps/<app_id>", methods=["DELETE"])
@require_login
def api_delete_app(app_id):
    apps = load_apps()
    new_apps = [a for a in apps if a["id"] != app_id]
    if len(new_apps) == len(apps):
        return jsonify({"success": False, "error": "应用不存在"})
    save_apps(new_apps)
    return jsonify({"success": True})


@app.route("/api/upload", methods=["POST"])
@require_login
def api_upload_apk():
    if "apk" not in request.files:
        return jsonify({"success": False, "error": "未上传文件"})

    file = request.files["apk"]
    if file.filename == "":
        return jsonify({"success": False, "error": "文件名为空"})

    safe_name = safe_filename(file.filename)
    if not safe_name.endswith(".apk"):
        safe_name += ".apk"

    # 保存到临时文件
    temp_path = APP_ROOT / "_tmp_uploads"
    temp_path.mkdir(exist_ok=True)
    local_file = temp_path / safe_name
    file.save(local_file)

    try:
        upload_to_server(local_file, safe_name)
        file_size = local_file.stat().st_size
        url = f"{SERVER_BASE_URL}/{urllib.parse.quote(safe_name)}"
        return jsonify({
            "success": True,
            "url": url,
            "size": format_size(file_size),
            "version": extract_version(safe_name),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        try:
            local_file.unlink()
        except Exception:
            pass


@app.route("/api/sync-openlist", methods=["POST"])
@require_login
def api_sync_openlist():
    def generate_log():
        try:
            load_openlist_config()
            yield "登录 OpenList...\n"
            token = login_openlist()
            yield "登录成功\n\n"

            apps = load_apps()
            openlist_apps = []
            for a in apps:
                url = a.get("url", "")
                if "appstore.cnmlynk.org" in url:
                    a["openlist_path"] = get_openlist_path(url)
                    openlist_apps.append(a)

            yield f"发现 {len(openlist_apps)} 个 OpenList 应用\n"

            if not SERVER_PASSWORD:
                yield "错误：未设置 ROOT_PASSWORD 环境变量\n"
                return

            client = ssh_client()
            yield "已连接服务器\n"

            updated = 0
            failed = 0

            for idx, app in enumerate(openlist_apps, 1):
                path = app["openlist_path"]
                original_name = Path(path).name
                safe_name = local_apk_name_for_app(app, original_name)
                remote_path = f"{SERVER_DOWNLOAD_DIR}/{safe_name}"
                new_url = f"{SERVER_BASE_URL}/{urllib.parse.quote(safe_name)}"

                # 检查服务器是否已有该文件
                stdin, stdout, stderr = client.exec_command(f"test -f {remote_path} && echo exists || echo missing")
                exists = stdout.read().decode().strip() == "exists"

                if exists and app.get("url", "").startswith(SERVER_BASE_URL):
                    yield f"[{idx}/{len(openlist_apps)}] {app['id']} 已是最新，跳过\n"
                    continue

                yield f"[{idx}/{len(openlist_apps)}] {app['id']} - {original_name}\n"

                download_url, size = get_openlist_download_url(token, path)
                if not download_url:
                    yield f"  [失败] 无法获取下载地址\n"
                    failed += 1
                    continue

                cmd = (
                    f"curl -fL --max-time 1800 -o {remote_path}.tmp -H 'User-Agent: DBStore-Sync/1.0' "
                    f"'{download_url}' && mv {remote_path}.tmp {remote_path}"
                )
                stdin, stdout, stderr = client.exec_command(cmd, timeout=1900)
                err = stderr.read().decode("utf-8", errors="replace")
                rc = stdout.channel.recv_exit_status()

                if rc != 0:
                    yield f"  [失败] {err[:200]}\n"
                    failed += 1
                    continue

                app["url"] = new_url
                app["size"] = format_size(size)
                app["version"] = extract_version(original_name)
                updated += 1
                yield f"  [成功] {new_url}\n"
                time.sleep(0.5)

            client.close()
            save_apps(apps)

            yield f"\n=== 同步完成 ===\n"
            yield f"更新: {updated}\n"
            yield f"失败: {failed}\n"

        except Exception as e:
            yield f"\n[错误] {str(e)}\n"

    from flask import Response
    return Response(generate_log(), mimetype="text/plain")


@app.route("/api/upload-icon", methods=["POST"])
@require_login
def api_upload_icon():
    if "icon" not in request.files:
        return jsonify({"success": False, "error": "未上传图标文件"})

    file = request.files["icon"]
    if file.filename == "":
        return jsonify({"success": False, "error": "文件名为空"})

    ext = Path(file.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return jsonify({"success": False, "error": "仅支持 png/jpg/gif/webp 格式"})

    safe_name = safe_filename(file.filename)
    if not any(safe_name.endswith(e) for e in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
        safe_name += ".png"

    icon_path = ICONS_DIR / safe_name
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    file.save(icon_path)

    # 尝试压缩太大的图标
    try:
        from PIL import Image
        img = Image.open(icon_path)
        if max(img.size) > 256:
            img.thumbnail((256, 256))
            img.save(icon_path)
    except Exception:
        pass

    return jsonify({"success": True, "icon": f"./icons/{safe_name}"})


@app.route("/api/export", methods=["GET"])
@require_login
def api_export():
    import io
    data = json.dumps(load_apps(), ensure_ascii=False, indent=2)
    return (
        data,
        200,
        {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Disposition": "attachment; filename=apps.json",
        },
    )


@app.route("/api/import", methods=["POST"])
@require_login
def api_import():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "未上传文件"})

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "error": "文件名为空"})

    try:
        content = file.read().decode("utf-8")
        new_apps = json.loads(content)
        if not isinstance(new_apps, list):
            return jsonify({"success": False, "error": "文件内容必须是应用数组"})
        save_apps(new_apps)
        return jsonify({"success": True, "count": len(new_apps)})
    except Exception as e:
        return jsonify({"success": False, "error": f"导入失败: {str(e)}"})


@app.route("/api/health-check", methods=["POST"])
@require_login
def api_health_check():
    apps = load_apps()
    results = []
    checked = 0
    bad = 0

    for app in apps[:50]:  # 一次最多检查 50 个
        url = app.get("url", "")
        if not url:
            results.append({"id": app["id"], "name": app.get("name", ""), "status": "no_url"})
            continue
        try:
            r = requests.head(url, timeout=10, allow_redirects=True)
            status = "ok" if 200 <= r.status_code < 400 else f"http_{r.status_code}"
            if status != "ok":
                bad += 1
        except Exception as e:
            status = f"error: {str(e)[:100]}"
            bad += 1
        results.append({"id": app["id"], "name": app.get("name", ""), "status": status})
        checked += 1

    return jsonify({"success": True, "checked": checked, "bad": bad, "results": results})


@app.route("/api/server-files", methods=["GET"])
@require_login
def api_server_files():
    if not SERVER_PASSWORD:
        return jsonify({"success": False, "error": "未设置 ROOT_PASSWORD"})

    try:
        client = ssh_client()
        sftp = client.open_sftp()
        files = []
        for entry in sftp.listdir_attr(SERVER_DOWNLOAD_DIR):
            if entry.filename.endswith(".apk"):
                files.append({
                    "name": entry.filename,
                    "size": format_size(entry.st_size),
                    "size_bytes": entry.st_size,
                    "mtime": entry.st_mtime,
                })
        sftp.close()
        client.close()

        # 标记是否被 apps.json 引用
        apps = load_apps()
        used_urls = {a.get("url", "") for a in apps}
        for f in files:
            f["used"] = f"{SERVER_BASE_URL}/{urllib.parse.quote(f['name'])}" in used_urls
        return jsonify({"success": True, "files": files})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/server-files/<path:filename>", methods=["DELETE"])
@require_login
def api_delete_server_file(filename):
    if not SERVER_PASSWORD:
        return jsonify({"success": False, "error": "未设置 ROOT_PASSWORD"})

    try:
        client = ssh_client()
        sftp = client.open_sftp()
        remote_path = f"{SERVER_DOWNLOAD_DIR}/{filename}"
        sftp.remove(remote_path)
        sftp.close()
        client.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/resync-openlist/<app_id>", methods=["POST"])
@require_login
def api_resync_app(app_id):
    if not SERVER_PASSWORD:
        return jsonify({"success": False, "error": "未设置 ROOT_PASSWORD"})

    apps = load_apps()
    app = next((a for a in apps if a["id"] == app_id), None)
    if not app:
        return jsonify({"success": False, "error": "应用不存在"})

    url = app.get("url", "")
    if "appstore.cnmlynk.org" not in url:
        return jsonify({"success": False, "error": "该应用不是 OpenList 来源"})

    path = get_openlist_path(url)
    original_name = Path(path).name
    safe_name = local_apk_name_for_app(app, original_name)
    remote_path = f"{SERVER_DOWNLOAD_DIR}/{safe_name}"
    new_url = f"{SERVER_BASE_URL}/{urllib.parse.quote(safe_name)}"

    def generate_log():
        try:
            load_openlist_config()
            yield "登录 OpenList...\n"
            token = login_openlist()
            yield "登录成功\n"

            client = ssh_client()
            yield "已连接服务器\n"

            yield f"重新同步 {app_id} - {original_name}\n"
            download_url, size = get_openlist_download_url(token, path)
            if not download_url:
                yield "[失败] 无法获取下载地址\n"
                client.close()
                return

            cmd = (
                f"curl -fL --max-time 1800 -o {remote_path}.tmp -H 'User-Agent: DBStore-Sync/1.0' "
                f"'{download_url}' && mv {remote_path}.tmp {remote_path}"
            )
            stdin, stdout, stderr = client.exec_command(cmd, timeout=1900)
            err = stderr.read().decode("utf-8", errors="replace")
            rc = stdout.channel.recv_exit_status()

            if rc != 0:
                yield f"[失败] {err[:200]}\n"
                client.close()
                return

            app["url"] = new_url
            app["size"] = format_size(size)
            app["version"] = extract_version(original_name)
            save_apps(apps)
            client.close()
            yield f"[成功] {new_url}\n"
        except Exception as e:
            yield f"[错误] {str(e)}\n"

    from flask import Response
    return Response(generate_log(), mimetype="text/plain")


if __name__ == "__main__":
    ensure_default_icon()
    if not SERVER_PASSWORD:
        safe_print("警告：未设置 ROOT_PASSWORD 环境变量，上传和同步功能不可用")
    port = 5000
    while True:
        try:
            safe_print(f"启动 DBStore 后台管理: http://127.0.0.1:{port}")
            app.run(host="0.0.0.0", port=port, debug=False)
            break
        except OSError as e:
            if "Address already in use" in str(e) or "Only one usage" in str(e):
                safe_print(f"端口 {port} 被占用，尝试 {port + 1}")
                port += 1
            else:
                raise
