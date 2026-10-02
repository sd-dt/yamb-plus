#!/bin/bash
# 一键体检 —— 用法: bash monitor/status.sh
# 所有密钥/端口/容器名来自 .env 与 config/fleet.json（见 lib/env.sh）
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/env.sh"
TOKEN=${NAPCAT_TOKEN:?缺少 NAPCAT_TOKEN（复制 .env.example 为 .env 并填写）}
CTRL_KEY=${CTRL_KEY:?缺少 CTRL_KEY}
CTRL_PORT=${CTRL_PORT:-15100}
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
cd "$NAPCAT_COMPOSE_DIR" || exit 1

probe () {  # $1=端口  $2=名字  $3=取码命令
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

# 从 fleet.json 组装探测行：port|key 名称|取码命令
NAP_ROWS=$(fleet_json "f.napcat.map(n=>[n.port,(n.key||'?')+' '+(n.name||''),n.qr_script||('monitor/qr.sh '+(n.container||''))].join('|')).join(';')")
NAP_CONTAINERS=$(fleet_json "f.napcat.map(n=>n.container).join(' ')")

echo "=========== QQBot 体检 $(date '+%F %T') ==========="
echo ""
echo "--- 1. 容器状态 ---"
docker compose ps
echo ""
echo "--- 2. QQ 在线状态 ---"
IFS=';' read -ra _ROWS <<< "${NAP_ROWS:-}"
for _r in "${_ROWS[@]}"; do
  IFS='|' read -r _port _name _qr <<< "$_r"
  case "$_qr" in /*) _qrcmd="bash $_qr" ;; *) _qrcmd="bash $_MCBOT_ROOT/$_qr" ;; esac
  probe "$_port" "$_name" "$_qrcmd"
done
echo ""
echo "--- 3. 热备仲裁（控制器 /primary）---"
P=$(curl -s -m 8 -H "x-api-key: $CTRL_KEY" "http://127.0.0.1:$CTRL_PORT/primary")
case "$P" in *'"ok"'*) ;; *) P=$(curl -s -m 8 -H "Authorization: Bearer $CTRL_KEY" "http://127.0.0.1:$CTRL_PORT/primary") ;; esac
echo "$P"
echo "    （online:true 表示主号可用，备用号会自动闭嘴，避免双回复/双倍 token）"
echo -n "    备用号发送自检："
node -e "try{const s=require('$STATE_FILE');console.log(s.spareSendOk===undefined?'未做过':(s.spareSendOk?'正常 '+(s.spareSendAt||''):'异常! '+(s.spareSendNote||'')))}catch(e){console.log('读取失败')}"
echo ""
echo "--- 4. 最近掉线原因（关键）---"
for c in $NAP_CONTAINERS; do
  echo "  [$c]"
  docker logs --tail=400 "$c" 2>&1 | grep -iE "KickedOffLine|账号状态变更为离线|快速登录错误|Login Error|二维码已被扫描" | tail -3
done
echo ""
echo "--- 5. 监控最近记录 ---"
tail -6 "$MONITOR_LOG" 2>/dev/null
echo ""
echo "--- 6. 定时器 ---"
systemctl list-timers $MCBOT_TIMERS --no-pager 2>/dev/null | head -7
echo ""
echo "--- 7. 内存 ---"
free -h | head -2
echo ""
echo "--- 8. AstrBot 连接状态 ---"
docker compose logs --tail=800 astrbot 2>/dev/null | grep -iE "适配器已连接|适配器已被关闭" | tail -4
_i=0
for c in $NAP_CONTAINERS; do
  _i=$((_i + 1))
  echo -n "  WS 连接数（应为 1）[#$_i $c]："
  docker exec "$c" sh -c 'cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | grep -c ":1837"' 2>/dev/null
done
echo ""
echo "--- 9. 最近的 MC 任务 / 日报 ---"
tail -6 "${MC_LOG_FILE%/*}/daily.log" 2>/dev/null
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
  oom-kill)  echo "    >>> 判读：被 cgroup 内存上限杀掉，放宽 unit 的 MemoryMax" ;;
  *)         echo "    >>> 判读：（无记录，可能从未启动过）" ;;
esac
echo -n "    启用的账号："
FOUND=0
for f in "$YAMB_DIR"/config/bots/*.yaml; do
  [ -f "$f" ] || continue
  if grep -Eq "^enabled:[[:space:]]*true" "$f"; then basename "$f" .yaml | tr '\n' ' '; FOUND=1; fi
done
[ "$FOUND" = "0" ] && echo -n "（没有账号被启用）"
echo ""
echo "    日志尾部："
tail -5 "$YAMB_DIR/yamb.log" 2>/dev/null | sed 's/^/      /'
echo "    启停命令：systemctl --user start|stop|restart yamb   （日志 tail -f $YAMB_DIR/yamb.log）"
echo ""
echo "=========== 体检结束 ==========="
