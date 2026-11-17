import os
from pathlib import Path

import pytest

from gossip_snapshot import performance


def test_auto_workers_respect_affinity_quota_and_ceiling(monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: set(range(32)), raising=False)
    read = Path.read_text
    quota = ["max 100000"]
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **kw: (
            quota[0] if str(path) == "/sys/fs/cgroup/cpu.max" else read(path, *a, **kw)
        ),
    )
    assert performance.worker_count() == 8
    quota[0] = "250000 100000"
    assert performance.worker_count() == 2
    assert performance.worker_count(1) == 1
    assert performance.worker_count(16) == 16
    quota[0] = "50000 100000"
    assert performance.worker_count() == 1


@pytest.mark.parametrize("value", [-1, 65, 1.5, True])
def test_invalid_workers_fail(value):
    with pytest.raises(ValueError, match="workers"):
        performance.worker_count(value)


def test_cache_preflight_preserves_ram_headroom(monkeypatch):
    monkeypatch.setattr(
        performance,
        "memory_info",
        lambda: {
            "host_memory_total_bytes": 128 * performance.GIB,
            "host_memory_available_bytes": 100 * performance.GIB,
        },
    )
    result = performance.settings(4, 1)
    assert result["block_cache_bytes"] == 4 * performance.GIB
    assert result["verification_workers"] == 1
    with pytest.raises(ValueError, match="half"):
        performance.settings(64, 1)
    for invalid in (0, -1, float("nan"), float("inf"), 65):
        with pytest.raises(ValueError, match="block cache"):
            performance.settings(invalid)


def test_linux_telemetry_reports_rates_and_handles_missing_or_reset_counters(tmp_path):
    process = tmp_path / "123"
    process.mkdir()
    (tmp_path / "meminfo").write_text("MemTotal: 16000000 kB\nMemAvailable: 12000000 kB\n")
    (process / "status").write_text("Name:\tlnd\nVmRSS:\t2048 kB\n")

    def counters(cpu, read, write):
        fields = ["0"] * 22
        fields[0], fields[11], fields[12] = "R", str(cpu), "0"
        (process / "stat").write_text("123 (lnd (test)) " + " ".join(fields))
        (process / "io").write_text(f"read_bytes: {read}\nwrite_bytes: {write}\n")

    now = iter([0, 2, 4, 6])
    monitor = performance.Telemetry(123, proc=tmp_path, clock=lambda: next(now))
    monitor.ticks = 100
    counters(100, 0, 0)
    first = monitor.sample(0)
    assert "lnd_cpu_percent" not in first
    assert first["lnd_rss_bytes"] == 2048 * 1024
    assert first["host_memory_available_bytes"] == 12000000 * 1024
    counters(500, 4 * 1024**2, 8 * 1024**2)
    second = monitor.sample(8 * 1024**2)
    assert second["lnd_cpu_percent"] == 200
    assert second["host_receive_bytes_per_second"] == 4 * 1024**2
    assert second["lnd_disk_read_bytes_per_second"] == 2 * 1024**2
    assert second["lnd_disk_write_bytes_per_second"] == 4 * 1024**2
    counters(1, 0, 0)
    reset = monitor.sample(0)
    assert "host_receive_bytes_per_second" not in reset
    assert "lnd_cpu_percent" not in reset
    (process / "stat").unlink()
    (process / "status").unlink()
    (process / "io").unlink()
    missing = monitor.sample(0)
    assert "lnd_rss_bytes" not in missing
    assert "lnd_cpu_percent" not in missing
