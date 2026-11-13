"""Publication accounting owned exclusively by the RC control thread."""
import copy


class ConnectorControl:
    def _register_publication(self, identity, targets, attempt):
        if hasattr(self.robot, 'bind_attempt'): self.robot.bind_attempt(identity, attempt)
        self._last_submission = identity
        self._publications[identity] = {'targets': copy.deepcopy(targets), 'attempt': attempt,
                                      'generation': self.buffer.generation}

    def _collect_publications(self):
        if not getattr(self.robot, 'asynchronous', False): return False
        failure = None
        target_published = False
        for response in self.robot.poll_results():
            pending = self._publications.pop(response['id'], None)
            if pending is None: continue
            self._recorder.event('control', {'event': 'control.receipt', 'attempt_id': pending['attempt'],
                'connector_request_id': response['id'], 'success': response['success'],
                'arms': response.get('arms'), 'error': response.get('error')}, priority=True)
            if not response['success']:
                for arm in pending['targets']: self._published_targets.pop(arm, None)
                self._publish_fault = True
                # Freeze this generation before processing later successes in
                # the same receipt batch. Already submitted commands still run.
                self.buffer.invalidate()
                failure = response.get('error') or 'partial connector publication'
                continue
            self._published_targets.update(copy.deepcopy(pending['targets']))
            if pending['generation'] != self.buffer.generation: continue
            token = pending.get('buffer_token')
            if token is not None and self.buffer.confirm(token):
                target_published = True
            motion = pending.get('motion')
            if motion is not None and self._motion is motion:
                self._complete_motion(motion)
        if failure: raise RuntimeError(failure)
        return target_published

    def _complete_motion(self, motion):
        self._motion = None
        self._hold_name = motion['source']
        self._state = 'paused'
        if motion.get('complete') is not None: motion['complete'].set()
        self._emit(f"到达 {motion['source']} 指令目标；等待 resume")
        self.operations.update(motion.get('public_id'), 'succeeded', result=self.status())
