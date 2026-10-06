FROM python:3.12-slim

# GHCR uses the source label to link the package to the repo
LABEL org.opencontainers.image.source="https://github.com/nickwolf/bookclub" \
      org.opencontainers.image.description="Self-hosted book recommendation app: Hardcover + Audiobookshelf + Claude AI" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
