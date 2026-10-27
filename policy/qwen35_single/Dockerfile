FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS wheels
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY --from=policy-deps /requirements.lock /tmp/qwen35.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --wheel-dir /wheels --require-hashes -r /tmp/qwen35.lock
RUN python -m pip install --no-cache-dir setuptools==75.8.2 wheel==0.45.1 packaging==26.3
COPY packages/policy-sdk /src/policy-sdk
COPY packages/qwen35-policy /src/qwen35-policy
COPY vendor/qwen35 /src/qwen35-vendor
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels \
    /src/policy-sdk /src/qwen35-policy /src/qwen35-vendor

FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HF_HOME=/data/huggingface
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=policy-deps /requirements.lock /opt/qwen35/requirements.lock
# Bind the wheelhouse only for installation; it must not double the final image size.
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --no-deps /wheels/*.whl && python -m pip check
COPY scripts/install_qwen35_patches.py /opt/qwen35/scripts/install_qwen35_patches.py
COPY vendor/qwen35/lerobot/policies/qwen3_5vla/transformers_replace/models/qwen3_5/modeling_qwen3_5.py /opt/qwen35/vendor/qwen35/lerobot/policies/qwen3_5vla/transformers_replace/models/qwen3_5/modeling_qwen3_5.py
RUN python /opt/qwen35/scripts/install_qwen35_patches.py && \
    python /opt/qwen35/scripts/install_qwen35_patches.py --check && \
    chmod -R a+rX /opt/qwen35
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "rc_policy_sdk.host"]

FROM runtime AS test
COPY --from=policy-deps /test-requirements.lock /tmp/test.lock
RUN python -m pip install --no-cache-dir -r /tmp/test.lock && python -m pip check
COPY tests/test_qwen35_backend.py tests/test_qwen35_configuration.py tests/test_qwen35_runtime.py tests/test_qwen35_cuda_graph.py /opt/app/tests/
COPY policy /opt/app/policy
COPY vendor/qwen35 /opt/app/vendor/qwen35
COPY scripts /opt/app/scripts
COPY packages/qwen35-policy /opt/app/packages/qwen35-policy
RUN chmod -R a+rX /opt/app
WORKDIR /opt/app
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "tests/test_qwen35_backend.py", "tests/test_qwen35_configuration.py", "tests/test_qwen35_runtime.py", "tests/test_qwen35_cuda_graph.py"]
