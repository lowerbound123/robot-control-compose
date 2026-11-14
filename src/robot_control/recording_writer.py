"""Text persistence and asynchronous lossless video in the recorder subprocess."""
from bisect import bisect_left, bisect_right
from collections import OrderedDict, deque
import ast
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time

from rc_policy_sdk.tables import normalize_table_document, table_document_text
from .secret_config import public_value
from .table_output import _SecretMatcher, _hide_strings
from .recording_media import VideoStore, SessionLock, STORAGE

CAMERAS = ('head', 'left_wrist', 'right_wrist')
STREAMS = ('state', 'control', 'events', 'policy', 'llm_failure', 'session')
COLUMNS = ['sample_id', 'timestamp_ns', 'monotonic_ns'] + [f'{arm}_{i}' for arm in ('left','right') for i in range(7)]
MAX_TOKEN_INTERVALS = 8192
MAX_BARRIER_TOKENS = 8192
MAX_STOP_REQUESTS = 64
MAX_OPEN_REQUESTS = 4096
MAX_RECENT_LLM_EVENTS = 8192
_SECRET_WORDS = ('apikey', 'authorization', 'password', 'secret', 'accesstoken')
_CREDENTIAL_TEXT = re.compile(
    r'''(?i)(\b(?:api[_-]?key|(?:proxy[_-]?)?authorization|password|secret|access[_-]?token)\b["']?\s*[:=]\s*)'''
    r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:bearer|basic)\s+[^\s,;}\]"']+|[^\s,;}\]"']+)''')


def _sensitive(key):
    name = str(key).lower().replace('_', '').replace('-', '')
    return any(word in name for word in _SECRET_WORDS)


def clean(value, secrets=()):
    """JSON projection, with credential fields removed before persistence."""
    return _clean(value, _credential_matcher(secrets))


def _credential_matcher(secrets):
    variants = {variant for secret in secrets if secret for variant in (
        secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1])}
    return _SecretMatcher(variants) if variants else None


