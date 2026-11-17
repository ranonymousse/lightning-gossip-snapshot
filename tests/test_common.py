"""The shared helpers: one implementation each, reachable where callers import it from."""

import hashlib
import json
import re

from gossip_snapshot import analysis, collector, common, core, dashboard, performance


def test_each_helper_has_one_implementation():
    assert analysis.sha256_file is common.sha256_file
    assert analysis.write_json is common.write_json
    assert collector.save is core.save is common.save
    assert collector.utc_now is core.utc_now is common.utc_now
    assert collector.PROJECT == core.PROJECT == common.PROJECT
    assert (common.PROJECT / "versions.json").is_file()
    assert collector.GIB == core.GIB == dashboard.GIB == performance.GIB == 1024 * common.MIB


def test_time_and_hash_have_the_recorded_form(tmp_path):
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", common.utc_now())
    path = tmp_path / "file"
    path.write_bytes(b"gossip")
    assert common.sha256_file(path) == hashlib.sha256(b"gossip").hexdigest()


def test_save_replaces_a_private_file_and_leaves_no_temporary_one(tmp_path):
    path, plain = tmp_path / "status.json", tmp_path / "plain.json"
    common.save(path, {"state": "running"})
    value = {"state": "stopped", "limits": {"hours": 1.5, "b": None}}
    common.save(path, value)
    common.write_json(plain, value)
    assert json.loads(path.read_text()) == value
    # Both writers give the same text: sorted keys, two spaces, a final newline.
    assert path.read_text() == plain.read_text() and path.read_text().endswith("}\n")
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["plain.json", "status.json"]
