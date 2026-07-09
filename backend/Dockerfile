FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（如果需要，例如 mysql-client，但不需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 创建日志目录（如果需要在容器内写日志）
RUN mkdir -p /var/log/poe-monitor

# 暴露端口（仅用于文档，实际由 host 网络管理）
EXPOSE 5000

# 启动命令
CMD ["python", "main.py"]
