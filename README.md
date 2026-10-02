# yamb-plus

> **上游项目：[nanoite/yamb](https://github.com/nanoite/yamb)** —— 本项目基于上游的 Minecraft 挂机机器人
> （mineflayer + carpet 风格指令）构建，上游的安装与指令文档全部适用。yamb 本体以 **GPL-3.0-or-later**
> 授权（见仓库内 LICENSE），本仓库外围组件为 MIT（见下）。

yamb-plus 是一套「**用 QQ 管理你的 MC 挂机机器人**」的自托管全家桶，在上游 yamb 之上补齐了三块能力：

1. **QQ 控制层**：AstrBot 插件 + HTTP 控制器，管理员私聊 `!mcbot ...` 即可上/下号、连点、发游戏指令；
2. **双号热备**：两个 NapCat QQ 互为备份，主号掉线备用号自动接管，全掉线时发邮件告警 + 二维码自救；
3. **账号运维**：多账号热上/下号（不打扰在线账号）、掉线保活、一键体检、每小时时报。

所有敏感信息（QQ 号、MC 账号、bot 名、服务器地址、密钥）**一律不进代码**，只放在
`.env` + `config/fleet.json` + `config/credentials.yaml` 三份被 gitignore 的配置里。

## 架构

```
手机 QQ ──► NapCat A（主号）──┐
手机 QQ ──► NapCat B（备用）──┤  反向 WS
                             ▼
                        AstrBot 容器
                        └─ astrbot_plugin_mcbot   （管理员私聊指令解析）
                             │  HTTP (CTRL_PORT, CTRL_KEY)
                             ▼
                   controller/controller.js   ← lib/env.js + lib/fleet.js
                    ├─ /switch ──► controller/switch.sh ──► yamb 控制口 :15199（热上/下号）
                    ├─ /action ──► controller/actions.js ──► yamb HTTP API（连点/动作）
                    ├─ /connect ──► controller/connect.js   （进服提示/回执抓取）
                    └─ /restart-astrbot（写完白名单后延迟重启）
                             ▼
                yamb（上游 fork，多实例，每账号一个 chat 端口）
                             ▼
                       Minecraft 服务器

monitor/heartbeat.js   每小时时报（QQ 私聊/群）
monitor/monitor.js     掉线监控：仅全掉线才告警，邮件带二维码，自动重启不可达容器
monitor/report.js      MC 日报
monitor/status.sh      一键体检
```

## 快速开始

```bash
git clone <本仓库地址> yamb-plus && cd yamb-plus

# 1. 配置
cp .env.example .env && vi .env              # 密钥/端口/容器账号
cp config/fleet.example.json config/fleet.json && vi config/fleet.json
                                             # QQ号 / MC账号名 / bot名 / 管理员 / 通知对象
vi config/credentials.yaml                   # 微软登录凭据（可选，见下）
cp monitor/mail.conf.example.json monitor/mail.conf.json   # 或跑 monitor/mail-setup.sh

# 2. 放入 yamb 本体（需要差异分支提供的控制器接口，见 docs/yamb-fork.md）
git clone -b yamb-plus <你的yamb fork> yamb

# 3. 安装（生成单元、软链 .env、自检必填项）
bash deploy/install.sh

# 4. 起容器、扫码、启动服务
cd deploy/compose && docker compose up -d && cd -
bash monitor/qr.sh napcat          # 主号扫码（napcat-b 同理）
systemctl --user enable --now yamb-plus-controller yamb yamb-keepalive.timer

# 5. 把插件拷进 AstrBot
cp -r plugin/astrbot_plugin_mcbot <astrbot-data>/plugins/
docker restart astrbot

# 6. 验证
bash monitor/status.sh
```

## 配置接口一览

| 文件（全部 gitignore） | 谁在用 | 内容 |
| --- | --- | --- |
| `.env` | 所有组件 + compose + systemd | 端口、API key、NapCat token、QQ 账号（compose 用）、路径 |
| `config/fleet.json` | 控制器 / switch.sh / 插件 / 监控 | **QQ 号、MC 账号名、bot 显示名、管理员、通知对象** |
| `config/credentials.yaml` | yamb fork（loader） | 微软登录凭据（bot id → username/password），不写则走 bots yaml 里的 account 段 |
| `monitor/mail.conf.json` | mailer | SMTP 告警邮箱 |
| `config/forbidden.txt` | scripts/check-secrets.sh | 泄密扫描的字面禁串（每行一个，绝不入库；示例见 forbidden.example.txt） |
| `deploy/compose/docker-compose.yml` | docker compose | 由 `deploy/docker-compose.example.yml` 生成 |

`config/fleet.json` 字段：

```jsonc
{
  "accounts": [                     // MC 挂机账号（名字 = QQ 指令里用的账号名）
    { "name": "bot1", "num": "1", "port": 15101 }   // num: QQ 指令数字别名；port: yamb chat 端口
  ],
  "napcat": [                       // QQ 协议端实例（双号热备）
    { "key": "A", "name": "主号", "uin": "QQ号", "container": "napcat", "port": 3000,
      "qr_script": "monitor/qr.sh napcat" }
  ],
  "admins": [],                     // QQ 号：拥有全部权限
  "owners": [],                     // QQ 号：才能切换挂机账号（防手滑）
  "spare_self_ids": [],             // QQ 号：备用号 self_id（群里静默）
  "trusted_groups": [],             // 群号：信任的群
  "notify_user": "",                // 报时/告警私聊对象
  "notify_group": ""                // 报时/告警群（可空）
}
```

## 目录结构

```
lib/                    env(.js/.sh) 加载器 + fleet.json 加载器（所有组件共用）
controller/             控制器、switch.sh（热上/下号 + 保活）、动作转发、回执抓取
monitor/                时报 / 掉线监控 / 日报 / 体检 / 邮件 / 扫码工具
plugin/                 AstrBot 插件 astrbot_plugin_mcbot
deploy/                 install.sh、compose 模板、systemd 单元模板
docs/                   architecture / troubleshooting / yamb-fork
scripts/check-secrets.sh  提交前泄密扫描
```

## 常用运维

```bash
bash monitor/status.sh                                   # 一键体检
controller/switch.sh status                              # 挂机账号状态
controller/switch.sh enable bot1                         # 上号（只动点名账号）
controller/switch.sh disable bot1                        # 下号（在线账号不受影响）
bash monitor/qr.sh napcat                                # 主号掉线扫码重登
systemctl --user restart yamb-plus-controller            # 重启控制器
journalctl --user -u yamb -f                             # 看 yamb 日志（unit 落盘到 yamb/yamb.log）
```

QQ 侧指令（管理员私聊）：`!mcbot help` 看全部，核心是
`!mcbot list / start / stop / <账号> / <账号> <动作> / command <指令> / yamb <指令>`、`!admin <QQ号>`、`!qqbot stop`。

## 与上游的差异（yamb fork）

见 [docs/yamb-fork.md](docs/yamb-fork.md)：action API、outgoing 日志、热上/下号控制口、
keepalive/rp-fix 注入、微软登录凭据接口（`config/credentials.yaml`）。

## License

- `yamb/`（上游 fork 部分）：**GPL-3.0-or-later**，版权归上游作者
- 本仓库其余部分：**MIT**
