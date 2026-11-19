"""Build and verify a generated target binding; imports do not contact a host.

Package roots are discovered through DSM's package aliases, then checked
against bounded canonical layouts and the reviewed program catalogue. The
binding is public metadata. Its digest must be pinned by the generated entry.
"""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import sys
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID

from . import catalog, io

FORMAT = 'gift-ledger-dsm-event-target-v1'
PACKAGE_ALIAS = Path('/var/packages/ContainerManager')
API = Path('/usr/syno/bin/synowebapi')
SHA = re.compile('[0-9a-f]{64}')
PROJECT = re.compile('gift-ledger-[a-z0-9][a-z0-9-]{1,95}')


def require(ok, code):
    io.require(ok, code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def absolute(value):
    require(type(value) is str and 1 < len(value) < 512, 'target_path_invalid')
    p = PurePosixPath(value)
    require(p.is_absolute() and str(p) == value and '..' not in p.parts
            and not any(c in value for c in ('\n', '\r', '\x00', ',')), 'target_path_invalid')
    require(re.fullmatch(r'/volume[1-9][0-9]*/[^/]+(?:/[^/]+)*', value), 'target_storage_path_invalid')
    return value


def recognized_version(profile):
    hashes = profile.get('tool_hashes')
    require(type(hashes) is dict, 'registered_tool_manifest_required')
    core_hash = hashes.get('backup_ops/backup_ops.py')
    baseline = catalog.CORE_VERSIONS.get(core_hash)
    candidates = ([baseline] if baseline else []) + catalog.TOOLSET_VARIANTS.get(core_hash, []) + [
        row for row in catalog.RELEASE_VERSIONS if row['static_tools'].get('backup_ops/backup_ops.py') == core_hash]
    require(candidates, 'backup_version_not_supported')
    require(any(set(hashes) == set(row['static_tools']) | set(row['dynamic_tools']) for row in candidates),
            'registered_tool_set_not_supported')
    matches = [candidate for candidate in candidates
               if set(hashes) == set(candidate['static_tools']) | set(candidate['dynamic_tools'])
               and all(hashes[name] == expected for name, expected in candidate['static_tools'].items())]
    require(len(matches) == 1, 'registered_tool_version_not_supported')
    version = matches[0]
    require(all(type(hashes[name]) is str and SHA.fullmatch(hashes[name]) for name in version['dynamic_tools']), 'generated_tool_digest_invalid')
    return copy.deepcopy(version)


def make_binding(profile_path, profile_raw, *, entry, label, target_contract_raw=None):
    """Pure generation from an existing registration; never changes registration."""
    absolute(profile_path)
    require(type(profile_raw) is bytes and 0 < len(profile_raw) <= 65536, 'registered_profile_size_invalid')
    profile = io.parse(profile_raw)
    require(profile.get('format') == 'gift-ledger-backup-ops-profile-v1' and profile.get('version') == 1,
            'registered_profile_format_invalid')
    require(PROJECT.fullmatch(str(profile.get('project', ''))), 'target_project_invalid')
    require(re.fullmatch('[A-Za-z0-9:._-]{10,200}', str(profile.get('engine_id', ''))), 'target_engine_invalid')
    for key in ('instance_id', 'server_uuid'):
        try:
            require(str(UUID(profile[key])) == profile[key], 'target_instance_invalid')
        except (KeyError, TypeError, ValueError):
            raise ValueError('target_instance_invalid') from None
    version = recognized_version(profile)
    for key in ('installation_dir', 'toolkit_root', 'state_dir', 'store_dir', 'docker_bin'):
        absolute(profile.get(key))
    require(set(profile.get('containers', {})) == {'web', 'api', 'db'}, 'three_bound_services_required')
    identities = {}
    for role, row in profile['containers'].items():
        require(SHA.fullmatch(str(row.get('id', ''))) and row.get('name') == profile['project']+'-'+role+'-1',
                'target_service_identity_invalid')
        identities[role] = row['id']
    require(len(set(identities.values())) == 3, 'duplicate_service_identity')
    try:
        url = urlsplit(entry)
        require(url.scheme == 'https' and url.hostname and url.username is None and url.password is None
                and url.path in ('', '/') and not url.query and not url.fragment
                and (url.port is None or 1 <= url.port <= 65535), 'target_entry_invalid')
    except (TypeError, ValueError):
        raise ValueError('target_entry_invalid') from None
    if version['dynamic_tools']:
        require(type(target_contract_raw) is bytes and len(target_contract_raw) <= 65536,
                'portable_target_contract_required')
        require(digest(target_contract_raw) == profile['tool_hashes']['gui_v2/PORTABLE-DSM.json'],
                'portable_target_contract_changed')
        contract = io.parse(target_contract_raw)
        sites = [contract.get(role, {}) for role in ('source', 'restore')]
        selected = [row for row in sites if row.get('project') == profile['project']]
        require(len(selected) == 1, 'portable_target_site_ambiguous')
        site = selected[0]
        require(entry.rstrip('/') == 'https://{}:{}'.format(site.get('host'), site.get('port'))
                and site.get('installation') == profile['installation_dir'], 'portable_target_entry_mismatch')
    require(type(label) is str and 1 <= len(label) <= 60 and not any(ord(c) < 32 for c in label), 'target_label_invalid')
    return {'format': FORMAT, 'profile_path': profile_path, 'profile_sha256': digest(profile_raw),
            'core_version': version['id'], 'project': profile['project'], 'engine_id': profile['engine_id'],
            'instance_id': profile['instance_id'], 'server_uuid': profile['server_uuid'], 'containers': identities,
            'entry': entry.rstrip('/')+'/', 'label': label}


def safe_code(error):
    value = getattr(error, 'code', str(error))
    return value if isinstance(value, str) and re.fullmatch('[A-Za-z0-9_-]{1,100}', value) else type(error).__name__


def parse_ack(stdout, returncode):
    require(returncode == 0 and len(stdout) <= 65536, 'DSM_API_process_failed')
    offset = stdout.find('{')
    require(offset >= 0, 'DSM_API_reply_missing')
    try:
        value, used = io.json.JSONDecoder(object_pairs_hook=io.unique).raw_decode(stdout[offset:])
    except (TypeError, ValueError):
        raise ValueError('DSM_API_reply_invalid') from None
    require(type(value) is dict and not stdout[offset+used:].strip(), 'DSM_API_reply_ambiguous')
    if value.get('success') is not True:
        # Preserve only a bounded vendor number. Replies may contain paths or
        # credentials, so neither arbitrary text nor the full reply is exported.
        error = value.get('error')
        number = error.get('code') if type(error) is dict else None
        suffix = '_' + str(number) if type(number) is int and 0 <= number <= 2147483647 else ''
        raise ValueError('DSM_API_rejected' + suffix)
    return {'success': True}


def validate_registration(value, project, role, identity):
    """CM can retain an old ID after a Compose-only replacement."""
    require(role in ('web', 'api') and type(value) is dict, 'CM_registration_invalid')
    require(value.get('id') == identity, 'container_manager_identity_outdated')
    require(value.get('name') == project + '-' + role + '-1', 'CM_registration_name_changed')
    labels = value.get('labels')
    require(type(labels) is dict and labels.get('com.docker.compose.project') == project
            and labels.get('com.docker.compose.service') == role, 'CM_registration_labels_changed')
    require(type(value.get('enabled')) is bool, 'enabled_boolean_required')
    return {'identity_verified': True, 'enabled': value['enabled']}


class VendorFileMismatch(ValueError):
    """Export only catalogue member names and locally computed SHA256 values."""
    def __init__(self, code, observed, builds):
        super().__init__(code)
        self.files = [
            {'file': name, 'actual_sha256': observed[name],
             'expected_sha256_by_build': {build: pins[name] for build, pins in builds.items()}}
            for name in catalog.CONTAINER_MANAGER_BUILDS[catalog.BASE_CM_BUILD] if name in observed
        ]


def select_vendor_build(observed, builds):
    for build, pins in builds.items():
        if observed == pins:
            return build
    raise VendorFileMismatch('CM_vendor_file_set_unreviewed', observed, builds)


class Package:
    def __init__(self, target, variable, profiles, build=catalog.BASE_CM_BUILD):
        self.target, self.variable, self.profiles = target, variable, profiles
        self.consumer = target/'tool/docker_event_watcherd'
        self.build = build
        pins = catalog.CONTAINER_MANAGER_BUILDS[build]
        self.vendor_pins = MappingProxyType({target/name: value for name, value in pins.items()})

    @classmethod
    def discover(cls, *, backup_events=False):
        require(os.geteuid() == 0 and sys.version_info >= (3, 8), 'root_and_supported_python_required')
        require(platform.machine().lower() in ('x86_64', 'amd64'), 'DSM_architecture_not_supported')
        target, variable, profiles = (PACKAGE_ALIAS.joinpath(n).resolve(strict=True) for n in ('target', 'var', 'etc'))
        require(re.fullmatch(r'/volume[1-9][0-9]*/@appstore/ContainerManager', str(target)), 'package_target_location_not_supported')
        require(re.fullmatch(r'/volume[1-9][0-9]*/@appdata/ContainerManager', str(variable))
                or variable in (target/'var', PACKAGE_ALIAS/'var'), 'package_var_location_not_supported')
        require(re.fullmatch(r'/volume[1-9][0-9]*/@appconf/ContainerManager', str(profiles))
                or profiles in (target/'etc', PACKAGE_ALIAS/'etc'), 'package_profile_location_not_supported')
        for path in (target, variable, profiles):
            io.checked_directory(path)
        base_pins = catalog.CONTAINER_MANAGER_BUILDS[catalog.BASE_CM_BUILD]
        builds = catalog.CONTAINER_MANAGER_BUILDS if backup_events else {catalog.BASE_CM_BUILD: base_pins}
        observed = {}
        for name in base_pins:
            path = target/name
            require(all(not p.is_symlink() for p in (path, *path.parents)), 'pinned_path_link')
            limit = 65536 if name == 'webapi/SYNO.Docker.lib' else catalog.VENDOR_FILE_LIMIT
            raw = io.read_regular(path, limit)
            observed[name] = digest(raw)
            if name == 'webapi/SYNO.Docker.lib':
                contract_raw = raw
        package = cls(target, variable, profiles, select_vendor_build(observed, builds))
        document = io.parse(contract_raw)
        contract = document['SYNO.Docker.Container']
        require(contract['maxVersion'] == 1
                and {'start', 'stop'}.issubset({key for row in contract['methods']['1'] for key in row}),
                'installed_API_contract_changed')
        executable(API)
        executable(target/'usr/bin/docker')
        return package

    def identity(self):
        return (self.target, self.variable, self.profiles)


def executable(path):
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
            and os.access(path, os.X_OK), 'executable_owner_or_mode_invalid')
    return resolved


