# Inference subset; numerical transform bodies retain their source implementation.
from __future__ import annotations
import abc
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Optional
import draccus
import numpy as np
import torch
from lerobot.transforms.utils import resize_with_pad, resize_to_long_side
from lerobot.dataset_schemas import get_schema
from lerobot.utils.constants import OBS_IMAGE, OBS_IMAGES, OBS_STATE, ACTION
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


@DataTransformFn.register_subclass("resize_to_long_side")
@dataclass
class ResizeImagesToLongSideFn(DataTransformFn):
    """Preserve aspect ratio while resizing the longest side, without padding."""

    long_side: int
    mode: str = "bilinear"
    mapping: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.long_side <= 0:
            raise ValueError(f"long_side must be positive, got {self.long_side}")

    def hydrate(
        self, dataset: LeRobotDataset | StreamingLeRobotDataset
    ) -> ResizeImagesToLongSideFn:
        robot_type = dataset.meta.robot_type
        schema = get_schema(robot_type)
        return replace(self, mapping=schema.image_mapping)

    def __call__(self, data: DataDict) -> DataDict:
        for img_key in self.mapping.keys():
            data[img_key] = resize_to_long_side(data[img_key], self.long_side, self.mode)
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


@DataTransformFn.register_subclass("reorder_state_action")
@dataclass
class ReorderStateActionTransform(DataTransformFn):
    """
    Reorder action and state dimensions according to schema configuration.
    
    Uses flexible src->dst mapping format: [[src_start, src_end, dst_start, dst_end], ...]
    Creates a zero tensor and copies source slices to destination positions.
    Allows gaps (zero-filled regions) in the output.
    
    Example:
        action_reorder:
          - [0, 6, 0, 6]      # src[0:6] -> dst[0:6]
          - [6, 7, 7, 8]      # src[6:7] -> dst[7:8] (gap at index 6)
          - [7, 13, 8, 14]    # src[7:13] -> dst[8:14]
          - [13, 14, 15, 16]  # src[13:14] -> dst[15:16] (gap at indices 14,15)
        
        This creates a 16-dim output where indices 6, 14, 15 are zeros.
    """
    
    action_reorder: Optional[list[list[int]]] = None
    state_reorder: Optional[list[list[int]]] = None

    def hydrate(self, dataset: LeRobotDataset | StreamingLeRobotDataset) -> ReorderStateActionTransform:
        schema = get_schema(dataset.meta.robot_type)
        return replace(
            self,
            action_reorder=schema.action_reorder,
            state_reorder=schema.state_reorder,
        )

    def _reorder(self, tensor: torch.Tensor, reorder_spec: list[list[int]]) -> torch.Tensor:
        """
        Reorder tensor dimensions according to spec.
        
        Args:
            tensor: Input tensor with shape [..., D]
            reorder_spec: List of [src_start, src_end, dst_start, dst_end]
        
        Returns:
            Reordered tensor with potentially different last dimension size.
            Unfilled positions are zeros.
        """
        # Determine target size from max dst_end
        # target_size = max(dst_end for _, _, _, dst_end in reorder_spec)
        
        # Create zero tensor of target size
        output_shape = list(tensor.shape)
        # output_shape[-1] = target_size
        output = torch.zeros(output_shape, dtype=tensor.dtype, device=tensor.device)
        
        # Copy source slices to destination positions
        for src_start, src_end, dst_start, dst_end in reorder_spec:
            src_len = src_end - src_start
            dst_len = dst_end - dst_start
            if src_len != dst_len:
                raise ValueError(
                    f"Source and destination slice lengths must match: "
                    f"src[{src_start}:{src_end}] (len={src_len}) vs dst[{dst_start}:{dst_end}] (len={dst_len})"
                )
            output[..., dst_start:dst_end] = tensor[..., src_start:src_end]
        
        return output

    def __call__(self, data: DataDict) -> DataDict:
        # Reorder action if specified
        if self.action_reorder is not None:
            data[ACTION] = self._reorder(data[ACTION], self.action_reorder)
        
        # Reorder state if specified
        if self.state_reorder is not None:
            data[OBS_STATE] = self._reorder(data[OBS_STATE], self.state_reorder)
        
        return data


