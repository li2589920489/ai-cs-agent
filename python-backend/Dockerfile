# 电商客服 AI 智能体后端镜像
# 技术栈：FastAPI + OpenAI Agents SDK + 完整 RAG（ChromaDB + BGE + BM25 + 重排）+ FastMCP + LangGraph
#
# 说明：国内网络无法直连 Docker Hub，默认通过 DaoCloud 镜像源拉取基础镜像；
#      若已配置 registry-mirror 或能直连，可用 --build-arg BASE_IMAGE=python:3.13-slim 改回官方镜像。
ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
FROM ${BASE_IMAGE}

# 关闭 .pyc 生成、输出不缓冲；pip 走清华镜像源（国内加速）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

WORKDIR /app

# 先装 CPU 版 torch：本项目用 BGE-small 做检索，CPU 推理足够，
# 避免默认拉取 2GB+ 的 CUDA 依赖，控制镜像体积（轻量化）。
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# 安装其余依赖，充分利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝后端代码
COPY . .

# 数据目录（ChromaDB 向量库 + SQLite 知识库）挂载卷
VOLUME ["/app/data"]

EXPOSE 8000

# 启动 FastAPI 服务
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
