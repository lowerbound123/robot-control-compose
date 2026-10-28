"""本地 policy 能力示例：透传上层动作，空输入时保持当前关节姿态。"""

import json
import platform
import threading


def validate_config(config):
    """初始化和热更新前验证；不要在这里修改传入的配置。"""
    params = config["params"]
    if not isinstance(params["message"], str):
        raise ValueError("params.message 必须是字符串")
    interval = params["log_every_n_calls"]
    if type(interval) is not int or interval <= 0:
        raise ValueError("params.log_every_n_calls 必须是正整数")
    # 本例的空输入分支读取 qpos，不能将关节位置误标成末端位姿。
    if config["execution"]["control_mode"] != "qpos":
        raise ValueError("example 的空输入保持要求 execution.control_mode=qpos")


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


def initialize(config):
    """一次实例只初始化一次；锁用于保护与命令、观测、监控共享的状态。"""
    validate_config(config)
    context = {
        # 保留原始引用：SDK 接受热补丁后会原地更新这个对象。
        "config": config,
        "lock": threading.Lock(),
        "stats": _empty_stats(),
        "python": platform.python_version(),
        "released": False,
    }
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
    """有动作就原样返回；唯一例外是空输入时生成一帧当前 qpos。"""
    calls, message, interval = _record(context, images, runtime, "policy")
    if calls % interval == 0:
        print(f"example policy #{calls}：{message}", flush=True)

    # RC 控制示例，默认不执行。仅当前 policy() 推理线程可以请求暂停，
    # SDK 随成功结果提交给 RC；仍需返回合法动作，并由 RC 控制线程处理。
    # runtime.pause("example：演示暂停请求")

    # 不用 if action_chunk：NumPy 数组的真假判断有歧义。
    if len(action_chunk) > 0:
        return action_chunk
    return [list(state["qpos"])]


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
            "python": context["python"],
            "released": int(context["released"]),
        }


def example(context):
    """manifest 注册的同名无参数命令；返回值由框架作为 JSON 显示。"""
    return monitor(context)


def reset_context(context):
    """SDK 已等待在途回调结束；只清统计，不替换 config 或锁。"""
    with context["lock"]:
        context["stats"] = _empty_stats()
    print("example reset_context：统计已清空", flush=True)


def release(context):
    """初始化失败时可能收到 None；重复调用也只释放一次。"""
    if context is None:
        return
    with context["lock"]:
        if context["released"]:
            return
        context["released"] = True
    # 本例没有模型、后台线程或外部资源需要关闭；图片由 SDK 管理。
    print("example release：本地示例已释放", flush=True)
