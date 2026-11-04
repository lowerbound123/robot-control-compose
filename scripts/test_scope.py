"""按本次改动选择回归范围。默认只运行使用 example 的用例。"""
import argparse
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
QWEN = {"qwen35_single", "qwen35_twostage"}
LLM = {"llm_direction", "llm_failure"}
POLICIES = {"example", "hold", "pi05", "internvla_maas"} | QWEN | LLM


def suite(policies, interface):
    if interface:
        return ["tests"]
    paths = set()
    def add(pattern):
        matches = list((ROOT / "tests").glob(pattern))
        if not matches:
            raise ValueError(f"未找到回归用例：{pattern}")
        paths.update("tests/" + path.name for path in matches)

    add("test_example_*.py")
    if "hold" in policies:
        paths.add("tests/test_end_to_end.py::test_real_hold_example_consumes_absolute_targets")
    if "pi05" in policies:
        add("test_pi05_*.py")
    if "internvla_maas" in policies:
        add("test_internvla_maas_*.py")
    if QWEN <= policies:
        add("test_qwen35_*.py")
    elif policies & QWEN:
        # A change to one entry should not exercise the other workflow's model backend.
        for name in ("test_qwen35_deployment.py", "test_qwen35_policies.py", "test_qwen35_worker.py"):
            add(name)
    if "llm_direction" in policies:
        for name in ("test_llm_direction.py", "test_llm_providers.py", "test_llm_worker.py", "test_policy_secrets.py"):
            add(name)
    if "llm_failure" in policies:
        add("test_llm_failure*.py")
        add("test_llm_provider_config.py")
    elif "llm_direction" in policies:
        paths.add("tests/test_llm_provider_config.py::test_setup_creates_private_profiles_only_once_and_handler_returns_only_path")

    # Shared files parameterize the two Qwen entries / provider profiles by source ID.
    excluded = set()
    for family in (QWEN, LLM):
        if policies & family:
            excluded.update(family - policies)
    options = ["-k", " and ".join("not " + name for name in sorted(excluded))] if excluded else []
    if "pi05" in policies and "qwen35_single" not in policies:
        options += ["--deselect=tests/test_pi05_runtime.py::test_pi05_and_qwen_share_alignment_and_blending_but_not_gripper_offset"]
    return sorted(paths) + options


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--policy", nargs="+", choices=sorted(POLICIES), default=[], help="额外验证的策略来源，可列多个")
    group.add_argument("--interface", action="store_true", help="公共接口变更：运行完整 pytest 套件")
    parser.add_argument("--collect-only", action="store_true", help="只列出将运行的用例")
    args = parser.parse_args()
    policies = set(args.policy)
    packages = {"policy-sdk"}
    if args.interface:
        packages.update(p.parent.name for p in (ROOT / "packages").glob("*/src"))
    else:
        if policies & QWEN:
            packages.add("qwen35-policy")
        if "pi05" in policies:
            packages.add("pi05-policy")
        if "internvla_maas" in policies:
            packages.add("internvla-maas-policy")
        if policies & LLM:
            packages.add("llm-provider-config")
            packages.update(p.replace("_", "-") + "-policy" for p in policies & LLM)
    # pytest 的 pythonpath 只影响主进程；真正的 host/worker 和 ROS 仿真器也必须能导入本次源码。
    sources = [ROOT / "src", *(ROOT / "packages" / p / "src" for p in sorted(packages)),
               ROOT / "ros_ws/src/robot_control_test_sim"]
    env = os.environ.copy()
    roots = [str(path) for path in sources]
    if env.get("PYTHONPATH"):
        roots.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(roots)
    options = ["--collect-only"] if args.collect_only else []
    if env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1":
        options += ["-p", "pytest_asyncio.plugin"]
    os.chdir(ROOT)
    os.execve(sys.executable, [sys.executable, "-m", "pytest", "-q", *options,
                               *suite(policies, args.interface)], env)


if __name__ == "__main__":
    main()
