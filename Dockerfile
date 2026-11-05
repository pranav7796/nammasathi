# A public server: mock shops, unless MULTI_USER_LIVE and its requirements are met at startup.
FROM python:3.12-slim

ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir uv==0.9.5

WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app

RUN useradd --create-home saathi && chown -R saathi /srv
USER saathi

ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

# No access log: Swiggy's callback carries a one-time code in its query string.
CMD ["sh", "-c", "exec .venv/bin/uvicorn app.web:app --host 0.0.0.0 --port \"$PORT\" --no-access-log --proxy-headers --forwarded-allow-ips='*'"]
