FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

# 复制项目文件
COPY . .

RUN groupadd --gid 10001 poe && useradd --uid 10001 --gid 10001 --no-create-home poe \
    && mkdir -p /var/log/poe-monitor && chown -R poe:poe /var/log/poe-monitor
USER 10001:10001

EXPOSE 5000

# 启动命令
CMD ["python", "main.py"]
