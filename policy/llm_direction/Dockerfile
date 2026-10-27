FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS wheels
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY policy/llm_direction/requirements.lock /tmp/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --wheel-dir /wheels --require-hashes -r /tmp/requirements.lock
COPY packages/policy-sdk /src/policy-sdk
COPY packages/llm-direction-policy /src/llm-direction-policy
RUN python -m pip wheel --no-deps --wheel-dir /wheels /src/policy-sdk /src/llm-direction-policy

FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY policy/llm_direction/requirements.lock /opt/llm-direction/requirements.lock
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --no-deps /wheels/*.whl && python -m pip check
RUN python -c "from openai import OpenAI; c = OpenAI(api_key='build-check-not-a-key'); assert callable(c.decisions.create); c.close()"
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "rc_policy_sdk.host"]
CMD ["--host-id", "llm-direction", "--socket", "/run/robot-control/llm-direction.sock", "--policy-root", "/workspace/policy"]
