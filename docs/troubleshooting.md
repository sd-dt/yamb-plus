# 运维与排障手册

以下命令假设在仓库根目录执行。

## 1. 一键体检

```bash
bash monitor/status.sh
```

输出逐节判读：

- **1 容器状态**：napcat / astrbot / napcat-b 应全部 Up。
- **2 QQ 在线状态**：【离线·未登录】→ 第 3 节扫码；【API 不通】→ 容器卡死，`docker restart napcat`。
- **3 热备仲裁**：`/primary` 的 `online:true` = 主号可用，备用号会自动静默。
  `备用号发送自检：异常!` = 热备失效，见第 4 节。
- **4 最近掉线原因**：KickedOffLine（被顶号/风控）、快速登录错误（token 失效）等关键字。
- **8 WS 连接数**：每个 NapCat 容器对 AstrBot 的反向 WS 应为 1；0 = 掉了，重启 astrbot 容器。
- **10 yamb**：`Result` 判读——
  `success`=正常退出（多半是主动 stop）；`exit-code`=程序崩溃看 yamb.log；
  `signal`/`oom-kill`=被内存上限杀掉，放宽 unit 的 `MemoryMax`。

## 2. QQ 掉线自救

```bash
bash monitor/qr.sh napcat        # 打印主号新二维码（备用号: napcat-b）
```

手机 QQ 扫码登录后，`monitor/status.sh` 第 2 节应变【在线】。
全掉线时 monitor 会自动发二维码邮件（每 10 分钟一封），也可等邮件。

## 3. 备用号"显示在线但发不出消息"（热备失效）

1. 手机登录该号，手动发一条消息，确认账号本身没被风控；
2. 让 NapCat 重启协议层：`curl -X POST -H "Authorization: Bearer $NAPCAT_TOKEN" http://127.0.0.1:<备用端口>/set_restart`；
3. 出二维码就 `bash monitor/qr.sh napcat-b` 扫码重登；
4. 重测：下一轮时报的「备用号发送自检」应变正常。

## 4. 挂机账号管理（只动点名的账号）

```bash
controller/switch.sh status            # 各账号启用标记 + 游戏内状态
controller/switch.sh enable bot1       # 上号（其他在线账号不受影响）
controller/switch.sh disable bot1      # 下号
controller/switch.sh toggle bot1 bot2  # 逐个翻转
controller/switch.sh set bot1          # 只挂 bot1（未点名的全关——注意语义）
controller/switch.sh stop              # 全下线 + 明确停止（keepalive 不再拉起）
controller/switch.sh start             # 按上次启用集合拉起
controller/switch.sh ensure            # 手动触发一次保活（定时器每 2 分钟自动跑）
```

日志排查：`yamb/yamb.log`、`data/controller.log`。

## 5. yamb 被内存杀掉

`monitor/status.sh` 第 10 节 `Result=oom-kill/signal` → 放宽
`~/.config/systemd/user/yamb.service.d/memlimit.conf`（或 unit 模板里的 `MemoryMax`），
然后 `systemctl --user daemon-reload && systemctl --user restart yamb`。

## 6. 邮件告警配置 / 测试

```bash
bash monitor/mail-setup.sh                    # 交互式生成 monitor/mail.conf.json（SMTP 授权码不是邮箱密码）
node monitor/monitor.js --test-qr             # 只发一封二维码测试邮件
node monitor/monitor.js --probe               # 只打印两号状态，不发通知
```

## 7. 控制器 API

```bash
curl -H "x-api-key: $CTRL_KEY" http://127.0.0.1:$CTRL_PORT/health
curl -H "x-api-key: $CTRL_KEY" http://127.0.0.1:$CTRL_PORT/primary
curl -H "x-api-key: $CTRL_KEY" http://127.0.0.1:$CTRL_PORT/status
```

## 8. 插件（AstrBot 侧）

- 指令只认**管理员私聊**，群内静默（备用号 self_id 更是全程闭嘴）；
- `!admin <QQ号>` 加管理员后插件会调控制器延迟重启 AstrBot 让会话白名单生效；
- `!qqbot stop` 关 AI 聊天省 token（`!qqbot start` 恢复）；
- 插件配置全部来自环境变量 `CTRL_PORT/CTRL_KEY/FLEET_FILE`（compose 已注入），
  QQ 号/账号表在 `FLEET_FILE` 指向的 fleet.json 里，改完 `docker restart astrbot`。
