# API 与 Worker 使用同一个镜像，确保两边拥有完全相同的业务代码和 Python 依赖。
FROM python:3.12-slim

# 不生成 .pyc；日志不缓冲，方便 docker compose logs 实时观察；
# pip 不保留下载缓存，减少教学镜像体积。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# RapidOCR 的 OpenCV 运行库；无须图形桌面，Worker 只处理已缓存的图片。
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖文件，源码变化时可以复用 Docker 的 pip 安装缓存层。
COPY requirements.txt /app/requirements.txt
COPY constraints.txt /app/constraints.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app

# API/Worker 不需要 root 权限。使用固定的非特权 UID，避免 Celery 的 root 安全警告。
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 这是镜像默认命令，供 api 服务使用；docker-compose.yml 中 worker 会覆盖为 Celery。
CMD ["fastapi", "run", "main.py", "--host", "0.0.0.0", "--port", "8000"]
