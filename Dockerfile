FROM python:3.12-slim

# System deps for liboqs + build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git libssl-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Build liboqs from source (required for ML-KEM-768 / ML-DSA-65)
RUN git clone --depth 1 --branch 0.12.0 https://github.com/open-quantum-safe/liboqs.git /tmp/liboqs \
    && cd /tmp/liboqs && mkdir build && cd build \
    && cmake -DBUILD_SHARED_LIBS=ON -DCMAKE_INSTALL_PREFIX=/usr/local .. \
    && make -j$(nproc) && make install \
    && ldconfig && rm -rf /tmp/liboqs

WORKDIR /app

# Install Python deps first (layer caching)
COPY AQM_Database/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
