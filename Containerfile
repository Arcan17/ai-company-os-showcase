# AI Company OS — imagen de la demo (datos 100 % ficticios). Compatible con Podman y Docker.
#   construir:  podman build -f Containerfile -t aicos .      (o: docker build -f Containerfile -t aicos .)
#   correr:     ./start.sh                                     (un comando; ver README)
# Dos etapas: (1) compila el frontend, (2) imagen mínima solo con el backend y los estáticos. Usuario sin privilegios; el único sitio escribible es /data.

FROM node:22-bookworm-slim AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/app ./app
COPY --from=web /src/frontend/dist /app/frontend/dist
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin aicos && mkdir -p /data && chown aicos /data
USER 10001
ENV AICOS_STATIC_DIR=/app/frontend/dist \
    AICOS_DB_URL=sqlite:////data/aicos.db \
    AICOS_ALLOW_COMPANY_CREATION=0 \
    AICOS_LLM_PROVIDER=deterministic
VOLUME ["/data"]
EXPOSE 8731
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8731/api/health', timeout=2).status == 200 else 1)"
# --no-proxy-headers: la app decide el origen real del cliente por su cuenta (AICOS_TRUSTED_PROXIES si algún día hay un proxy de confianza).
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8731", "--no-proxy-headers"]
