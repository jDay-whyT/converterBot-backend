FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Install runtime dependencies for HEIF/RAW conversion.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    imagemagick \
    libheif1 \
    libde265-0 \
    libjpeg62-turbo \
    liblcms2-2 \
    zlib1g \
    libraw-bin \
    dcraw \
    darktable \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
