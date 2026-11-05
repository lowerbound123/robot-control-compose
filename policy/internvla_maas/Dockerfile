FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS wheels
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY --from=policy-deps /build-requirements.lock /tmp/build.lock
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --require-hashes -r /tmp/build.lock
COPY --from=policy-deps /requirements.lock /tmp/runtime.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --no-build-isolation --wheel-dir /wheels --require-hashes -r /tmp/runtime.lock

FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPYCACHEPREFIX=/tmp/rc-pycache
COPY --from=policy-deps /requirements.lock /opt/internvla-maas/requirements.lock
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --no-deps /wheels/*.whl && python -m pip check
WORKDIR /workspace
ENTRYPOINT ["python", "/workspace/packages/policy-sdk/src/rc_policy_sdk/source.py", "--module", "rc_policy_sdk.host"]

FROM runtime AS test
COPY --from=policy-deps /test-requirements.lock /tmp/test.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/test.lock && python -m pip check
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "tests/test_internvla_maas_configuration.py", "tests/test_internvla_maas_codec.py", "tests/test_internvla_maas_images.py", "tests/test_internvla_maas_backend.py", "tests/test_internvla_maas_runtime.py"]
