FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all source files
COPY . .

# Environment variables (Pre-configured so NO manual setup is needed)
ENV PYTHONUNBUFFERED=1
ENV PORT=10000
ENV TELEGRAM_BOT_TOKEN="8921341725:AAFRPRj9xxPIuEyNLTR8Iob0TtIC54n1hfE"
ENV TELEGRAM_CHAT_ID="771305593"
ENV BINANCE_API_KEY="dummy_key"
ENV BINANCE_API_SECRET="dummy_secret"

# Start bot
CMD ["python", "main.py"]
