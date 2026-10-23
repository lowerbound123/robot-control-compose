"""RC-owned model registrations and remote frame pins survive policy process death."""
import json
import threading
import time
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .config import atomic_json


class ExternalResources:
    def __init__(self, pool, recovery_path, *, start=False):
        self.pool = pool
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
            atomic_json(self.recovery_path, {'remote_models':records})
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

    def begin_close(self,worker):
        with self._lock:
            self._closing_workers.add(worker)
            records=[r for r in self._records.values() if r['worker'] is worker]
        for record in records:
            try:
                self._http(record,'/v1/models/unload',{'owner_id':record['owner_id'],
                                                     'expected_instance_id':record['instance_id']})
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

    def close_worker(self, worker, timeout=35.):
        forced=False
        with self._lock:
            owned = [(rid,record) for rid,record in self._records.items() if record['worker'] is worker]
        for rid, record in owned:
            deadline=time.monotonic()+timeout
            try:
                # Even an unloaded server revokes this owner, so a delayed load
                # packet from a dead policy cannot initialize another model later.
                status=self._http(record,'/v1/status',timeout=min(2.,timeout))
                if record['instance_id'] is not None and status.get('instance_id') != record['instance_id']:
                    raise RuntimeError('server restarted; old remote readers cannot be confirmed')
                op=self._http(record,'/v1/models/unload',{'owner_id':record['owner_id'],
                                                        'expected_instance_id':record['instance_id']},timeout=min(2.,timeout))
                while True:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:
                        raise TimeoutError('remote unload deadline exceeded')
                    result=self._http(record,'/v1/operations/'+op['operation_id'],timeout=min(2.,remaining))
                    if result['status']=='succeeded':
                        outcome=result['result']
                        if record['instance_id'] is not None and outcome.get('instance_id') != record['instance_id']:
                            raise RuntimeError('server restarted during unload; old remote readers cannot be confirmed')
                        if outcome['state']!='unloaded' or outcome.get('worker_pid') is not None:
                            raise RuntimeError('remote worker exit was not confirmed')
                        forced |= outcome.get('cleanup_forced',False)
                        break
                    if result['status']=='failed':
                        raise RuntimeError(result.get('error'))
                    time.sleep(min(.05,remaining))
                with self._lock:
                    for lease in record['leases'].values():
                        lease.release()
                    self._records.pop(rid,None)
                    self._persist()
            except Exception as exc:
                with self._lock:
                    self._persist()
                raise RuntimeError(f'remote model/image cleanup unconfirmed; retained {self.recovery_path}: {exc}') from exc
        return forced

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)
            if self._thread.is_alive():
                raise RuntimeError('external lease monitor did not exit')
