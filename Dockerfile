FROM python:3.13-slim

ENV DENO_INSTALL=/root/.deno
ENV PATH="/root/.deno/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
    unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh -o /tmp/install-deno.sh \
    && sh /tmp/install-deno.sh \
    && command -v deno \
    && deno --version \
    && rm /tmp/install-deno.sh

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

ENV BOT_DATABASE_PATH=/app/data/bot.sqlite3
ENV TINYASR_PROFILE_DIR=/app/data/tinyasr-profile

CMD ["python", "app.py"]
