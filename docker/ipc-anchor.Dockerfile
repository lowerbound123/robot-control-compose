FROM python:3.14-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae
ENV PYTHONPYCACHEPREFIX=/tmp/rc-pycache
ENTRYPOINT ["python", "/workspace/docker/ipc-anchor.py"]
HEALTHCHECK --interval=1s --timeout=2s --retries=30 CMD python /workspace/docker/ipc-anchor.py --healthcheck
