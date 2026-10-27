"""RC-owned model registrations and remote frame pins survive policy process death."""
import json
import threading
import time
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .config import atomic_json


class ExternalResources:
    def __init__(self, pool, recovery_path, *, start=False, deployment_id=None, anchor_id=None, anchor_epoch=None):
        self.pool = pool
        self.deployment_id = deployment_id
        self.anchor_id = anchor_id
        self.anchor_epoch = anchor_epoch
        self.recovery_path = recovery_path
        self._lock = threading.RLock()
        self._records = {}
        self._closing_workers = set()
        self._stop = threading.Event()
        self._thread = None
        if start:
            self.start()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._poll, name='rc-external-leases', daemon=False)
            self._thread.start()

    def _persist(self):
        records = []
        for rid, record in self._records.items():
            records.append({k:v for k,v in record.items() if k not in ('worker','leases')}
                           | {'remote_id':rid,'requests':{q:lease.descriptors for q,lease in record['leases'].items()}})
        if records:
            atomic_json(self.recovery_path, {'version':3, 'deployment_id':self.deployment_id, 'anchor_id':self.anchor_id, 'anchor_epoch':self.anchor_epoch, 'remote_models':records})
        else:
            self.recovery_path.unlink(missing_ok=True)

    def handle(self, worker, message):
        method, rid = message['method'], message['remote_id']
        with self._lock:
            if worker in self._closing_workers and method in ('register','retain'):
                raise RuntimeError('RC is closing this policy; no new external work is accepted')
            if method == 'register':
                parsed = urlparse(message['endpoint'])
                if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('invalid model endpoint')
                if rid in self._records:
                    raise ValueError('remote model already registered')
                self._records[rid] = dict(worker=worker,endpoint=message['endpoint'].rstrip('/'),
                                         owner_id=message['owner_id'],model_id=message['model_id'],
                                         generation=None,instance_id=message.get('instance_id'),leases={})
            else:
                record = self._records[rid]
                if record['worker'] is not worker:
                    raise ValueError('remote registration belongs to another worker')
                if method == 'generation':
                    record['generation'] = message['generation']
                elif method == 'retain':
                    request = message['request_id']
                    if request in record['leases']:
                        raise ValueError('request lease already registered')
                    record['leases'][request] = self.pool.retain(message['images'])
                elif method == 'released':
                    lease = record['leases'].pop(message['request_id'],None)
                    if lease:
                        lease.release()
                else:
                    raise ValueError('unknown external resource method')
            self._persist()
        return {'retained':True}

    def begin_close(self,worker, *, deadline=None):
        with self._lock:
            self._closing_workers.add(worker)
            records=[r for r in self._records.values() if r['worker'] is worker]
        for record in records:
            try:
                self._http(record,'/v1/models/unload',{'owner_id':record['owner_id'],
                                                     'expected_instance_id':record['instance_id']},
                           timeout=min(1.,max(.001,deadline-time.monotonic())) if deadline else 1.)
            except Exception:
                pass  # Final close must confirm exit; failure never releases pins.

    @staticmethod
    def _http(record, path, body=None, timeout=1.):
        data = None if body is None else json.dumps(body).encode()
        request = Request(record['endpoint']+path, data=data, headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=timeout) as response:
            return json.load(response)

    def _poll(self):
        while not self._stop.wait(.2):
            with self._lock:
                leases = [(rid,record,request) for rid,record in self._records.items() for request in record['leases']]
            for rid, record, request in leases:
                if self._stop.is_set():
                    return
                try:
                    ack = self._http(record,'/v1/requests/'+quote(request,safe=''))
                    if ack.get('images_released') is True:
                        with self._lock:
                            if self._records.get(rid) is record:
                                lease=record['leases'].pop(request,None)
                                if lease:
                                    lease.release()
                                    self._persist()
                except Exception:
                    # 404, connection failure and timeout never acknowledge release.
                    pass

    @staticmethod
    def _release_remote(record, deadline, http=None):
        http = http or ExternalResources._http
        def left():
            value=deadline-time.monotonic()
            if value<=0:
                raise TimeoutError('external cleanup deadline exceeded')
            return min(2.,value)
        status=http(record,'/v1/status',timeout=left())
        if not record.get('instance_id') or status.get('instance_id') != record['instance_id']:
            raise RuntimeError('server instance differs; old remote readers cannot be confirmed')
        op=http(record,'/v1/models/unload',{'owner_id':record['owner_id'],
                    'expected_instance_id':record['instance_id']},timeout=left())
        while True:
            result=http(record,'/v1/operations/'+op['operation_id'],timeout=left())
            if result['status']=='succeeded':
                outcome=result['result']
                if outcome.get('instance_id')!=record['instance_id']:
                    raise RuntimeError('server restarted: instance changed during unload')
                if outcome.get('state')!='unloaded' or outcome.get('worker_pid') is not None:
                    raise RuntimeError('external worker exit unconfirmed')
                return outcome.get('cleanup_forced',False)
            if result['status']=='failed':
                raise RuntimeError(result.get('error'))
            time.sleep(min(.05,left()))

    def close_worker(self, worker, timeout=35., *, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic()+timeout
        forced=False
        with self._lock:
            owned = [(rid,record) for rid,record in self._records.items() if record['worker'] is worker]
        errors=[]
        for rid,record in owned:
            try:
                forced |= self._release_remote(record,deadline,self._http)
                with self._lock:
                    for lease in record['leases'].values():
                        lease.release()
                    self._records.pop(rid,None)
                    self._persist()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            with self._lock:
                self._persist()
            raise RuntimeError(f'remote model/image cleanup unconfirmed; retained {self.recovery_path}: ' + '; '.join(errors))
        return forced

    @staticmethod
    def _reclaim_segments(descriptors, deadline=None):
        import mmap
        import re
        import posix_ipc
        from rc_policy_sdk.images import _HEADER, MAX_IMAGE_BYTES
        opened = []
        try:
            unique = {}
            for descriptor in descriptors:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('image recovery deadline exceeded')
                name = descriptor.get('shm_name')
                if (not isinstance(name, str) or not re.fullmatch(r'/rc[0-9a-f]{22}', name) or
                        descriptor.get('pool_id') != name[3:15] or
                        type(descriptor.get('nbytes')) is not int or not 0 < descriptor['nbytes'] <= MAX_IMAGE_BYTES or
                        type(descriptor.get('frame_id')) is not int or descriptor['frame_id'] <= 0):
                    raise RuntimeError('invalid recovered shared image identity')
                identity = (descriptor['nbytes'], descriptor['frame_id'])
                if name in unique and unique[name] != identity:
                    raise RuntimeError('conflicting recovered frame identities; rebuild IPC namespace')
                unique[name] = identity
            # Validate every segment before unlinking any of them. All local and
            # external readers have already been proved gone by the caller.
            for name, identity in unique.items():
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('image recovery deadline exceeded')
                try:
                    segment = posix_ipc.SharedMemory(name, read_only=True)
                except posix_ipc.ExistentialError:
                    continue
                opened.append(segment)
                if segment.size < _HEADER.size + identity[0]:
                    raise RuntimeError('recovered image size mismatch; rebuild IPC namespace')
                with mmap.mmap(segment.fd, _HEADER.size, access=mmap.ACCESS_READ) as mapping:
                    if _HEADER.unpack(mapping[:]) != identity:
                        raise RuntimeError('recovered frame mismatch; rebuild IPC namespace')
            for segment in opened:
                try: segment.unlink()
                except posix_ipc.ExistentialError: pass
        finally:
            for segment in opened:
                segment.close_fd()

    @classmethod
    def recover(cls, directory, deadline, deployment_id, *, anchor_id=None, anchor_epoch=None, defer_namespace_cleanup=False):
        """Revoke every old owner, then reclaim only proven frames in the same IPC instance."""
        from pathlib import Path
        journals = []
        for path in sorted(Path(directory).glob('*.json')):
            data=json.loads(path.read_text())
            if data.get('version')!=3 or data.get('deployment_id')!=deployment_id:
                raise RuntimeError('external recovery journal ownership mismatch')
            journals.append((path,data))
        forced=False
        # Keep descriptors until ALL registered external readers are gone, even
        # if a crash interrupts this recovery pass between owners.
        for path,data in journals:
            for record in data['remote_models']:
                if not record.get('exit_confirmed'):
                    record['cleanup_forced'] = cls._release_remote(record,deadline)
                    record['exit_confirmed'] = True
                    atomic_json(path,data)
                forced |= record.get('cleanup_forced',False)
        descriptors=[]
        for _,data in journals:
            items=[image for record in data['remote_models'] for frames in record.get('requests',{}).values()
                   for image in frames.values()]
            if items and (not data.get('anchor_id') or
                          not defer_namespace_cleanup and (data['anchor_id'] != anchor_id or data.get('anchor_epoch') != anchor_epoch)):
                raise RuntimeError('old anchor/IPC namespace is not bound; retain journal and recreate RC plus anchor')
            descriptors.extend(items)
        if defer_namespace_cleanup:
            return forced  # Host down removes journals only after exact anchor removal.
        cls._reclaim_segments(descriptors,deadline)
        for path,_ in journals:
            path.unlink()
        return forced

    @staticmethod
    def finish_namespace_cleanup(directory, deployment_id, removed_anchors):
        from pathlib import Path
        for path in Path(directory).glob('*.json'):
            data=json.loads(path.read_text())
            if (data.get('deployment_id') != deployment_id or
                    any(not record.get('exit_confirmed') for record in data['remote_models'])):
                raise RuntimeError('external namespace cleanup lacks owner exit proof')
            if any(record.get('requests') for record in data['remote_models']) and data.get('anchor_id') not in removed_anchors:
                raise RuntimeError('external namespace cleanup lacks anchor removal proof')
            path.unlink()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)
            if self._thread.is_alive():
                raise RuntimeError('external lease monitor did not exit')
