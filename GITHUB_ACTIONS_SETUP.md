# GitHub Actions 定时同步 — 配置步骤

> 每 6 小时自动从 OpenList 拉一次 APK 列表，覆盖 `apps.json` / `apps_remote.json`，
> 并 commit & push 到 main。Netlify 监听 main 分支自动重新部署。

## 一次性配置（5 分钟）

### 1. 在 GitHub 仓库配 4 个 Secret

打开 `https://github.com/xbrooke/apps/settings/secrets/actions` → **New repository secret**，依次添加：

| Name | Value | 说明 |
|---|---|---|
| `OPENLIST_BASE` | `http://appstore.cnmlynk.org` | OpenList 地址 |
| `OPENLIST_USER` | `xudabing` | 用户名 |
| `OPENLIST_PASS` | `xb123321` | 密码 |
| `GH_PAT` | （见下） | 推送用的 Personal Access Token |

`GH_PAT` 申请：
1. 打开 `https://github.com/settings/tokens/new`
2. Note: `dbstore-sync`
3. Expiration: `No expiration`（或长期）
4. **Scopes**：只勾 `repo`（Full control of private repositories）
5. Generate token → 复制字符串（ghp_xxx...）→ 填到上面 `GH_PAT`

> 为什么不用默认 `GITHUB_TOKEN`：默认 token 在某些 workflow 配置下推不到受保护分支，PAT 更稳。

### 2. 推送 workflow 文件

```
git add .github/workflows/sync.yml
git commit -m "ci: add GitHub Actions sync workflow"
git push
```

### 3. 第一次手动触发

打开 `https://github.com/xbrooke/apps/actions` → 选 `Sync OpenList → apps.json` →
**Run workflow** → 选 main → 点绿色按钮。

看执行日志，正常的话 ~30 秒结束，`apps.json` 会被覆盖提交。

### 4. 等定时自动跑

配置时间表（北京时间）：

| 时刻 | 状态 |
|---|---|
| 02:00 | ✅ |
| 08:00 | ✅ |
| 14:00 | ✅ |
| 20:00 | ✅ |

> 定时是 GitHub 服务器 UTC，cron 字段已换算：`0 18,0,6,12 * * *`（UTC）

---

## 日常使用

### 改 cron（每天 1 次 / 每 12 小时）

编辑 `.github/workflows/sync.yml` 的 `cron:` 行：
- `0 0 * * *`   → 每天 08:00（北京时间）
- `0 */6 * * *` → 每 6 小时
- `0 9 * * *`   → 每天 17:00（北京时间）

### 手动触发

Actions 页面 → `Sync OpenList → apps.json` → **Run workflow**。

### 失败时排查

- 点进失败的 run → 看 `Run sync` 那步的日志
- 80% 是 `OPENLIST_PASS` 错了（去 Secret 改）
- 20% 是网络（重试即可）

### 立即跑一次本地覆盖再推

```
python sync_openlist_to_apps_json.py --url-mode openlist
git add apps.json apps_remote.json
git commit -m "chore: manual sync"
git push
```

---

## 链路

```
┌──────────────┐  cron  ┌────────────────┐  fs/get+sign  ┌──────────────┐
│ GitHub       │ ─────► │ sync.yml       │ ────────────► │ OpenList     │
│ Actions      │        │ ubuntu runner  │               │ appstore.    │
│ (00,06,12,18)│        │                │ ◄──────────── │ cnmlynk.org  │
└──────┬───────┘        └────────────────┘   apps.json   └──────────────┘
       │ push
       ▼
┌──────────────┐  build  ┌──────────────┐
│ GitHub       │ ──────► │ Netlify      │ → 用户看到新 apps.json
│ xbrooke/apps │         │ 站点          │
└──────────────┘         └──────────────┘
```

---

## 撤销 / 禁用

如果某次 sync 出了大错：

```
# 在 GitHub 网页上 revert 那个 commit
git revert <commit-sha>
git push
```

或禁用定时：编辑 `.github/workflows/sync.yml`，把 `schedule:` 段注释掉。
