"""Numeric utility-container resource observations; never a safety decision.

The tmpfs value is the largest observed statvfs sample, not an exact peak.
Linux ru_maxrss values for self and children are separate kernel observations;
they cannot be added to infer a simultaneous process-tree peak.
"""

import math
import os
import re
import sys
import threading
from pathlib import Path, PurePosixPath

FORMAT = "gift-ledger-backup-resource-sample-v1"
CGROUP_UNAVAILABLE = "cgroup-unavailable-or-unmapped"


class ResourceMeter:
    def __init__(
        self,
        interval_seconds=0.1,
        *,
        platform=None,
        statvfs_reader=None,
        usage_reader=None,
        cgroup_reader=None,
    ):
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (float, int))
            or not math.isfinite(interval_seconds)
            or interval_seconds < 0.05
        ):
            raise ValueError("resource_sampling_interval_invalid")
        self.interval = float(interval_seconds)
        self.platform = platform if platform is not None else sys.platform
        self.statvfs = statvfs_reader or getattr(os, "statvfs", None)
        self.usage = usage_reader or self._linux_usage
        self.cgroup = cgroup_reader or self._cgroup_counter
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None
        self.started = False
        self.state = {
            "format": FORMAT,
            "available": False,
            "samples": 0,
            "sampling_interval_seconds": self.interval,
            "max_sampled_tmpfs_bytes": None,
            "self_peak_rss_bytes": None,
            "children_peak_rss_bytes": None,
            "container_peak_bytes": None,
            "container_peak_source": None,
            "tmpfs_peak_is_exact": False,
            "rss_values_are_additive": False,
            "scope": "utility-container-only",
            "error_code": None,
        }

    @staticmethod
    def _linux_usage():
        # Import here: Windows can load worker and pure tests without resource.
        import resource

        return (
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        )

    @staticmethod
    def _kernel_text(filename):
        with Path(filename).open("rb") as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("resource_proc_too_large")
        return raw.decode("utf-8", errors="strict")

    @staticmethod
    def _cgroup_paths(cgroups, mountinfo):
        """Map only this process's membership into a kernel controller mount.

        Never assume a v1 controller root is this container. Mount metadata is
        read from /proc, never supplied by the host profile or public commands.
        """

        def absolute(raw):
            raw = re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m.group(1), 8)), raw)
            if (
                not raw.startswith("/")
                or any(part in {".", ".."} for part in raw.split("/"))
                or any(ord(character) < 32 for character in raw)
                or "\\" in raw
            ):
                raise ValueError("resource_cgroup_path_invalid")
            return PurePosixPath(raw)

        memberships = []
        for line in cgroups.splitlines():
            hierarchy, controllers, raw = line.split(":", 2)
            if not hierarchy.isdigit():
                raise ValueError("resource_cgroup_membership_invalid")
            if hierarchy == "0" and controllers == "":
                memberships.append(("cgroup2", absolute(raw)))
            elif "memory" in controllers.split(","):
                memberships.append(("cgroup", absolute(raw)))
        paths = []
        for line in mountinfo.splitlines():
            left, right = line.split(" - ", 1)
            fields, filesystem = left.split(), right.split()
            if (
                len(fields) < 6
                or len(filesystem) < 3
                or filesystem[0] not in {"cgroup2", "cgroup"}
            ):
                continue
            kind = filesystem[0]
            if kind == "cgroup" and "memory" not in filesystem[2].split(","):
                continue
            root, mount = absolute(fields[3]), absolute(fields[4])
            # Restrict numeric counter reads to the normal kernel cgroup tree.
            if (
                mount != PurePosixPath("/sys/fs/cgroup")
                and PurePosixPath("/sys/fs/cgroup") not in mount.parents
            ):
                continue
            for membership_kind, membership in memberships:
                if membership_kind != kind or (
                    membership != root and root not in membership.parents
                ):
                    continue
                if kind == "cgroup" and membership == root == PurePosixPath("/"):
                    continue  # An ambiguous v1 global root is not a container peak.
                relative = membership.relative_to(root)
                filename = (
                    "memory.peak" if kind == "cgroup2" else "memory.max_usage_in_bytes"
                )
                source = (
                    "cgroup-v2-memory.peak"
                    if kind == "cgroup2"
                    else "cgroup-v1-memory.max_usage_in_bytes"
                )
                paths.append((Path(str(mount / relative / filename)), source))
        return paths

    @classmethod
    def _cgroup_counter(cls):
        try:
            paths = cls._cgroup_paths(
                cls._kernel_text("/proc/self/cgroup"),
                cls._kernel_text("/proc/self/mountinfo"),
            )
            for filename, source in paths:
                try:
                    with filename.open("rb") as stream:
                        raw = stream.read(65)
                    value = raw.strip()
                    if len(raw) <= 64 and value.isdigit():
                        return int(value), source
                except OSError:
                    continue
        except (OSError, ValueError, UnicodeError):
            pass
        return None, CGROUP_UNAVAILABLE

    def _sample(self):
        with self.lock:
            if self.state["error_code"]:
                return
            try:
                usage = self.statvfs("/tmp")
                fragment = usage.f_frsize or usage.f_bsize
                if (
                    type(fragment) is not int
                    or fragment <= 0
                    or type(usage.f_blocks) is not int
                    or type(usage.f_bfree) is not int
                    or not 0 <= usage.f_bfree <= usage.f_blocks
                ):
                    raise ValueError("resource_statvfs_invalid")
                tmpfs_bytes = (usage.f_blocks - usage.f_bfree) * fragment
                own, children = self.usage()
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                    for value in (own, children)
                ):
                    raise ValueError("resource_rusage_invalid")
                container, source = self.cgroup()
                if container is not None and (
                    type(container) is not int or container < 0
                ):
                    container, source = None, None
                self.state["samples"] += 1
                for field, value in (
                    ("max_sampled_tmpfs_bytes", tmpfs_bytes),
                    ("self_peak_rss_bytes", int(own * 1024)),
                    ("children_peak_rss_bytes", int(children * 1024)),
                ):
                    self.state[field] = max(self.state[field] or 0, value)
                if container is not None:
                    self.state["container_peak_bytes"] = max(
                        self.state["container_peak_bytes"] or 0, container
                    )
                    self.state["container_peak_source"] = source
                elif self.state["container_peak_bytes"] is None:
                    self.state["container_peak_source"] = source
                self.state["available"] = True
            except Exception:  # noqa: BLE001 -- telemetry is optional and never discloses exception text
                self.state["available"] = False
                self.state["error_code"] = "resource_sampling_unavailable"
                self.stopped.set()

    def _run(self):
        while not self.stopped.wait(self.interval):
            self._sample()

    def start(self):
        if self.started:
            return self
        self.started = True
        if not self.platform.startswith("linux") or self.statvfs is None:
            self.state["error_code"] = "resource_sampling_unsupported"
            return self
        self._sample()
        if not self.stopped.is_set():
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def snapshot(self):
        # A checkpoint sample complements the bounded periodic sampler.
        if self.started and self.platform.startswith("linux"):
            self._sample()
        with self.lock:
            return dict(self.state)

    def stop(self):
        self.stopped.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)
