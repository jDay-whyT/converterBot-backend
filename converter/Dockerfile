FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Install dependencies for DNG/RAW conversion and HEIF support
# libraw-bin provides dcraw_emu with better DNG/ProRAW support
# Additional libraries for better format support
RUN apt-get update && apt-get install -y --no-install-recommends \
    imagemagick \
    libheif1 \
    libde265-0 \
    libraw-bin \
    dcraw \
    libjpeg-turbo-progs \
    libgomp1 \
    liblcms2-2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
