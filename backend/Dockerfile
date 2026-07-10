FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.lock .
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock

COPY alembic.ini .
COPY app ./app
COPY migrations ./migrations
COPY sql ./sql

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
