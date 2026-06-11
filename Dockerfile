FROM python:3.11-slim

WORKDIR /app

# System dependencies (build-essential for any sdist-only packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# ── CPU-only torch FIRST ──────────────────────────────────────────────────────
# Default PyPI torch on Linux drags in ~2.5 GB of CUDA wheels. Railway has no
# GPU — the CPU wheel (~200 MB) satisfies requirements.txt's torch>=2.2.0 and
# pip skips reinstalling it.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# ── App dependencies (cached layer — only rebuilds when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Pre-bake FinBERT so containers don't re-download it from HF on every boot
RUN python -c "from transformers import AutoTokenizer, AutoModelForSequenceClassification; \
    AutoTokenizer.from_pretrained('ProsusAI/finbert'); \
    AutoModelForSequenceClassification.from_pretrained('ProsusAI/finbert')"

# ── Copy application source (changes here don't invalidate the pip layers) ────
COPY app/ ./app/

# ── Runtime directories ───────────────────────────────────────────────────────
RUN mkdir -p /data/vibe-runtime/live/robinhood /data/vibe-runtime/live/consent

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
