FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1
ENV OPENBLAS_NUM_THREADS=1
ENV HONEYCHAIN_TORCH_THREADS=1
ENV YOLO_CONFIG_DIR=/tmp/Ultralytics
ENV MALLOC_TRIM_THRESHOLD_=65536

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The blockchain Solidity/ABI source lives at the repository root. It is not
# needed by the FastAPI runtime because the on-chain ABI is embedded in main.py.
RUN mkdir -p /data /tmp/Ultralytics

ENV HONEYCHAIN_DB=/data/honeychain.db

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
