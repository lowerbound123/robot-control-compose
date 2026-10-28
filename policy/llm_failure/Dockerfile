FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS wheels
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY policy/llm_failure/requirements.lock /tmp/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --wheel-dir /wheels --require-hashes -r /tmp/requirements.lock
COPY packages/policy-sdk /src/policy-sdk
COPY packages/llm-failure-policy /src/llm-failure-policy
RUN python -m pip wheel --no-deps --wheel-dir /wheels /src/policy-sdk /src/llm-failure-policy

FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY policy/llm_failure/requirements.lock /opt/llm-failure/requirements.lock
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --no-deps /wheels/*.whl && python -m pip check
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "rc_policy_sdk.host"]
CMD ["--host-id", "llm_failure", "--socket", "/run/robot-control/llm_failure.sock", "--policy-root", "/workspace/policy"]

FROM runtime AS test
COPY policy/llm_failure/test-requirements.lock /tmp/test-requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/test-requirements.lock && python -m pip check
COPY tests/llm_failure_helpers.py tests/llm_http_helpers.py tests/test_llm_failure.py tests/test_llm_failure_providers.py /opt/tests/
RUN chmod -R a+rX /opt/tests
WORKDIR /opt/tests
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "-p", "no:cacheprovider"]
