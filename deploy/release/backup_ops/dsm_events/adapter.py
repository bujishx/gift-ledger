"""Intercept exact bound Web/API calls; retain core locking and recovery proof."""
import copy
import re


class PlannedStopError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(ok, code):
    if not ok:
        raise PlannedStopError(code)


class BackupStopAdapter:
    """Intercept only the core's exact Web/API stop/start calls.

    provider.prepare(binding) must establish a fresh verified event channel.
    provider.stop(role, binding) performs one DSM stop and returns only after
    the exact die and later same-consumer event barrier has been validated.
    provider.start(role, binding) performs one DSM start (no retry).
    Provider cleanup must NEVER restart applications. Core recovery alone may
    request start, after a matching worker safe_to_restore receipt.
    """
    def __init__(self, raw_driver, profile, provider, save):
        self.raw = raw_driver
        self.profile = copy.deepcopy(profile)
        self.provider = provider
        self.save = save
        self.binding = None
        self.resume_authorized = False
        self.prepared = False
        self.record = {'format': 'gift-ledger-s1-backup-stop-adapter-v1',
                       'actions': [], 'channel_prepared': False,
                       'worker_safe_to_restore_verified': False,
                       'automatic_rescue_requested': False}

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def bind_worker(self, profile, journal, recovering=False):
        require(self.binding is None, 'adapter_worker_already_bound')
        for key in ('project', 'engine_id', 'instance_id', 'server_uuid', 'schema_revision', 'containers'):
            require(profile[key] == self.profile[key], 'adapter_profile_changed')
        require(re.fullmatch('[0-9a-f]{32}', journal['session_id'])
                and journal['project'] == profile['project'] and journal['instance_id'] == profile['instance_id'],
                'adapter_session_binding_invalid')
        for role in ('web', 'api', 'db'):
            original = journal['original'][role]
            expected = profile['containers'][role]
            require(original['id'] == expected['id'] and original['image'] == expected['image']
                    and original['name'].lstrip('/') == expected['name']
                    and type(original['running']) is bool, 'adapter_original_state_changed')
        self.binding = {'project': profile['project'], 'engine_id': profile['engine_id'],
                        'instance_id': profile['instance_id'], 'session_id': journal['session_id'],
                        'recovering': recovering,
                        'containers': copy.deepcopy(profile['containers']),
                        'original': copy.deepcopy(journal['original'])}
        self.record['binding'] = copy.deepcopy(self.binding)
        self._save()

    def _save(self):
        self.save(copy.deepcopy(self.record))

    def inspect(self, name):
        return self.raw.inspect(name)

    def _state(self, role):
        require(self.binding is not None, 'adapter_worker_not_bound')
        expected = self.binding['containers'][role]
        row = self.raw.inspect(expected['id'])
        require(row['id'] == expected['id'] and row['image'] == expected['image']
                and row['name'].lstrip('/') == expected['name']
                and row['project'] == self.binding['project'], 'adapter_service_identity_changed')
        return row

    def worker_verified(self, core, profile, reply):
        core.check_worker_status(profile, reply['status'])
        require(reply.get('safe_to_restore') is True, 'adapter_worker_cannot_prove_resume_safe')
        require(self.binding is not None, 'adapter_worker_not_bound')
        self.record['worker_safe_to_restore_verified'] = True
        self._save()
        self.resume_authorized = True

    def before_worker_run(self):
        if self.prepared:
            result = self.provider.retire()
            require(result.get('retired') is True, 'adapter_event_cleanup_requires_observation')
            self.record['event_resources_retired_before_backup'] = True
            self._save()

    def prepare_channel(self):
        if not self.prepared:
            proof = self.provider.prepare(copy.deepcopy(self.binding))
            require(type(proof) is dict and proof.get('initial_channel_verified') is True,
                    'adapter_event_channel_unverified')
            self.record['channel_prepared'] = True
            self._save()
            self.prepared = True

    def call(self, *args, **kwargs):
        if not args or args[0] not in ('stop', 'start'):
            return self.raw.call(*args, **kwargs)
        service_tokens = {token: role for role, row in self.profile['containers'].items()
                          for token in (row['id'], row['name'], '/' + row['name'])}
        targeted = {service_tokens[token] for token in args[1:] if token in service_tokens}
        if not targeted:
            # Worker lifecycle remains on the frozen core's already-bound path.
            return self.raw.call(*args, **kwargs)
        require(len(targeted) == 1, 'adapter_multiple_services_forbidden')
        role = next(iter(targeted))
        require(role in ('web', 'api'), 'adapter_database_lifecycle_forbidden')
        action = args[0]
        expected = self.profile['containers'][role]
        exact = ('stop','--time','30',expected['id']) if action == 'stop' else ('start',expected['id'])
        require(args == exact and not kwargs, 'adapter_lifecycle_shape_not_reviewed')
        row = self._state(role)
        require(self.binding['original'][role]['running'], 'adapter_originally_stopped_service')
        require(row['running'] is (action == 'stop'), 'adapter_state_changed_do_not_repeat')
        require(not any(x['role'] == role and x['action'] == action for x in self.record['actions']),
                'adapter_action_already_attempted')
        if action == 'stop':
            self.prepare_channel()
        else:
            require(self.resume_authorized, 'adapter_worker_proof_required_before_start')
        action_record = {'role': role, 'action': action, 'id': expected['id'], 'name': expected['name'],
                         'intent_saved': True, 'acknowledged': False}
        self.record['actions'].append(action_record)
        self._save()  # Persist intent before the provider call, never retry on timeout.
        try:
            proof = getattr(self.provider, action)(role, copy.deepcopy(expected))
            require(type(proof) is dict and proof.get('success') is True, 'adapter_DSM_action_not_acknowledged')
            if action == 'stop':
                require(proof.get('event_barrier_verified') is True, 'adapter_stop_event_barrier_unverified')
            after = self._state(role)
            require(after['running'] is (action == 'start'), 'adapter_action_state_unverified')
            action_record['acknowledged'] = True
            if action == 'stop': action_record['event_barrier_verified'] = True
            self._save()
        except BaseException as error:
            code = getattr(error, 'code', str(error))
            action_record['error_code'] = code if isinstance(code, str) and re.fullmatch(
                '[A-Za-z0-9_-]{1,100}', code) else 'adapter_action_outcome_requires_observation'
            self._save()
            raise
        return expected['id']


class WorkerProofProxy:
    def __init__(self, worker, adapter, core, profile):
        self.worker, self.adapter, self.core, self.profile = worker, adapter, core, copy.deepcopy(profile)

    def __getattr__(self, name):
        return getattr(self.worker, name)

    def receive(self, expected, *args, **kwargs):
        reply = self.worker.receive(expected, *args, **kwargs)
        if expected == 'verified':
            self.adapter.worker_verified(self.core, self.profile, reply)
        return reply

    def send(self, command):
        if command == 'run':
            self.adapter.before_worker_run()
        return self.worker.send(command)


def wrap_worker_factory(core, adapter, factory):
    def wrapped(profile, driver, journal, run_dir, recover=False):
        require(driver is adapter, 'adapter_driver_mismatch')
        adapter.bind_worker(profile, journal, recovering=recover)
        if any(adapter._state(role)['running'] for role in ('web','api')):
            # Under the core lock, before it creates a worker or acquires a DB
            # maintenance lease. A failed channel leaves applications untouched.
            adapter.prepare_channel()
        worker = factory(profile, driver, journal, run_dir, recover=recover)
        return WorkerProofProxy(worker, adapter, core, profile)
    return wrapped
