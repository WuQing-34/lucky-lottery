#!/usr/bin/env bash
# 幸运大转盘 —— 一键部署脚本（在服务器 OrcaTerm 里执行）
# 用法：sudo bash deploy.sh
# 作用：把当前目录下的应用铺到 /opt/lottery，构建镜像并启动容器。
#      重复执行安全（幂等），升级时重跑即可，data/ 目录不会被覆盖。
set -euo pipefail

APP_DIR=/opt/lottery
PUB_PORT=80
PUB_IP="${PUB_IP:-62.234.140.218}"      # 需要时可用 PUB_IP=x.x.x.x sudo -E bash deploy.sh 覆盖
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

say() { printf '%s\n' "$*"; }
die() { printf '!! %s\n' "$*" >&2; exit 1; }

# ---------- 0. 前置检查（出问题立刻说清楚，别让你猜） ----------
command -v docker >/dev/null 2>&1 || die "没找到 docker 命令。先确认 Docker 装好了：docker --version"

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  die "没找到 compose 命令（compose 插件和 docker-compose 都没有）。"
fi
say "==> 使用命令：$DC"

if ! docker info >/dev/null 2>&1; then
  die "连不上 Docker 守护进程。确认加了 sudo，且 dockerd 在跑：systemctl status docker"
fi

# 检查宿主机端口是否已被占用（可能是上一次部署的容器，属正常）
if command -v ss >/dev/null 2>&1; then
  if ss -lntH 2>/dev/null | grep -q ":$PUB_PORT "; then
    say "==> 提示：宿主机 $PUB_PORT 端口已在监听（可能是已部署的旧容器，稍后会重建）"
  fi
fi

say "==> 源目录: $SRC_DIR"
say "==> 目标目录: $APP_DIR"

# ---------- 1. 铺文件（data/ 只创建、不覆盖） ----------
mkdir -p "$APP_DIR/app" "$APP_DIR/data" "$APP_DIR/nginx"
cp -a "$SRC_DIR"/app/. "$APP_DIR"/app/
cp -f "$SRC_DIR"/Dockerfile "$SRC_DIR"/docker-compose.yml "$APP_DIR"/
[ -f "$SRC_DIR/docker-compose.auth.yml" ] && cp -f "$SRC_DIR"/docker-compose.auth.yml "$APP_DIR"/
[ -f "$SRC_DIR/nginx/lottery.conf" ] && cp -f "$SRC_DIR"/nginx/lottery.conf "$APP_DIR"/nginx/
[ -f "$SRC_DIR/README.md" ] && cp -f "$SRC_DIR"/README.md "$APP_DIR"/README.md

# ---------- 2. 关键文件齐不齐 ----------
for f in app.py regions.json templates/user.html templates/admin.html; do
  [ -f "$APP_DIR/app/$f" ] || die "缺少文件 app/$f —— 部署包不完整或解压没解干净"
done
say "==> 文件已就位（app.py / regions.json / templates 全部齐全）"

cd "$APP_DIR"

# ---------- 3. 基础镜像：本地有就不联网 ----------
BASE_IMG=python:3.12-slim
if docker image inspect "$BASE_IMG" >/dev/null 2>&1; then
  say "==> 基础镜像 $BASE_IMG 本地已有，无需拉取"
else
  say "==> 本地没有 $BASE_IMG，开始拉取（国内可能偏慢，请耐心）"
  docker pull "$BASE_IMG" || die "拉取 $BASE_IMG 失败。可改 Dockerfile 第一行换成服务器上已有的 python 镜像：
   先跑 docker images | grep python 看看有什么，再把 FROM 那行改成对应镜像名。"
fi

# ---------- 4. 构建 + 启动 ----------
say "==> 构建镜像"
$DC build web

say "==> 启动容器"
$DC up -d web

# ---------- 5. 等健康检查 ----------
say "==> 等待服务就绪"
ready=0
for i in $(seq 1 20); do
  sleep 2
  if docker exec lottery-web python -c \
      "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" \
      >/dev/null 2>&1; then
    ready=1
    say "==> 服务已就绪"
    break
  fi
done
if [ "$ready" != "1" ]; then
  say ""
  say "!! 40 秒内没起来。下面是诊断信息："
  $DC ps
  say "---- 容器日志（最后 50 行）----"
  docker logs --tail 50 lottery-web 2>&1 || true
  die "服务未就绪，把上面这段发给我。"
fi

# ---------- 6. 汇总 ----------
say ""
say "==================== 部署完成 ===================="
$DC ps
say "--------------------------------------------------"
say "  容器内自检："
docker exec lottery-web python -c \
  "import urllib.request as u; print('   ', u.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read().decode())"
say "  区划数据自检（应为 省级 34 / 市级 366 / 区县级 3439）："
docker exec lottery-web python -c "
import json
d = json.load(open('/app/regions.json', encoding='utf-8'))
print('    省级 %d / 市级 %d / 区县级 %d' % (
    len(d),
    sum(len(p['ch']) for p in d),
    sum(len(c['ch']) for p in d for c in p['ch'])))
"
say "--------------------------------------------------"
say "  公网访问（若打不开，90% 是防火墙 $PUB_PORT 没放行）："
if [ "$PUB_PORT" = "80" ]; then
  say "    用户端：http://$PUB_IP/"
  say "    管理端：http://$PUB_IP/admin   （默认密码 admin888，务必立刻改）"
else
  say "    用户端：http://$PUB_IP:$PUB_PORT/"
  say "    管理端：http://$PUB_IP:$PUB_PORT/admin   （默认密码 admin888，务必立刻改）"
fi
say "--------------------------------------------------"
say "  备份数据：tar -czf lottery-data-\$(date +%m%d).tar.gz -C $APP_DIR data"
say "  查看日志：docker logs --tail 100 -f lottery-web"
say "  重启服务：cd $APP_DIR && $DC restart web"
say "=================================================="
