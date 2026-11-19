"""Bounded receipt and file operations, with explicit package context."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

LABEL = 'org.gift-ledger.planned-backup'
IMAGE_FORMAT = '{"id":{{json .Id}},"volumes":{{json (index .Config "Volumes")}}}'
GOOD_FORMAT = ('{"id":{{json .ID}},"compose":{{json (.Label "com.docker.compose.project")}},'
               '"project":{{json (.Label "org.gift-ledger.gui-v2-project")}}}')
MARKER_FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
    '"state":{{json .State}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}},'
    '"network":{{json .HostConfig.NetworkMode}},"read_only":{{json .HostConfig.ReadonlyRootfs}},'
    '"privileged":{{json .HostConfig.Privileged}},"restart":{{json .HostConfig.RestartPolicy.Name}}}')

def require(ok, code):
    if not ok:
        raise ValueError(code)


def unique(items):
    out = {}
    for key, value in items:
        require(key not in out, 'duplicate_json_field')
        out[key] = value
    return out


def parse(raw):
    return json.loads(raw, object_pairs_hook=unique)


def read_regular(path, maximum=65536):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= maximum, 'public_file_invalid')
        value = stream.read(maximum + 1)
        require(len(value) <= maximum, 'public_file_limit')
        after = os.fstat(stream.fileno())
        require((info.st_size, info.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
                'public_file_changed')
        return value


def checked_directory(path):
    require(path.is_absolute() and path != Path('/'), 'directory_scope_invalid')
    require(all(not p.is_symlink() for p in (path, *path.parents)), 'linked_directory')
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            'directory_owner_or_mode')
    return (info.st_dev, info.st_ino)


def save_json(path, value):
    checked_directory(path.parent)
    pending = path.with_name('.' + path.name + '.tmp')
    fd = os.open(str(pending), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'wb') as stream:
        stream.write((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        stream.flush(); os.fsync(stream.fileno())
    require(not path.is_symlink(), 'receipt_link')
    os.replace(str(pending), str(path))


def open_directory(path):
    """Open each component without following links, including public parents.

    A DSM shared directory is transport, not an authentication boundary. Holding
    dirfds also prevents a renamed public directory redirecting our writes.
    """
    require(path.is_absolute() and '..' not in path.parts, 'directory_scope_invalid')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd); fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_export(directory_fd, name, raw):
    require('/' not in name and name not in ('', '.', '..'), 'export_name_invalid')
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o644, dir_fd=directory_fd)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    # No existing public file is ever overwritten or treated as authoritative.
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_size == len(raw) and stream.read(len(raw) + 1) == raw,
                'export_readback_changed')


class ReportStorage:
    """Private live evidence, then one-way public export after the trial.

    The small root-owned /run record remains as a no-replay/audit record. It has
    no secrets and no executable service. Public files never release a barrier.
    """
    def __init__(self, public, private):
        self.public, self.private = public, private
        self.fd = None

    def prepare(self):
        require(os.geteuid() == 0, 'task_must_run_as_root')
        checked_directory(self.private.parent)
        require(not self.private.exists() and not self.private.is_symlink(),
                'private_record_exists_do_not_repeat')
        self.private.mkdir(mode=0o700)
        self.private_identity = checked_directory(self.private)
        info = self.private.stat()
        require(stat.S_IMODE(info.st_mode) == 0o700, 'private_record_mode_invalid')
        parent = open_directory(self.public.parent)
        try:
            os.mkdir(self.public.name, mode=0o755, dir_fd=parent)
            self.fd = os.open(self.public.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=parent)
            info = os.fstat(self.fd)
            self.public_identity = (info.st_dev, info.st_ino)
        finally:
            os.close(parent)

    def check_public_identity(self):
        fd = open_directory(self.public)
        try:
            info = os.fstat(fd)
            require((info.st_dev, info.st_ino) == self.public_identity, 'public_report_directory_changed')
        finally:
            os.close(fd)

    def publish(self):
        require(checked_directory(self.private) == self.private_identity, 'private_record_changed')
        self.check_public_identity()
        files = {}
        for name in ('执行回执.json', '结果.txt'):
            raw = read_regular(self.private / name, 262144)
            write_export(self.fd, name, raw)
            files[name] = hashlib.sha256(raw).hexdigest()
        receipts = self.private / 'event-receipts'
        if receipts.exists() or receipts.is_symlink():
            checked_directory(receipts)
            os.mkdir('event-receipts', mode=0o755, dir_fd=self.fd)
            fd = os.open('event-receipts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
            try:
                for kind in ('initial', 'die', 'after'):
                    path = receipts / (kind + '.json')
                    if not path.exists() and not path.is_symlink():
                        continue
                    raw = read_regular(path, 8192)
                    write_export(fd, path.name, raw)
                    files['event-receipts/' + path.name] = hashlib.sha256(raw).hexdigest()
            finally:
                os.close(fd)
        manifest = {'format': 'gift-ledger-s1-event-ack-export-v1',
            'private_authority': str(self.private), 'public_files_used_for_lifecycle': False,
            'private_audit_record_retained': True, 'files': files}
        raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        write_export(self.fd, '报告清单.json', raw)
        self.check_public_identity()

    def close(self):
        if self.fd is not None:
            os.close(self.fd); self.fd = None


def inventory(driver):
    value = {}
    for kind, args in (('containers', ('ps', '-aq', '--no-trunc')),
                       ('volumes', ('volume', 'ls', '-q')),
                       ('networks', ('network', 'ls', '-q', '--no-trunc'))):
        raw = driver.call(*args, timeout=15)
        require(len(raw) < 131072, 'resource_inventory_limit')
        rows = raw.splitlines()
        require(len(rows) <= 500 and len(rows) == len(set(rows)), 'resource_inventory_invalid')
        value[kind] = sorted(rows)
    return value


def check_other_operations(driver, trial):
    raw = driver.call('ps', '--no-trunc', '--format', GOOD_FORMAT, timeout=15)
    require(len(raw) < 65536, 'container_list_limit')
    rows = [parse(line) for line in raw.splitlines() if line]
    require(len(rows) <= 100, 'container_list_limit')
    for row in rows:
        related = str(row.get('compose', '')).startswith(trial.PROJECT) or row.get('project') == trial.PROJECT
        require(not related or row.get('id') in trial.IDS.values(), 'another_target_operation_running')


def validate_receipt(value, kind, config, identity, intent_ns, intent_mono, prior_mono=0):
    require(isinstance(value, dict) and value.get('format') == 'gift-ledger-s1-consumer-event-ack-v1',
            'event_receipt_invalid')
    name = config['web_name'] if kind == 'die' else config['marker_names'][kind]
    require(value.get('kind') == kind and value.get('actor_id') == identity
            and value.get('actor_name') == name and value.get('nonce') == config['nonce'],
            'event_receipt_identity_changed')
    require(value.get('epoch') == config['epoch'] and value.get('producer_verified') is True,
            'event_receipt_producer_not_verified')
    for key in ('event_time_ns', 'observed_time_ns', 'observed_monotonic_ns'):
        require(type(value.get(key)) is int, 'event_receipt_time_invalid')
    require(intent_ns <= value['event_time_ns'] <= config['deadline_ns']
            and intent_ns <= value['observed_time_ns'] <= config['deadline_ns']
            and value['event_time_ns'] <= value['observed_time_ns'] + 5_000_000_000
            and value['observed_monotonic_ns'] >= intent_mono
            and value['observed_monotonic_ns'] > prior_mono, 'event_receipt_stale_or_out_of_order')
    if kind == 'die':
        require(value.get('enabled_at_hook') is False, 'planned_stop_flag_not_observed')
    return value


def snapshot_file(fd, name, helper):
    """Bounded, no-follow read with a stable identity; never exports raw values."""
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file_fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0
                and not before.st_mode & 0o022 and before.st_nlink == 1
                and before.st_size <= 65536, 'profile_owner_type_or_size_changed')
        raw = stream.read(65537)
        after = os.fstat(stream.fileno())
        signature = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
                               st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        require(len(raw) == before.st_size and signature(before) == signature(after), 'profile_changed_during_read')
        value = helper.parse(raw)
        require(type(value) is dict, 'profile_json_object_required')
        # Values may be vendor-specific. Ownership follows the pinned private
        # trial (which required this exact nonce name absent before creation),
        # root-only write permissions, and that trial's creation time window.
        return {'device': before.st_dev, 'inode': before.st_ino, 'uid': before.st_uid,
                'gid': before.st_gid, 'mode': oct(stat.S_IMODE(before.st_mode)),
                'links': before.st_nlink, 'bytes': len(raw), 'mtime_ns': before.st_mtime_ns,
                'ctime_ns': before.st_ctime_ns, 'sha256': hashlib.sha256(raw).hexdigest()}

class Environment:
    """Per-invocation verified package context; no shared mutable host globals."""
    LABEL, IMAGE_FORMAT, GOOD_FORMAT, MARKER_FORMAT = LABEL, IMAGE_FORMAT, GOOD_FORMAT, MARKER_FORMAT
    require = staticmethod(require)
    parse = staticmethod(parse)
    read_regular = staticmethod(read_regular)
    checked_directory = staticmethod(checked_directory)
    save_json = staticmethod(save_json)
    open_directory = staticmethod(open_directory)
    inventory = staticmethod(inventory)
    check_other_operations = staticmethod(check_other_operations)
    validate_receipt = staticmethod(validate_receipt)

    def __init__(self, package):
        self.CONSUMER = package.consumer
        self.VENDOR_PINS = dict(package.vendor_pins)
        self.VAR_ROOT = package.variable
        self.ALLOWED_VAR_ROOTS = (package.variable,)
        self.PROFILE_ROOT = package.profiles

    def active_epoch(self, hook, expected=None):
        candidates = []
        folders = [Path('/proc') / str(expected['pid'])] if expected else list(Path('/proc').iterdir())
        require(len(folders) <= 8192, 'process_inventory_limit')
        for folder in folders:
            if not folder.name.isdigit():
                continue
            try:
                executable = (folder / 'exe').resolve(strict=True)
                if executable != self.CONSUMER:
                    continue
                info = hook.process_stat((folder / 'stat').read_text('ascii'))
                require(folder.stat().st_uid == 0, 'consumer_owner_changed')
                candidates.append({'pid': info['pid'], 'start_ticks': info['start_ticks'],
                                   'executable': str(executable)})
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        require(len(candidates) == 1, 'single_consumer_required')
        require(expected is None or candidates[0] == expected, 'consumer_epoch_changed')
        return candidates[0]

    def verify_marker(self, value, name, image, nonce, identity=None):
        require(isinstance(value, dict), 'marker_inspect_invalid')
        require(re.fullmatch('[0-9a-f]{64}', str(value.get('id', ''))), 'marker_id_invalid')
        require(identity is None or value['id'] == identity, 'marker_id_changed')
        state = value.get('state', {})
        require(value.get('name') == '/' + name and value.get('image') == image
                and value.get('labels', {}).get(self.LABEL) == nonce, 'marker_identity_changed')
        require(state.get('Status') == 'created' and state.get('Running') is False
                and state.get('StartedAt') == '0001-01-01T00:00:00Z'
                and value.get('mounts') == [] and value.get('network') == 'none'
                and value.get('read_only') is True and value.get('privileged') is False
                and value.get('restart') == 'no', 'marker_was_started_or_configuration_changed')
        return value['id']
