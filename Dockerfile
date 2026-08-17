# ============================================================
# AutoResearch 后端镜像
# 构建：docker build -t autoresearch-backend .
# 运行：docker run -p 8000:8000 --env-file .env autoresearch-backend
# ============================================================
FROM python:3.14-slim

WORKDIR /app

# 先复制依赖清单，利用 Docker 层缓存避免每次重装。
# 使用清华 PyPI 镜像加速（国内网络），可改为官方源：-i https://pypi.org/simple
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 复制后端代码
COPY backend/ .

# SQLite 开发模式的数据目录
RUN mkdir -p /app/data

EXPOSE 8000

# 默认启动 FastAPI（uvicorn）
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
