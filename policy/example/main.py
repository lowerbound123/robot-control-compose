"""本地 policy 能力示例：透传上层动作，空输入时按控制模式保持实测姿态。"""

from collections.abc import Mapping
import json
import platform
import threading
import time
import uuid


_MODULE_ID = uuid.uuid4().hex


def _chunk_size(value):
    if type(value) is not int or not 1 <= value <= 256:
        raise ValueError("params.chunk_size 必须是 1 到 256 之间的整数")
    return value


def validate_config(config):
    """初始化和热更新前验证；不要在这里修改传入的配置。"""
    params = config["params"]
    if not isinstance(params["message"], str):
        raise ValueError("params.message 必须是字符串")
    interval = params["log_every_n_calls"]
    if type(interval) is not int or interval <= 0:
        raise ValueError("params.log_every_n_calls 必须是正整数")
    _chunk_size(params.get("chunk_size", 1))  # 旧的本机 manifest 仍可运行。
    delay = params.get("inference_delay_s", 0)
    if type(delay) not in (int, float) or not 0 <= delay <= 5:
        raise ValueError("params.inference_delay_s 必须在 0 到 5 秒之间")
    if not isinstance(params.get("reader_url", ""), str):
        raise ValueError("params.reader_url 必须是字符串")
    if config["execution"]["control_mode"] not in ("qpos", "epos", "mixed"):
        raise ValueError("execution.control_mode 必须是 qpos、epos 或 mixed")


def _empty_stats():
    return {
        "calls": 0,
        "observations": 0,
        "last_callback": "none",
        "observation_id": "",
        "consumed_actions": 0,
        "control_epoch": 0,
        "config_version": 0,
        "image_count": 0,
        "images": "{}",
    }


def initialize(config, *, runtime=None):
    """每次初始化创建独立 context；同进程重初始化时模块身份保持一致。"""
    validate_config(config)
    context = {
        # 保留原始引用：SDK 接受热补丁后会原地更新这个对象。
        "config": config,
        "lock": threading.Lock(),
        "stats": _empty_stats(),
        "python": platform.python_version(),
        "context_id": uuid.uuid4().hex,
        "released": False,
        # 仅共享命令使用初始化时的 session；无全局状态服务的独立 worker 仍可跑动作。
        # pause/finish 必须使用 policy() 当轮的 runtime。
        "session": runtime,
        "pending_result": None,
        "reader": None,
    }
    if config["params"].get("reader_url"):
        from .remote_reader import RemoteReader
        context["reader"] = RemoteReader(config["params"]["reader_url"], runtime)
    print("example initialize：本地示例已就绪", flush=True)
    return context


def _record(context, images, runtime, callback):
    # images 是 SDK 从 POSIX 共享内存映射的只读 NumPy 数组集合，
    # images.descriptors 提供时间戳等元数据。无需自己 open/mmap/unlink。
    # 这里只读一个像素；不修改、复制整帧，也不把数组/切片留在 context。
    summaries = {}
    for name, image in images.items():
        summaries[name] = {
            "shape": list(image.shape),
            "dtype": str(image.dtype),
            "timestamp": float(images.descriptors[name]["timestamp"]),
            "readonly": not image.flags.writeable,
            "sample": str(image.flat[0]),
        }
    # monitor 仅接受标量，因此将多相机摘要编码为字符串；没有保存图像视图。
    image_summary = json.dumps(summaries, ensure_ascii=False)
    with context["lock"]:
        stats = context["stats"]
        stats["calls" if callback == "policy" else "observations"] += 1
        stats.update(
            last_callback=callback,
            observation_id=str(runtime.observation_id),
            consumed_actions=int(runtime.consumed_actions),
            control_epoch=int(runtime.control_epoch),
            config_version=int(runtime.config_version),
            image_count=len(summaries),
            images=image_summary,
        )
        params = context["config"]["params"]
        return stats["calls"], params["message"], params["log_every_n_calls"]