def pinned_file(path, expected, maximum):
    require(SHA.fullmatch(str(expected)), 'pinned_digest_invalid')
    require(all(not p.is_symlink() for p in (path, *path.parents)), 'pinned_path_link')
    raw = io.read_regular(path, maximum)
    require(digest(raw) == expected, 'pinned_file_changed')
    return raw


class Binding:
    API = API
    safe_code = staticmethod(safe_code)
    parse_ack = staticmethod(parse_ack)

    def __init__(self, config, *, backup_events=False):
        require(type(config) is dict and config.get('format') == FORMAT, 'event_target_format_invalid')
        self.config = copy.deepcopy(config)
        self.backup_events = backup_events
        self.PROFILE = Path(absolute(config['profile_path']))
        self.PROJECT, self.ENGINE, self.INSTANCE = (config[k] for k in ('project', 'engine_id', 'instance_id'))
        self.IDS = dict(config['containers'])
        self.package = None

    def checked_registration(self, role):
        require(role in ('web', 'api') and self.package is not None, 'CM_registration_scope_invalid')
        path = self.package.profiles / (self.PROJECT + '-' + role + '-1.config')
        io.checked_directory(path.parent)
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                    and not info.st_mode & 0o022 and 0 < info.st_size <= 65536,
                    'CM_registration_file_invalid')
            raw = stream.read(65537)
            after = os.fstat(stream.fileno())
            current = path.lstat()
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            require(len(raw) == info.st_size and all(getattr(info, k) == getattr(after, k)
                    == getattr(current, k) for k in fields), 'CM_registration_changed_during_read')
        value = io.parse(raw)
        return validate_registration(value, self.PROJECT, role, self.IDS[role])

    def validate_package(self):
        package = Package.discover(backup_events=self.backup_events)
        if self.package is not None:
            require(package.identity() == self.package.identity(), 'package_roots_changed')
            require(package.build == self.package.build, 'CM_vendor_build_changed_during_operation')
        raw = pinned_file(self.PROFILE, self.config['profile_sha256'], 65536)
        profile = io.parse(raw)
        self.version = recognized_version(profile)
        self.TOOLS = Path(profile['toolkit_root'])
        for name, expected in profile['tool_hashes'].items():
            pinned_file(self.TOOLS/name, expected, 2*1024**2)
        contract = (io.read_regular(self.TOOLS/'gui_v2/PORTABLE-DSM.json')
                    if self.version['dynamic_tools'] else None)
        regenerated = make_binding(str(self.PROFILE), raw, entry=self.config['entry'],
                                   label=self.config['label'], target_contract_raw=contract)
        require(regenerated == self.config, 'event_target_binding_changed')
        require(Path(profile['docker_bin']).resolve(strict=True) == executable(package.target/'usr/bin/docker'),
                'target_Docker_package_mismatch')
        self.package = package
        return profile

    def load_core(self):
        profile = self.validate_package()
        # A fresh isolated entry must not use a dependency imported from another
        # installation, even if its content happens to match.
        for name in ('backup_store', 'backup_export'):
            if name in sys.modules:
                require(Path(sys.modules[name].__file__).resolve() == (self.TOOLS/'backup_ops'/(name+'.py')).resolve(),
                        'foreign_core_dependency_already_loaded')
        path = self.TOOLS/'backup_ops/backup_ops.py'
        spec = importlib.util.spec_from_file_location('bound_backup_core', path)
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        for module, member in ((core.backup_store, 'backup_ops/backup_store.py'), (core.backup_export, 'backup_ops/backup_export.py')):
            require(Path(module.__file__).resolve() == (self.TOOLS/member).resolve(), 'core_import_path_changed')
            pinned_file(Path(module.__file__), profile['tool_hashes'][member], 2*1024**2)
        profile = core.validate_profile(profile)
        return core, profile, core.Docker(profile['docker_bin'])

    def checked_state(self, driver, role):
        require(role in self.IDS, 'service_role_invalid')
        value = driver.inspect(self.IDS[role])
        require(value['id'] == self.IDS[role] and value['name'] == '/'+self.PROJECT+'-'+role+'-1'
                and value['project'] == self.PROJECT, 'container_identity_changed')
        return {key: value.get(key) for key in ('id', 'name', 'project', 'running', 'status', 'health', 'oom', 'exit_code')}
