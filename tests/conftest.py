from datetime import UTC, datetime

import pytest

from gossip_snapshot.dashboard import GIB


@pytest.fixture
def demo_status():
    return {
        "state": "running",
        "collector_revision": {"commit": "abcdef012345" + "0" * 28, "dirty": False},
        "collector_running": True,
        "wallet_state": "SERVER_ACTIVE",
        "elapsed_seconds": 1452,
        "updated_at": datetime.now(UTC).isoformat(),
        "limits": {"hours": 1, "max_download_gb": 10, "max_data_gb": 30, "min_free_gb": 10},
        "node": {
            "block_height": 950000,
            "synced_to_chain": True,
            "synced_to_graph": True,
            "num_peers": 5,
        },
        "graph_channels": 32000,
        "readiness_samples": 1,
        "readiness": {"ready": False, "reasons": ["need at least three samples"]},
        "resources": {
            "free_bytes": 112 * GIB,
            "observer_disk_bytes": 4.8 * GIB,
            "host_received_bytes": 2.3 * GIB,
            "lnd_cpu_percent": 265.0,
            "lnd_rss_bytes": 3.2 * GIB,
            "host_memory_available_bytes": 104 * GIB,
            "host_receive_bytes_per_second": 6.4 * 1024**2,
            "lnd_disk_read_bytes_per_second": 1.2 * 1024**2,
            "lnd_disk_write_bytes_per_second": 4.8 * 1024**2,
        },
        "performance": {"block_cache_bytes": 4 * GIB, "cpu_capacity": 8, "verification_workers": 8},
    }
