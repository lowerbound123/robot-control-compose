FROM docker:28.5.1-cli AS docker-cli
FROM docker/compose-bin:v2.39.1 AS compose-cli
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS dependencies
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=compose-cli /docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates build-essential ffmpeg && rm -rf /var/lib/apt/lists/*
ENV PATH="/opt/rc/bin:${PATH}"
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    export UV_PYTHON_INSTALL_DIR=/opt/cpython && \
    uv python install 3.14.10t --no-bin && \
    "$(uv python find --managed-python 3.14.10t)" -m venv /opt/rc
COPY requirements/rc.lock /tmp/rc.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/rc.lock && python3 -m pip check && \
    python3 -c 'import sys, _posixshmem, numpy, mcp.server, textual; from scipy.spatial.transform import Rotation; from PIL import Image; assert not sys._is_gil_enabled()'

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
