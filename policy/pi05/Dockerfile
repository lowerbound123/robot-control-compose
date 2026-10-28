FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS wheels
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY --from=policy-deps /requirements.lock /tmp/pi05.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --wheel-dir /wheels --require-hashes -r /tmp/pi05.lock
RUN python -m pip install --no-cache-dir setuptools==81.0.0 wheel==0.45.1 packaging==25.0
COPY packages/policy-sdk /src/policy-sdk
COPY packages/pi05-policy /src/pi05-policy
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels /src/policy-sdk /src/pi05-policy

FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/tmp USER=robot-control LOGNAME=robot-control \
    HF_HOME=/data/huggingface HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=policy-deps /requirements.lock /opt/pi05/requirements.lock
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --no-deps /wheels/*.whl && python -m pip check
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "rc_policy_sdk.host"]

FROM runtime AS test
COPY --from=policy-deps /test-requirements.lock /tmp/test.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/test.lock && python -m pip check
COPY tests/test_pi05_configuration.py tests/test_pi05_runtime.py tests/test_pi05_backend.py tests/pi05_helpers.py tests/test_async_chunks.py tests/test_qwen35_runtime.py /opt/app/tests/
COPY tests/test_pi05_worker.py tests/conftest.py /opt/app/tests/
COPY tests/host_helpers.py tests/test_host_transport.py tests/test_policy_monitor_worker.py /opt/app/helpers/
COPY policy/pi05 /opt/app/policy/pi05
# RC is only the test client here; it is not installed in the runtime stage.
COPY src/robot_control /opt/app/src/robot_control
# Qwen is present only as a lightweight comparison fixture in the test stage.
COPY packages/qwen35-policy /opt/app/packages/qwen35-policy
ENV PYTHONPATH=/opt/app/src:/opt/app/helpers
RUN chmod -R a+rX /opt/app
WORKDIR /opt/app
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "-p", "no:cacheprovider", "tests"]
