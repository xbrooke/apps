#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DBStore 后台管理应用
功能：
1. 管理本地 apps.json（增删改查、搜索、排序）
2. 上传 APK 到 /opt/dbdns/static/downloads/
3. 同步 OpenList 应用到本地服务器
"""
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
        </div>

        <div id="serverStatus" style="display:none; background:#fff3cd; color:#856404; padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:14px; border:1px solid #ffeeba;">
            未设置 ROOT_PASSWORD 环境变量，APK 上传和 OpenList 同步功能不可用。请在启动命令前加上：<code>$env:ROOT_PASSWORD="你的密码"</code>
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
        </div>

        <table>
            <thead>
                <tr>
                    <th>图标</th>
                    <th>ID</th>
                    <th>名称</th>
                    <th>分类</th>
                    <th>版本</th>
                    <th>大小</th>
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

    <div class="toast" id="toast"></div>

    <script>
        let apps = [];
        let categories = [];

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
            document.getElementById('openlistCount').textContent = apps.filter(a => a.url && a.url.includes('appstore.cnmlynk.org')).length;
            document.getElementById('localCount').textContent = apps.filter(a => a.url && a.url.includes('39.108.105.65')).length;
            document.getElementById('categoryCount').textContent = categories.length;
        }

        function getSource(app) {
            if (app.url && app.url.includes('appstore.cnmlynk.org')) return 'openlist';
            if (app.url && app.url.includes('39.108.105.65')) return 'local';
            return 'other';
        }

        function renderApps() {
            const search = document.getElementById('searchInput').value.toLowerCase();
            const category = document.getElementById('categoryFilter').value;
            const source = document.getElementById('sourceFilter').value;

            const filtered = apps.filter(a => {
                if (search && !(`${a.id} ${a.name} ${a.category} ${a.version}`.toLowerCase().includes(search))) return false;
                if (category && a.category !== category) return false;
                if (source && getSource(a) !== source) return false;
                return true;
            });

            const tbody = document.getElementById('appTableBody');
            tbody.innerHTML = filtered.map((a, idx) => {
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
                    <td class="url-cell" title="${a.url || ''}"><a href="${a.url || '#'}" target="_blank">${a.url ? '打开' : '-'}</a></td>
                    <td>
                        <button class="btn btn-primary" style="padding:4px 10px;font-size:12px;" onclick="editApp(${realIdx})">编辑</button>
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

        async function saveApp(e) {
            e.preventDefault();
            const index = parseInt(document.getElementById('editIndex').value);
            const apkFile = document.getElementById('apkFile').files[0];

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
                safe_name = safe_filename(original_name)
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
