FROM python:3.13-slim

# uv installs the exact locked dependency set from uv.lock
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 grimoire && mkdir /data && chown grimoire /data
USER grimoire

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["grimoire", "--host", "0.0.0.0", "--port", "8080"]
