#!/bin/bash
# SnowLuma 登录二维码：截屏后裁切登录窗口区域
echo "正在截取 SnowLuma 登录窗口（1920x1080 屏幕）..."
docker exec -e DISPLAY=:1 snowluma ffmpeg -y -loglevel error -f x11grab -video_size 1920x1080 -i :1 -frames:v 1 /tmp/sl-full.png
docker exec snowluma ffmpeg -y -loglevel error -i /tmp/sl-full.png -vf crop=360:360:800:390 /tmp/sl-qr.png
docker cp snowluma:/tmp/sl-qr.png /tmp/snowluma-qrcode.png
docker cp snowluma:/tmp/sl-full.png /tmp/snowluma-full.png
echo
echo "二维码已保存：/tmp/snowluma-qrcode.png"
echo "整屏截图（窗口位置变了就看这张）：/tmp/snowluma-full.png"
echo
echo "如果当前没有二维码（已登录或窗口已切换），截图里就不是登录界面，"
echo "需要重登时先执行：docker restart snowluma   然后等 30 秒再跑本脚本。"
