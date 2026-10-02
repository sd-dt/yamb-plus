#!/bin/bash
# 同步到 GitHub（交接文档《GitHub上传途径》的方法：REST API blob→tree→commit→ref，不走 git push）
# 流程：泄密扫描 -> 本地提交 -> API 上传主仓库(main) + yamb fork(yamb-plus 分支)，幂等。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

echo "== 1. 泄密扫描 =="
bash scripts/check-secrets.sh "$REPO"

echo "== 2. 本地提交 =="
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git -c user.name="yamb-plus" -c user.email="yamb-plus@users.noreply.github.com" \
    commit -q -m "sync: $(date '+%F %T')"
else
  echo "   工作区干净"
fi

echo "== 3. 上传主仓库 =="
python3 scripts/upload-github.py --dir "$REPO" --repo sd-dt/yamb-plus --branch main \
  --message "sync: $(date '+%F %T')"

echo "== 4. 同步 yamb fork（yamb-plus 分支） =="
YAMB_GIT_DIR="${YAMB_GIT_DIR:-/opt/qqbot/yamb}"
if [ -d "$YAMB_GIT_DIR/.git" ]; then
  python3 scripts/upload-github.py --dir "$YAMB_GIT_DIR" --repo sd-dt/yamb --branch yamb-plus \
    --message "sync: $(date '+%F %T')"
else
  echo "   （未找到 $YAMB_GIT_DIR，跳过 fork 同步）"
fi

echo "== 已同步 GitHub =="
