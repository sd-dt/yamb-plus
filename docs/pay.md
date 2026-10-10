# 每天凌晨自动登录转钱（pay）

## 功能
- 在 QQ 里登记专用"转钱小号"（微软账号 + 密码），每天 04:00 自动：登录 → 等进服 → 发转钱指令 → 下线。
- 登录失败自动排到队尾重试，**每个小号每天最多尝试 4 次**（可用 `PAY_MAX_TRIES` 调整）。
- 转钱期间临时下线其他挂机号（避免占满 MAX_ONLINE、保证指令走小号），结束后自动恢复。
- 执行结果私聊通知（NapCat HTTP）。

## QQ 指令（挂在 mcbot 插件/控制器上，仅主人可用）
| 指令 | 说明 |
| --- | --- |
| `!mcbot addpay <账号> <微软密码>` | 登记转钱小号：生成 `config/bots/<账号>.yaml`（enabled:false，端口自动从 15105 起分配）并登记进 pay.json |
| `!mcbot delpay <账号>` | 从名单移除（yaml 保留但停用） |
| `!mcbot paylist` | 查看收款人/金额/小号名单/今日尝试情况 |
| `!mcbot paytest [账号]` | 立刻跑一次完整流程 |
| `!mcbot payset <收款人> <金额>` | 设置收款人与金额（全号统一，写进 pay.json） |

## 配置文件
- `pay.json`：收款人 / 金额 / 指令模板（默认 `/pay {to} {amount}`，可用 `{to}` `{amount}` 占位）/ 通知 QQ / 小号名单 / 端口映射。样例见 `config/pay.example.json`。
- `pay-state.json`：当日尝试计数与成功名单（每天自动重置），同目录自动生成 `pay.lock` 防并发。

## 部署
1. 把 `scripts/pay-daily.js` 放到服务器（本仓库默认部署在 `/opt/qqbot/mcbot/pay-daily.js`）。
2. 用 `deploy/mc-pay.service.template` 生成 systemd service（替换 `__NODE__` / 密钥等占位符，**建议 chmod 600**），`deploy/mc-pay.timer` 的触发时间是每天 04:00。
3. 必填环境变量：`PAY_CTRL_KEY`（控制器 API key）、`PAY_YAMB_KEY`（yamb 控制口 key）、`PAY_NAPCAT_TOKEN`（NapCat HTTP token，不填则只打日志不私聊）。
4. `sudo systemctl daemon-reload && sudo systemctl enable --now mc-pay.timer`。

## 设计要点 / 坑
- **yamb 在"0 个 enabled bot"时启动即退出**。因此登记小号时如果 yamb 没在跑或没有启用的挂机号，**不要硬重启 yamb**；pay-daily 在启动小号前会先把小号的 yaml 置为 `enabled: true`，若 yamb 未运行则直接 `systemctl --user start`（此时小号恰好撑起启动条件），否则走 yamb 控制口 `/api/instances/start` 热上线。
- 小号上下号 = 改 yaml `enabled` + yamb 控制口热操作（与 switch.sh 的 hot_apply 同路数），**不改动 switch.sh**（保活逻辑零风险）。
- 控制器需要两个小补丁才能服务小号：yambCall 端口回退读 `pay.json.ports`；`/command` 的 enabled 列表并上"已启用的小号"。新增 `/pay-add` `/pay-del` `/pay-list`(GET/POST) `/pay-run` 四个端点。
- `pay.json` 需要容器（AstrBot 写 payset）与宿主用户（控制器/pay-daily）双向可写 → `chmod 666`。
- 成功判定以"转钱指令发送成功"为准，日报里附服务器回复原文，最终以服务器实际到账为准。
