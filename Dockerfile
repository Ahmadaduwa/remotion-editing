FROM nvidia/cuda:12.6.3-runtime-ubuntu22.04 AS cuda-stage

# Stage 2: Actual runtime
FROM node:22-bookworm-slim

# Copy CUDA libraries from nvidia runtime image
COPY --from=cuda-stage /usr/local/cuda-12.6/targets/x86_64-linux/lib/libcublas* /usr/lib/x86_64-linux-gnu/
COPY --from=cuda-stage /usr/local/cuda-12.6/targets/x86_64-linux/lib/libcudart* /usr/lib/x86_64-linux-gnu/
COPY --from=cuda-stage /usr/local/cuda-12.6/targets/x86_64-linux/lib/libcufft* /usr/lib/x86_64-linux-gnu/
COPY --from=cuda-stage /usr/local/cuda-12.6/targets/x86_64-linux/lib/libcusparse* /usr/lib/x86_64-linux-gnu/
COPY --from=cuda-stage /usr/local/cuda-12.6/compat/ /usr/local/cuda-12.6/compat/

ENV LD_LIBRARY_PATH=/usr/local/cuda-12.6/compat:/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH

# Chrome + Python + Thai fonts
RUN apt-get update && apt-get install -y \
    libnss3 libdbus-1-3 libatk1.0-0 libgbm-dev libasound2 \
    libxrandr2 libxkbcommon-dev libxfixes3 libxcomposite1 \
    libxdamage1 libatk-bridge2.0-0 libpango-1.0-0 libcairo2 \
    libcups2 fontconfig ffmpeg \
    python3 python3-pip fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji locales \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

RUN sed -i 's/^# *th_TH.UTF-8 UTF-8/th_TH.UTF-8 UTF-8/' /etc/locale.gen && locale-gen th_TH.UTF-8 2>/dev/null || true

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install
RUN npx remotion browser ensure --log none 2>/dev/null || true

COPY tsconfig.json remotion.config.ts ./
COPY src/ ./src/
COPY public/ ./public/
COPY *.py ./

RUN pip3 install fastapi uvicorn aiosqlite faster-whisper pythainlp python-multipart opencv-python-headless mediapipe --break-system-packages

RUN usermod -u 1000 node && groupmod -g 1000 node 2>/dev/null
RUN mkdir -p /app/data /app/assets /app/public /app/input /app/output && chown -R node:node /app /home/aduwa 2>/dev/null || true

USER node

EXPOSE 8000

CMD ["python3", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
