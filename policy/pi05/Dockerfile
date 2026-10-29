FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS wheels
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY --from=policy-deps /requirements.lock /tmp/pi05.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --wheel-dir /wheels --require-hashes -r /tmp/pi05.lock
RUN python -m pip install --no-cache-dir setuptools==81.0.0 wheel==0.45.1 packaging==25.0

FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/tmp USER=robot-control LOGNAME=robot-control \
    HF_HOME=/data/huggingface HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=policy-deps /requirements.lock /opt/pi05/requirements.lock
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --no-deps /wheels/*.whl && python -m pip check
ENV PYTHONPYCACHEPREFIX=/tmp/rc-pycache
WORKDIR /workspace
ENTRYPOINT ["python", "/workspace/packages/policy-sdk/src/rc_policy_sdk/source.py", "--module", "rc_policy_sdk.host"]

FROM runtime AS test
COPY --from=policy-deps /test-requirements.lock /tmp/test.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/test.lock && python -m pip check
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "-p", "no:cacheprovider", "tests"]
