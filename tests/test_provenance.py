"""Files that the reported session recorded by hash are unchanged in this release."""

from pathlib import Path

from gossip_snapshot.common import sha256_file

ROOT = Path(__file__).resolve().parents[1]
# SHA-256 as the session behind `results/` recorded them: the exporter's sources in its
# build manifest, the two verification modules at the session's start, and the lock file
# as copied into the observer directory.
RECORDED = {
    "cmd/gossip-export/main.go": "d5402ef95c4c4441b681240e48a3687517e390d718de28856a4d4637cce369f1",
    "cmd/gossip-export/main_test.go": (
        "b1020319bd64b785954ef616cb39ef62d2af64ac19bd39de755f31580fd7082b"
    ),
    "src/gossip_snapshot/wire.py": (
        "e226065f27076f84007ac26fa6f6ff55abefa62f87dabc328f9bf5e4253ec891"
    ),
    "src/gossip_snapshot/verification.py": (
        "633ae63c1a2c68fa86af8e833a66246dacf3d78fb9ac6e393a265bcc12b37778"
    ),
    "uv.lock": "857f7268aa0834ee03676ea295d6ce2e209e447b822ac9bc6bcd1120f1920cdb",
}


def test_files_recorded_by_the_reported_session_are_unchanged():
    for name, digest in RECORDED.items():
        assert sha256_file(ROOT / name) == digest, name
