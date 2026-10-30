"""Disk/PNG work lives only in the recorder subprocess."""
from concurrent.futures import ThreadPoolExecutor
from bisect import bisect_left, bisect_right
from collections import OrderedDict, deque
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time

from .secret_config import public_value

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
    if isinstance(value, dict):
        sensitive_patch = _sensitive(value.get('path', ''))
        return {str(k): '***' if _sensitive(k) or (k == 'value' and sensitive_patch)
                else clean(v, secrets) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, secrets) for v in value]
    if hasattr(value, 'tolist'):
        return clean(value.tolist(), secrets)
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        value = public_value(value)
        for secret in secrets:
            for variant in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
                value = value.replace(variant, '***')
        # Model response bodies and stdout events can themselves be JSON strings.
        # Redact their decoded leaves, then re-escape, preserving parseability.
        decoder = json.JSONDecoder()
        for match in re.finditer(r'[\[{]', value):
            try:
                parsed, end = decoder.raw_decode(value[match.start():])
            except ValueError:
                continue
            projected = clean(parsed, secrets)
            middle = (json.dumps(projected, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                      if projected != parsed else value[match.start():match.start() + end])
            return clean(value[:match.start()], secrets) + middle + clean(value[match.start() + end:], secrets)
        return _CREDENTIAL_TEXT.sub(r'\1***', value)
    return public_value(value)


def encoded_line(data, secrets=()):
    text = json.dumps(clean(data, secrets), ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
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
        self.encoders = None
        try:
            self.encoders = ThreadPoolExecutor(max_workers=3, thread_name_prefix='record-png')
            for name in STREAMS + ('samples',):
                self.files[name] = (self.root/(name+'.jsonl')).open('a', encoding='utf-8')
            for name in ('epos','qpos','control_epos','control_qpos'):
                stream = (self.root/(name+'.csv')).open('a', newline='', encoding='utf-8')
                self.files[name] = stream
                self.csv[name] = csv.writer(stream)
                self.csv[name].writerow(COLUMNS)
            self.config('initial', metadata)
            self.event('session', dict(event='record.started', session_id=identity, timestamp_ns=time.time_ns(), hz=30))
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

    def image(self, camera, frame, sample_id=None):
        from PIL import Image
        pixels = frame['image']
        # Content address includes shape and type; no raw-image dedup index is retained.
        digest = hashlib.sha256(str((pixels.shape, str(pixels.dtype))).encode())
        digest.update(memoryview(pixels).cast('B'))
        key = digest.hexdigest()
        folder = self.root/'assets'/camera
        folder.mkdir(parents=True, exist_ok=True)
        asset = folder/(key+'.png')
        if not asset.exists():
            temporary = folder/(key+'.part')
            try:
                with temporary.open('xb') as stream:
                    Image.fromarray(pixels).save(stream, format='PNG', compress_level=1, optimize=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                # link refuses replacement even if another producer races us.
                try:
                    os.link(temporary, asset)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
            sync_directory(folder)
        destination = asset
        if sample_id is not None:
            folder = self.root/'images'/camera
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder/f'{sample_id:09d}.png'
            os.link(asset, destination)
            sync_directory(folder)
            sync_directory(folder.parent)
            sync_directory(self.root)
        return {k:v for k,v in frame.items() if k != 'image'} | {
            'file':str(destination.relative_to(self.root)), 'width':pixels.shape[1], 'height':pixels.shape[0], 'sha256':key}

    def frames(self, frames, sample_id=None):
        futures = {camera:self.encoders.submit(self.image, camera, frame, sample_id)
                   for camera,frame in frames.items() if frame.get('valid', True) and frame.get('image') is not None}
        return {camera:future.result() for camera,future in futures.items()}

    def sample(self, payload):
        images = self.frames(payload['frames'], payload['sample_id'])
        for camera in CAMERAS:
            frame = payload['frames'].get(camera, {})
            if camera not in images:
                images[camera] = {k:v for k,v in frame.items() if k != 'image'} | {'missing': True, 'file':None}
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
        # Every referenced record is durable before the sample becomes visible.
        for name in ('epos', 'qpos', 'control_epos', 'control_qpos', 'state'):
            stream = self.files[name]
            stream.flush()
            os.fsync(stream.fileno())
        commit = {k:v for k,v in payload.items() if k not in ('frames','state','controls')} | {
            'images':images, 'control_events':payload.get('controls', {}),
            'event':'sample_commit', 'record_session_id':self.identity, 'rc_run_id':self.run_id}
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

    def flush(self):
        for stream in self.files.values():
            stream.flush()
            os.fsync(stream.fileno())

    def close(self, error=None):
        if self._closed:
            return
        failure = None
        try:
            for value in self.open_requests.values():
                if value.get('kind') == 'llm_failure':
                    self._write('llm_failure', {**value, 'event': 'llm_failure.recording_incomplete',
                                              'reason': 'recording_error' if error else 'recording_stopped',
                                              'end_outside_recording': True})
            self.event('session', dict(event='record.error' if error else 'record.stopped', error=error,
                timestamp_ns=time.time_ns(), unfinished_requests=[list(k) for k in self.open_requests], **self.stats))
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
        if self.encoders is not None:
            try:
                self.encoders.shutdown(wait=True, cancel_futures=True)
            except BaseException as exc:
                failure = exc
        for stream in self.files.values():
            try:
                stream.close()
            except BaseException as exc:
                failure = failure or exc
        return failure


def writer_main(receiver, responder, directory, run_id):
    """Only this process opens log files. Parent may terminate it on a deadline."""
    base = session = None
    stopping = deque()
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
            if receiver.poll(.05):
                packet = receiver.recv()
                kind, token = packet['kind'], packet['token']
                error = None
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
                            closing.close(packet['error'])
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
                            session.sample(packet['data'])
                        elif kind == 'event':
                            data = packet['data']
                            if packet.get('frames'):
                                data = {**data, 'image_files':session.frames(packet['frames'])}
                            session.event(packet['stream'], data)
                except Exception as exc:
                    error = clean(f'{type(exc).__name__}: {exc}', credentials.secrets)
                    if session is not None:
                        failed = session.identity
                        closing, session = session, None
                        try: closing.close(error)
                        except Exception: pass
                        responder.send({'kind':'failed','session':failed,'error':error})
                    if kind == 'start':
                        responder.send({'kind':'request_error','request':packet['request'],'error':error})
                    elif kind in ('stop', 'shutdown'):
                        responder.send({'kind':'stopped','request':packet['request'],'error':error,'stats':{}})
                processed.add(token)
                responder.send({'kind':'ack','token':token, 'stats':session.stats.copy() if session else None})
                # In particular, do not retain the last acknowledged RGB sample
                # while poll() waits for another packet.
                packet = None
                data = None
            shutdown = False
            while stopping and processed.covers(stopping[0]['pending']):
                boundary = stopping.popleft()
                error = None
                stats = session.stats.copy() if session else {}
                try:
                    final_entry = boundary.get('final_entry')
                    if session is not None:
                        if final_entry is not None:
                            session.event('events', {**final_entry, 'event':'rc.shutdown_summary',
                                'phase':'data_drained_before_writer_close'})
                        closing, session = session, None
                        closing.close()
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
