FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt flask

COPY . .

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "YourIpoBot.web.app"]
