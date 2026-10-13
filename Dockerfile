# ============================================================================
#  predictive-maintenance —— 统一运行镜像
#
#  设计要点：
#    · 基础镜像固定到 python:3.12-slim（与本项目开发环境一致；3.15 会导致
#      onnxruntime 之类依赖没有 wheel，本仓库踩过这个坑）
#    · 采集/分析/桥接/时序库这些核心脚本【只用标准库】，所以镜像不需要装任何
#      pip 依赖；只有建模脚本需要 numpy/scikit-learn，放在 modeling 目标里
#    · 用非 root 用户运行（工业网关/边缘盒子的基本安全要求）
#    · 数据统一挂在 /app/data（compose 里映射到宿主机 ./data），容器重建不丢数据
#
#  构建：
#    docker build -t predictive-maintenance:latest .
#    docker build -t predictive-maintenance:modeling --target modeling .
# ============================================================================
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8

WORKDIR /app

# 核心运行时零依赖：只拷脚本，不需要 pip install
COPY *.py ./
COPY run_demo.ps1 ./
COPY README.md LICENSE requirements.txt ./

# 数据/日志/产物目录（compose 会挂载宿主机目录覆盖）
RUN mkdir -p /app/data /app/logs /app/docs /app/models \
    && useradd -m -u 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# 默认跑"采集器"；实际由 compose 为每个服务覆盖 command
CMD ["python", "collector.py", "--help"]


# ---------------------------------------------------------------- 建模镜像
# 只有跑 train_baseline.py / train_rul.py 时才需要这一层（约多 200MB）
FROM base AS modeling

USER root
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        numpy scikit-learn
USER appuser
