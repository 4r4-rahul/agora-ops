FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY trading_platform/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy platform source
COPY trading_platform/ ./trading_platform/

# Create data directory for SQLite journal
RUN mkdir -p /data
ENV DATABASE_URL=sqlite+aiosqlite:////data/trade_journal.db

# Non-root user
RUN useradd -m -u 1000 trader
RUN chown -R trader:trader /app /data
USER trader

EXPOSE 8000

CMD ["uvicorn", "trading_platform.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
