# yamb fork（差异分支 `yamb-plus`）

上游：**https://github.com/nanoite/yamb**（GPL-3.0-or-later）。
yamb-plus 需要的控制器接口上游没有，因此维护一个差异分支；用 fork + 分支而非直接
vendor 进本仓库，好处是能持续跟上游、边界清晰、dist 保持纯 tsc 产物。

## 分支包含的改动

1. **Action API**（`src/api/action-service.ts`、`src/api/game-service.ts`、`routes/game.ts`）：
   无状态游戏动作接口 `POST /api/action`（attack/use/jump/move/look/dig/...），
   连点逻辑放在控制器层，yamb 保持无状态。
2. **热上/下号控制口**（`src/config/loader.ts` 的 `loadBotConfigById` + `src/index.ts`）：
   `127.0.0.1:$YAMB_CONTROL_PORT`，`x-api-key: $YAMB_API_KEY`，
   `GET /api/instances`、`POST /api/instances/start|stop {"account": "..."}` ——
   控制器/switch.sh 借此做到"上下号只动点名账号"。
3. **outgoing 日志**（`bot-runtime.ts`）：供控制器抓取游戏内回执。
4. **rp-fix.js / keepalive.js**（根目录，systemd `NODE_OPTIONS --require` 注入）：
   资源包修复 + 进程心跳兜底。
5. **微软登录凭据接口**（`src/config/loader.ts`）：
   - 新增读取 `config/credentials.yaml`（路径可用环境变量 `YAMB_CREDENTIALS_FILE` 覆盖）：

     ```yaml
     # bot id -> 凭据；未列出的 bot 走 bots/*.yaml 里的 account 段（上游行为）
     bot1:
       username: player_a
       password: ""
     ```

   - 有凭据条目时覆盖对应 bot 的 `account.username/password/auth`，
     没有这个文件时行为与上游完全一致；
   - 微软登录的**令牌缓存**继续由 yamb 的 `MC_PROFILES_FOLDER`（.env）管理，
     credentials.yaml 只解决"账号名/密码不该出现在 bots yaml（可能被截图/提交）"的问题。

## 使用方式

```bash
# 1) fork nanoite/yamb，然后把差异分支推上去（或在本地把分支推给你的 fork）
git clone -b yamb-plus <你的fork> yamb-plus-repo
# 2) 放到本仓库约定的位置
mv yamb-plus-repo yamb
# 3) yamb 自己的 .env（MC 服务器地址等）按上游文档配置
```

`yamb/` 整个目录已被本仓库 gitignore，它的版本管理归它自己的仓库。

## 导出的补丁

差异分支相对上游的提交可用 `git format-patch` 导出为补丁文件，在没有 fork 权限时
也能离线重放：

```bash
cd yamb && git format-patch <上游基线提交>..yamb-plus -o /tmp/yamb-plus-patches/
```
