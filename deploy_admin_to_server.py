#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
部署 DBStore 后台管理到服务器 39.108.105.65
默认保留服务器端已有的 apps.json / openlist.config.json，避免覆盖线上数据。
如需强制覆盖，请设置环境变量 FORCE_DATA_OVERWRITE=true
"""
import os
import sys
import json
import time
from pathlib import Path
from paramiko import SSHClient, AutoAddPolicy, Transport
from paramiko.auth_handler import AuthenticationException

SERVER_HOST = "39.108.105.65"
SERVER_USER = "root"
SERVER_PASSWORD = os.environ.get("ROOT_PASSWORD", "")
FORCE_DATA_OVERWRITE = os.environ.get("FORCE_DATA_OVERWRITE", "false").lower() == "true"
REMOTE_DIR = "/opt/dbstore-admin"
REMOTE_VENV = f"{REMOTE_DIR}/venv"
LOCAL_DIR = Path(__file__).parent
FILES_TO_UPLOAD = [
    "admin_app.py",
    "apps.json",
    "openlist.config.json",
]
DIRS_TO_UPLOAD = [
    "icons",
]


def safe_print(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "ignore").decode("ascii", "ignore"))


def ssh_exec(ssh, cmd, timeout=30):
    safe_print(f"$ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "ignore")
    err = stderr.read().decode("utf-8", "ignore")
    rc = stdout.channel.recv_exit_status()
    if out:
        safe_print(out)
    if err:
        safe_print(f"[stderr] {err}")
    return rc, out, err


def upload_file(sftp, local, remote):
    safe_print(f"上传 {local} -> {remote}")
    sftp.put(str(local), remote)


def upload_dir(ssh, sftp, local_dir, remote_dir):
    ssh_exec(ssh, f"mkdir -p {remote_dir}")
    for item in local_dir.rglob("*"):
        if item.is_file():
            rel = item.relative_to(local_dir)
            remote_path = f"{remote_dir}/{rel.as_posix()}"
            ssh_exec(ssh, f"mkdir -p {Path(remote_path).parent.as_posix()}")
            safe_print(f"上传 {item} -> {remote_path}")
            sftp.put(str(item), remote_path)


def main():
    if not SERVER_PASSWORD:
        safe_print("错误：请先设置 ROOT_PASSWORD 环境变量")
        safe_print("例如：$env:ROOT_PASSWORD=\"你的密码\"")
        sys.exit(1)

    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    try:
        ssh.connect(SERVER_HOST, username=SERVER_USER, password=SERVER_PASSWORD, timeout=20)
    except AuthenticationException:
        safe_print("SSH 登录失败，请检查 ROOT_PASSWORD")
        sys.exit(1)

    sftp = ssh.open_sftp()

    # 创建远程目录
    ssh_exec(ssh, f"mkdir -p {REMOTE_DIR}")

    # 上传文件（apps.json / openlist.config.json 默认保留服务器端，避免覆盖线上数据）
    for filename in FILES_TO_UPLOAD:
        local = LOCAL_DIR / filename
        if not local.exists():
            safe_print(f"跳过不存在的文件: {local}")
            continue
        remote = f"{REMOTE_DIR}/{filename}"
        if filename in ("apps.json", "openlist.config.json") and not FORCE_DATA_OVERWRITE:
            try:
                sftp.stat(remote)
                safe_print(f"保留服务器端 {filename}，如需覆盖请设置 FORCE_DATA_OVERWRITE=true")
                continue
            except Exception:
                pass
        upload_file(sftp, local, remote)

    # 上传目录
    for dirname in DIRS_TO_UPLOAD:
        local = LOCAL_DIR / dirname
        if not local.exists():
            safe_print(f"跳过不存在的目录: {local}")
            continue
        upload_dir(ssh, sftp, local, f"{REMOTE_DIR}/{dirname}")

    # 创建 requirements.txt
    requirements = "flask>=2.0\nrequests>=2.25\nparamiko>=2.7\nPillow>=8.0\n"
    req_local = LOCAL_DIR / "admin_requirements.txt"
    req_local.write_text(requirements, encoding="utf-8")
    upload_file(sftp, req_local, f"{REMOTE_DIR}/requirements.txt")

    # 安装 Python3、pip、virtualenv
    ssh_exec(ssh, "which python3 || apt-get update && apt-get install -y python3 python3-pip python3-venv", timeout=120)

    # 创建虚拟环境
    ssh_exec(ssh, f"python3 -m venv {REMOTE_VENV} 2>/dev/null || rm -rf {REMOTE_VENV} && python3 -m venv {REMOTE_VENV}", timeout=120)

    # 安装依赖
    ssh_exec(ssh, f"{REMOTE_VENV}/bin/pip install -r {REMOTE_DIR}/requirements.txt", timeout=180)

    # 生成随机密钥
    import secrets
    flask_secret = secrets.token_urlsafe(32)
    admin_password = os.environ.get("ADMIN_PASSWORD", "dbstore2026")

    # 创建 .env 环境变量文件（权限 600）
    env_content = f"""ROOT_PASSWORD={SERVER_PASSWORD}
ADMIN_PASSWORD={admin_password}
FLASK_SECRET_KEY={flask_secret}
"""
    ssh_exec(ssh, f"cat > {REMOTE_DIR}/.env <<'EOF'\n{env_content}EOF\nchmod 600 {REMOTE_DIR}/.env")

    # 创建启动脚本
    start_script = f"""#!/bin/bash
cd {REMOTE_DIR}
source {REMOTE_DIR}/.env
exec {REMOTE_VENV}/bin/python {REMOTE_DIR}/admin_app.py
"""
    ssh_exec(ssh, f"cat > {REMOTE_DIR}/start.sh <<'EOF'\n{start_script}EOF\nchmod +x {REMOTE_DIR}/start.sh")

    # 创建 systemd 服务
    service_content = f"""[Unit]
Description=DBStore Admin App
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={REMOTE_DIR}
EnvironmentFile=-{REMOTE_DIR}/.env
ExecStart={REMOTE_VENV}/bin/python {REMOTE_DIR}/admin_app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    ssh_exec(ssh, f"cat > /etc/systemd/system/dbstore-admin.service <<'EOF'\n{service_content}EOF")

    # 停止旧进程
    ssh_exec(ssh, "systemctl stop dbstore-admin 2>/dev/null; pkill -f 'admin_app.py' 2>/dev/null; sleep 2")

    # 重新加载并启动
    ssh_exec(ssh, "systemctl daemon-reload && systemctl enable dbstore-admin && systemctl start dbstore-admin", timeout=30)

    # 开放防火墙
    ssh_exec(ssh, "which ufw && ufw allow 5000/tcp || true", timeout=30)
    ssh_exec(ssh, "which firewall-cmd && firewall-cmd --add-port=5000/tcp --permanent && firewall-cmd --reload || true", timeout=30)
    ssh_exec(ssh, "iptables -C INPUT -p tcp --dport 5000 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 5000 -j ACCEPT", timeout=30)

    sftp.close()
    ssh.close()

    safe_print("部署完成")
    safe_print(f"访问地址: http://{SERVER_HOST}:5000")
    safe_print(f"默认管理密码: {admin_password}")
    safe_print("建议首次登录后尽快修改默认管理密码（设置 ADMIN_PASSWORD 环境变量后重新部署）")


if __name__ == "__main__":
    main()
