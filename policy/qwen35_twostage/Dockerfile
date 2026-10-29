FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS wheels
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY --from=policy-deps /requirements.lock /tmp/qwen35.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --wheel-dir /wheels --require-hashes -r /tmp/qwen35.lock
RUN python -m pip install --no-cache-dir setuptools==75.8.2 wheel==0.45.1 packaging==26.3

FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HF_HOME=/data/huggingface
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=policy-deps /requirements.lock /opt/qwen35/requirements.lock
# Bind the wheelhouse only for installation; it must not double the final image size.
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --no-deps /wheels/*.whl && python -m pip check
ENV PYTHONPYCACHEPREFIX=/tmp/rc-pycache
WORKDIR /workspace
ENTRYPOINT ["python", "/workspace/packages/policy-sdk/src/rc_policy_sdk/source.py", "--module", "rc_policy_sdk.host"]

FROM runtime AS test
COPY --from=policy-deps /test-requirements.lock /tmp/test.lock
RUN python -m pip install --no-cache-dir -r /tmp/test.lock && python -m pip check
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "tests/test_qwen35_backend.py", "tests/test_qwen35_configuration.py", "tests/test_qwen35_runtime.py", "tests/test_qwen35_cuda_graph.py"]
