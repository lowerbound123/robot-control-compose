FROM python:3.14-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae AS builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY policy/hold/requirements.lock /tmp/policy.lock
RUN python -m pip wheel --wheel-dir /wheels -r /tmp/policy.lock

FROM python:3.14-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=builder /wheels /wheels
COPY policy/hold/requirements.lock /tmp/policy.lock
RUN python -m pip install --no-index --find-links=/wheels -r /tmp/policy.lock && \
    python -m pip check && rm -rf /wheels /tmp/policy.lock
ENV PYTHONPYCACHEPREFIX=/tmp/rc-pycache
WORKDIR /workspace
ENTRYPOINT ["python", "/workspace/packages/policy-sdk/src/rc_policy_sdk/source.py", "--module", "rc_policy_sdk.host"]
CMD ["--host-id", "hold", "--socket", "/run/robot-control/hold.sock", "--policy-root", "/workspace/policy"]
