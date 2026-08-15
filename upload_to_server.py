import os
import sys
import paramiko

HOST = "39.108.105.65"
USER = "root"
PASSWORD = os.environ.get("ROOT_PASSWORD", "").strip()
REMOTE_DIR = "/opt/dbdns/static/downloads"

if len(sys.argv) < 2:
    print("用法: python upload_to_server.py <本地文件路径> [远程文件名]")
    print("示例: python upload_to_server.py C:\\Users\\Santali\\Desktop\\app.apk")
    sys.exit(1)

local_path = sys.argv[1]
remote_name = sys.argv[2] if len(sys.argv) >= 3 else os.path.basename(local_path)
remote_path = f"{REMOTE_DIR}/{remote_name}"

if not os.path.exists(local_path):
    print(f"错误：本地文件不存在: {local_path}")
    sys.exit(1)

if not PASSWORD:
    print("错误：请设置环境变量 ROOT_PASSWORD")
    print("PowerShell: $env:ROOT_PASSWORD=\"你的密码\"")
    sys.exit(1)

try:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"连接 {HOST} ...")
    client.connect(HOST, username=USER, password=PASSWORD, timeout=30)

    sftp = client.open_sftp()
    print(f"上传 {local_path} -> {remote_path}")
    sftp.put(local_path, remote_path)
    sftp.close()
    client.close()

    url = f"http://{HOST}/static/downloads/{remote_name}"
    print("\n上传完成")
    print(f"直链: {url}")
except Exception as e:
    print(f"\n上传失败: {e}", file=sys.stderr)
    sys.exit(1)
