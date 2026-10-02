# syntax=docker/dockerfile:1
# Python 3.11 slim, pinned by digest so every build uses the same base image.
# To update: docker pull python:3.11-slim && docker inspect --format '{{index .RepoDigests 0}}' python:3.11-slim
FROM python:3.11-slim@sha256:bab1b7ef4b450c81002278d035eff85ebe394ae94df904f7a3ba14f7e16e487b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Unprivileged user: the application never runs as root.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin aegis

# Locked, hash-verified dependencies (see requirements.in).
COPY requirements.txt .
RUN pip install --require-hashes -r requirements.txt

COPY src ./src
COPY runbooks ./runbooks
COPY scripts ./scripts
COPY evals ./evals

USER aegis
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
