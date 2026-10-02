#!/bin/bash
# yamb 账号控制器 v4 —— 支持多号（上限 2）+ 保活（ensure）
#
#   switch.sh status                查看所有账号：启用标记 + 游戏内状态
#   switch.sh start                 启动 yamb（用当前启用标记；没有标记会提示）
#   switch.sh stop                  停服务 + 清空启用标记 + 记下"上次启用集合" + 标记"明确停止"
#   switch.sh ensure [--quiet]      幂等保活（给定时器用）：
#                                     · 有"明确停止"标记 -> 什么都不做
#                                     · 服务没跑 -> 拉起来
#                                     · 启用标记空了但历史有记录 -> 恢复历史集合
#                                     · 有号掉线 -> 等宽限期(默认120秒) -> 重启一次 -> 冷却900秒
#   switch.sh set     <账号...>     只挂这些号
#   switch.sh enable  <账号...>     追加启用
#   switch.sh disable <账号...>     关闭指定号
#   switch.sh toggle  <账号...>     逐个翻转
#   switch.sh <账号名>              等价于 toggle <账号名>（翻转）
set -u
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/env.sh"

YAMB=$YAMB_DIR
BOTS="$YAMB/config/bots"
UNIT=yamb.service
API_KEY=${YAMB_API_KEY:?缺少 YAMB_API_KEY（复制 .env.example 为 .env 并填写）}
CTRL_PORT=${YAMB_CONTROL_PORT:-15199}   # _p66: yamb 热上下号控制口
ACCOUNTS=$(fleet_json "f.accounts.map(a=>a.name).join(' ')")
MAX_ONLINE=2
LAST_FILE="$HOME/.mcbot-last-enabled"
STOP_FILE="$HOME/.mcbot-explicitly-stopped"
KL_FILE="$HOME/.mcbot-offline-since"
GRACE=${MCBOT_KEEPALIVE_GRACE:-120}
COOLDOWN=${MCBOT_KEEPALIVE_COOLDOWN:-900}
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

MC_PORT_MAP=$(fleet_json "f.accounts.map(a=>a.name+'='+a.port).join(' ')")
port_of () {
  for kv in $MC_PORT_MAP; do
    [ "${kv%%=*}" = "$1" ] && { echo "${kv#*=}"; return 0; }
  done
  echo ""
}
enabled_of ()  { awk '/^enabled:/{print $2; exit}' "$BOTS/$1.yaml" 2>/dev/null; }
set_enabled () { sed -i "s/^enabled: .*/enabled: $2/" "$BOTS/$1.yaml"; }
is_known ()    { for a in $ACCOUNTS; do [ "$a" = "$1" ] && return 0; done; return 1; }
enabled_list (){ for a in $ACCOUNTS; do [ "$(enabled_of "$a")" = "true" ] && echo -n "$a "; done; }
online_of ()   { # 0=在线 1=不在线/无响应
  local p r
  p=$(port_of "$1"); [ -n "$p" ] || return 1
  r=$(curl -s -m 4 -H "x-api-key: $API_KEY" "http://127.0.0.1:$p/api/status" 2>/dev/null)
  case "$r" in *'"minecraft":true'*) return 0 ;; *) return 1 ;; esac
}

uniq_accounts () {
  local out=""
  for a in $ACCOUNTS; do
    case " $* " in *" $a "*) out="$out$a " ;; esac
  done
  printf '%s' "${out% }"
}
count_of () { local n=0; for _ in $1; do n=$((n + 1)); done; echo "$n"; }
apply_set () { # 把启用标记设成 $1
  for a in $ACCOUNTS; do
    case " $1 " in *" $a "*) set_enabled "$a" true ;; *) set_enabled "$a" false ;; esac
  done
}

