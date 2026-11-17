"""Exercise build setup failures without Go, network access or an LND clone."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def build_project(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(root / "scripts" / "build_exporter.py", scripts)
    shutil.copy2(root / "versions.json", project)
    tools = tmp_path / "tools"
    tools.mkdir()
    return project, tools


def fake_tool(tools, name, body):
    path = tools / name
    path.write_text(f"#!{sys.executable}\n{body}\n")
    path.chmod(0o755)


def invoke_build(project, tools):
    return subprocess.run(
        [sys.executable, str(project / "scripts" / "build_exporter.py")],
        cwd=project,
        env={**os.environ, "PATH": str(tools)},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("missing", ["go", "git"])
def test_missing_build_tool_fails_before_clone(build_project, missing):
    project, tools = build_project
    available = "git" if missing == "go" else "go"
    fake_tool(tools, available, "raise SystemExit('must not execute before prerequisite check')")
    result = invoke_build(project, tools)
    assert result.returncode != 0
    assert f"Missing build prerequisites on PATH: {missing}" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (project / ".cache").exists()
    assert not (project / "bin").exists()


@pytest.mark.parametrize("version", ["go1.18.1", "go1.20.14"])
def test_old_go_fails_before_clone(build_project, version):
    project, tools = build_project
    fake_tool(tools, "git", "raise SystemExit('must not clone with old Go')")
    fake_tool(
        tools,
        "go",
        "import os, sys\n"
        "assert sys.argv[1:] == ['version']\n"
        "assert os.environ['GOTOOLCHAIN'] == 'local'\n"
        f"print('go version {version} linux/amd64')",
    )
    result = invoke_build(project, tools)
    assert result.returncode != 0
    assert "Go bootstrap >= 1.21 is required" in result.stderr
    assert version in result.stderr
    assert "Traceback" not in result.stderr
    assert not (project / ".cache").exists()


def test_usable_go_reaches_checkout_validation(build_project):
    project, tools = build_project
    # An existing checkout is reused. Its wrong commit must still be rejected: a
    # successful prerequisite check must not skip pinning.
    (project / ".cache" / "lnd").mkdir(parents=True)
    fake_tool(tools, "go", "print('go version go1.21.0 linux/amd64')")
    fake_tool(
        tools,
        "git",
        "import sys\nassert sys.argv[1:] == ['rev-parse', 'HEAD']\nprint('wrong-commit')",
    )
    result = invoke_build(project, tools)
    assert result.returncode != 0
    assert "Wrong LND revision" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (project / "bin").exists()
