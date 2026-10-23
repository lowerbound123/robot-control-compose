"""Keyboard policy-chain editor; holds a draft, never performs model or file I/O."""
import time

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class PolicyManagerAction(Message):
    def __init__(self, action):
        super().__init__()
        self.action = action


class CellAction(Message):
    def __init__(self, action):
        super().__init__()
        self.action = action


class ChainCells(OptionList):
    BINDINGS = [Binding('a', 'insert_above', show=False), Binding('b', 'insert_below', show=False),
                Binding('delete', 'delete_cell', show=False), Binding('d', 'delete_twice', show=False),
                Binding('j', 'cursor_down', show=False), Binding('k', 'cursor_up', show=False)]
    _last_d = 0.

    def action_insert_above(self): self.post_message(CellAction('above'))
    def action_insert_below(self): self.post_message(CellAction('below'))
    def action_delete_cell(self): self.post_message(CellAction('delete'))

    def action_delete_twice(self):
        now = time.monotonic()
        if now - self._last_d < .5:
            self._last_d = 0.
            self.action_delete_cell()
        else:
            self._last_d = now

    def on_key(self, event):
        if event.key != 'd': self._last_d = 0.

    def on_blur(self): self._last_d = 0.


class AvailablePolicies(OptionList):
    BINDINGS = [Binding('j', 'cursor_down', show=False), Binding('k', 'cursor_up', show=False)]


class PolicyManagerPanel(Widget):
    BINDINGS = [Binding('escape', 'escape', show=False), Binding('shift+enter', 'apply', show=False),
                Binding('ctrl+s', 'save', show=False)]
    DEFAULT_CSS = """
    PolicyManagerPanel { display: none; height: 14; min-height: 8; max-height: 50vh; border: solid $accent; }
    PolicyManagerPanel #chain-columns { height: 1fr; }
    PolicyManagerPanel .chain-column { width: 1fr; height: 1fr; min-width: 0; }
    PolicyManagerPanel .chain-label { height: 1; text-style: bold; }
    PolicyManagerPanel OptionList { height: 1fr; border: none; padding: 0 1; }
    PolicyManagerPanel #chain-cells { border-right: solid $accent; }
    PolicyManagerPanel #chain-note { height: 1; }
    PolicyManagerPanel #chain-keys { height: auto; max-height: 2; color: $text-muted; }
    """

    def __init__(self):
        super().__init__(id='policy-manager')
        self.policies = []
        self.applied = None
        self.saved = []
        self.catalog = []
        self.token = None
        self.initialized = False
        self.pending = None
        self.error = ''

    def compose(self):
        with Horizontal(id='chain-columns'):
            with Vertical(classes='chain-column'):
                yield Static('当前 policy 链', classes='chain-label')
                yield ChainCells(id='chain-cells')
            with Vertical(classes='chain-column'):
                yield Static('可用 policy', classes='chain-label')
                yield AvailablePolicies(id='chain-available')
        yield Static('', id='chain-note')
        yield Static('a/b 上/下插入 · Enter 选择/替换 · Delete/dd 删除 · Esc 返回/关闭 · Shift+Enter 应用 · Ctrl+S 保存', id='chain-keys')

    def load_snapshot(self, snapshot):
        if not self.initialized:
            self.policies = list(snapshot['policies'])
            self.initialized = True
        self.token = snapshot['token']
        self.catalog = snapshot['catalog']
        self.saved = list(snapshot['saved'])
        self.applied = snapshot['applied']
        self.pending = None
        self.error = ''
        self.refresh_draft()

    def refresh_draft(self, selected=None):
        left = self.query_one('#chain-cells', OptionList)
        if selected is None: selected = left.highlighted or 0
        left.clear_options()
        seen = set()
        for index, identity in enumerate(self.policies):
            suffix = ' ↪ 共享实例' if identity in seen else ''
            left.add_option(Option(Text(f'{index + 1:02}  {identity}{suffix}')))
            seen.add(identity)
        if not self.policies:
            left.add_option(Option('空草稿：按 a/b 添加', disabled=True))
        left.highlighted = min(selected, max(0, len(self.policies) - 1))
        self._refresh_catalog()
        self.refresh_note()

    def refresh_note(self, message=None):
        text = message or self.error or ' · '.join([
            '未应用' if self.policies != self.applied else '已应用',
            '未保存' if self.policies != self.saved else '已保存',
            '顺序重复项共享 worker/context',
        ])
        self.query_one('#chain-note', Static).update(Text(text))

    def _candidate(self, identity):
        result = list(self.policies)
        index, replace = self.pending or ((self.query_one('#chain-cells', OptionList).highlighted or 0), bool(result))
        if replace: result[index] = identity
        else: result.insert(index, identity)
        return result

    def _refresh_catalog(self):
        right = self.query_one('#chain-available', OptionList)
        right.clear_options()
        modes = {row['id']: row.get('control_mode') for row in self.catalog if row['valid']}
        for row in self.catalog:
            reason = row.get('error', '') if not row['valid'] else ''
            candidate = self._candidate(row['id'])
            if not reason and len({modes.get(identity) for identity in candidate}) > 1:
                reason = 'control_mode 不兼容'
            label = f"{row['id']}  [{row.get('policy_host', '?')} / {row.get('control_mode', '?')}]"
            if reason: label += f' — {reason}'
            right.add_option(Option(Text(label), disabled=bool(reason)))
        right.highlighted = 0 if self.catalog else None

    def on_cell_action(self, event):
        event.stop()
        if self.disabled: return
        left = self.query_one('#chain-cells', OptionList)
        index = left.highlighted or 0
        self.error = ''
        if event.action == 'delete':
            if self.policies:
                self.policies.pop(index)
                self.pending = None
                self.refresh_draft(index)
            return
        self.pending = (index + (event.action == 'below' and bool(self.policies)), False)
        self._refresh_catalog()
        self.refresh_note('选择要插入的 policy；Enter 确认，Esc 取消')
        self.query_one('#chain-available', OptionList).focus()

    def on_option_list_option_selected(self, event):
        event.stop()
        if event.option_list.id == 'chain-cells':
            self.pending = (event.option_index, True)
            self._refresh_catalog()
            self.refresh_note('选择替换 policy；Enter 确认，Esc 取消')
            self.query_one('#chain-available', OptionList).focus()
        else:
            identity = self.catalog[event.option_index]['id']
            selected = self.pending[0] if self.pending else (self.query_one('#chain-cells', OptionList).highlighted or 0)
            self.policies = self._candidate(identity)
            self.pending = None
            self.error = ''
            self.refresh_draft(selected)
            self.query_one('#chain-cells', OptionList).focus()

    def action_escape(self):
        if self.pending is not None:
            self.pending = None
            self.refresh_draft()
            self.query_one('#chain-cells', OptionList).focus()
        else:
            self.post_message(PolicyManagerAction('close'))

    def action_apply(self): self.post_message(PolicyManagerAction('apply'))
    def action_save(self): self.post_message(PolicyManagerAction('save'))
