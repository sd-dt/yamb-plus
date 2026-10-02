#!/bin/bash
# 打印指定 NapCat 容器的最新登录二维码 —— 用法: bash monitor/qr.sh <容器名>
C=${1:?用法: qr.sh <容器名>}
OLD=$(docker logs --tail 300 "$C" 2>&1 | grep -oE 'https://txz\.qq\.com/p\?k=[^ ]*' | tail -1)
echo "正在等待 NapCat 刷新二维码（最长 140 秒，一刷新就立刻打印）..."
NEW=""
for i in $(seq 1 70); do
  sleep 2
  NEW=$(docker logs --tail 300 "$C" 2>&1 | grep -oE 'https://txz\.qq\.com/p\?k=[^ ]*' | tail -1)
  if [ -n "$NEW" ] && [ "$NEW" != "$OLD" ]; then break; fi
done
echo
echo "################# 全新二维码（约 2 分钟有效）#################"
docker logs --tail 45 "$C" 2>&1 | tail -40
echo "#########################################################"
echo
echo "把上面那条 https://txz.qq.com/... 复制到【登录着该账号的手机】浏览器打开并授权，"
echo "或者直接用手机 QQ 扫上面那团方块二维码。"
