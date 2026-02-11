# Build stage: compile latest LibRaw for better DNG/ProRAW support
FROM python:3.11-slim AS builder

ENV LIBRAW_VERSION=0.21.2

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    ca-certificates \
    pkg-config \
    libjpeg-dev \
    liblcms2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
RUN wget -q https://www.libraw.org/data/LibRaw-${LIBRAW_VERSION}.tar.gz \
    && tar -xzf LibRaw-${LIBRAW_VERSION}.tar.gz \
    && cd LibRaw-${LIBRAW_VERSION} \
    && ./configure --prefix=/usr/local \
    && make -j$(nproc) \
    && make install

# Runtime stage
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Copy compiled LibRaw from builder
COPY --from=builder /usr/local/bin/dcraw_emu /usr/local/bin/
COPY --from=builder /usr/local/lib/libraw*.so* /usr/local/lib/

RUN apt-get update && apt-get install -y --no-install-recommends \
    imagemagick \
    libheif1 \
    libde265-0 \
    libjpeg62-turbo \
    liblcms2-2 \
    dcraw \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
