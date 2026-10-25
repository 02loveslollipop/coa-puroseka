FROM node:24-bookworm-slim AS web
WORKDIR /build
COPY web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build
FROM python:3.15-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY scripts/import_data.py scripts/import_data.py
COPY scripts/setup_access.py scripts/setup_access.py
COPY scripts/database_roles.py scripts/database_roles.py
COPY artifacts/*.gz artifacts/
COPY artifacts/*.json artifacts/
COPY artifacts/ontology.ttl artifacts/
COPY --from=web /build/dist web/dist
RUN useradd --uid 10001 --create-home appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health',timeout=5)"
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
