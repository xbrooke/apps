# DBStore 自动同步指南

## 一次性配置（首次部署时做）

### 1. 配 OpenList 凭据

`openlist.config.json`（**已在 .gitignore 中，不会被推送**）：

```json
{
  "base_url": "http://appstore.cnmlynk.org",
  "username": "xudabing",
  "password": "xb123321",
  "url_mode": "openlist"
}
```

### 2. 配 Windows 任务计划

打开 PowerShell（管理员），把以下命令**贴一次**：

```powershell
schtasks /Create /SC HOURLY /MO 6 /TN "DBStore-AutoSync" `
  /TR "C:\Users\Santali\Desktop\apps\auto_sync.bat" /ST 00:00
```

> 含义：每 6 小时、首次运行在 00:00，自动跑 `auto_sync.bat`。

查看任务：

```powershell
schtasks /Query /TN "DBStore-AutoSync" /V /FO LIST
```

立即跑一次（不等触发）：

```powershell
schtasks /Run /TN "DBStore-AutoSync"
```

卸载：

```powershell
schtasks /Delete /TN "DBStore-AutoSync" /F
```

### 3. 确认 Netlify 监听 GitHub

- Netlify 控制台 → 站点 → **Site settings → Build & deploy → Continuous deployment**
- 确认 Repository = `xbrooke/apps`，Branch = `main`
- 每次 push 到 `main`，Netlify 自动构建并发布

---

## 日常使用

### 手动跑一次同步

```
cd C:\Users\Santali\Desktop\apps
auto_sync.bat
```

输出三段：
- `[1/3] sync done.`：OpenList 拉到了
- `[2/3] mirrored to apps_remote.json`
- `[3/3] pushed to origin/main, Netlify will redeploy`：1-2 分钟后车机看到新列表

### 跑一次后只补图标（不重写 apps.json）

```
python fetch_icons.py
git add icons/ apps.json
git commit -m "chore: 补图标"
git push
```

### 紧急：撤销刚才 push 的同步

```
git revert HEAD
git push
```

---

## 文件清单

| 文件 | 作用 |
|---|---|
| `sync_openlist_to_apps_json.py` | 拉取 OpenList 列表 → 写 apps.json |
| `auto_sync.bat` | 串起 sync + git push（一键） |
| `run_sync.bat` | 只跑 sync（不 git push） |
| `openlist.config.json` | OpenList 凭据（**不入仓**） |
| `netlify.toml` | Netlify 构建配置 |
| `apps.json` / `apps_remote.json` | 应用清单（同步产物，**入仓**） |

---

## 排错

| 现象 | 解决 |
|---|---|
| `[ERR] sync failed` | 看 `sync.log` 末尾 Python 错误；通常是密码错或网络问题 |
| `[ERR] git push failed` | 远程领先本地：先 `git pull --rebase` 再 push |
| 车机点链接 401 | `apps.json` 里 url 缺 `?sign=...`；重新跑 `auto_sync.bat` |
| 任务计划没触发 | `schtasks /Query /TN "DBStore-AutoSync"` 看下一次运行时间；确认电脑没休眠 |
