"""Per-action decisions, sparse HTTP inference, canonical async-add fusion."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import numpy as np
from model_client.actions import ActionProcessor
from model_client.client import ModelClient
from rc_policy_sdk.images import map_images
from rc_policy_sdk.transitions import TrajectoryAddTransition


class ModelSession:
    def __init__(self, config, runtime):
        self.params = copy.deepcopy(config['params'])
        self.runtime = runtime
        self.owner_id = 'rc-' + uuid4().hex
        self.remote_id = self.owner_id
        self._local = threading.local()
        self._lock = threading.Lock()
        self._clients = []
        self._closed = False
        self._registered = False
        self.generation = None
        self.instance_id = None
        self.observe_enabled = False
        self.subtask_enabled = False
        client = self.client()
        profiles = client.models()['models']
        profile = next((p for p in profiles if p['id']==self.params['model_id']),None)
        if profile is None or 'error' in profile:
            raise ValueError(f"model configuration unavailable: {self.params['model_id']}")
        self.observe_enabled = profile['backend']=='qwena1' or bool(self.params.get('continuous_observations',False))
        subtask = self.params['subtask']
        if subtask['enabled'] and profile['backend'] not in ('qwen3_5_vla_ki_wan','mock'):
            raise ValueError('subtask is only supported by Qwen3.5 or mock')
        self.subtask_enabled = subtask['enabled']
        status = client.status()
        if status['state']!='unloaded' or status.get('owner_id') is not None:
            raise RuntimeError('model-server instance must be unloaded and idle before policy activation')
        self.instance_id = status['instance_id']
        client.expected_instance_id = self.instance_id
        # Registration is acknowledged before load, including partial initialize.
        runtime.register_remote(self.remote_id,self.params['endpoint'],self.owner_id,self.params['model_id'],self.instance_id)
        self._registered=True
        overrides=copy.deepcopy(self.params['load_overrides'])
        if profile['backend']=='qwen3_5_vla_ki_wan':
            overrides.setdefault('model',{}).update(
                workflow=subtask['workflow'] if subtask['enabled'] else 'single',
                subtask_prefetch=subtask['prefetch'] if subtask['enabled'] else False)
        operation=client.load(self.params['model_id'],overrides,wait=False)
        self.finish_load(operation['operation_id'])

    def finish_load(self, operation):
        result=self.client().wait_operation(operation,timeout=self.params['timeouts']['load_s'])
        if result['result']['instance_id'] != self.instance_id:
            raise RuntimeError('server instance changed during model loading')
        self.generation=result['result']['generation']
        self.runtime.update_remote(self.remote_id,self.generation)

    def client(self):
        client=getattr(self._local,'client',None)
        if client is None:
            client=ModelClient(self.params['endpoint'],timeout=self.params['timeouts']['request_s'],
                               connect_timeout=self.params['timeouts']['connect_s'],owner_id=self.owner_id,
                               expected_instance_id=self.instance_id)
            self._local.client=client
            with self._lock:
                self._clients.append(client)
        return client

    def prepare(self,method,descriptors,runtime):
        request_id=self.owner_id+':'+method+':'+uuid4().hex
        # ACK happens inside the borrowing callback, BEFORE it can return.
        runtime.retain_remote(self.remote_id,request_id,descriptors)
        return request_id

    def request(self, method,state,descriptors,runtime,params,prepared=None):
        request_id=prepared or self.prepare(method,descriptors,runtime)
        client=self.client()
        kwargs=dict(state=state,instruction=params['instruction'],observation_id=runtime.observation_id,
                    request_id=request_id,generation=self.generation)
        if method=='infer':
            kwargs['include_canonical']=True
        try:
            if params['image_transport']=='shm':
                result=getattr(client,method)(images=descriptors,**kwargs)
            else:
                with map_images(descriptors) as images:
                    result=getattr(client,method)(images=images,**kwargs)
            if result.get('images_released') is True:
                runtime.release_images(self.remote_id,request_id)
            if result['generation'] != self.generation:
                raise RuntimeError('model generation changed externally')
            if method=='infer':
                print(f"model infer {request_id}: consumed={runtime.consumed_actions} steps={len(result['action_chunk'])}",flush=True)
            return result
        except Exception:
            lease=client.leases.get(request_id)
            if lease and lease.images_released:
                runtime.release_images(self.remote_id,request_id)
            raise
        finally:
            # Request IDs are unique; the RC keeps unresolved leases separately.
            client.leases.pop(request_id,None)

    def reset(self):
        status=self.client().status()
        if (status['generation']!=self.generation or status['instance_id']!=self.instance_id
                or status.get('owner_id')!=self.owner_id):
            raise RuntimeError('model instance/generation changed externally; resume refused')
        operation=self.client().reset(wait=False)
        result=self.client().wait_operation(operation['operation_id'],timeout=self.params['timeouts']['request_s'])
        if result['result']['instance_id'] != self.instance_id:
            raise RuntimeError('server instance changed during context reset')
        self.generation=result['result']['generation']
        self.runtime.update_remote(self.remote_id,self.generation)

    def close(self):
        if self._closed:
            return
        self._closed=True
        try:
            if self._registered:
                operation=self.client().unload(wait=False)
                result=self.client().wait_operation(operation['operation_id'],timeout=self.params['timeouts']['release_s'])
                if result['result']['instance_id'] != self.instance_id:
                    raise RuntimeError('server instance changed during model unload; cleanup unconfirmed')
                if result['result'].get('cleanup_forced',False):
                    raise RuntimeError('remote model required forced termination')
        finally:
            with self._lock:
                for client in self._clients:
                    client.close()


class Bridge:
    def __init__(self, config, runtime, *, session=None):
        self._executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='bridge-infer')
        self._refresh_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='bridge-refresh')
        self.pending=None
        self.refresh_pending=None
        self._request=None
        self.canonical=np.empty((0,14),np.float32)
        self._consumed=None
        self._epoch=None
        self.last_trim=0
        self.last_refresh_consumed=None
        self._deferred_refresh=False
        self._closed=False
        if session is not None:
            self.session=session
        else:
            self.session=ModelSession.__new__(ModelSession)
            ModelSession.__init__(self.session,config,runtime)

    def _await_refresh(self,params):
        if not params['subtask'].get('prefetch',True) and self.refresh_pending is not None:
            self.refresh_pending.result()

    def _launch(self,state,images,runtime,params):
        self._await_refresh(params)
        snapshot=copy.deepcopy(params)
        self._request=(runtime.consumed_actions,runtime.control_epoch,snapshot)
        prepared=self.session.prepare('infer',images,runtime)
        self.pending=self._executor.submit(self.session.request,'infer',state,images,runtime,snapshot,prepared)

    def _install(self,result,old,params):
        new=np.asarray(result['canonical_action_chunk'],dtype=np.float32)
        if new.ndim!=2 or new.shape[1]!=14 or len(new)==0 or not np.isfinite(new).all():
            raise ValueError('invalid canonical action chunk from model-server')
        if params['mode']=='async-add' and len(old):
            new=TrajectoryAddTransition(**params['fusion']).apply(old,new)
        self.canonical=new
        processor=ActionProcessor({'processor':result['action_processor']})
        return np.stack([processor.process(row,subtask=result.get('subtask','')) for row in new]).astype(np.float32)

    def _refresh(self,state,images,runtime,params,*,installed=False):
        if self.refresh_pending and self.refresh_pending.done():
            result=self.refresh_pending.result()
            self.refresh_pending=None
            if not result.get('accepted'):
                self._deferred_refresh=True
        subtask=params['subtask']
        if not subtask['enabled']:
            return
        due=runtime.consumed_actions-self.last_refresh_consumed>=subtask['refresh_every_actions']
        if not (due or self._deferred_refresh) or self.refresh_pending is not None:
            return
        lead=len(self.canonical)
        if params['mode']=='async-add':
            lead=max(0,lead-params['request_when_remaining'])
        if not installed and (self.pending is not None or lead<subtask['minimum_lead_actions']):
            self._deferred_refresh=True
            return
        prepared=self.session.prepare('refresh',images,runtime)
        self.refresh_pending=self._refresh_executor.submit(self.session.request,'refresh',state,images,runtime,copy.deepcopy(params),prepared)
        self._deferred_refresh=False
        self.last_refresh_consumed=runtime.consumed_actions

    def step(self,state,action_chunk,images,config,runtime):
        if self._closed:
            raise RuntimeError('bridge is closing')
        params=config['params']
        if self._epoch is None:
            self._epoch=runtime.control_epoch
            self._consumed=runtime.consumed_actions
            self.last_refresh_consumed=runtime.consumed_actions
        if runtime.control_epoch!=self._epoch:
            raise RuntimeError('control epoch changed without resetting policy context')
        delta=runtime.consumed_actions-self._consumed
        if delta<0:
            raise ValueError('consumed action counter moved backwards')
        self.canonical=self.canonical[delta:]
        self._consumed=runtime.consumed_actions
        old=np.asarray(action_chunk,dtype=np.float32).reshape((-1,14))
        if len(old)!=len(self.canonical):
            raise RuntimeError('canonical mirror does not match RC remaining actions')
        installed=False
        if self.pending is not None and (self.pending.done() or len(old)==0):
            result=self.pending.result()
            count,epoch,snapshot=self._request
            self.pending=None
            if epoch!=runtime.control_epoch:
                raise RuntimeError('stale model request')
            self.last_trim=runtime.consumed_actions-count
            result=dict(result)
            result['canonical_action_chunk']=result['canonical_action_chunk'][self.last_trim:]
            if len(result['canonical_action_chunk']):
                old=self._install(result,self.canonical,snapshot)
                installed=True
        if len(old)==0:
            # Initial request, starvation, or an entirely trimmed result. This
            # is a real inference, never an artificial placeholder action.
            self._await_refresh(params)
            result=self.session.request('infer',state,images,runtime,copy.deepcopy(params))
            self.last_trim=0
            old=self._install(result,self.canonical,params)
            installed=True
        self._refresh(state,images,runtime,params,installed=installed)
        if (params['mode']=='async-add' and self.pending is None and not installed
                and len(old)<=params['request_when_remaining']):
            self._launch(state,images,runtime,params)
        return old

    def observe(self,state,images,config,runtime):
        if self.session.observe_enabled and not self._closed:
            self.session.request('observe',state,images,runtime,copy.deepcopy(config['params']))

    def _drain(self):
        for future in (self.pending,self.refresh_pending):
            if future:
                try:
                    future.result()
                except Exception:
                    pass  # Results from the old control epoch are discarded.
        self.pending=None
        self.refresh_pending=None

    def reset(self):
        self._drain()
        self.session.reset()
        self.canonical=np.empty((0,14),np.float32)
        self._epoch=None
        self._consumed=None
        self._deferred_refresh=False
        self.last_refresh_consumed=None

    def close(self):
        if self._closed:
            return
        self._closed=True
        self._drain()
        self._executor.shutdown(wait=True)
        self._refresh_executor.shutdown(wait=True)
        self.session.close()
