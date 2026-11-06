FROM python:3.10-slim-bookworm@sha256:5be5aaecb962a41567bc8041f1e9e3432cb085b037fe057f9a562630b2e4b370 AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY connector/mock/requirements.lock /tmp/requirements.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements.lock && python3 -m pip check
WORKDIR /workspace
