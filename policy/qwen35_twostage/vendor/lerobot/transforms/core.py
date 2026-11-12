# Adapted for inference-only robot-control deployment; see PROVENANCE.md.
from __future__ import annotations
import abc
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Optional
import draccus
import torch
import numpy as np
from lerobot.transforms.utils import resize_with_pad
from lerobot.utils.constants import OBS_IMAGE, OBS_IMAGES, OBS_STATE, ACTION
from lerobot.dataset_schemas import get_schema
DataDict = dict[str, Any]

class DataTransformFn(draccus.ChoiceRegistry, abc.ABC):
    @abc.abstractmethod
    def __call__(self, data: DataDict) -> DataDict: ...

    def hydrate(self, dataset) -> DataTransformFn:
        """Override to inject dataset-specific parameters. Default: no-op."""
        return self


@DataTransformFn.register_subclass("composite")
@dataclass(frozen=True)
class CompositeTransform(DataTransformFn):
    """A composite transform that applies a sequence of transforms in order."""

    transforms: list[DataTransformFn]

    def __call__(self, data: DataDict) -> DataDict:
        for transform in self.transforms:
            data = transform(data)
        return data


def compose(transforms: list[DataTransformFn]) -> DataTransformFn:
    """Compose a sequence of transforms into a single transform."""
    return CompositeTransform(transforms)


@DataTransformFn.register_subclass("resize_with_pad")
@dataclass
class ResizeImagesWithPadFn(DataTransformFn):
    height: int
    width: int
    mode: str = "bilinear"
    mapping: dict[str, str] = field(default_factory=dict)

    def hydrate(self, dataset: LeRobotDataset | StreamingLeRobotDataset) -> ResizeImagesWithPadFn:
        robot_type = dataset.meta.robot_type
        schema = get_schema(robot_type)
        mapping = schema.image_mapping
        return replace(self, mapping=mapping)

    def __call__(self, data: DataDict) -> DataDict:
        for img_key in self.mapping.keys():
            data[img_key] = resize_with_pad(data[img_key], self.height, self.width, self.mode)
        return data


@DataTransformFn.register_subclass("remap_image_key")
@dataclass
class RemapImageKeyTransformFn(DataTransformFn):
    """
    Remap image keys to new key names.
    Example:
        mapping = {
            "images.rgb.head": f"{OBS_IMAGES}.image0", 
            "images.rgb.hand_left": f"{OBS_IMAGES}.image1", 
            "images.rgb.hand_right": f"{OBS_IMAGES}.image2", 
        }
    """
    mapping: dict[str, str] = field(default_factory=dict)

    def hydrate(self, dataset: LeRobotDataset | StreamingLeRobotDataset) -> RemapImageKeyTransformFn:
        robot_type = dataset.meta.robot_type
        schema = get_schema(robot_type)
        mapping = schema.image_mapping
        return replace(self, mapping=mapping)

    def __call__(self, data: DataDict) -> DataDict: 
        for old_key, new_key in self.mapping.items():
            data[new_key] = data.pop(old_key)
            data[f"{new_key}_mask"] = torch.tensor(True)
        # create missing keys if necessary
        if len(self.mapping) < 3:
            data[f"{OBS_IMAGES}.image2"] = torch.ones_like(data[f"{OBS_IMAGES}.image0"])
            data[f"{OBS_IMAGES}.image2_mask"] = torch.tensor(False)
        if len(self.mapping) < 2:
            data[f"{OBS_IMAGES}.image1"] = torch.ones_like(data[f"{OBS_IMAGES}.image0"])
            data[f"{OBS_IMAGES}.image1_mask"] = torch.tensor(False)
        return data


