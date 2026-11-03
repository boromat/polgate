# Two stages: the wheel and the spaCy model are built once, the runtime image keeps neither pip's
# cache nor the build tools.
FROM python:3.12-slim AS build

WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1

# pyproject.toml reads README.md as the long description and puts LICENSE in the wheel, so both go
# in beside it.
COPY pyproject.toml README.md LICENSE ./
# Only the hub is copied: the masking engine, polanon, is one of its dependencies and comes from
# PyPI with the install below.
COPY polgate ./polgate

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir ".[ocr]" \
 && /opt/venv/bin/python -m spacy download pl_core_news_md


FROM python:3.12-slim AS runtime

# Tesseract is what makes scanned uploads readable; without it the hub reports "OCR unavailable".
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-pol \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 hub \
 && mkdir -p /data && chown hub:hub /data

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
# Everything the hub keeps goes to the volume: the database, the uploads and the keys it generates
# when none are configured. The defaults would put the database beside the installed package,
# which the service account cannot write to.
ENV DATABASE_URL=sqlite:////data/polgate.db UPLOAD_DIR=/data/uploads

# The volume holds the database, the uploads and, when VAULT_KEY is empty, the generated key.
VOLUME ["/data"]
USER hub
WORKDIR /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status == 200 else 1)"

CMD ["uvicorn", "polgate.main:app", "--host", "0.0.0.0", "--port", "8000"]
