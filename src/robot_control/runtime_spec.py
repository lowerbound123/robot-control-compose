"""Frozen deployment inputs and exact ownership-bearing runtime handles."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

LABEL = 'io.robot-control.'


def project_name(deployment, policy, run, generation):
    # Hash the original ID, so underscores never collide with hyphens.
    digest = hashlib.sha256(json.dumps([deployment, policy, run, generation]).encode()).hexdigest()[:24]
    return f'rcp-{digest}'


@dataclass(frozen=True)
class DeploymentSpec:
    policy_id: str
    directory: str
    config_version: int
    config_json: str
    model_json: str
    image_id: str
    anchor_id: str
    socket: str
    anchor_epoch: str = ""


@dataclass
class DeploymentHandle:
    deployment_id: str
    daemon_id: str
    policy_id: str
    run_id: str
    generation: int
    project: str
    anchor_id: str
    socket: str
    anchor_epoch: str = ''
    container_id: str = ''
    host_instance_id: str = ''
    phase: str = 'intent'
    uncertain: bool = False
    cleanup_forced: bool = False
    resources: list = field(default_factory=list)

    def labels(self):
        return {LABEL + key: str(value) for key, value in dict(deployment=self.deployment_id,
            policy=self.policy_id, run=self.run_id, generation=self.generation, project=self.project).items()}


@dataclass(frozen=True)
class ContainerExitProof:
    deployment_id: str
    daemon_id: str
    policy_id: str
    run_id: str
    generation: int
    container_id: str
    host_instance_id: str
    removed: bool
    forced: bool
    # Identity token is issued only by the manager and checked by the bound process.
    authority: object = field(repr=False, compare=False)


def map_host_path(value, directory, context):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(directory) / path
    path = path.resolve()
    client = Path(context['client_root']).resolve()
    if path.is_relative_to(client):
        return str(Path(context['host_root']) / path.relative_to(client))
    # Absolute paths outside the mounted project already name host resources.
    return str(path)
