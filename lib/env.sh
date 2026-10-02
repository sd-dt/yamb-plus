# 供本仓库 shell 脚本 source：加载仓库根目录 .env，并提供 fleet.json 的 bash 解析
_MCBOT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$_MCBOT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$_MCBOT_ROOT/.env"
  set +a
fi

: "${FLEET_FILE:=$_MCBOT_ROOT/config/fleet.json}"
: "${YAMB_DIR:=$_MCBOT_ROOT/yamb}"
: "${NAPCAT_COMPOSE_DIR:=$_MCBOT_ROOT/deploy/compose}"
: "${STATE_FILE:=$_MCBOT_ROOT/data/state.json}"
: "${MONITOR_LOG:=$_MCBOT_ROOT/data/monitor.log}"
: "${MC_LOG_FILE:=$_MCBOT_ROOT/data/last-run.log}"
# 相对路径统一按仓库根目录解析
for _v in FLEET_FILE YAMB_DIR NAPCAT_COMPOSE_DIR STATE_FILE MONITOR_LOG MC_LOG_FILE; do
  eval "case \"\${$_v}\" in /*) ;; *) $_v=\"$_MCBOT_ROOT/\${$_v}\" ;; esac"
done

# fleet_json <js表达式>：用 node 从 fleet.json 取值（yamb 本身依赖 node，因此 node 必然可用）
# 例： fleet_json "f.accounts.map(a=>a.name).join(' ')"
fleet_json () {
  node -e '
    const fs = require("fs");
    const f = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    const out = eval(process.argv[2]);
    if (out !== undefined && out !== null) console.log(out);
  ' "$FLEET_FILE" "$1" 2>/dev/null
}