def _remember_hidden(value, hidden):
    """Carry already-hidden string leaves into the other table projections."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if item and item != '***':
                hidden.add(item)
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('nonfinite JSON number')
    return value


class _TextNumber:
    """A nonfinite token inside diagnostic text, never a business JSON value."""
    def __init__(self, text):
        self.text = text


def _fragment_float(text):
    value = float(text)
    return value if math.isfinite(value) else _TextNumber(text)


def _fragment_json(value):
    """Serialize cleaned text structure while retaining nonfinite spellings.

    Strict business/copy validation remains separate. Quoted strings such as
    "NaN" still go through json.dumps; only decoder-created tokens stay bare.
    """
    if isinstance(value, _TextNumber):
        return value.text
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key, ensure_ascii=False) + ':' + _fragment_json(item)
                              for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(_fragment_json(item) for item in value) + ']'
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _json_candidates(text):
    """Index balanced containers without parsing arbitrary text recursively.

    Work backwards so each quote/container jumps over its already-indexed
    contents. Candidates inside quoted text are still available if an enclosing
    fragment is invalid, matching the ordinary log fragment semantics.
    """
    tokens, escaped = [], False
    for index, char in enumerate(text):
        if char in '{}[]' or (char == '"' and not escaped):
            tokens.append((char, index))
        escaped = not escaped if char == '\\' else False
    tails, depths = [None] * (len(tokens) + 1), [0] * (len(tokens) + 1)
    candidates, quote = [], None
    for index in range(len(tokens) - 1, -1, -1):
        char, start = tokens[index]
        if char in '}]':
            tails[index] = index
        elif char == '"':
            if quote is not None:
                tails[index], depths[index] = tails[quote + 1], depths[quote + 1]
            quote = index
        else:
            end = tails[index + 1]
            if end is not None and tokens[end][0] == {'[': ']', '{': '}'}[char]:
                depth = depths[index + 1] + 1
                candidates.append((start, tokens[end][1] + 1, depth))
                tails[index], depths[index] = tails[end + 1], max(depth, depths[end + 1])
    return reversed(candidates)


def _clean(value, matcher, hidden=None):
    if isinstance(value, _TextNumber):
        return value
    if isinstance(value, dict):
        sensitive_patch = _sensitive(value.get('path', ''))
        projected = {}
        for key, item in value.items():
            if _sensitive(key) or (key == 'value' and sensitive_patch):
                if hidden is not None:
                    _remember_hidden(item, hidden)
                projected[str(key)] = '***'
            else:
                projected[str(key)] = _clean(item, matcher, hidden)
        return projected
    if isinstance(value, (list, tuple)):
        return [_clean(v, matcher, hidden) for v in value]
    if hasattr(value, 'tolist'):
        return _clean(value.tolist(), matcher, hidden)
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        value = public_value(value)
        if matcher is not None:
            value = matcher.hide(value)
        # Model response bodies and stdout events can themselves be JSON strings.
        # Redact their decoded leaves, then re-escape, preserving parseability.
        # Walk disjoint fragments: depth follows JSON nesting, never row count.
        # Diagnostic strings can contain nonfinite number tokens. Keep their
        # structural context for redaction without accepting them as business
        # JSON or converting an overflow spelling (1e999) into Infinity.
        decoder = json.JSONDecoder(parse_float=_fragment_float, parse_constant=_TextNumber)
        parts, cursor, failed_end, retry_depth = [], 0, 0, 0
        for start, candidate_end, depth in _json_candidates(value):
            if start < cursor or (start < failed_end and depth > retry_depth):
                continue
            try:
                parsed, end = decoder.raw_decode(value, start)
                projected = _clean(parsed, matcher, hidden)
                middle = _fragment_json(projected) if projected != parsed else value[start:end]
            except RecursionError:
                # A string can look deeply nested without being nested input
                # data. Retain its text and try shallower contained fragments;
                # halve probe depth instead of retrying every wrapping bracket.
                failed_end, retry_depth = candidate_end, depth // 2
                continue
            except ValueError:
                continue
            parts.extend((_clean_credential_text(value[cursor:start], hidden), middle))
            cursor = end
        parts.append(_clean_credential_text(value[cursor:], hidden))
        return ''.join(parts)
    return public_value(value)


def _clean_credential_text(value, hidden):
    def replace(match):
        if hidden is not None:
            secret = match[2]
            if secret[:1] in ('"', "'") and secret[-1:] == secret[:1]:
                _remember_hidden(secret[1:-1], hidden)
                try:
                    secret = json.loads(secret) if secret.startswith('"') else ast.literal_eval(secret)
                except (ValueError, SyntaxError, RecursionError):
                    try:
                        secret = ast.literal_eval(secret)
                    except (ValueError, SyntaxError, RecursionError):
                        secret = secret[1:-1]
            _remember_hidden(secret, hidden)
        return match[1] + '***'
    return _CREDENTIAL_TEXT.sub(replace, value)


def _clean_table_record(data, matcher, secrets):
    """Only the explicit top-level display envelope has table semantics.

    Business JSON nested in the event continues through ordinary ``clean``.
    Sanitize structured copies first, then derive the full text once; reparsing
    the TSV loses sensitive-column relationships and repeats all cell work.
    """
    projected = _clean({key: value for key, value in data.items()
                        if key not in ('message', 'table_document', 'copy_json')}, matcher)
    hidden = set()
    try:
        original = json.loads(data['copy_json'], parse_float=_finite_float, parse_constant=_finite_float)
    except (ValueError, TypeError, RecursionError):
        original, valid_copy = data['copy_json'], False
    else:
        valid_copy = True
    result = _clean(original, matcher, hidden)
    try:
        document = normalize_table_document(data['table_document'])
    except (ValueError, TypeError, RecursionError):
        document = None
    for table in document['tables'] if document is not None else ():
        columns = table['columns']
        for row in table['rows']:
            sensitive_value = any(
                column in ('path', 'key', 'field', 'fields', 'name', '项目', '字段')
                and isinstance(cell, str) and _sensitive(cell)
                for column, cell in zip(columns, row))
            for index, column in enumerate(columns):
                if _sensitive(column) or (sensitive_value and column in ('value', 'value_text', '值')):
                    _remember_hidden(row[index], hidden)
                    row[index] = '***'
    document = _clean(document, matcher, hidden)
    # Reuse one linear matcher for every title, column, cell and original JSON
    # copy. Include configured raw/escaped variants without scanning per key.
    table_matcher = _credential_matcher((*secrets, *hidden))
    if table_matcher is not None:
        result = _hide_strings(result, table_matcher)
        for table in document['tables'] if document is not None else ():
            for key in ('title', 'columns', 'rows'):
                table[key] = _hide_strings(table[key], table_matcher)
    text = json.dumps(result, ensure_ascii=False, allow_nan=False) if valid_copy else result
    if document is None or not valid_copy:
        # Invalid display metadata is not persisted as a second, unsafe copy.
        return {**projected, 'message': text, 'copy_json': text}
    return {**projected, 'table_document': document, 'message': table_document_text(document),
            'copy_json': text}


def encoded_line(data, secrets=()):
    matcher = _credential_matcher(secrets)
    if (isinstance(data, dict) and 'table_document' in data
            and isinstance(data.get('message'), str) and isinstance(data.get('copy_json'), str)):
        data = _clean_table_record(data, matcher, secrets)
    else:
        data = _clean(data, matcher)
    text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
    return text.encode('utf-8', errors='backslashreplace')


def line(stream, data, secrets=()):
    stream.write(encoded_line(data, secrets).decode('utf-8'))


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Credentials:
    """Remember bounded configured credentials, including after a session stops."""
    def __init__(self):
        self.values = set()
        self.secrets = ()
        self.size = 0

    def remember(self, data, sensitive=False):
        if isinstance(data, dict):
            patch = _sensitive(data.get('path', ''))
            for key, value in data.items():
                self.remember(value, sensitive or _sensitive(key) or (patch and key == 'value'))
        elif isinstance(data, (list, tuple)):
            for value in data:
                self.remember(value, sensitive)
        elif sensitive and isinstance(data, str) and data.strip() not in ('', '***', '[REDACTED]'):
            for value in (data, data.strip()):
                if value not in self.values:
                    size = len(value.encode('utf-8', errors='backslashreplace'))
                    if len(self.values) >= 4096 or self.size + size > 1024 * 1024:
                        raise ValueError('credential redaction capacity exceeded')
                    self.values.add(value)
                    self.size += size
            self.secrets = tuple(sorted(self.values, key=len, reverse=True))


class ProcessedTokens:
    """Exact bounded intervals, retained for this writer's complete lifetime."""
    def __init__(self):
        self.intervals = []

    def add(self, token):
        if type(token) is not int or token < 1:
            raise ValueError('invalid recording token')
        intervals = self.intervals
        index = bisect_left(intervals, (token, token))
        start = end = token
        if index and intervals[index - 1][1] >= token - 1:
            index -= 1
            start, end = intervals.pop(index)
            end = max(end, token)
        while index < len(intervals) and intervals[index][0] <= end + 1:
            low, high = intervals.pop(index)
            start, end = min(start, low), max(end, high)
        if len(intervals) >= MAX_TOKEN_INTERVALS:
            raise RuntimeError('recording token interval capacity exceeded')
        intervals.insert(index, (start, end))

    def contains(self, token):
        if type(token) is not int or token < 1:
            raise ValueError('invalid recording barrier token')
        index = bisect_right(self.intervals, (token, float('inf'))) - 1
        return index >= 0 and self.intervals[index][1] >= token

    def covers(self, pending):
        return all(self.contains(token) for token in pending)


