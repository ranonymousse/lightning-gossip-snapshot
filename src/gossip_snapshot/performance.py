"""Local resource discovery and Linux process telemetry, with no dependencies."""

import math
import os
import time
from pathlib import Path

from .common import GIB

# The automatic choice of verification processes ends here, whatever the machine offers.
AUTO_WORKERS = 8
MAX_WORKERS = 64  # The most that a request may name.
# LND's block cache in GiB. Blocks come from the local Bitcoin Core, so a small one does.
DEFAULT_BLOCK_CACHE_GB = 2
MAX_BLOCK_CACHE_GB = 64
# The cache counts serialized bytes and holds more than twice that in memory, so it may
# claim at most this share of the memory that is available when a session starts.
CACHE_MEMORY_SHARE = 0.5
KIB = 1024  # /proc states memory in kB of 1024 bytes.


def cpu_capacity():
    """Respect affinity and a visible cgroup v2 quota without changing Go's runtime."""
    count = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    count = max(1, count or 1)
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            count = min(count, max(1, math.floor(int(quota) / int(period))))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return count


def worker_count(requested=0):
    """Verification processes to use: the request, or for 0 one per CPU up to `AUTO_WORKERS`."""
    if type(requested) is not int or not 0 <= requested <= MAX_WORKERS:
        raise ValueError(f"workers must be an integer from 0 (auto) to {MAX_WORKERS}")
    return min(AUTO_WORKERS, cpu_capacity()) if requested == 0 else requested


def memory_info(proc=Path("/proc")):
    """Total and available memory of the host in bytes, or nothing without /proc/meminfo."""
    try:
        fields = dict(line.split(":", 1) for line in (proc / "meminfo").read_text().splitlines())
        return {
            "host_memory_total_bytes": int(fields["MemTotal"].split()[0]) * KIB,
            "host_memory_available_bytes": int(fields["MemAvailable"].split()[0]) * KIB,
        }
    except (OSError, KeyError, ValueError):
        return {}


def check_block_cache(cache_gb):
    """Refuse a block cache size outside the supported range."""
    if not math.isfinite(cache_gb) or not 0 < cache_gb <= MAX_BLOCK_CACHE_GB:
        raise ValueError(f"block cache must be greater than 0 and at most {MAX_BLOCK_CACHE_GB} GiB")


def settings(cache_gb=DEFAULT_BLOCK_CACHE_GB, workers=0):
    """The performance settings that a session records: block cache, workers and host capacity."""
    check_block_cache(cache_gb)
    memory = memory_info()
    available = memory.get("host_memory_available_bytes")
    if available is not None and cache_gb * GIB > available * CACHE_MEMORY_SHARE:
        raise ValueError("block cache exceeds half the available memory; lower --block-cache-gb")
    return {
        "block_cache_bytes": int(cache_gb * GIB),
        "verification_workers": worker_count(workers),
        "cpu_capacity": cpu_capacity(),
        **memory,
    }


class Telemetry:
    """Sample only our LND child. Missing optional /proc fields never stop collection."""

    def __init__(self, pid, *, proc=Path("/proc"), clock=time.monotonic):
        self.pid, self.proc, self.clock = pid, proc, clock
        self.previous = None
        self.ticks = os.sysconf("SC_CLK_TCK")

    def sample(self, received):
        """Memory now, and the CPU, network and disk rates since the previous sample.

        `received` is the number of bytes the host has received so far.
        """
        now = self.clock()
        result = memory_info(self.proc)
        counters = {"host_rx": received}
        process = self.proc / str(self.pid)
        try:
            # comm may contain spaces and parentheses; fields after its last ')'
            # start at field 3. utime/stime are fields 14/15.
            fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
            counters["cpu_seconds"] = (int(fields[11]) + int(fields[12])) / self.ticks
        except (OSError, ValueError, IndexError):
            pass
        try:
            lines = (process / "status").read_text().splitlines()
            rss = next(line for line in lines if line.startswith("VmRSS:"))
            result["lnd_rss_bytes"] = int(rss.split()[1]) * KIB
        except (OSError, ValueError, StopIteration):
            pass
        try:
            fields = dict(line.split(":", 1) for line in (process / "io").read_text().splitlines())
            counters["disk_read"] = int(fields["read_bytes"])
            counters["disk_write"] = int(fields["write_bytes"])
        except (OSError, ValueError, KeyError):
            pass
        if self.previous is not None:
            before, old = self.previous
            elapsed = now - before
            if elapsed > 0:
                for key, output, scale in (
                    ("cpu_seconds", "lnd_cpu_percent", 100),
                    ("host_rx", "host_receive_bytes_per_second", 1),
                    ("disk_read", "lnd_disk_read_bytes_per_second", 1),
                    ("disk_write", "lnd_disk_write_bytes_per_second", 1),
                ):
                    if key in counters and key in old and counters[key] >= old[key]:
                        result[output] = round((counters[key] - old[key]) / elapsed * scale, 2)
        self.previous = now, counters
        return result
