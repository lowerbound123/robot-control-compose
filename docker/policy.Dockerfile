FROM python:3.14-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae AS builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY requirements/policy.lock /tmp/policy.lock
RUN python -m pip wheel --wheel-dir /wheels -r /tmp/policy.lock
COPY packages/policy-sdk /sdk
RUN python -m pip wheel --no-deps --wheel-dir /wheels /sdk

FROM python:3.14-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=builder /wheels /wheels
COPY requirements/policy.lock /tmp/policy.lock
RUN python -m pip install --no-index --find-links=/wheels -r /tmp/policy.lock rc-policy-sdk && \
    python -m pip check && rm -rf /wheels /tmp/policy.lock
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "rc_policy_sdk.host"]
CMD ["--host-id", "hold", "--socket", "/run/robot-control/hold.sock", "--policy-root", "/workspace/policy"]
