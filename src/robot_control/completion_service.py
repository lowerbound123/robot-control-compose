"""Bounded daemon completion queries; UI calls only read cached metadata/results."""
from dataclasses import replace
import json
import os
import threading
import time

from .completion import CompletionItem, analyze


def worker_binding(worker):
    """Only in-memory identity fields; no RPC, locks, or lifecycle transitions."""
    if worker is None:
        return None
    return (id(worker), getattr(worker, 'session_id', None), getattr(worker, 'runtime_id', None),
            getattr(getattr(worker, 'host', None), 'instance_id', None),
            getattr(worker, 'context_id', None), getattr(worker, 'config_version', None),
            getattr(worker, '_state', None), getattr(worker, '_reinitializing', False),
            getattr(worker, 'cleanup_confirmed', False))


class CompletionService:
    DEBOUNCE = .1
    TIMEOUT = 1.
    CACHE_TTL = 1.
    LIMIT = 200
    MAX_ACTIVE = 4

    def __init__(self, controller, tools=(), *, catalog=None, debounce=DEBOUNCE,
                 timeout=TIMEOUT, cache_ttl=CACHE_TTL, refresh_interval=.1):
        self.controller = controller
        self._catalog = dict(catalog or {})
        self._catalog['tools'] = tuple(tools) or self._catalog.get('tools', ())
        self._tools = self._catalog['tools']
        self.debounce, self.timeout, self.cache_ttl = debounce, timeout, cache_ttl
        self.refresh_interval = refresh_interval
        self._lock = threading.RLock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._revision, self._input = 0, None
        self._ready, self._wanted = None, None
        self._active, self._cache = {}, {}
        self._sources = None
        self._scheduler = threading.Thread(target=self._run, name='rc-completion-scheduler', daemon=True)
        self._scheduler.start()
        self._metadata = None
        self._discovery = None
        if callable(getattr(controller, 'completion_snapshot', None)):
            self._metadata = threading.Thread(target=self._refresh, name='rc-completion-metadata', daemon=True)
            self._metadata.start()
            if getattr(controller, 'registry', None) is not None:
                self._discovery = threading.Thread(target=self._discover_sources,
                                                   name='rc-completion-sources', daemon=True)
                self._discovery.start()

    def _prepare(self, text, cursor):
        self._revision += 1
        result = analyze(text, cursor, self._catalog)
        result.revision = self._revision
        self._input = (text, cursor)
        self._wanted = None
        request = result.path_request
        if request is not None:
            identity = request.get('instance')
            entry = self._catalog.get('instances', {}).get(identity, {})
            worker = entry.get('worker')
            binding = worker_binding(worker)
            key = (identity, binding, json.dumps(request, sort_keys=True, ensure_ascii=False))
            job = {'result': result, 'worker': worker, 'binding': binding, 'key': key,
                   'target': identity or '__rc__', 'due': time.monotonic() + self.debounce}
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] <= self.cache_ttl:
                # Even cache hits must recheck the current controller binding off UI.
                job['cached'] = cached[1]
                job['due'] = time.monotonic()
            self._wanted = job
        return result

    def request(self, text, cursor):
        """Immediate static analysis. Never invokes controller methods or performs I/O."""
        with self._lock:
            result = self._prepare(text, cursor)
            self._ready = None
        self._wake.set()
        return result

    def poll(self):
        with self._lock:
            result, self._ready = self._ready, None
            if result and (result.revision != self._revision or (result.text, result.cursor) != self._input):
                return None
            return result

    def close(self):
        """A stalled filesystem/RPC cannot delay UI shutdown or Python exit."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._wanted = self._ready = None
            self._revision += 1

    def _discover_sources(self):
        # One independent daemon keeps a slow mount from freezing live instance
        # identity updates. A stuck scan never spawns additional scan tasks.
        while not self._stop.is_set():
            try:
                sources = [row['id'] for row in self.controller.registry.list() if row.get('valid')]
                with self._lock:
                    if not self._stop.is_set():
                        self._sources = sources
            except Exception:
                pass  # Keep the last discovered source list; live data is separate.
            self._stop.wait(1.)

    def _refresh(self):
        while not self._stop.is_set():
            try:
                snapshot = self.controller.completion_snapshot()
                snapshot = dict(snapshot, tools=self._tools)
                with self._lock:
                    if self._sources is not None:
                        snapshot['sources'] = sorted(set(snapshot.get('sources', ())) | set(self._sources))
                    if snapshot != self._catalog and not self._stop.is_set():
                        self._catalog = snapshot
                        if self._input is not None:
                            self._ready = self._prepare(*self._input)
                        self._wake.set()
            except Exception:
                # Completion metadata is advisory; retain the last known catalog.
                pass
            self._stop.wait(self.refresh_interval)

    def _matches(self, job):
        result = job['result']
        return (not self._stop.is_set() and result.revision == self._revision
                and (result.text, result.cursor) == self._input)

    def _run(self):
        while not self._stop.is_set():
            launch = None
            now = time.monotonic()
            with self._lock:
                for job in list(self._active.values()):
                    if not job.get('timed_out') and now - job['started'] >= self.timeout:
                        job['timed_out'] = True
                        if self._matches(job):
                            self._ready = replace(job['result'], lint='路径建议查询超时；仍可手动输入')
                pending = self._wanted
                if (pending is not None and pending['due'] <= now
                        and pending['target'] not in self._active and len(self._active) < self.MAX_ACTIVE):
                    self._wanted = None
                    pending['started'] = now
                    self._active[pending['target']] = pending
                    launch = pending
                # Old bindings never grow the cache without bound.
                self._cache = {key: value for key, value in self._cache.items()
                               if now - value[0] <= self.cache_ttl}
                if len(self._cache) > 256:
                    self._cache = dict(list(self._cache.items())[-256:])
            if launch is not None:
                threading.Thread(target=self._query, args=(launch,),
                                 name='rc-completion-path', daemon=True).start()
            self._wake.wait(.01)
            self._wake.clear()

    def _still_bound(self, job):
        request, worker = job['result'].path_request, job['worker']
        if request.get('instance') is None:
            return True
        if worker_binding(worker) != job['binding']:
            return False
        snapshot = getattr(self.controller, 'completion_snapshot', None)
        if callable(snapshot):
            entry = snapshot().get('instances', {}).get(request['instance'], {})
            return worker_binding(entry.get('worker')) == job['binding']
        return True

    def _query(self, job):
        request = job['result'].path_request
        try:
            if 'cached' in job:
                if not self._still_bound(job):
                    raise RuntimeError('实例已变化；路径建议已过期')
                response = job['cached']
            elif request.get('instance') is not None:
                worker = job['worker']
                query = getattr(worker, 'complete_path', None)
                if not callable(query):
                    raise RuntimeError('此实例未提供路径补全能力')
                if not self._still_bound(job):
                    raise RuntimeError('实例已变化；路径建议已过期')
                response = query(request['selector'], request['prefix'], timeout=self.timeout)
            else:
                from rc_policy_sdk.path_completion import list_paths
                response = list_paths(request['spec'], request['prefix'], request['base_dir'],
                                      limit=self.LIMIT, timeout=self.timeout)
                if request.get('pose'):
                    # pose restore treats relative *.json as project-root paths.
                    # Absolute suggestions point unambiguously at state_saves.
                    response = dict(response, items=[dict(item, text=os.path.abspath(os.path.join(
                        request['base_dir'], item['text']))) for item in response.get('items', ())])
            if not isinstance(response, dict):
                raise ValueError('路径建议响应无效')
            bound = self._still_bound(job)
        except Exception as exc:
            response, bound = {'items': [], 'error': str(exc)}, True
        with self._lock:
            if self._active.get(job['target']) is job:
                self._active.pop(job['target'], None)
            if bound and not job.get('timed_out') and not self._stop.is_set():
                if 'cached' not in job:
                    self._cache[job['key']] = (time.monotonic(), response)
                pending = self._wanted
                if pending is not None and pending['key'] == job['key'] and self._matches(pending):
                    self._wanted = None
                    self._ready = self._with_response(pending['result'], response)
                elif self._matches(job):
                    self._ready = self._with_response(job['result'], response)
        self._wake.set()

    @classmethod
    def _with_response(cls, result, response):
        items = []
        for item in response.get('items', ())[:cls.LIMIT]:
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                value = item['text']
                if item.get('kind') == 'directory' and not value.endswith('/'):
                    value += '/'
                items.append(CompletionItem(str(item.get('name', value)), value, kind=item.get('kind', 'file')))
        error = response.get('error')
        hint = result.hint + (' · 仅显示前 200 项，请继续输入缩小范围' if response.get('truncated') else '')
        return replace(result, items=items, hint=hint,
                       lint=f'路径建议不可用：{error}；仍可手动输入' if error else result.lint)
