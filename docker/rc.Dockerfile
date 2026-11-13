FROM docker:28.5.1-cli AS docker-cli
FROM docker/compose-bin:v2.39.1 AS compose-cli
FROM python:3.10-slim-bookworm@sha256:5be5aaecb962a41567bc8041f1e9e3432cb085b037fe057f9a562630b2e4b370 AS dependencies
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=compose-cli /docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements/rc.lock /tmp/rc.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/rc.lock && python3 -m pip check

FROM dependencies AS runtime
ENV PYTHONPYCACHEPREFIX=/tmp/rc-pycache
WORKDIR /workspace
ENTRYPOINT ["/bin/bash", "/workspace/docker/rc-entrypoint.sh"]
CMD ["python3", "-m", "robot_control", "--config", "/workspace/configs/rc.mock.yaml"]

FROM runtime AS test
COPY requirements/test.lock /tmp/test.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/test.lock && python3 -m pip check
ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
CMD ["python3", "-m", "pytest", "-q", "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider"]