def policy(context, state, action_chunk, images, config, *, runtime):
    """只有这条推理回调可以登记控制请求；是否消费或丢弃动作由 RC 决定。"""
    # 在入口认领本轮请求；读图或模拟慢推理期间到达的命令留给下一轮。
    with context["lock"]:
        request = context["pending_result"]
        context["pending_result"] = None
    calls, message, interval = _record(context, images, runtime, "policy")
    if calls % interval == 0:
        print(f"example policy #{calls}：{message}", flush=True)

    delay = config["params"].get("inference_delay_s", 0)
    if delay:
        time.sleep(delay)  # 只阻塞本次推理，observe/命令/monitor 仍可用。
    if context["reader"] is not None:
        context["reader"].read(runtime, images.descriptors)
    if request is not None:
        outcome, reason = request
        if outcome == "pause":
            runtime.pause(reason)
        elif outcome == "fault":
            raise RuntimeError(reason)
        else:
            runtime.finish(reason, outcome=outcome)

    # mixed 的空输入也是一个非空字典。看帧数，不测试字典/NumPy 数组的真假。
    frames = action_chunk["actions"] if isinstance(action_chunk, Mapping) else action_chunk
    if len(frames) > 0:
        return action_chunk
    mode = config["execution"]["control_mode"]
    size = config["params"].get("chunk_size", 1)
    if mode == "mixed":
        modes = [("qpos", "epos")[i % 2] for i in range(size)]
        return {"actions": [list(state["qpos" if m == "qpos" else "eef"]) for m in modes],
                "modes": modes}
    return [list(state["qpos" if mode == "qpos" else "eef"]) for _ in range(size)]


def wants_observations(context):
    """SDK 在初始化时查询能力；本例始终订阅额外观测。"""
    return True


def observe(context, state, images, config, *, runtime):
    """可与推理并发的观测回调；只记录摘要，不产生动作。"""
    _record(context, images, runtime, "observe")
    # 回调返回前不得保留图片视图。SDK 关闭映射后才向 RC 确认释放，
    # observe 的返回值不用于动作输出，也不能在此调用 runtime.pause()。


def monitor(context):
    """快速只读快照：无图像、网络、模型或控制 I/O，只返回内建标量。"""
    with context["lock"]:
        params = context["config"]["params"]
        return {
            **context["stats"],
            "message": params["message"],
            "log_every_n_calls": params["log_every_n_calls"],
            "chunk_size": params.get("chunk_size", 1),
            "inference_delay_s": params.get("inference_delay_s", 0),
            "instance": context["config"].get("id", "example"),
            "python": context["python"],
            "module_id": _MODULE_ID,
            "context_id": context["context_id"],
            "released": int(context["released"]),
        }


def example(context):
    """manifest 注册的同名无参数命令；返回值由框架作为 JSON 显示。"""
    return monitor(context)


def next_result(context, outcome, message):
    """只排队，不在命令线程调用 runtime.pause/finish；reset 会清除旧意图。"""
    if outcome not in ("pause", "completed", "terminated", "fault"):
        raise ValueError("结果必须是 pause、completed、terminated 或 fault")
    with context["lock"]:
        if context["pending_result"] is not None:
            raise ValueError("已有等待推理的结果")
        context["pending_result"] = (outcome, message)
    return {"queued": outcome}


def set_chunk_size(context, size):
    """同一冷字段补丁可由 manifest 声明不同的初始化范围。"""
    return {"params.chunk_size": _chunk_size(size)}


def reinitialize(context):
    """保留当前配置，在本进程创建新 context。"""
    return {}


def shared_read(context):
    version, snapshot = context["session"].globals.snapshot()
    return {"version": version, "value": snapshot.get("example_note")}


def shared_write(context, message, version):
    """只更新示例自己的键；其他写者已提交时，冲突会原样回报给调用者。"""
    return context["session"].globals.patch({"example_note": message}, expected_version=version)


def reset_context(context):
    """SDK 已等待在途回调结束；清除统计和待发请求，不替换 config 或锁。"""
    with context["lock"]:
        context["stats"] = _empty_stats()
        context["pending_result"] = None
    print("example reset_context：统计已清空", flush=True)


def release(context):
    """初始化失败时可能收到 None；重复调用也只释放一次。"""
    if context is None:
        return
    with context["lock"]:
        if context["released"]:
            return
        context["released"] = True
    # 外部 reader 的退出证明由 RC 收取；这里不能自行释放仍在读取的图像。
    print("example release：本地示例已释放", flush=True)