do_status () {
  local svc; svc=$(systemctl --user is-active "$UNIT" 2>/dev/null)
  local on; on=$(enabled_list)
  echo "服务状态 : ${svc:-未知}"
  echo "启用账号 : $(count_of "$on") / $MAX_ONLINE  ->  ${on:-（无）}"
  [ -f "$STOP_FILE" ] && echo "注意     : 处于【明确停止】状态（保活不会自动拉起；用 start/set/toggle 解除）"
  [ -f "$LAST_FILE" ] && echo "上次集合 : $(cat "$LAST_FILE" 2>/dev/null)"
  echo "------------------------------------------------------------"
  for a in $ACCOUNTS; do
    local en st
    en=$(enabled_of "$a")
    if [ "$en" = "true" ]; then
      if online_of "$a"; then st="在线 ✓"; else st="不在线（服务可能正在启动/重连）"; fi
    else
      st="未启用"
    fi
    printf '  %-14s enabled=%-5s %s\n' "$a" "$en" "$st"
  done
}

do_ensure () {
  local quiet="${1:-}"
  local svc want cur off now last gap
  svc=$(systemctl --user is-active "$UNIT" 2>/dev/null)

  # 保活暂停开关（维护/测试时用：touch 这个文件即可，ensure 一律不动）
  if [ -f "$HOME/.mcbot-keepalive-paused" ]; then
    [ -n "$quiet" ] || echo "保活已暂停（存在 ~/.mcbot-keepalive-paused），不动"
    return 0
  fi

  # ① 明确停止过 -> 一律不动（这是"你下的令"）
  if [ -f "$STOP_FILE" ]; then
    [ -n "$quiet" ] || echo "处于【明确停止】状态，保活不动（用 start/set/toggle 解除）"
    return 0
  fi

  cur=$(enabled_list)
  want=$(cat "$LAST_FILE" 2>/dev/null || echo "")

  # ② 启用标记空了，但历史有记录 -> 恢复（防"被误清空后永远不上线"）
  if [ -z "$cur" ] && [ -n "$want" ]; then
    apply_set "$want"
    cur=$(enabled_list)
    echo "启用标记曾被清空 -> 已从历史恢复：$cur"
  fi
  if [ -z "$cur" ]; then
    [ -n "$quiet" ] || echo "没有任何启用标记（也没有历史记录），不动"
    return 0
  fi

  # ③ 服务没跑 -> 拉起来
  if [ "$svc" != "active" ]; then
    systemctl --user start "$UNIT" 2>/dev/null
    echo "yamb 服务不在运行 -> 已启动（启用：$cur）"
    rm -f "$KL_FILE"
    return 0
  fi

  # ④ 服务在跑 -> 逐个查在线
  off=""
  for a in $cur; do online_of "$a" || off="$off$a "; done
  if [ -z "$off" ]; then
    rm -f "$KL_FILE"
    [ -n "$quiet" ] || echo "全部在线：$cur"
    return 0
  fi

  # ⑤ 有掉线：先看宽限期（防瞬时抖动被误判）
  now=$(date +%s)
  if [ ! -f "$KL_FILE" ]; then
    echo "$now|$off" > "$KL_FILE"
    echo "检测到未在线：$off（开始计时，${GRACE} 秒后再看）"
    return 0
  fi
  last=$(cut -d'|' -f1 "$KL_FILE" 2>/dev/null || echo "$now")
  gap=$(( now - last ))
  if [ "$gap" -lt "$GRACE" ]; then
    [ -n "$quiet" ] || echo "未在线：$off（已 ${gap}s / 宽限 ${GRACE}s，暂不动）"
    return 0
  fi

  # ⑥ 超过宽限期 -> 冷却检查 -> 重启一次
  local cd_file="$HOME/.mcbot-last-restart"
  local lr=0
  [ -f "$cd_file" ] && lr=$(cat "$cd_file" 2>/dev/null || echo 0)
  if [ $(( now - lr )) -lt "$COOLDOWN" ]; then
    echo "未在线：$off，但上次重启才 $(( now - lr ))s（冷却 ${COOLDOWN}s），本次不动"
    return 0
  fi
  # 服务器侧问题（维护/关门/网络）时，重启 yamb 没有任何用，只会添乱
  if tail -120 "$YAMB/yamb.log" 2>/dev/null | grep -qaE "ECONNREFUSED|ETIMEDOUT|ECONNRESET|Connection refused|Connection timed out|connect ETIMEDOUT|Unknown host|getaddrinfo|连接被拒绝|连接超时|无法连接"; then
    echo "日志显示连不上服务器（可能在维护）-> 跳过重启，等服务器恢复"
    rm -f "$KL_FILE"
    rm -f "$cd_file"
    return 0
  fi

  echo "$now" > "$cd_file"
  rm -f "$KL_FILE"
  echo "未在线：$off（持续 $(( now - last ))s）-> 重启 yamb（掉线约 40 秒）"
  systemctl --user restart "$UNIT"
}

