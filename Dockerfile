# Stage: Backend + Final Image
FROM python:3.12-slim
WORKDIR /workspace

# Install system dependencies for headless Chromium (Playwright)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    libx11-6 \
    libxcb1 \
    libxrandr2 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxext6 \
    libxkbcommon0 \
    libdbus-1-3 \
    libgtk-3-0 \
    libdrm2 \
    libgbm1 \
    libxshmfence1 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libpangocairo-1.0-0 \
    libcups2 \
    libnss3 \
    libnspr4 \
    libglib2.0-0 \
    fonts-liberation \
    && apt-get install -y --no-install-recommends fonts-noto-cjk || true \
    && apt-get install -y --no-install-recommends libasound2 libasound2t64 || true \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONIOENCODING=utf-8
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

# Download Playwright Chromium at build time
RUN python -m playwright install chromium

COPY backend/ ./backend/
COPY start.py ./

# Create data and logs directories
RUN mkdir -p /workspace/data /workspace/logs

EXPOSE 7861

ENV PORT=7861
ENV ACCOUNTS_FILE=/workspace/data/accounts.json
ENV USERS_FILE=/workspace/data/users.json
ENV SESSIONS_FILE=/workspace/data/sessions.json
ENV PYTHONPATH=/workspace

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:${PORT}/healthz || exit 1

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "7861", "--workers", "1"]