class SessionFiles:
    def __init__(self, root, identity, metadata, *, run_id=None, credentials=None):
        self.root, self.identity = Path(root), identity
        self.run_id = run_id if run_id is not None else metadata.get('run_id')
        self.credentials = credentials if credentials is not None else Credentials()
        self.credentials.remember(metadata)
        self.root.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.files, self.csv, self.previous = {}, {}, {}
        self.stats = dict(samples_written=0, repeated_frames=0, missing_frames=0, missed_samples=0)
        self.open_requests = OrderedDict()
        self._recent_llm = OrderedDict()
        self._closed = False
        self.videos = self.session_lock = None
        try:
            self.session_lock = SessionLock(self.root)
            self.videos = VideoStore(self.root)
            metadata = {**metadata, 'image_storage': STORAGE}
            for name in STREAMS + ('samples',):
                self.files[name] = (self.root/(name+'.jsonl')).open('a', encoding='utf-8')
            for name in ('epos','qpos','control_epos','control_qpos'):
                stream = (self.root/(name+'.csv')).open('a', newline='', encoding='utf-8')
                self.files[name] = stream
                self.csv[name] = csv.writer(stream)
                self.csv[name].writerow(COLUMNS)
            self.config('initial', metadata)
            self.event('session', dict(event='record.started', session_id=identity, timestamp_ns=time.time_ns(), hz=30, image_storage=STORAGE))
            self.flush()
            sync_directory(self.root)
            sync_directory(self.root.parent)
        except BaseException:
            self._close_handles()
            # mkdir(exist_ok=False) established ownership of this new directory.
            shutil.rmtree(self.root, ignore_errors=True)
            raise

    def _write(self, stream, data):
        line(self.files.get(stream, self.files['events']),
             {**data, 'record_session_id': self.identity, 'rc_run_id': self.run_id}, self.credentials.secrets)

    def _track(self, key, value):
        if key not in self.open_requests and len(self.open_requests) >= MAX_OPEN_REQUESTS:
            old_key, old = self.open_requests.popitem(last=False)
            self._write('session', {'event': 'record.request_tracking_gap', 'count': 1,
                                   'reason': 'capacity', 'request': list(old_key)})
            if old.get('kind') == 'llm_failure':
                self._write('llm_failure', {**old, 'event': 'llm_failure.recording_incomplete',
                                           'reason': 'tracking_capacity', 'end_outside_recording': True})
        self.open_requests[key] = value

    def config(self, identity, data):
        self.credentials.remember(data)
        folder = self.root/'configs'
        folder.mkdir(exist_ok=True)
        name = identity if identity == 'initial' else hashlib.sha256(identity.encode()).hexdigest()
        path = folder/(name+'.json')
        if not path.exists():
            temporary = folder/(name+'.part')
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    line(stream, {**data, 'record_session_id': self.identity, 'rc_run_id': self.run_id},
                         self.credentials.secrets)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            sync_directory(folder)
            sync_directory(self.root)

    def event(self, stream, data):
        if stream == 'configs':
            self.config(str(data['identity']), data)
            return
        if stream == 'policy':
            key = ('policy', str(self.run_id), *(str(data.get(k,'')) for k in
                   ('instance_id','runtime_id','session_id','request_id','generation','config_version','observation_id')))
            if data.get('event') in ('policy.start','policy.aggregate_start'):
                self._track(key, {'kind': 'policy'})
            elif data.get('event') in ('policy.result','policy.error','policy.cancel','policy.timeout','policy.aggregate_result','policy.aggregate_error','policy.aggregate_cancel'):
                if key not in self.open_requests:
                    data = {**data, 'start_outside_recording': True}
                self.open_requests.pop(key, None)
        self._write(stream, data)
        if stream == 'events' and isinstance(data.get('message'), str):
            text = data['message']
            # RC prefixes policy stdout with its authoritative instance ID.
            instance = data.get('instance_id')
            if text.startswith('[') and '] ' in text:
                printed_instance, text = text[1:].split('] ', 1)
                instance = instance or printed_instance
            if text.startswith('{'):
                try:
                    event = json.loads(text)
                except (ValueError, TypeError):
                    return
                if isinstance(event, dict) and str(event.get('event', '')).startswith('llm_failure.'):
                    self._llm_event(event, instance or event.get('instance_id'), data)

    def _llm_event(self, event, instance, source):
        query = event.get('query_id')
        phase = event.get('event')
        identity = (str(self.run_id), str(instance), str(query))
        duplicate = (*identity, str(phase))
        if query is not None and duplicate in self._recent_llm:
            return
        if query is not None:
            if len(self._recent_llm) >= MAX_RECENT_LLM_EVENTS:
                self._recent_llm.popitem(last=False)
            self._recent_llm[duplicate] = None
        data = {**event, 'policy_instance_id': instance, 'received_at': source.get('time'),
                'received_timestamp_ns': source.get('timestamp_ns'),
                'received_monotonic_ns': source.get('monotonic_ns')}
        for name in ('runtime_id', 'session_id', 'host_instance_id', 'deployment_id', 'generation', 'config_version'):
            if name in source:
                data['policy_' + name] = source[name]
        if query is None:
            data['request_identity_missing'] = True
            self._write('llm_failure', data)
            return
        key = ('llm_failure', *identity)
        tracked = self.open_requests.get(key)
        if phase == 'llm_failure.request.started':
            tracked = {'kind': 'llm_failure', 'query_id': query, 'policy_instance_id': instance,
                       'start_outside_recording': False, 'request_terminal_seen': False,
                       **{k: v for k, v in data.items() if k.startswith('policy_')}}
        elif tracked is None:
            tracked = {'kind': 'llm_failure', 'query_id': query, 'policy_instance_id': instance,
                       'start_outside_recording': True, 'request_terminal_seen': False,
                       **{k: v for k, v in data.items() if k.startswith('policy_')}}
        data['start_outside_recording'] = tracked['start_outside_recording']
        if phase in ('llm_failure.request.completed', 'llm_failure.request.error'):
            tracked['request_terminal_seen'] = True
        tracked['last_event'] = phase
        if phase in ('llm_failure.collection.accepted', 'llm_failure.collection.discarded'):
            data['terminal_outside_recording'] = not tracked['request_terminal_seen']
            self.open_requests.pop(key, None)
        else:
            self._track(key, tracked)
        self._write('llm_failure', data)

    def frames(self, frames, sample_id=None):
        return (self.videos.observation(frames) if sample_id is None
                else self.videos.sample(frames, sample_id))

    def stats_snapshot(self):
        return {**self.stats, **self.videos.stats()}

    def sample(self, payload):
        images, pending = self.frames(payload['frames'], payload['sample_id'])
        for camera in CAMERAS:
            frame = payload['frames'].get(camera, {})
            if images[camera].get('missing'):
                self.stats['missing_frames'] += 1
            else:
                source = (frame.get('sequence'), frame.get('timestamp'))
                images[camera]['repeated'] = self.previous.get(camera) == source
                self.stats['repeated_frames'] += int(images[camera]['repeated'])
                self.previous[camera] = source
        prefix = [payload[k] for k in COLUMNS[:3]]
        state = payload.get('state') or {}
        for name, field in (('epos','eef'), ('qpos','qpos')):
            values = state.get(field)
            self.csv[name].writerow(prefix + (list(values) if values is not None and len(values)==14 else ['']*14))
        for name, mode in (('control_epos','epos'), ('control_qpos','qpos')):
            values = []
            for arm in ('left','right'):
                target = payload.get('controls', {}).get(arm)
                values += list(target['values']) if target and target['mode']==mode else ['']*7
            self.csv[name].writerow(prefix + values)
        self.event('state', {k:v for k,v in payload.items() if k not in ('frames','controls')})
        # Text is durable before sample_commit. Video acceptance is separate
        # from durability, which is proven by video_segments.jsonl.
        for name in ('epos', 'qpos', 'control_epos', 'control_qpos', 'state'):
            stream = self.files[name]
            stream.flush()
            os.fsync(stream.fileno())
        commit = {k:v for k,v in payload.items() if k not in ('frames','state','controls')} | {
            'images':images, 'control_events':payload.get('controls', {}),
            'event':'sample_commit', 'record_session_id':self.identity, 'rc_run_id':self.run_id,
            'image_storage': STORAGE}
        stream = self.files['samples']
        stream.flush()
        fd = stream.fileno()
        offset = os.lseek(fd, 0, os.SEEK_END)
        remaining = memoryview(encoded_line(commit, self.credentials.secrets))
        try:
            while remaining:
                count = os.write(fd, remaining)
                if count <= 0:
                    raise OSError('sample commit write made no progress')
                remaining = remaining[count:]
            os.fsync(fd)
        except BaseException:
            # No buffered commit bytes can be replayed during close after a
            # partial write. Keep the previously committed JSONL prefix intact.
            try:
                os.ftruncate(fd, offset)
                os.fsync(fd)
            except OSError:
                pass
            raise
        self.stats['samples_written'] += 1
        self.stats['missed_samples'] += int(payload.get('missed', False))
        return pending

    def flush(self):
        for stream in self.files.values():
            stream.flush()
            os.fsync(stream.fileno())

    def close(self, error=None, *, deadline=None):
        if self._closed:
            return
        failure = None
        try:
            try:
                self.videos.close(deadline if deadline is not None else time.monotonic()+30)
            except Exception as exc:
                failure = exc
                error = error or str(exc)
            for value in self.open_requests.values():
                if value.get('kind') == 'llm_failure':
                    self._write('llm_failure', {**value, 'event': 'llm_failure.recording_incomplete',
                                              'reason': 'recording_error' if error else 'recording_stopped',
                                              'end_outside_recording': True})
            self.event('session', dict(event='record.error' if error else 'record.stopped', error=error,
                timestamp_ns=time.time_ns(), unfinished_requests=[list(k) for k in self.open_requests], **self.stats_snapshot()))
            self.flush()
        except BaseException as exc:
            failure = exc
        finally:
            self._closed = True
            cleanup = self._close_handles()
            self.open_requests.clear()
            self._recent_llm.clear()
        if failure is not None:
            raise failure
        if cleanup is not None:
            raise cleanup

    def _close_handles(self):
        failure = None
        if self.videos is not None:
            try: self.videos.cancel()
            except BaseException as exc: failure = exc
        for stream in self.files.values():
            try:
                stream.close()
            except BaseException as exc:
                failure = failure or exc
        if self.session_lock is not None:
            self.session_lock.close()
        return failure


