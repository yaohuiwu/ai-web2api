# ai-web2api —— Playwright 驱动真实浏览器，所以镜像必须带 Chromium + 系统依赖。
#
# 基础镜像用官方 Playwright 的 Python 镜像：它已经预装 Chromium 及全部系统库，
# 版本与 pip 装的 playwright 包对齐（1.62.0），比手写 `playwright install --with-deps`
# 更快也更稳（避免 apt 依赖缺失导致浏览器起不来）。
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Playwright 浏览器已在镜像里，不再下载；运行时也禁用自动下载
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # 容器内默认无头（config.yaml 也可覆盖；compose 里可用 .env 覆盖）
    WEB2API_HEADLESS=true \
    # 配置文件位置由 compose 挂载进来
    AI_WEB2API_CONFIG=/app/config.yaml

WORKDIR /app

# 可选：Xvfb，用 WEB2API_HEADLESS=false 时跑 headful（ChatGPT Sentinel 会拦 headless）。
# 很小（几 MB），默认不启用。
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用层缓存：改代码不会触发重装依赖）
COPY pyproject.toml uv.lock README.md ./
RUN pip install --upgrade pip uv \
    && uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && pip install -r requirements.txt \
    && rm requirements.txt

# 再拷源码与静态资源
COPY src/ ./src/
COPY config.yaml config.fake.yaml ./
COPY tests/ ./tests/

RUN pip install --no-deps .

# 登录态 / 会话持久化目录（compose 用匿名卷或绑定挂载覆盖）
VOLUME ["/app/profiles"]

EXPOSE 8000

# 容器内必须监听所有网卡，宿主才能访问（config.yaml 默认已是 0.0.0.0）
# entrypoint：WEB2API_HEADLESS=false 时自动用 xvfb-run 跑 headful；否则原样 headless。
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "ai_web2api.main"]
