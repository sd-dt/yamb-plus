# 每天凌晨自动登录转钱（pay）

## 功能
- 在 QQ 里登记专用"转钱小号"（微软账号 + 密码），每天 04:00 自动：登录 → 等进服 → 发转钱指令 → 下线。
- 登录失败自动排到队尾重试，**每个小号每天最多尝试 4 次**（可用 `PAY_MAX_TRIES` 调整）。
- 转钱期间临时下线其他挂机号（避免占满 MAX_ONLINE、保证指令走小号），结束后自动恢复。
- 执行结果私聊通知（NapCat HTTP）。
- 支持单号开关：`payon/payoff` 控制每个小号是否参与每日自动转钱（`pay.json` 的 `paySwitch` 键，缺省=开；`paytest` 手动点名不受开关限制）。

## QQ 指令（挂在 mcbot 插件/控制器上，仅主人可用）
| 指令 | 说明 |
| --- | --- |
| `!mcbot addpay <账号>` | 登记转钱小号：生成 `config/bots/<账号>.yaml`（enabled:false，password 恒为空，端口自动从 15105 起分配），并当场发微软官方网址 + 一次性设备授权码 |
| `!mcbot delpay <账号>` | 从名单移除（尽力下线该号，yaml 改名 `.removed-日期` 备份） |
| `!mcbot paylist` | 查看收款人/金额/小号名单/今日尝试情况 |
| `!mcbot paytest [账号]` | 立刻跑一次完整流程 |
| `!mcbot payauth <账号>` | 重新发设备授权码（token 失效/登录失败时用） |
| `!mcbot payset <收款人> <金额>` | 设置收款人与金额（全号统一，写进 pay.json） |
| `!mcbot payoff <账号>` | 暂停该小号的每日自动转钱（paytest 手动点名不受影响） |
| `!mcbot payon <账号>` | 恢复该小号的每日自动转钱 |

## 授权方式（_p70 设备码流，免密码）
- `addpay`/`payauth` 会拉起 `scripts/ms-device-auth.js`（与 yamb 登录完全同构：同 token 缓存目录 `MC_PROFILES_FOLDER`、同 `flow: live`、同 authTitle），用 prismarine-auth 走微软官方设备码流程。
- QQ 里只出现 `https://www.microsoft.com/link` 和一次性授权码（15 分钟有效）；**微软密码全程不经过聊天记录、不落任何配置文件**。
- 浏览器完成授权后 token 写进本地缓存（`<sha1(账号)前6>_live-cache.json` 等），yamb 登录时直接复用缓存，不再要密码。
- 授权脚本完成/失败都会私聊通知；授权状态文件在 `/opt/qqbot/mcbot/pay-auth/`（700/600，只含网址和授权码，不含 token）。

## 安全强化清单
- 微软密码不再出现在 QQ/配置/日志（设备码授权替代）。
- token 缓存目录 `chmod 700`；授权状态目录/文件 700/600。
- `pay.json` 666（容器 payset 与宿主双向写，但不含任何凭据）。
- systemd 单元含密钥的 `mc-pay.service` 为 600；`ms-device-auth.js` 的密钥全走环境变量。

## 配置文件
- `pay.json`：收款人 / 金额 / 指令模板（默认 `/pay {to} {amount}`，可用 `{to}` `{amount}` 占位）/ 通知 QQ / 小号名单 / 端口映射 / `paySwitch`（`{账号: bool}`，缺省=开，控制每个号是否参与每日自动转钱）。样例见 `config/pay.example.json`。
- `pay-state.json`：当日尝试计数与成功名单（每天自动重置），同目录自动生成 `pay.lock` 防并发。

## 部署
1. 把 `scripts/pay-daily.js`（转钱执行器）与 `scripts/ms-device-auth.js`（设备码授权器）放到服务器（本仓库默认部署在 `/opt/qqbot/mcbot/pay-daily.js`）。
2. 用 `deploy/mc-pay.service.template` 生成 systemd service（替换 `__NODE__` / 密钥等占位符，**建议 chmod 600**），`deploy/mc-pay.timer` 的触发时间是每天 04:00。
3. 必填环境变量：`PAY_CTRL_KEY`（控制器 API key）、`PAY_YAMB_KEY`（yamb 控制口 key）、`PAY_NAPCAT_TOKEN`（NapCat HTTP token，不填则只打日志不私聊）。
4. `sudo systemctl daemon-reload && sudo systemctl enable --now mc-pay.timer`。

## 设计要点 / 坑
- **yamb 在"0 个 enabled bot"时启动即退出**。因此登记小号时如果 yamb 没在跑或没有启用的挂机号，**不要硬重启 yamb**；pay-daily 在启动小号前会先把小号的 yaml 置为 `enabled: true`，若 yamb 未运行则直接 `systemctl --user start`（此时小号恰好撑起启动条件），否则走 yamb 控制口 `/api/instances/start` 热上线。
- 小号上下号 = 改 yaml `enabled` + yamb 控制口热操作（与 switch.sh 的 hot_apply 同路数），**不改动 switch.sh**（保活逻辑零风险）。
- 控制器需要两个小补丁才能服务小号：yambCall 端口回退读 `pay.json.ports`；`/command` 的 enabled 列表并上"已启用的小号"。新增 `/pay-add` `/pay-del` `/pay-list`(GET/POST) `/pay-run` 四个端点。
- `pay.json` 需要容器（AstrBot 写 payset）与宿主用户（控制器/pay-daily）双向可写 → `chmod 666`。
- **小号 yaml 必须有 `helpLines`**（_p73b）：yamb 的 config loader 对缺 helpLines 的 bot 直接 `process.exit(1)`，表现为 yamb 启动 2 秒即退、控制口 15199 永远不监听。手工建的小号 yaml 要补。
- **pgrep 自匹配坑**（_p73b）：`pgrep -f "yamb/dist/index.js"` 会匹配到 execSync 的 `sh -c` 包装进程自身 → `yambAlive()` 恒 true、永远不走 systemctl 拉起分支；模式必须写成 `yamb/dist/index[.]js`。
- **yamb 刚被 systemctl 拉起时控制口未监听**（_p73b）：`systemctl --user start`（Type=simple）立即返回，yamb 启动需数秒；pay-daily 对 `/api/instances/start` 的 ECONNREFUSED 做重试等待（默认最多 30s，`PAY_CTRL_WAIT_MS` 可调）。
- 成功判定以"转钱指令发送成功"为准，日报里附服务器回复原文，最终以服务器实际到账为准。
