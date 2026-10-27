FROM python:3.14-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae
COPY docker/ipc-anchor.py /usr/local/bin/ipc-anchor.py
ENTRYPOINT ["python", "/usr/local/bin/ipc-anchor.py"]
HEALTHCHECK --interval=1s --timeout=2s --retries=30 CMD python -c 'import json,os; d=json.load(open("/run/robot-control/anchor-ready.json")); u=int(open("/proc/1/status").read().split("Uid:")[1].split()[0]); assert u==d["uid"]==int(os.environ["RC_UID"]) and u>0'
