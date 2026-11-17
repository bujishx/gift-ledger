"""Pure first-release Windows MAX_PATH budgeting for supported host workflows.

No filesystem probes, path permissions, registry changes or Docker path changes.
Callers retain their existing path/ownership checks and invoke this on Windows
before creating directories, starting workers or stopping services. S010/S011
are the supported schemas; adding longer generated names requires this contract
to be updated. Linux container/volume paths are deliberately outside this scope.
"""

import re


FILE_LIMIT = 259
DIRECTORY_LIMIT = 247
SCHEMA = "schema_v2_s011"
_ID = "0" * 32
_UUID = "00000000-0000-0000-0000-000000000000"
_CIPHER = "backup-" + _ID + ".glgui.age"
_STATUS = "status-" + _UUID + "-" + SCHEMA + "-" + "0" * 16 + ".json"
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?\Z", re.I)


class WindowsPathBudgetError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _root(value):
    # This lexical check grants no access and does not widen TARGET permissions.
    if (type(value) is not str or not value or len(value) > 4096
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise WindowsPathBudgetError("windows_path_budget_invalid")
    path = value.replace("\\", "/")
    if (not re.match(r"^[A-Za-z]:/", path) or "//" in path
            or any(c in path[2:] for c in ':<>"|?*,')
            or any(not part or part in (".", "..") or part.endswith((".", " "))
                   or _DEVICE.fullmatch(part) for part in path[3:].split("/"))):
        raise WindowsPathBudgetError("windows_path_budget_invalid")
    return path


def _units(path):
    return len(path.encode("utf-16-le")) // 2


def check_path(path, *, directory=False):
    """Check one absolute local Windows path, returning only its UTF-16 size."""
    if type(directory) is not bool:
        raise WindowsPathBudgetError("windows_path_budget_invalid")
    path = _root(path)
    size = _units(path)
    if size > (DIRECTORY_LIMIT if directory else FILE_LIMIT):
        raise WindowsPathBudgetError("windows_path_budget_exceeded")
    # Every parent that might be created must fit the traditional directory cap.
    parent = path if directory else path.rsplit("/", 1)[0]
    if _units(parent) > DIRECTORY_LIMIT:
        raise WindowsPathBudgetError("windows_path_budget_exceeded")
    return size


def check_paths(*, state_dir, store_dir, exchange_dir=None, workspace_dir=None,
                installation_dir=None):
    """Budget current backup + optional v3 installation/recovery host paths.

    Parameters are existing validated absolute Windows path strings. Values are
    never rewritten or returned as filesystem/Docker inputs. Errors contain no
    supplied paths. Choose a shorter independent site root before installation
    if this fails; moving an enrolled site requires its normal migration flow.
    """
    files, directories = set(), set()

    def add(root, relative, *, directory=False, atomic=False):
        path = _root(root) + ("/" + relative if relative else "")
        (directories if directory else files).add(path)
        parent = path if directory else path.rsplit("/", 1)[0]
        while len(parent) > 3:
            directories.add(parent)
            parent = parent.rsplit("/", 1)[0]
        if atomic:
            base, name = path.rsplit("/", 1)
            files.add(base + "/." + name + "." + _ID)

    run = "runs/" + _ID
    for name in ("run.json",):
        add(state_dir, run + "/" + name, atomic=True)
    for name in (_CIPHER, _STATUS, "status-" + _UUID + ".json", "completed-" + _ID + ".json"):
        add(state_dir, run + "/exchange/" + name)
    for relative in ("bundle-manifests/" + _ID + ".json", "audits/" + _ID + ".json",
                     "copy-readbacks/" + _ID + ".json", "cloud-readbacks/" + _ID + ".json",
                     "last-result.json", "schedule-enabled.json"):
        add(state_dir, relative, atomic=True)
    add(state_dir, "coordinator.lock")
    for branch in ("pending/" + _ID + "-" + "0" * 16, "artifacts/" + _ID):
        for name in (_CIPHER, "backup.json", "verify.json", "BUNDLE.json", "SHA256SUMS"):
            add(store_dir, branch + "/" + name)
    add(store_dir, ".backup-store.lock")
    # Both recovery and historical S010->S011 upgrades stage encrypted inputs.
    for branch in ("restores", "upgrades"):
        folder = branch + "/" + _ID
        for name in ("workflow.json", "target-before.json", "before.json"):
            add(state_dir, folder + "/" + name, atomic=True)
        for name in (_CIPHER, "request.json"):
            add(state_dir, folder + "/upload/" + name)
    if exchange_dir is not None:
        for name in (_STATUS, _CIPHER, "completed-" + _ID + ".json",
                     "key-target-" + _UUID + ".json", "key-import-" + _ID + ".json"):
            add(exchange_dir, name)
    if workspace_dir is not None:
        add(workspace_dir, "profile.json", atomic=True)
        add(workspace_dir, "key-transfer-" + _ID + "/key-transfer-" + _ID + ".age")
        for prefix in ("key-transfer-", "enrollment-"):
            for name in ("request.json", "status.json"):
                add(workspace_dir, prefix + _ID + "/" + name, atomic=True)
    if installation_dir is not None:
        # Longest current generated tooling/requests/assets paths, plus runtime
        # atomic records. Explicit representatives cover shorter packet members.
        for relative in ("tooling/operations_v3/isolated_probe_executor.py",
                         "tooling/gui_v2/certificate_renewal.py",
                         "requests/key-import/REQUEST_REQUIRED.txt",
                         "requests/restore/" + _CIPHER,
                         "assets/schema_v2_s011.sql", "key-prepare/docker-compose.yml"):
            add(installation_dir, relative)
        for name in ("RUNTIME_STATE.json", "HANDOVER_FENCE.json", "PROBE_GUARD.json"):
            add(installation_dir, name, atomic=True)
        add(installation_dir, "isolated-probes/" + _ID + "/STATE.json", atomic=True)
        add(installation_dir, "isolated-probes/" + _ID + "/nginx.conf")
        add(installation_dir, "RUNTIME.lock")
    file_sizes = [check_path(path) for path in sorted(files)]
    directory_sizes = [check_path(path, directory=True) for path in sorted(directories)]
    return {"format": "gift-ledger-windows-path-budget-v1", "success": True,
            "file_limit_utf16": FILE_LIMIT, "directory_limit_utf16": DIRECTORY_LIMIT,
            "max_file_utf16": max(file_sizes), "max_directory_utf16": max(directory_sizes),
            "checked_files": len(files), "checked_directories": len(directories),
            "supported_schema": SCHEMA}
