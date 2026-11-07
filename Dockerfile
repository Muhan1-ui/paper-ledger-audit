FROM python:3.12-slim

WORKDIR /app

ENV PIP_DEFAULT_TIMEOUT=120

COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 5 -r requirements.txt

COPY outputs ./outputs

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000
