"""Real PTY input-assistance fixture; no robot, policy worker, or network."""
import json
from pathlib import Path
from queue import Queue
import sys
from threading import Event

from robot_control.tui import RobotControlApp, CommandInput
from robot_control.completion_panel import CompletionPanel


class Controller:
    def __init__(self, root):
        self.config_path = root / 'rc.yaml'
        self.outputs = root / 'outputs'
        self.events = Queue()
        self.events.put({'level': 'info', 'message': '输入辅助验收 · 中文 / history / Tab / 方向键'})
        self.done = Event()
        self.exit_code = 0
        self.commands = []

    def start(self):
        pass

    def _statusbar_snapshot(self):
        return {'state': 'paused', 'policy': 'example'}

    def submit(self, line):
        self.commands.append(line)
        if line.strip() == 'exit':
            self.done.set()

    def completion_snapshot(self):
        return {'sources': ['example'], 'rc_config': {'frequency_hz': 30},
                'pose_root': str(self.outputs / 'state_saves'),
                'instances': {'example': {'commands': {}, 'config_hints': {'help': '配置提示', 'settings': {
                    'params.message': {'name': '消息', 'type': 'str', 'help': '完整原始消息'},
                    'params.mode': {'name': '模式', 'type': 'enum', 'choices': ['普通', '仔细', '快速'],
                                    'help': '选择检查模式；提示不会执行命令'},
                }}}}}


class TerminalApp(RobotControlApp):
    def on_mount(self):
        super().on_mount()
        self.set_interval(.04, self.observe)
        self._capture_state = None

    def observe(self):
        field = self.query_one(CommandInput)
        panel = self.query_one(CompletionPanel)
        result = field.completion_result
        data = {'value': field.value, 'cursor': field.cursor_position,
                'ghost': field._suggestion, 'menu': field.menu_open,
                'items': [item.label for item in result.items] if result else [],
                'selected': field.selected_completion,
                'columns': panel.columns, 'rows': panel.visible_rows,
                'input_y': field.region.y, 'panel_bottom': panel.region.bottom,
                'width': self.size.width, 'commands': list(self.controller.commands)}
        state = (field.value, bool(field._suggestion), field.menu_open, self.size.width)
        if state != self._capture_state:
            if field._suggestion:
                self.save_screenshot('history.svg', str(self.controller.config_path.parent))
            if field.menu_open and '普通' in data['items']:
                self.save_screenshot('menu.svg', str(self.controller.config_path.parent))
            self._capture_state = state
        output = Path(sys.argv[1])
        staging = output.with_suffix('.tmp')
        staging.write_text(json.dumps(data, ensure_ascii=False))
        staging.replace(output)


if __name__ == '__main__':
    output = Path(sys.argv[1])
    TerminalApp(Controller(output.parent)).run()
