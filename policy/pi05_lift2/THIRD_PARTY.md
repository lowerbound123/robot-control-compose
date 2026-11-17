# Pi05 LIFT2 推理运行时来源

参考版本在 2026-11-17 从服务器已知可用的 `qwen3_5_dev/postrain4internvla/lerobot_pi05_runtime/src` 核对；没有使用 `qwen3_5_vqa` 的部署脚本，也不在运行时导入上述目录。

- `src/rc_pi05_lift2/modeling.py`：根据参考 `lerobot/policies/pi05/modeling_pi05.py` 提取推理算子。保留 Physical Intelligence / Hugging Face 的 Apache 2.0 版权头；删除训练、数据集、动作队列和 Hub 加载逻辑，模型直接构造相同架构并严格加载完整 checkpoint。
- `dependencies/transformers_patch/models/`：五个参考 Transformers 4.57.1 文件逐字节保留，安装只发生在本策略独立镜像构建时。没有更改原 Pi05 或 Qwen 镜像。
- `backend.py`：独立实现参考部署的 RGB resize/pad、mean/std、状态 token、连续14维动作还原。模型输出后仍通过本项目原有的单次夹爪处理和 SDK 缓存/融合。
- 基础 Apache 2.0 许可见 `dependencies/LICENSE-APACHE-2.0`；参考仓库自己的许可见 `dependencies/LICENSE-REFERENCE`。保留这些文件和原始版权声明。

模型推理数学保持参考行为：raw image embedding；语言 embedding 乘 sqrt(hidden_size)；prefix/suffix 不额外除 sqrt；Gemma 内部不重复乘 sqrt；保留 AdaRMS、KV cache、固定步数 Euler 去噪及选定权重的 fp32。

参考模型文件 SHA256：`bf2215dcb9983bc8e917cdeba80070ecdf07c59d092dae6b251ef79ba6202946`。

| Transformers 补丁 | SHA256 |
|---|---|
| `models/gemma/configuration_gemma.py` | `26fd0d0f52730ab32b6f337eb73ae9a26cb71ae0915bc49eced1d0e2ed8d0ec6` |
| `models/gemma/modeling_gemma.py` | `243b189e0524c534abd9efeaf7b69bb2c2487cbb9afa9584e7a93b07a849adb4` |
| `models/paligemma/modeling_paligemma.py` | `c67d4e8c4c12cf18acf7cc97888f6b18f34a57c17f97ee2caa5c261316a1d4ba` |
| `models/siglip/check.py` | `8668ce45b26907e7368123073f590040ed6dfe9b144312a7e017fc544da5f21f` |
| `models/siglip/modeling_siglip.py` | `ef2e99500f263fdd78db9d4d9a255e29d844eb92f53c25ad640f8056700ef84b` |

`tests/fixtures/pi05_lift2/` 的配置仅保留推理相关字段；权重形状来自实际 pi05_zero 的 safetensors header，不包含训练权重。`reference_output.json` 是在已知 dev 环境中，以缩小架构、确定性合成参数、固定输入和噪声运行得到的参考输出；仅替换基础大模型加载，不修改参考前向算子。它用于防止后续升级悄悄改变模型数学，不能替代完整模型真机验收。
