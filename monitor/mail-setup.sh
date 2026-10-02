#!/bin/bash
# 交互式配置邮件告警，并立即发一封测试邮件
set -u
CONF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mail.conf.json"

echo "======================================================"
echo "  邮件告警配置"
echo "  发件用 QQ 邮箱（smtp.qq.com:465）"
echo "  ⚠ 第 2 项要填【SMTP 授权码】，不是 QQ 密码"
echo "     获取：QQ邮箱网页版 → 设置 → 账户 →"
echo "           POP3/IMAP/SMTP服务 → 开启 → 生成授权码（需短信验证）"
echo "======================================================"
echo
read -rp  "1) 发件邮箱（如 you@example.com）: " USERMAIL
read -rsp "2) SMTP 授权码（输入时不显示）: " PASS; echo
read -rp  "3) 收件邮箱（直接回车 = 同发件邮箱）: " TOMAIL
[ -z "${TOMAIL:-}" ] && TOMAIL="$USERMAIL"

if [ -z "$USERMAIL" ] || [ -z "$PASS" ]; then
  echo "!! 发件邮箱或授权码为空，已取消"
  exit 1
fi

cat > "$CONF" <<EOF
{
  "enabled": true,
  "host": "smtp.qq.com",
  "port": 465,
  "secure": true,
  "user": "$USERMAIL",
  "pass": "$PASS",
  "from": "$USERMAIL",
  "to": "$TOMAIL"
}
EOF
chmod 600 "$CONF"
echo
echo "已写入 $CONF（权限 600）"
echo "正在发送测试邮件 ..."
node "$(dirname "$CONF")/mailer.js" "【测试】QQ机器人服务器 邮件告警已打通" "收到这封邮件说明带外告警通道已生效。以后 NapCat/QQ 掉线时，即使 QQ 发不出消息，你也能收到邮件。"
