#!/bin/bash
# QQBot 一键体检 v3（双号版 + yamb 详情）—— 用法: bash /opt/qqbot/status.sh
TOKEN=${ONEBOT_TOKEN:-change-me}
CTRL_KEY=${MCBOT_CTRL_KEY:-change-me}
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
cd /opt/qqbot || exit 1

probe () {  # $1=端口  $2=名字  $3=取码脚本
  local R
  R=$(curl -s -m 8 -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$1/get_login_info")
  echo -n ">>> $2："
  if echo "$R" | grep -q '"user_id"'; then
    echo "$R" | sed 's/.*"nickname":"\([^"]*\)".*"user_id":"\?\([0-9]*\)"\?.*/【在线】\1 (\2)/'
  elif echo "$R" | grep -q '"status":"failed"'; then
    echo "【离线·未登录】需要扫码 -> bash $3"
  else
    echo "【API 不通】容器可能卡死 —— $R"
  fi
}

echo "=========== QQBot 体检 $(date '+%F %T') 双号版 ==========="
echo ""
echo "--- 1. 容器状态 ---"
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -Ei 'NAMES|snowluma|astrbot|yamb'
echo ""
echo "--- 2. QQ 在线状态 ---"
probe 3000 "A 地图bot2.0     " /opt/qqbot/qr_snowluma.sh
# _p68: probe 3001 "B 地图bot2备用线路" /opt/qqbot/qr-b.sh   # 双号已取消
echo ""
echo "--- 3. 热备仲裁（控制器 /primary）---"
P=$(curl -s -m 8 -H "x-api-key: $CTRL_KEY" http://127.0.0.1:15100/primary)
case "$P" in *'"ok"'*) ;; *) P=$(curl -s -m 8 -H "Authorization: Bearer $CTRL_KEY" http://127.0.0.1:15100/primary) ;; esac
echo "$P"
echo "    （online:true 表示主号可用，备用号会自动闭嘴，避免双回复/双倍 token）"
echo -n "    备用号发送自检："; node -e "try{const s=require('/opt/qqbot/state.json');console.log(s.spareSendOk===undefined?'未做过':(s.spareSendOk?'正常 '+(s.spareSendAt||''):'异常! '+(s.spareSendNote||'')))}catch(e){console.log('读取失败')}"
echo ""
echo "--- 4. 最近掉线原因（关键）---"
for c in snowluma; do
  echo "  [$c]"
  docker logs --tail=400 "$c" 2>&1 | grep -iE "KickedOffLine|账号状态变更为离线|快速登录错误|Login Error|二维码已被扫描" | tail -3
done
echo ""
echo "--- 5. 监控最近记录 ---"
tail -6 /opt/qqbot/monitor.log 2>/dev/null
echo ""
echo "--- 6. 定时器 ---"
systemctl list-timers mc-daily-login.timer qqbot-monitor.timer qqbot-backup.timer qqbot-heartbeat.timer --no-pager 2>/dev/null | head -7
echo ""
echo "--- 7. 内存 ---"
free -h | head -2
echo ""
echo "--- 8. AstrBot 连接状态 ---"
docker logs --tail=800 astrbot 2>/dev/null | grep -iE "适配器已连接|适配器已被关闭" | tail -4
echo -n "  OneBot WS-Client 连接次数（应有记录）："
docker logs snowluma 2>&1 | grep -c '\[astrbot\] connected'
echo ""
echo "--- 9. 最近的 MC 任务 / 日报 ---"
tail -6 /opt/qqbot/mc/daily.log 2>/dev/null
echo ""
echo "--- 10. yamb（MC 挂机）---"
ACT=$(systemctl --user is-active yamb.service 2>/dev/null)
if [ "$ACT" = "active" ]; then
  echo "  状态：运行中 ✅（stop 不会自动拉起，注意别停久了）"
else
  echo "  状态：已停止 ⏹（$ACT）  想挂机：systemctl --user start yamb"
fi
systemctl --user show yamb.service \
  -p Result -p ExecMainStatus -p ExecMainCode -p NRestarts \
  -p MemoryHigh -p MemoryMax -p ActiveEnterTimestamp -p InactiveEnterTimestamp 2>/dev/null \
  | sed 's/^/    /'
case "$(systemctl --user show yamb.service -p Result --value 2>/dev/null)" in
  success)   echo "    >>> 判读：正常退出（多半是主动 stop，或 !mcbot stop / !mcbot <账号名> 触发）" ;;
  exit-code) echo "    >>> 判读：程序自己崩了，看 yamb.log 死前几行" ;;
  signal)    echo "    >>> 判读：被信号杀掉（可能是内存超限），查 MemoryMax 与 dmesg" ;;
  oom-kill)  echo "    >>> 判读：被 cgroup 内存上限杀掉，放宽 memlimit.conf 里的 MemoryMax" ;;
  *)         echo "    >>> 判读：（无记录，可能从未启动过）" ;;
esac
echo -n "    启用的账号："
FOUND=0
for f in /opt/qqbot/yamb/config/bots/*.yaml; do
  [ -f "$f" ] || continue
  if grep -Eq "^enabled:[[:space:]]*true" "$f"; then basename "$f" .yaml | tr '\n' ' '; FOUND=1; fi
done
[ "$FOUND" = "0" ] && echo -n "（没有账号被启用）"
echo ""
echo "    内存保险丝："
sed 's/^/      /' "$HOME/.config/systemd/user/yamb.service.d/memlimit.conf" 2>/dev/null || echo "      （无 drop-in）"
echo "    日志尾部："
tail -5 /opt/qqbot/yamb/yamb.log 2>/dev/null | sed 's/^/      /'
echo "    启停命令：systemctl --user start|stop|restart yamb   （日志 tail -f /opt/qqbot/yamb/yamb.log）"
echo ""
echo "=========== 体检结束 ==========="
