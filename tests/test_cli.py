from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def needs_node() -> None:
    if shutil.which("node") is None or shutil.which("npm") is None:
        pytest.skip("node and npm are required for js tests")


def run_kern(project: Path, *args: str, input_text: str | None = None, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.pop("KERN_HOME", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "kern", *args],
        cwd=project,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture()
def project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["uv", "venv", "--python", "3.14"], cwd=project, check=True)
    subprocess.run(
        ["uv", "pip", "install", "--python", str(project / ".venv" / "bin" / "python"), "ipykernel"],
        cwd=project,
        check=True,
    )
    try:
        yield project
    finally:
        run_kern(project, "stop", "py")
        run_kern(project, "stop", "py@scratch")
        run_kern(project, "stop", "js")


@pytest.fixture()
def pandas_project(project: Path) -> Path:
    subprocess.run(
        ["uv", "pip", "install", "--python", str(project / ".venv" / "bin" / "python"), "pandas"],
        cwd=project,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return project


def test_persists_state_and_uses_project_storage(project: Path) -> None:
    first = run_kern(project, "py", "x = 41")
    assert first.returncode == 0, first.stderr

    second = run_kern(project, "py", "x + 1")
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "42"

    assert (project / ".kern" / "sessions.json").exists()
    assert list((project / ".kern" / "runtime").glob("*.json"))
    assert list((project / ".kern" / "logs").glob("*.log"))


def test_stdin_json_and_named_session(project: Path) -> None:
    assert run_kern(project, "py", "x = 10").returncode == 0

    stdin_result = run_kern(project, "py", input_text="y = x * 2\ny\n")
    assert stdin_result.returncode == 0, stdin_result.stderr
    assert stdin_result.stdout.strip() == "20"

    named_set = run_kern(project, "py@scratch", "x = 100")
    assert named_set.returncode == 0, named_set.stderr
    named_get = run_kern(project, "py@scratch", "x + 1")
    assert named_get.stdout.strip() == "101"

    default_get = run_kern(project, "--json", "py", "x + 1")
    assert default_get.returncode == 0, default_get.stderr
    payload = json.loads(default_get.stdout)
    assert payload["ok"] is True
    assert payload["events"][0]["type"] == "result"
    assert payload["events"][0]["text"] == "11"


def test_artifact_filenames_do_not_collide_after_restart(project: Path) -> None:
    code = "\n".join(
        [
            "from IPython.display import Image, display",
            "import base64",
            "png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=')",
            "display(Image(data=png))",
        ]
    )

    first = run_kern(project, "py", code)
    assert first.returncode == 0, first.stderr
    restart = run_kern(project, "restart", "py")
    assert restart.returncode == 0, restart.stderr
    second = run_kern(project, "py", code)
    assert second.returncode == 0, second.stderr

    artifact_paths = [
        Path(match.group(1))
        for output in (first.stdout, second.stdout)
        for match in re.finditer(r"\[kern artifact\] image/png (.+)", output)
    ]
    assert len(artifact_paths) == 2
    assert artifact_paths[0] != artifact_paths[1]
    assert all(path.exists() for path in artifact_paths)


def test_rich_result_also_prints_text_plain(pandas_project: Path) -> None:
    result = run_kern(
        pandas_project,
        "py",
        "\n".join(
            [
                "import pandas as pd",
                "pd.DataFrame({'team': ['api', 'web'], 'latency_ms': [142, 118]})",
            ]
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "[kern artifact] text/html " in result.stdout
    assert "team" in result.stdout
    assert "latency_ms" in result.stdout
    assert "api" in result.stdout


def test_timeout_error_and_later_reuse(project: Path) -> None:
    slow_success = run_kern(project, "py", "import time; time.sleep(1.2); 99")
    assert slow_success.returncode == 0, slow_success.stderr
    assert slow_success.stdout.strip() == "99"

    timed_out = run_kern(project, "--timeout", "1", "py", "import time; time.sleep(2)")
    assert timed_out.returncode == 124
    assert "execution timed out" in timed_out.stderr

    reused = run_kern(project, "py", "2 + 2")
    assert reused.returncode == 0, reused.stderr
    assert reused.stdout.strip() == "4"


def test_kern_home_override(project: Path, tmp_path: Path) -> None:
    kern_home = tmp_path / "kern-home"
    result = run_kern(project, "py", "z = 3", env_extra={"KERN_HOME": str(kern_home)})
    assert result.returncode == 0, result.stderr

    assert (kern_home / "sessions.json").exists()
    assert list((kern_home / "runtime").glob("*.json"))
    assert not (project / ".kern" / "sessions.json").exists()

    stopped = run_kern(project, "stop", "py", env_extra={"KERN_HOME": str(kern_home)})
    assert stopped.returncode == 0


def test_bootstrap_installs_missing_ipykernel(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is required for uv-created venv bootstrap")

    project = tmp_path / "bootstrap-project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["uv", "venv", "--python", "3.14"], cwd=project, check=True)

    missing = run_kern(project, "py", "1 + 1")
    assert missing.returncode == 3
    assert "does not have ipykernel installed" in missing.stderr

    bootstrapped = run_kern(project, "--bootstrap", "py", "value = 7")
    assert bootstrapped.returncode == 0, bootstrapped.stderr

    persisted = run_kern(project, "py", "value + 1")
    assert persisted.returncode == 0, persisted.stderr
    assert persisted.stdout.strip() == "8"

    run_kern(project, "stop", "py")


def test_js_bootstrap_project_modules_imports_and_persistence(tmp_path: Path) -> None:
    needs_node()

    project = tmp_path / "js-project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["npm", "init", "-y"], cwd=project, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["npm", "install", "left-pad"], cwd=project, check=True, stdout=subprocess.DEVNULL)

    missing = run_kern(project, "js", "1 + 1")
    assert missing.returncode == 3
    assert "does not have tslab installed" in missing.stderr

    bootstrapped = run_kern(project, "--bootstrap", "js", "var value = 41; value")
    assert bootstrapped.returncode == 0, bootstrapped.stderr
    assert bootstrapped.stdout.strip() == "41"
    assert (project / ".kern" / "kernels" / "js" / "node_modules" / ".bin" / "tslab").exists()

    persisted = run_kern(project, "js", "value + 1")
    assert persisted.returncode == 0, persisted.stderr
    assert persisted.stdout.strip() == "42"

    require_result = run_kern(project, "js", 'const leftPad = require("left-pad"); leftPad("x", 3, "0")')
    assert require_result.returncode == 0, require_result.stderr
    assert require_result.stdout.strip() == "00x"

    import_result = run_kern(project, "js", 'import path from "node:path"; path.basename("a/b.txt")')
    assert import_result.returncode == 0, import_result.stderr
    assert import_result.stdout.strip() == "b.txt"

    await_result = run_kern(project, "js", 'const mod = await import("node:path"); mod.basename("a/b.txt")')
    assert await_result.returncode == 0, await_result.stderr
    assert await_result.stdout.strip() == "b.txt"

    run_kern(project, "stop", "js")


def test_js_execution_does_not_rewrite_kern_bookkeeping(tmp_path: Path) -> None:
    needs_node()

    project = tmp_path / "js-watch-project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["npm", "init", "-y"], cwd=project, check=True, stdout=subprocess.DEVNULL)

    bootstrapped = run_kern(project, "--bootstrap", "js", "var value = 41; value")
    assert bootstrapped.returncode == 0, bootstrapped.stderr

    registry = project / ".kern" / "sessions.json"
    lock = project / ".kern" / "sessions.lock"
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in [registry, lock]
    }

    time.sleep(0.01)
    result = run_kern(project, "js", "value + 1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "42"

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in [registry, lock]
    }
    assert after == before

    run_kern(project, "stop", "js")


def test_js_stdin_json_and_named_session(tmp_path: Path) -> None:
    needs_node()

    project = tmp_path / "js-io-project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["npm", "init", "-y"], cwd=project, check=True, stdout=subprocess.DEVNULL)

    bootstrapped = run_kern(project, "--bootstrap", "js", "var x = 10; x")
    assert bootstrapped.returncode == 0, bootstrapped.stderr

    stdin_result = run_kern(
        project,
        "js",
        input_text="var y = x * 2;\ny\n",
    )
    assert stdin_result.returncode == 0, stdin_result.stderr
    assert stdin_result.stdout.strip() == "20"

    named_set = run_kern(project, "js@scratch", "var x = 100; x")
    assert named_set.returncode == 0, named_set.stderr
    assert named_set.stdout.strip() == "100"

    named_get = run_kern(project, "js@scratch", "x + 1")
    assert named_get.returncode == 0, named_get.stderr
    assert named_get.stdout.strip() == "101"

    default_get = run_kern(project, "--json", "js", "x + 1")
    assert default_get.returncode == 0, default_get.stderr
    payload = json.loads(default_get.stdout)
    assert payload["ok"] is True
    assert payload["events"] == [
        {"type": "stream", "text": "11\n", "name": "stdout"},
    ]

    run_kern(project, "stop", "js")
    run_kern(project, "stop", "js@scratch")


def test_js_ls_stop_and_restart(tmp_path: Path) -> None:
    needs_node()

    project = tmp_path / "js-management-project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["npm", "init", "-y"], cwd=project, check=True, stdout=subprocess.DEVNULL)

    bootstrapped = run_kern(project, "--bootstrap", "js", "var value = 7; value")
    assert bootstrapped.returncode == 0, bootstrapped.stderr

    listed = run_kern(project, "ls")
    assert listed.returncode == 0, listed.stderr
    assert "js" in listed.stdout
    pid_before_restart = re.search(r"^js\s+(\d+)", listed.stdout, re.MULTILINE)
    assert pid_before_restart is not None

    restarted = run_kern(project, "restart", "js")
    assert restarted.returncode == 0, restarted.stderr
    assert "restarted js" in restarted.stdout

    listed_after_restart = run_kern(project, "ls")
    assert listed_after_restart.returncode == 0, listed_after_restart.stderr
    pid_after_restart = re.search(r"^js\s+(\d+)", listed_after_restart.stdout, re.MULTILINE)
    assert pid_after_restart is not None
    assert pid_after_restart.group(1) != pid_before_restart.group(1)

    after_restart = run_kern(project, "js", "var valueAfterRestart = 1; valueAfterRestart")
    assert after_restart.returncode == 0, after_restart.stderr
    assert after_restart.stdout.strip() == "1"

    stopped = run_kern(project, "stop", "js")
    assert stopped.returncode == 0, stopped.stderr
    assert "stopped js" in stopped.stdout

    listed_after_stop = run_kern(project, "ls")
    assert listed_after_stop.returncode == 0, listed_after_stop.stderr
    assert "no running sessions" in listed_after_stop.stdout


def test_js_error_returns_nonzero_and_session_remains_usable(tmp_path: Path) -> None:
    needs_node()

    project = tmp_path / "js-error-project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["npm", "init", "-y"], cwd=project, check=True, stdout=subprocess.DEVNULL)

    bootstrapped = run_kern(project, "--bootstrap", "js", "var value = 5; value")
    assert bootstrapped.returncode == 0, bootstrapped.stderr

    failed = run_kern(project, "js", 'throw new Error("boom")')
    assert failed.returncode == 1
    assert "Error: boom" in failed.stderr

    reused = run_kern(project, "js", "value + 1")
    assert reused.returncode == 0, reused.stderr
    assert reused.stdout.strip() == "6"

    run_kern(project, "stop", "js")