# ---- _p66: 热上下号（只动点名账号，其它在线账号连接不断开）----
ctrl_api () { # <method> <path> <json>
  curl -s -m 25 -X "$1" -H "x-api-key: $API_KEY" -H 'Content-Type: application/json' \
       -d "$3" "http://127.0.0.1:$CTRL_PORT$2" 2>/dev/null
}

ctrl_alive () {
  local r
  r=$(curl -s -m 4 -H "x-api-key: $API_KEY" "http://127.0.0.1:$CTRL_PORT/api/instances" 2>/dev/null)
  case "$r" in *'"ok":true'*) return 0 ;; *) return 1 ;; esac
}

# hot_apply <要下线的账号...> <要上线的账号...>
hot_apply () {
  local a r fail=0
  for a in $1; do
    r=$(ctrl_api POST /api/instances/stop "{\"account\":\"$a\"}")
    case "$r" in
      *'"ok":true'*)   echo "   已下线 $a" ;;
      *"没有在运行"*) echo "   $a 本来就没在运行（无需下线）" ;;
      *) echo "   ❌ 下线 $a 失败：${r:-(控制口无响应)}"; fail=$((fail + 1)) ;;
    esac
  done
  for a in $2; do
    r=$(ctrl_api POST /api/instances/start "{\"account\":\"$a\"}")
    case "$r" in
      *'"ok":true'*) echo "   已上线 $a（约 15~25 秒后进服）" ;;
      *"已在运行"*)  echo "   $a 已在运行（无需上线）" ;;
      *) echo "   ❌ 上线 $a 失败：${r:-(控制口无响应)}"; fail=$((fail + 1)) ;;
    esac
  done
  [ "$fail" -eq 0 ] && return 0
  return 1
}

