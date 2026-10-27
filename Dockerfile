FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ALFA_RUNTIME_DIR=/app/var
WORKDIR /app
RUN useradd --uid 10001 --create-home alfa
COPY requirements.lock.txt /app/requirements.lock.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.lock.txt
COPY --chown=alfa:alfa . /app
RUN python -X utf8 scripts/setup_search.py --download --download-only
RUN mkdir -p /app/var /app/.cache && chown -R alfa:alfa /app/var /app/.cache
USER alfa
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python","-c","import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3).read()"]
CMD ["python","-X","utf8","-m","alfa_app","serve","--host","0.0.0.0","--port","8000"]
