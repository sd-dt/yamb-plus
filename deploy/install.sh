#!/bin/bash
# yamb-plus 安装脚本（用户级 systemd，无需 root）
# 用法: bash deploy/install.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_BIN=$(command -v node || { echo "!! 需要 node（yamb 本体依赖）"; exit 1; })

echo "== 1. 生成配置文件（已存在的跳过） =="
if [ ! -f "$REPO/.env" ]; then
  cp "$REPO/.env.example" "$REPO/.env"
  chmod 600 "$REPO/.env"
  # 自动生成两个 API key；NAPCAT_TOKEN / QQ 账号必须用户自己填
  if command -v openssl >/dev/null; then
    sed -i "s|^CTRL_KEY=$|CTRL_KEY=$(openssl rand -hex 16)|" "$REPO/.env"
    sed -i "s|^YAMB_API_KEY=$|YAMB_API_KEY=$(openssl rand -hex 16)|" "$REPO/.env"
  fi
  echo "   已创建 $REPO/.env —— 【必填项】请编辑后重跑本脚本："
  echo "     NAPCAT_TOKEN / NAPCAT_A_ACCOUNT / NAPCAT_B_ACCOUNT / ASTRBOT_DATA_DIR / FLEET_FILE_HOST_DIR"
else
  echo "   .env 已存在，跳过"
fi
[ -f "$REPO/config/fleet.json" ] || cp "$REPO/config/fleet.example.json" "$REPO/config/fleet.json"
[ -f "$REPO/monitor/mail.conf.json" ] || cp "$REPO/monitor/mail.conf.example.json" "$REPO/monitor/mail.conf.json"
mkdir -p "$REPO/data" "$REPO/deploy/compose"
[ -f "$REPO/deploy/compose/docker-compose.yml" ] || cp "$REPO/deploy/docker-compose.example.yml" "$REPO/deploy/compose/docker-compose.yml"
# compose 同目录放一份 .env，docker compose 才能读到变量
[ -e "$REPO/deploy/compose/.env" ] || ln -s "$REPO/.env" "$REPO/deploy/compose/.env"

echo "== 2. yamb 本体（来自你的 fork 或上游） =="
if [ ! -d "$REPO/yamb" ]; then
  echo "   把 yamb 克隆/放到 $REPO/yamb 后重跑本脚本，例如："
  echo "     git clone -b yamb-plus <你的fork地址> $REPO/yamb"
  echo "   （上游：https://github.com/nanoite/yamb —— 但 yamb-plus 需要差异分支里的控制器接口）"
else
  echo "   $REPO/yamb 已存在，跳过"
fi
[ -d "$REPO/yamb" ] || { echo "（未检测到 yamb，跳过 unit 安装；装好后重跑本脚本）"; exit 0; }

echo "== 3. 安装 systemd 用户单元 =="
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
mkdir -p "$UNIT_DIR"
gen() { # gen <模板> <目标>
  sed -e "s|@MCBOT_HOME@|$REPO|g" -e "s|@YAMB_DIR@|$REPO/yamb|g" -e "s|@NODE_BIN@|$NODE_BIN|g" \
    "$REPO/deploy/systemd/$1" > "$UNIT_DIR/$2"
}
gen yamb-plus-controller.service.template yamb-plus-controller.service
gen yamb.service.template yamb.service
gen yamb-keepalive.service.template yamb-keepalive.service
cp "$REPO/deploy/systemd/yamb-keepalive.timer" "$UNIT_DIR/yamb-keepalive.timer"
# 用户的 yamb 若没有 fork 增强的 rp-fix/keepalive，去掉 NODE_OPTIONS 引用
if [ ! -f "$REPO/yamb/rp-fix.js" ] || [ ! -f "$REPO/yamb/keepalive.js" ]; then
  sed -i '/^Environment="NODE_OPTIONS=/d' "$UNIT_DIR/yamb.service"
fi

echo "== 4. 检查必填配置 =="
missing=0
while IFS='=' read -r k v; do
  case "$k" in NAPCAT_TOKEN|NAPCAT_A_ACCOUNT|NAPCAT_B_ACCOUNT|ASTRBOT_DATA_DIR|FLEET_FILE_HOST_DIR)
    if [ -z "${v// /}" ]; then echo "   !! .env 缺少 $k"; missing=1; fi ;;
  esac
done < <(grep -vE '^\s*#|^\s*$' "$REPO/.env")
[ "$missing" = 1 ] && echo "   补齐上面缺的键后，重新 docker compose up -d 并重启服务"

systemctl --user daemon-reload
echo ""
echo "== 完成 =="
echo "  启动容器:   cd $REPO/deploy/compose && docker compose up -d"
echo "  启动控制器: systemctl --user enable --now yamb-plus-controller"
echo "  启动挂机:   systemctl --user enable --now yamb yamb-keepalive.timer"
echo "  登录 QQ:    bash $REPO/monitor/qr.sh napcat   （备用号同理 napcat-b）"
echo "  体检:       bash $REPO/monitor/status.sh"
echo "  邮件告警:   bash $REPO/monitor/mail-setup.sh"
echo "  插件安装:   把 plugin/astrbot_plugin_mcbot/ 拷进 AstrBot data/plugins/ 并重启 astrbot 容器"
