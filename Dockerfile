FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# ── Install TradingAgents from GitHub ────────────────────────────────────────
RUN git clone --depth=1 https://github.com/TauricResearch/TradingAgents.git /opt/TradingAgents \
    && pip install --no-cache-dir /opt/TradingAgents

# ── Install Vibe-Trading from GitHub ─────────────────────────────────────────
RUN git clone --depth=1 https://github.com/HKUDS/Vibe-Trading.git /opt/Vibe-Trading \
    && pip install --no-cache-dir /opt/Vibe-Trading || true
# The '|| true' allows the container to start even if Vibe-Trading's install
# fails (e.g. missing optional deps); the SDK availability check in
# broker/connector.py will degrade gracefully to dry-run mode.

# ── Install our app dependencies ──────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy application source ───────────────────────────────────────────────────
COPY app/ ./app/

# ── Runtime directories ───────────────────────────────────────────────────────
RUN mkdir -p /data/vibe-runtime/live/robinhood /data/vibe-runtime/live/consent

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
