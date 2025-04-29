FROM ubuntu:24.04

RUN apt update && apt upgrade -y

RUN apt install -y \
  python3-fastapi \
  python3-uvicorn \
  python3-httpx \
  python3-pydantic \
  python3-cachetools \
  python3-prometheus-client

WORKDIR /app

COPY app.py .

CMD ["python3", "app.py"]

