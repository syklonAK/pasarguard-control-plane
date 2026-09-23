FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
CMD ["uvicorn","control_plane.app:app","--host","0.0.0.0","--port","8000"]
