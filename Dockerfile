# Imagem da API de análise. Substitui o "pip install" a cada boot do container:
# agora as versões são fixas, o build é reprodutível e não depende de rede na
# subida.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Sao_Paulo

WORKDIR /app

# Camada de dependências primeiro: só muda quando requirements.txt muda.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ruff.toml ./

# A análise nunca precisou de root: roda sem privilégio.
RUN useradd --create-home --uid 10001 avaliador
USER avaliador

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