def writer_main(receiver, responder, directory, run_id):
    """Only this process opens log files. Parent may terminate it on a deadline."""
    base = session = None
    stopping = deque()
    pending_acks = {}
    input_closed = False
    eof_deadline = None
    processed = ProcessedTokens()
    credentials = Credentials()
    try:
        try:
            os.nice(5)
        except (AttributeError, OSError):
            pass
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        base = (root/f'rc_{run_id}.jsonl').open('a', encoding='utf-8')
        responder.send({'kind':'ready'})
        last_flush = time.monotonic()
        while True:
            packet = None
            try:
                if not input_closed and receiver.poll(.005 if pending_acks or stopping else .05):
                    packet = receiver.recv()
            except EOFError:
                input_closed = True
                eof_deadline = time.monotonic() + 5
            if packet is not None:
                kind, token = packet['kind'], packet['token']
                error = None
                accepted = []
                try:
                    if kind == 'start':
                        if session is not None:
                            raise RuntimeError('previous recording still open')
                        session = SessionFiles(packet['path'], packet['session'], packet.get('data', {}),
                                               run_id=run_id, credentials=credentials)
                        responder.send({'kind':'started','request':packet['request'],'session':session.identity})
                    elif kind in ('stop','shutdown'):
                        if len(packet['pending']) > MAX_BARRIER_TOKENS or len(stopping) >= MAX_STOP_REQUESTS:
                            raise RuntimeError('recording stop barrier capacity exceeded')
                        pending = frozenset(packet['pending'])
                        for value in pending:
                            if type(value) is not int or value < 1:
                                raise ValueError('invalid recording barrier token')
                        stopping.append({**packet, 'pending': pending})
                    elif kind == 'abort':
                        if session is not None and session.identity == packet['session']:
                            closing, session = session, None
                            closing.videos.cancel()
                            try: closing.close(packet['error'], deadline=packet.get('deadline', time.monotonic()+1))
                            except Exception: pass  # Abort already reports the explicit recording failure.
                        responder.send({'kind':'aborted','session':packet['session'],'error':packet['error']})
                    elif kind == 'gap':
                        data = {'event':'diagnostic_gap', 'count':packet['count'],
                                'record_session_id':packet.get('session'), 'rc_run_id':run_id}
                        if session is not None and packet.get('session') == session.identity:
                            session.event('session', data)
                        elif base is not None:
                            line(base, {**data, 'message':f"diagnostic records dropped: {packet['count']}"}, credentials.secrets)
                    elif kind == 'log':
                        if base is not None:
                            try:
                                line(base, {**packet['data'], 'rc_run_id':run_id}, credentials.secrets)
                            except OSError as exc:
                                base.close(); base = None
                                responder.send({'kind':'log_error','error':str(exc)})
                        if session is not None and packet.get('session') == session.identity:
                            session.event('events', packet['data'])
                    elif session is not None and packet.get('session') == session.identity:
                        if kind == 'sample':
                            accepted = session.sample(packet['data'])
                        elif kind == 'event':
                            data = packet['data']
                            if packet.get('frames'):
                                images, accepted = session.frames(packet['frames'])
                                data = {**data, 'image_files':images}
                            session.event(packet['stream'], data)
                except Exception as exc:
                    error = clean(f'{type(exc).__name__}: {exc}', credentials.secrets)
                    if session is not None:
                        failed = session.identity
                        closing, session = session, None
                        try:
                            closing.videos.cancel()
                            closing.close(error, deadline=time.monotonic()+1)
                        except Exception: pass
                        responder.send({'kind':'failed','session':failed,'error':error})
                    if kind == 'start':
                        responder.send({'kind':'request_error','request':packet['request'],'error':error})
                    elif kind in ('stop', 'shutdown'):
                        responder.send({'kind':'stopped','request':packet['request'],'error':error,'stats':{}})
                pending_acks[token] = accepted
                accepted = None
                # In particular, do not retain the last acknowledged RGB sample
                # while poll() waits for another packet.
                packet = None
                data = None
            if session is not None:
                try: session.videos.check()
                except Exception as exc:
                    error = clean(f'{type(exc).__name__}: {exc}', credentials.secrets)
                    closing, session = session, None
                    try:
                        closing.videos.cancel()
                        closing.close(error, deadline=time.monotonic()+1)
                    except Exception: pass
                    responder.send({'kind':'failed','session':closing.identity,'error':error})
            for ack_token, futures in list(pending_acks.items()):
                if all(future.done() for future in futures):
                    processed.add(ack_token)
                    responder.send({'kind':'ack','token':ack_token,
                                    'stats':session.stats_snapshot() if session else None})
                    del pending_acks[ack_token]
            futures = None
            shutdown = False
            while stopping and processed.covers(stopping[0]['pending']):
                if session is not None:
                    boundary = stopping[0]
                    session.videos.begin_close(boundary.setdefault('deadline', time.monotonic()+30))
                    if not session.videos.closed():
                        break  # Keep accepting/flushing ordinary logs during sealing.
                boundary = stopping.popleft()
                error = None
                stats = session.stats_snapshot() if session else {}
                try:
                    final_entry = boundary.get('final_entry')
                    if session is not None:
                        if final_entry is not None:
                            session.event('events', {**final_entry, 'event':'rc.shutdown_summary',
                                'phase':'data_drained_before_writer_close'})
                        closing, session = session, None
                        closing.close(deadline=boundary.get('deadline', time.monotonic()+30))
                        stats = closing.stats_snapshot()
                    if base is not None:
                        if final_entry is not None:
                            line(base, {**final_entry, 'rc_run_id':run_id, 'writer_drained':True}, credentials.secrets)
                        base.flush(); os.fsync(base.fileno())
                except Exception as exc:
                    error = clean(str(exc), credentials.secrets)
                responder.send({'kind':'stopped','request':boundary['request'],'error':error,'stats':stats})
                shutdown = shutdown or boundary['kind'] == 'shutdown'
            if shutdown:
                break
            if input_closed:
                if (not pending_acks and not stopping) or time.monotonic() >= eof_deadline: break
                time.sleep(.001)
            if time.monotonic()-last_flush >= 1:
                try:
                    if base is not None:
                        base.flush(); os.fsync(base.fileno())
                except OSError as exc:
                    if base is not None:
                        try: base.close()
                        except OSError: pass
                        base = None
                    responder.send({'kind':'log_error','error':clean(str(exc), credentials.secrets)})
                try:
                    if session is not None:
                        session.flush()
                except (OSError, ValueError) as exc:
                    failed = session.identity
                    closing, session = session, None
                    error = clean(str(exc), credentials.secrets)
                    try: closing.close(error)
                    except Exception: pass
                    responder.send({'kind':'failed','session':failed,'error':error})
                last_flush = time.monotonic()
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        try: responder.send({'kind':'fatal','error':clean(str(exc), credentials.secrets)})
        except (OSError, EOFError): pass
    finally:
        if session is not None:
            try: session.close('writer terminated')
            except Exception: pass
        if base is not None:
            try: base.close()
            except OSError: pass
        receiver.close(); responder.close()
