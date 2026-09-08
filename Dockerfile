FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system vdai && useradd --system --gid vdai --home-dir /app vdai

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install . && chown -R vdai:vdai /app

USER vdai

CMD ["vdai-digest", "run"]
