# Build context: ./backend   (docker build -t ail-ay2627-s1-aishani-fitinerary ./backend)
# Python 3.13 (not the playbook's 3.11): the student's code and test suite target 3.13; OR-Tools ships 3.13 wheels.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 1001 appuser
WORKDIR /app

# Pinned runtime dependencies only (tests need requirements-dev.txt, which is not installed here)
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
USER appuser

# Cloud Run injects PORT (default 8080). Our own client-IP logic reads X-Forwarded-For (TRUSTED_PROXY_HOPS), so uvicorn's
# proxy-header rewriting is switched off, and its access log (which can contain query strings) is off.
ENV PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8080'), timeout=5)" || exit 1
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log --no-proxy-headers"]
