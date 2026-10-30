FROM python:3.12-slim AS base

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV GUNICORN_CMD_ARGS="--workers=2 --timeout=90 --access-logfile=-"

EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "quotesapi.app:app"]
