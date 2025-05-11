FROM ubuntu:24.04

RUN apt update && apt upgrade -y

RUN apt install -y \
  python3-fastapi \
  python3-uvicorn \
  python3-httpx \
  python3-pydantic \
  python3-cachetools \
  python3-prometheus-client \
  python3-requests

WORKDIR /app

COPY trx_balance_exporter.py .

CMD ["python3", "trx_balance_exporter.py"]

