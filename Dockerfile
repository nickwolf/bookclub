FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    uvicorn==0.30.6 \
    jinja2==3.1.4 \
    python-multipart==0.0.9 \
    httpx==0.27.2 \
    anthropic==1.11.0

COPY app/ .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
