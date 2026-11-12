"""Small checkpoint metadata fixture; never substitutes preprocessing code."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def checkpoint(directory, *, robot_type='arx_acone'):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    config = {'type': 'qwen3_5_context_video_ki', 'chunk_size': 50, 'max_state_dim': 32,
              'max_action_dim': 32, 'context_slot_count': 32, 'context_slot_stride': 15,
              'context_fps': 30., 'context_resize_long_side': 192,
              'image_resize_mode': 'long_side', 'image_resize_long_side': 256,
              'max_num_views': 3, 'num_inference_steps': 10, 'max_prompt_tokens': 850,
              'vlm_model_name_or_path': 'Qwen/Qwen3.5-4B'}
    train = {'dataset': {'action_mode': 'delta', 'norm_mode': 'mean_std', 'pad_keys': [],
                        'data_transforms': {'inputs': [{'type': 'qwen3_5_context_video_ki_processor',
                          'vlm_model_name_or_path': '/training/machine/processor',
                          'max_prompt_tokens': 850, 'max_state_dim': 32, 'max_num_views': 3,
                          'context_slot_count': 32, 'context_fps': 30., 'context_meta_position': 'last',
                          'predict_future_images': True, 'tokenize_state': False}]}}}
    stats = {}
    for prefix in ('states', 'actions'):
        for name, size in [('left_joint', 6), ('left_gripper', 1), ('right_joint', 6), ('right_gripper', 1)]:
            stats[f'{prefix}.{name}.position'] = {'mean': [1.]*size, 'std': [2.]*size,
                                                 'q01': [-1.]*size, 'q99': [3.]*size}
    for name, data in [('config', config), ('train_config', train), ('stats', {robot_type: stats})]:
        (directory / f'{name}.json').write_text(json.dumps(data))
    (directory / 'model.safetensors').write_bytes(b'test replaces only model weight loading')
    return directory


def model_file(directory, *, x=25, pipelined=False, robot_type='arx_acone'):
    directory = Path(directory)
    checkpoint(directory / 'weights', robot_type=robot_type)
    path = directory / 'model.json'
    path.write_text(json.dumps({'model': {'checkpoint': 'weights'}, 'runtime': {'instruction': 'untangle'},
                               'execution': {'replan_every_actions': x, 'pipelined': pipelined}}))
    return path
