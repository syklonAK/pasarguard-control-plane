FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY migrations ./migrations
COPY src ./src
RUN pip install --no-cache-dir .
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 MIGRATIONS_DIR=/app/migrations
CMD ["uvicorn","control_plane.v04_app:app","--host","0.0.0.0","--port","8000"]
