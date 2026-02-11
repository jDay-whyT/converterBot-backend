FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    imagemagick \
    libheif1 \
    libde265-0 \
    libjpeg62-turbo \
    liblcms2-2 \
    zlib1g \
    wget \
    ca-certificates \
    libraw-bin \
    && rm -rf /var/lib/apt/lists/*

# Install LibRaw 0.21.2 from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    autoconf \
    automake \
    libtool \
    pkg-config \
    libjpeg62-turbo-dev \
    liblcms2-dev \
    zlib1g-dev \
    && cd /tmp \
    && wget -q https://www.libraw.org/data/LibRaw-0.21.2.tar.gz \
    && tar -xzf LibRaw-0.21.2.tar.gz \
    && cd LibRaw-0.21.2 \
    && autoreconf --install \
    && ./configure --prefix=/usr \
    && make -j$(nproc) \
    && make install \
    && ldconfig \
    && cd / \
    && rm -rf /tmp/LibRaw-0.21.2* \
    && apt-get purge -y --auto-remove \
        gcc \
        g++ \
        make \
        autoconf \
        automake \
        libtool \
        pkg-config \
        libjpeg62-turbo-dev \
        liblcms2-dev \
        zlib1g-dev \
        wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
