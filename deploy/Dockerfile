# 幸运大转盘 —— 容器镜像
# 纯 Python 标准库，零第三方依赖，无需 pip install
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HOST=0.0.0.0 \
    TZ=Asia/Shanghai

WORKDIR /app

# 应用代码（app.py + regions.json + templates/）
COPY app/ /app/

# 数据目录：compose 里会挂成卷，保证重建镜像不丢数据
RUN mkdir -p /app/data

EXPOSE 8000

# 容器自检：/api/health 通不过就标记 unhealthy
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["python", "app.py"]
