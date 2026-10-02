#!/bin/bash
# 泄密扫描：确认仓库里没有 QQ号 / MC号 / bot名 / 服务器地址 / 密钥
# 用法: bash scripts/check-secrets.sh [目录]   命中即退出码 1
#
# 字面禁串来自 config/forbidden.txt（已被 .gitignore 忽略，绝不入库 —— 否则扫描器自己就是泄密源）：
#   每行一个敏感串，# 开头是注释。示例见 config/forbidden.example.txt。
TARGET=${1:-"$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"}
FORBIDDEN_FILE="${FORBIDDEN_FILE:-$TARGET/config/forbidden.txt}"

# 注：yamb/ 是上游 fork 的独立 git 仓库（本仓库 gitignore），其内容由它自己管理，这里不扫；
# forbidden.txt 本身是禁串清单，也排除（否则必然自命中）。
GREP_EX=(--exclude-dir=.git --exclude-dir=yamb --exclude-dir=node_modules --exclude=check-secrets.sh --exclude=forbidden.txt)

# 字面禁串：真实 QQ 号 / 群号 / MC 账号 / 域名 / API key / SMTP 授权码 / bot 显示名
LITERAL=()
if [ -f "$FORBIDDEN_FILE" ]; then
  mapfile -t LITERAL < <(grep -vE '^\s*(#|$)' "$FORBIDDEN_FILE")
else
  echo "!! 未找到禁串清单 $FORBIDDEN_FILE —— 字面扫描已跳过（仅剩通用模式）。"
  echo "   请复制 config/forbidden.example.txt 为 config/forbidden.txt 并填入你自己的敏感串。"
fi

# 模式：9 位以上纯数字（QQ/群号/UIN）、邮箱、常见域名（白名单除外）
PATTERN_NUM='[0-9]{9,}'
PATTERN_MAIL='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
PATTERN_DOMAIN='[A-Za-z0-9-]+\.(cn|com|net|org|io|me)\b'

hit=0
for s in "${LITERAL[@]}"; do
  while IFS=: read -r f l; do
    echo "!! 字面禁串 [$s] $f:$l"
    hit=1
  done < <(grep -rnF "${GREP_EX[@]}" -- "$s" "$TARGET" 2>/dev/null)
done

for p in "$PATTERN_NUM" "$PATTERN_MAIL" "$PATTERN_DOMAIN"; do
  while IFS=: read -r f l; do
    # 白名单：示例域名 / QQ 官方登录链接 / 端口号 / 版本号 / 文档里的占位
    case "$l" in
      *example.com*|*example.org*|*txz.qq.com*|*smtp.qq.com*|*mlikiowa*|*soulter*) continue ;;
      *nanoite*|*github.com*|*yamb-plus*|*workbuddy*) continue ;;
    esac
    case "$l" in
      *'127.0.0.1'*|*0.0.0.0*|*25565*|*'15100'*|*'15101'*|*'15102'*|*'15103'*|*'15104'*|*'15199'*|*'3000'*|*'3001'*|*'6185'*|*'6099'*|*'6100'*) continue ;;
      *'example'*) continue ;;
      *)
        echo "!! 模式命中 [$p] $f:$l"
        hit=1
        ;;
    esac
  done < <(grep -rnE "${GREP_EX[@]}" -- "$p" "$TARGET" 2>/dev/null)
done

if [ "$hit" = 0 ]; then
  echo "OK：未发现敏感信息（$TARGET）"
else
  echo "!! 发现疑似敏感信息，禁止提交！逐条人工确认或清洗后重扫。"
  exit 1
fi
