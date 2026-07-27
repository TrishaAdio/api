FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Kiro CLI is the inference backend; there is no HTTP API to call instead.
RUN curl -fsSL https://cli.kiro.dev/install | bash
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kiro_openai ./kiro_openai

ENV KIRO_BRIDGE_WORKDIR=/work
RUN mkdir -p /work

EXPOSE 8000
CMD ["uvicorn", "kiro_openai.server:app", "--host", "0.0.0.0", "--port", "8000"]