@DataTransformFn.register_subclass("normalize")
@dataclass
class NormalizeTransformFn(DataTransformFn):
    """
    Normalize specified keys in a DataDict using precomputed statistics.

    Args:
        selected_keys: list of keys to normalize (e.g. ["observation.state", "actions"]).
            If None, will normalize all keys that exist in norm_stats.
        mode: normalization mode ("mean_std" or "min_max").
        norm_stats: dictionary containing normalization parameters.

    Example:
        norm_stats = {
            "observation.state": {"mean": ..., "std": ..., "min": ..., "max": ...},
            "action": {"mean": ..., "std": ..., "min": ..., "max": ...},
        }
    """

    selected_keys: Optional[list[str]] = None
    mode: str = "mean_std"  # "mean_std" or "min_max"
    norm_stats: dict[str, dict[str, Any]] = field(default_factory=dict)

    def hydrate(self, dataset: LeRobotDataset | StreamingLeRobotDataset) -> NormalizeTransformFn:
        schema = get_schema(dataset.meta.robot_type)
        selected_keys = schema.get_state_keys() + schema.get_action_keys()
        return replace(self, norm_stats=dataset.meta.stats, selected_keys=selected_keys)

    def __call__(self, data: DataDict) -> DataDict:
        eps = 1e-6

        keys = self.selected_keys if self.selected_keys is not None else list(self.norm_stats.keys())

        for key in keys:
            if key not in data:
                logging.warning(
                    f"[NormalizeTransformFn] Key '{key}' not found in data — skipping normalization."
                )
                continue
            if key not in self.norm_stats:
                logging.warning(
                    f"[NormalizeTransformFn] No normalization stats found for key '{key}' — skipping."
                )
                continue

            x = data[key]
            stats = self.norm_stats[key]

            if self.mode == "mean_std":
                mean = torch.from_numpy(stats["mean"]).to(x)
                std = torch.from_numpy(stats["std"]).to(x)
                x = ((x - mean) / (std + eps))
            elif self.mode == "min_max":
                min_v = torch.from_numpy(stats["min"]).to(x)
                max_v = torch.from_numpy(stats["max"]).to(x)
                # align with openpi and lerobot official implementation
                x = 2 * (x - min_v) / (max_v - min_v + eps) - 1
            elif self.mode == "q01_q99":
                min_v = torch.from_numpy(stats["q01"]).to(x)
                max_v = torch.from_numpy(stats["q99"]).to(x)
                # align with openpi and lerobot official implementation
                x = 2 * (x - min_v) / (max_v - min_v + eps) - 1
            else:
                raise ValueError(f"Unknown normalization mode: {self.mode}")

            data[key] = x
        return data


@DataTransformFn.register_subclass("unnormalize")
@dataclass
class UnNormalizeTransformFn(DataTransformFn):
    """
    Unnormalize specified keys in a DataDict using precomputed statistics.

    Args:
        selected_keys: list of keys to unnormalize (e.g. ["observation.state", "actions"]).
            If None, will unnormalize all keys that exist in norm_stats.
        mode: unnormalization mode ("mean_std" or "min_max").
        norm_stats: dictionary containing unnormalization parameters.

    Example:
        norm_stats = {
            "observation.state": {"mean": ..., "std": ..., "min": ..., "max": ...},
            "action": {"mean": ..., "std": ..., "min": ..., "max": ...},
        }
    """

    selected_keys: Optional[list[str]] = None
    mode: str = "mean_std"  # "mean_std" or "min_max"
    norm_stats: dict[str, dict[str, Any]] = field(default_factory=dict)

    def hydrate(self, dataset: LeRobotDataset | StreamingLeRobotDataset) -> UnNormalizeTransformFn:
        schema = get_schema(dataset.meta.robot_type)
        selected_keys = schema.get_state_keys() + schema.get_action_keys()
        return replace(self, norm_stats=dataset.meta.stats, selected_keys=selected_keys)

    def __call__(self, data: DataDict) -> DataDict:
        eps = 1e-6

        keys = self.selected_keys if self.selected_keys else list(self.norm_stats.keys())

        for key in keys:
            if key not in data:
                logging.warning(
                    f"[UnNormalizeTransformFn] Key '{key}' not found in data — skipping unnormalization."
                )
                continue
            if key not in self.norm_stats:
                logging.warning(
                    f"[UnNormalizeTransformFn] No stats found for key '{key}' — skipping unnormalization."
                )
                continue

            x = data[key]
            stats = self.norm_stats[key]

            if self.mode == "mean_std":
                mean = torch.from_numpy(stats["mean"]).to(x)
                std = torch.from_numpy(stats["std"]).to(x)
                x = x * (std + eps) + mean
            elif self.mode == "min_max":
                min_v = torch.from_numpy(stats["min"]).to(x)
                max_v = torch.from_numpy(stats["max"]).to(x)
                # align with openpi and lerobot official implementation
                x = (x + 1) / 2 * (max_v - min_v + eps) + min_v
            elif self.mode == "q01_q99":
                min_v = torch.from_numpy(stats["q01"]).to(x)
                max_v = torch.from_numpy(stats["q99"]).to(x)
                # align with openpi and lerobot official implementation
                x = (x + 1) / 2 * (max_v - min_v + eps) + min_v
            else:
                raise ValueError(f"Unknown unnormalization mode: {self.mode}")

            data[key] = x

        return data

