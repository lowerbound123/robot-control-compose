#!/usr/bin/env python3
"""Apply the source tutorial's Qwen3.5 patch to exactly Transformers 5.2.0.

Run in the dedicated model environment. --check is read-only; an unpatched
installation exits nonzero. No checkpoint files or package downloads occur.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from importlib import metadata, util
from pathlib import Path

VERSION = "5.2.0"
SOURCE = (
    Path(__file__).resolve().parents[1]
    / "vendor/qwen35/lerobot/policies/qwen3_5vla/transformers_replace/models/qwen3_5/modeling_qwen3_5.py"
)


def install(*, check: bool = False) -> Path:
    version = metadata.version("transformers")
    if version != VERSION:
        raise RuntimeError(
            f"Qwen3.5 patch requires transformers=={VERSION}, found {version}"
        )
    spec = util.find_spec("transformers")
    if spec is None or spec.origin is None:
        raise RuntimeError("transformers package not found")
    destination = Path(spec.origin).parent / "models/qwen3_5/modeling_qwen3_5.py"
    if not destination.is_file():
        raise FileNotFoundError(
            f"Expected Transformers Qwen3.5 module missing: {destination}"
        )
    expected = hashlib.sha256(SOURCE.read_bytes()).digest()
    if hashlib.sha256(destination.read_bytes()).digest() == expected:
        return destination
    if check:
        raise RuntimeError(
            "Qwen3.5 Transformers patch is missing or differs; run this script without --check"
        )
    backup = destination.with_suffix(".py.pre-rc-qwen35")
    if not backup.exists():
        shutil.copy2(destination, backup)
    shutil.copyfile(SOURCE, destination)
    if hashlib.sha256(destination.read_bytes()).digest() != expected:
        raise RuntimeError("patch verification failed")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(f"Qwen3.5 patch verified: {install(check=args.check)}")


if __name__ == "__main__":
    main()
