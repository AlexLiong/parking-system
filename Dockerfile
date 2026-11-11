# Run at delivery root: docker build -f parking-system/Dockerfile -t parking-course .
ARG PARKING_BASE_IMAGE=python:3.14-slim
FROM ${PARKING_BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PARKING_CONFIG=/app/parking-system/config.production.yaml
WORKDIR /app/parking-system
COPY parking-system/requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY parking-system/ ./
COPY assets_3d/ /app/assets_3d/
RUN useradd --create-home --uid 10001 parking && mkdir -p data && chown parking:parking data
USER parking
EXPOSE 8000
VOLUME ["/app/parking-system/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)"
CMD ["python","-m","uvicorn","server.main:app","--host","0.0.0.0","--port","8000","--workers","1"]
