# 架构与数据流

## 组件

| 组件 | 进程/落点 | 职责 |
| --- | --- | --- |
| NapCat A/B | docker 容器 | QQ 协议端，HTTP API（默认 3000/3001，token 在 .env） |
| AstrBot | docker 容器 | QQ 消息框架，反向 WS 连两个 NapCat |
| astrbot_plugin_mcbot | AstrBot 插件 | 解析管理员私聊 `!mcbot/!admin/!qqbot` 指令 → 调控制器 |
| controller.js | systemd 用户单元 `yamb-plus-controller` | HTTP 控制器（默认 127.0.0.1:15100 + docker 网桥） |
| switch.sh | 控制器调用 / keepalive 定时器 | yamb 服务启停 + 多账号热上/下号 + 掉线保活 |
| actions.js | 控制器内嵌 | 连点/动作白名单转发到 yamb |
| connect.js | 控制器内嵌 | 进服/退服播报、QQ→游戏回执抓取 |
| heartbeat.js | 定时器 | 每小时时报（两号状态 + yamb 状态） |
| monitor.js | 定时器 | 掉线监控仲裁：一在线全安静；全掉线邮件（带二维码）+ 自动重启不可达容器 |
| report.js | 定时器 | MC 日报 |
| yamb | systemd 用户单元 `yamb` | 上游 fork，多实例挂机（每账号一个 chat 端口） |

## 关键判定规则

### 热备（/primary）
控制器探测主号 NapCat 是否在线：主号在线 → 返回 `online:true`；插件据此让备用号 self_id
在群里完全静默（避免双回复/双倍 token）。备用号只做「主号挂了还能发消息」的兜底。

### 上/下号只动点名账号（switch.sh）
- 想要的集合 vs 当前启用集合做差：`added = want - cur`、`removed = cur - want`；
- 都为空 → 什么都不做（在线账号绝不重启）；
- yamb 控制口（默认 15199）可用 → 逐个热启/热停，**不影响其他在线账号**；
- 控制口不可用 → 退回 `systemctl --user restart yamb`（会踢掉所有在线号，仅作故障恢复）；
- `/switch` 的 `set` 模式是唯一会关掉未点名账号的模式（把没点名的全关）。

### keepalive（ensure）
有「明确停止」标记 → 不动；服务没跑 → 拉起；有号掉线 → 等宽限期（默认 120s）→
整服务重启一次 → 冷却 900s。属故障恢复兜底，日常切换走热上/下号。

### 备用号发送自检
monitor 每轮用备用号实际发一条探测消息：显示在线但发送被拒（风控）→ 告警
「热备已失效」，处理步骤见 docs/troubleshooting.md。

## 配置加载

- Node 组件：`lib/env.js`（找仓库根 `.env`）+ `lib/fleet.js`（`config/fleet.json`）
- Shell 组件：`lib/env.sh`（source `.env`，`fleet_json` 解析 fleet.json）
- 插件（Python）：读环境变量 `CTRL_PORT/CTRL_KEY/FLEET_FILE`（compose 注入），
  账号表来自 `FLEET_FILE` 指向的 fleet.json
- systemd：unit 里 `EnvironmentFile=<repo>/.env`