# do_apply <模式> <账号...>
do_apply () {
  local mode="$1"; shift
  local args cur want a

  if [ $# -eq 0 ]; then
    echo "❌ 没给账号名。可用：$ACCOUNTS"
    echo "   用法：switch.sh $mode <账号...>（全部下线用 switch.sh stop）"
    return 2
  fi
  for a in "$@"; do
    if ! is_known "$a"; then
      echo "❌ 未知账号：$a"; echo "   可用：$ACCOUNTS"; return 2
    fi
  done

  args=$(uniq_accounts "$@")
  cur=$(uniq_accounts "$(enabled_list)")
  [ -z "$args" ] && { echo "❌ 参数里没有有效账号（可用：$ACCOUNTS）"; return 2; }

  case "$mode" in
    set)     want="$args" ;;
    enable)  want=$(uniq_accounts "$cur $args") ;;
    disable)
      local tmp=""
      for a in $cur; do case " $args " in *" $a "*) ;; *) tmp="$tmp$a " ;; esac; done
      want=$(uniq_accounts "$tmp") ;;
    toggle)
      local tmp=""
      for a in $cur; do case " $args " in *" $a "*) ;; *) tmp="$tmp$a " ;; esac; done
      for a in $args; do case " $cur " in *" $a "*) ;; *) tmp="$tmp$a " ;; esac; done
      want=$(uniq_accounts "$tmp") ;;
    *) echo "❌ 未知模式：$mode"; return 2 ;;
  esac

  local n; n=$(count_of "$want")
  if [ "$n" -gt "$MAX_ONLINE" ]; then
    echo "❌ 最多同时挂 $MAX_ONLINE 个号，这次会变成 $n 个：${want:-（无）}"
    echo "   当前启用：${cur:-（无）}"; return 2
  fi
  if [ "$want" = "$cur" ]; then
    echo "启用集合没变化：${cur:-（无）}"
    return 0
  fi

  apply_set "$want"
  echo "$want" > "$LAST_FILE"     # 记下"上次启用集合"，供 ensure 恢复
  rm -f "$STOP_FILE"              # 用户明确动作 -> 解除"明确停止"
  rm -f "$KL_FILE"
  # _p66: 只对"本次指令点名的账号"做上下号；未提及的在线账号保持连接不动
  local added="" removed="" a
  for a in $want; do case " $cur " in *" $a "*) ;; *) added="$added$a " ;; esac; done
  for a in $cur;  do case " $want " in *" $a "*) ;; *) removed="$removed$a " ;; esac; done

  if [ -z "$added" ] && [ -z "$removed" ]; then
    echo "启用集合：${cur:-（无）} -> ${want:-（无）}（无实际变化）"
    return 0
  fi

  local svc
  svc=$(systemctl --user is-active "$UNIT" 2>/dev/null)
  if [ "$svc" != "active" ]; then
    systemctl --user start "$UNIT"
    echo "yamb 未在运行 -> 已启动（启用：${want:-(无)}）"
    return 0
  fi

  if ctrl_alive; then
    if hot_apply "$removed" "$added"; then
      echo "启用集合：${cur:-（无）} -> ${want:-（无）}（热上下号完成，未提及的在线账号连接未断开）"
      return 0
    fi
    echo "⚠️ 部分账号热操作失败（启用标记已更新；未动的账号不受影响，保活稍后会自愈）"
    return 1
  fi

  # 兜底：控制口不可用（旧版 yamb / 控制口没起来）-> 退回整服务重启（所有号都会掉线重连）
  echo "（热上下号不可用）启用集合：${cur:-（无）} -> ${want:-（无）}，重启 yamb（全部号掉线约 40 秒）..."
  systemctl --user restart "$UNIT"
  return 0
}

case "${1:-status}" in
  status)  do_status ;;
  ensure)  shift; do_ensure "${1:-}" ;;
  start)
    cur=$(enabled_list)
    if [ -z "$cur" ]; then
      want=$(cat "$LAST_FILE" 2>/dev/null || echo "")
      if [ -n "$want" ]; then apply_set "$want"; cur=$(enabled_list); echo "启用标记为空 -> 从历史恢复：$cur"; fi
    fi
    if [ -z "$cur" ]; then
      echo "❌ 当前没有任何启用标记（也没有历史记录）"
      echo "   请先指定要挂的号：switch.sh 1|2|3|4 或 switch.sh set <账号...>"
      exit 2
    fi
    [ -n "$cur" ] && echo "$cur" > "$LAST_FILE"
    rm -f "$STOP_FILE" "$KL_FILE"
    systemctl --user start "$UNIT"
    echo "已启动：$cur"
    ;;
  stop)
    systemctl --user stop "$UNIT"
    cur=$(enabled_list)
    [ -n "$cur" ] && echo "$cur" > "$LAST_FILE"   # 先记下，供以后恢复
    for a in $ACCOUNTS; do set_enabled "$a" false; done
    touch "$STOP_FILE"                            # 明确停止：保活不许自动拉起
    rm -f "$KL_FILE"
    echo "已停止，并已清空启用标记（上次集合已记下：$(cat "$LAST_FILE" 2>/dev/null)）"
    echo "保活不会自动拉起（这是你下的令）。要恢复：!mcbot start 或 !mcbot 1 2"
    ;;
  set|enable|disable|toggle)
    m="$1"; shift; do_apply "$m" "$@" ;;
  "")
    do_status ;;
  *)
    if is_known "$1"; then do_apply toggle "$1"
    else
      echo "❌ 未知参数：$1"
      echo "用法：switch.sh status|ensure|start|stop|set|enable|disable|toggle [账号...]"
      exit 2
    fi
    ;;
esac
