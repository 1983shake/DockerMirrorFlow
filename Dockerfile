FROM python:3.11-slim

LABEL org.opencontainers.image.title="DockerMirrorFlow"
LABEL org.opencontainers.image.description="多源聚合，流式加速 —— 多 Registry 镜像代理加速服务"
LABEL org.opencontainers.image.source="https://github.com/1983shake/dockermirrorflow"

WORKDIR /app

# 依赖
COPY requirements.txt .
#RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 代码
COPY . .
RUN mkdir -p data config

EXPOSE 8000

CMD ["python", "-m", "app.main"]