from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty
from typing import Any
from uuid import uuid4

from jupyter_client import BlockingKernelClient
from jupyter_client.connect import write_connection_file


DEFAULT_SESSION = "default"
VALID_KERNELS = {"py", "js"}
KERN_HOME_ENV = "KERN_HOME"


class KernError(Exception):
    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class KernelIdent:
    kernel: str
    session: str


@dataclass
class Runtime:
    executable: str
    version: str


@dataclass
class SessionRecord:
    id: str
    scope: str
    kernel: str
    session: str
    executable: str
    version: str
    pid: int
    connection_file: str
    log_file: str
    created_at: float
    last_used_at: float


@dataclass
class OutputEvent:
    type: str
    text: str | None = None
    name: str | None = None
    mime: str | None = None
    path: str | None = None
    ok: bool | None = None
    execution_count: int | None = None


@dataclass
class ExecutionResult:
    ok: bool
    execution_count: int | None
    events: list[OutputEvent]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "ls":
            return list_sessions()
        if argv and argv[0] in {"stop", "restart"}:
            command = argv.pop(0)
            return manage_session(command, argv)
        return run(argv)
    except KernError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kern",
        description="Execute code in a persistent Jupyter kernel.",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Install missing kernel support for the selected runtime.",
    )
    parser.add_argument("--json", action="store_true", help="Emit one JSON object.")
    parser.add_argument("--timeout", type=float, default=None, help="Maximum seconds to wait for execution.")
    parser.add_argument("args", nargs="*")
    ns = parser.parse_args(argv)

    if not ns.args:
        raise KernError("missing kernel ident; expected py, py@session, js, or js@session", 2)

    ident = ns.args[0]
    parsed = parse_ident(ident)
    code = read_code(ns.args[1:])
    scope = find_scope(Path.cwd())
    runtime = resolve_runtime(parsed.kernel, Path.cwd())

    if parsed.kernel == "py" and not has_ipykernel(runtime.executable):
        if not ns.bootstrap:
            raise KernError(
                "selected Python does not have ipykernel installed\n"
                f"python: {runtime.executable}\n\n"
                f"Run:\n  kern --bootstrap {ident} {quote_code_for_hint(code)}\n"
                f"or:\n  {runtime.executable} -m pip install ipykernel",
                3,
            )
        install_ipykernel(runtime.executable)
    elif parsed.kernel == "js" and not has_tslab(scope):
        if not ns.bootstrap:
            raise KernError(
                "selected project does not have tslab installed for kern\n\n"
                f"Run:\n  kern --bootstrap {ident} {quote_code_for_hint(code)}",
                3,
            )
        install_tslab(scope)
    elif parsed.kernel == "js":
        patch_tslab(scope)
        expose_node_types(scope)

    record = ensure_session(scope, parsed, runtime)
    result = execute(record, code, timeout=ns.timeout)
    render_result(result, json_mode=ns.json)
    if record.kernel != "js":
        touch_session(record)
    return 0 if result.ok else 1


def manage_session(command: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"kern {command}")
    parser.add_argument("ident")
    ns = parser.parse_args(argv)
    parsed = parse_ident(ns.ident)
    scope = find_scope(Path.cwd())
    runtime = resolve_runtime(parsed.kernel, Path.cwd())
    session_id = make_session_id(scope, parsed, runtime)
    with locked_registry(scope) as registry:
        record_data = registry.get(session_id)
        if record_data is None:
            print(f"no running session for {ns.ident}")
            return 0
        record = session_record_from_data(record_data)
        stop_record(record)
        registry.pop(session_id, None)
    if command == "restart":
        ensure_session(scope, parsed, runtime)
        print(f"restarted {ns.ident}")
    else:
        print(f"stopped {ns.ident}")
    return 0


def list_sessions() -> int:
    scope = find_scope(Path.cwd())
    rows: list[SessionRecord] = []
    with locked_registry(scope) as registry:
        for session_id, data in list(registry.items()):
            record = session_record_from_data(data)
            if is_session_alive(record):
                rows.append(record)
            else:
                registry.pop(session_id, None)

    if not rows:
        print("no running sessions")
        return 0

    print(f"{'IDENT':<14} {'PID':<8} {'RUNTIME':<36} {'SCOPE'}")
    for record in sorted(rows, key=lambda row: (row.scope, row.kernel, row.session)):
        ident = record.kernel if record.session == DEFAULT_SESSION else f"{record.kernel}@{record.session}"
        runtime = shorten_middle(runtime_label(record), 36)
        print(f"{ident:<14} {record.pid:<8} {runtime:<36} {record.scope}")
    return 0


def parse_ident(ident: str) -> KernelIdent:
    if "@" in ident:
        kernel, session = ident.split("@", 1)
        if not session:
            raise KernError("session name cannot be empty", 2)
    else:
        kernel, session = ident, DEFAULT_SESSION
    if kernel not in VALID_KERNELS:
        raise KernError("unsupported kernel ident; expected py, py@session, js, or js@session", 2)
    if "/" in session or "\\" in session:
        raise KernError("session name cannot contain path separators", 2)
    return KernelIdent(kernel=kernel, session=session)


def read_code(parts: list[str]) -> str:
    if parts == ["-"]:
        return sys.stdin.read()
    if parts:
        return " ".join(parts)
    if sys.stdin.isatty():
        raise KernError("missing code; pass a code string or pipe stdin", 2)
    return sys.stdin.read()


def find_scope(cwd: Path) -> Path:
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            return path.resolve()
    return cwd.resolve()


def resolve_python(cwd: Path) -> str:
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        candidate = Path(virtual_env) / "bin" / "python"
        if candidate.exists():
            return str(candidate.absolute())

    for path in [cwd, *cwd.parents]:
        candidate = path / ".venv" / "bin" / "python"
        if candidate.exists():
            return str(candidate.absolute())

    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        candidate = Path(conda_prefix) / "bin" / "python"
        if candidate.exists():
            return str(candidate.absolute())

    python = shutil.which("python3")
    if python:
        return str(Path(python).resolve())
    raise KernError("could not find python3 on PATH", 3)


def resolve_runtime(kernel: str, cwd: Path) -> Runtime:
    if kernel == "py":
        python = resolve_python(cwd)
        return Runtime(executable=python, version=python_version(python))
    if kernel == "js":
        node = shutil.which("node")
        if node is None:
            raise KernError("could not find node on PATH", 3)
        node_path = str(Path(node).resolve())
        return Runtime(executable=node_path, version=node_version(node_path))
    raise KernError(f"unsupported kernel: {kernel}", 2)


def python_version(python: str) -> str:
    result = subprocess.run(
        [python, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def node_version(node: str) -> str:
    result = subprocess.run(
        [node, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise KernError(f"failed to run {node} --version", 3)
    return result.stdout.strip()


def has_ipykernel(python: str) -> bool:
    result = subprocess.run(
        [python, "-c", "import ipykernel"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def install_ipykernel(python: str) -> None:
    print(f"installing ipykernel into {python}", file=sys.stderr)
    if has_pip(python):
        command = [python, "-m", "pip", "install", "ipykernel"]
    else:
        uv = shutil.which("uv")
        if uv is None:
            raise KernError(
                f"{python} does not have pip installed and uv is not available on PATH",
                3,
            )
        command = [uv, "pip", "install", "--python", python, "ipykernel"]
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise KernError(f"failed to install ipykernel into {python}", 3)


def has_pip(python: str) -> bool:
    result = subprocess.run(
        [python, "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def has_tslab(scope: Path) -> bool:
    return tslab_bin(scope).exists()


def install_tslab(scope: Path) -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise KernError("could not find npm on PATH; npm is required for kern --bootstrap js", 3)
    prefix = kern_home(scope) / "kernels" / "js"
    ensure_private_dir(prefix)
    print(f"installing tslab into {prefix}", file=sys.stderr)
    result = subprocess.run(
        [npm, "install", "--prefix", str(prefix), "tslab"],
        stdout=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise KernError("failed to install tslab", 3)
    patch_tslab(scope)
    expose_node_types(scope)


def patch_tslab(scope: Path) -> None:
    converter = kern_home(scope) / "kernels" / "js" / "node_modules" / "tslab" / "dist" / "converter.js"
    if not converter.exists():
        return
    text = converter.read_text(encoding="utf-8")
    marker = "kern patch: ignore mutable .kern paths"
    if marker in text:
        return

    text = text.replace(
        "function createConverter(options) {\n    const cwd = ts.sys.getCurrentDirectory();",
        "function createConverter(options) {\n"
        "    const cwd = ts.sys.getCurrentDirectory();\n"
        f"    // {marker}\n"
        "    const kernIgnoredPaths = [\n"
        "        (0, tspath_1.normalizeJoin)(cwd, \".kern\", \"runtime\"),\n"
        "        (0, tspath_1.normalizeJoin)(cwd, \".kern\", \"logs\"),\n"
        "        (0, tspath_1.normalizeJoin)(cwd, \".kern\", \"artifacts\"),\n"
        "        (0, tspath_1.normalizeJoin)(cwd, \".kern\", \"sessions.json\"),\n"
        "        (0, tspath_1.normalizeJoin)(cwd, \".kern\", \"sessions.lock\"),\n"
        "    ];\n"
        "    function isKernIgnoredPath(path) {\n"
        "        const normalized = (0, tspath_1.normalizeSlashes)(path);\n"
        "        return kernIgnoredPaths.some((ignored) => normalized === ignored || normalized.startsWith(ignored + \"/\"));\n"
        "    }",
    )
    text = text.replace(
        "    sys.readDirectory = function (path, extensions, exclude, include, depth) {\n"
        "        return ts.sys.readDirectory(forwardTslabPath(cwd, path), extensions, exclude, include, depth);\n"
        "    };",
        "    sys.readDirectory = function (path, extensions, exclude, include, depth) {\n"
        "        return ts.sys.readDirectory(forwardTslabPath(cwd, path), extensions, exclude, include, depth)\n"
        "            .filter((entry) => !isKernIgnoredPath(entry));\n"
        "    };",
    )
    text = text.replace(
        "        // Note: File watchers for real files and virtual files are mixed here.\n",
        "        if (isKernIgnoredPath(path)) {\n"
        "            return { close: () => { } };\n"
        "        }\n"
        "        // Note: File watchers for real files and virtual files are mixed here.\n",
    )
    text = text.replace(
        "    // This takes several hundreds millisecs.\n"
        "    const host = ts.createWatchCompilerHost(Array.from(rootFiles), {",
        "    sys.watchDirectory = (path, callback, recursive, options) => {\n"
        "        return ts.sys.watchDirectory(path, (fileName) => {\n"
        "            if (!isKernIgnoredPath(fileName)) {\n"
        "                callback(fileName);\n"
        "            }\n"
        "        }, recursive, options);\n"
        "    };\n"
        "    // This takes several hundreds millisecs.\n"
        "    const host = ts.createWatchCompilerHost(Array.from(rootFiles), {",
    )
    if marker not in text:
        raise KernError("failed to patch tslab converter", 3)
    converter.write_text(text, encoding="utf-8")


def expose_node_types(scope: Path) -> None:
    source = kern_home(scope) / "kernels" / "js" / "node_modules" / "@types" / "node"
    if not source.exists():
        return
    target = scope / "node_modules" / "@types" / "node"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(os.path.relpath(source, target.parent), target_is_directory=True)
    except OSError:
        shutil.copytree(source, target)


def tslab_bin(scope: Path) -> Path:
    return kern_home(scope) / "kernels" / "js" / "node_modules" / ".bin" / "tslab"


def ensure_session(scope: Path, ident: KernelIdent, runtime: Runtime) -> SessionRecord:
    session_id = make_session_id(scope, ident, runtime)
    with locked_registry(scope) as registry:
        existing = registry.get(session_id)
        if existing is not None:
            record = session_record_from_data(existing)
            if is_session_alive(record):
                return record
            registry.pop(session_id, None)

        ensure_private_dir(runtime_dir(scope))
        ensure_private_dir(log_dir(scope))
        connection_file = runtime_dir(scope) / f"{session_id}.json"
        log_file = log_dir(scope) / f"{session_id}.log"
        if ident.kernel == "js":
            write_connection_file(fname=str(connection_file), key=uuid4().hex.encode("ascii"))
        else:
            write_connection_file(fname=str(connection_file))
        chmod_private_file(connection_file)
        with log_file.open("ab") as log:
            proc = subprocess.Popen(
                kernel_command(scope, ident.kernel, runtime.executable, connection_file),
                cwd=str(scope),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        chmod_private_file(log_file)
        record = SessionRecord(
            id=session_id,
            scope=str(scope),
            kernel=ident.kernel,
            session=ident.session,
            executable=runtime.executable,
            version=runtime.version,
            pid=proc.pid,
            connection_file=str(connection_file),
            log_file=str(log_file),
            created_at=time.time(),
            last_used_at=time.time(),
        )
        registry[session_id] = asdict(record)

    try:
        client = connect_client(record, timeout=15)
        client.stop_channels()
    except KernError:
        with locked_registry(scope) as registry:
            current = registry.get(session_id)
            if current and current.get("pid") == record.pid:
                registry.pop(session_id, None)
        raise
    return record


def kernel_command(scope: Path, kernel: str, executable: str, connection_file: Path) -> list[str]:
    if kernel == "py":
        return [executable, "-m", "ipykernel_launcher", "-f", str(connection_file)]
    if kernel == "js":
        return [
            executable,
            str(tslab_bin(scope)),
            "kernel",
            "--js",
            "--config-path",
            str(connection_file),
        ]
    raise KernError(f"unsupported kernel: {kernel}", 2)


def execute(record: SessionRecord, code: str, timeout: float | None) -> ExecutionResult:
    client = connect_client(record, timeout=timeout or 15)
    events: list[OutputEvent] = []
    ok = True
    artifact_count = 0
    execution_count: int | None = None
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        msg_id = client.execute(code, allow_stdin=False, store_history=True)
        while True:
            remaining = remaining_timeout(deadline)
            try:
                msg = client.get_iopub_msg(timeout=remaining)
            except (TimeoutError, Empty):
                if deadline is None:
                    continue
                interrupt_record(record)
                raise KernError("execution timed out", 124) from None

            parent = msg.get("parent_header", {})
            if parent.get("msg_id") != msg_id:
                continue

            msg_type = msg["header"]["msg_type"]
            content = msg["content"]
            if msg_type == "status" and content.get("execution_state") == "idle":
                break
            if msg_type == "stream":
                events.append(OutputEvent(type="stream", name=content.get("name"), text=content.get("text", "")))
            elif msg_type in {"execute_result", "display_data"}:
                execution_count = content.get("execution_count", execution_count)
                data = content.get("data", {})
                artifact_events, artifact_count = save_artifacts(record, data, execution_count, artifact_count)
                events.extend(artifact_events)
                if "text/plain" in data:
                    events.append(OutputEvent(type="result", text=normalize_text(data["text/plain"]), execution_count=execution_count))
            elif msg_type == "error":
                ok = False
                traceback = "\n".join(content.get("traceback", []))
                events.append(OutputEvent(type="error", text=traceback))
            elif msg_type == "execute_input":
                execution_count = content.get("execution_count", execution_count)
        reply_timeout = 1.0 if deadline is None else max(0.1, deadline - time.monotonic())
        reply = get_shell_reply(client, msg_id, timeout=reply_timeout)
        if reply.get("status") != "ok":
            ok = False
        return ExecutionResult(ok=ok, execution_count=execution_count, events=events)
    finally:
        client.stop_channels()


def get_shell_reply(client: BlockingKernelClient, msg_id: str, timeout: float) -> dict[str, Any]:
    while True:
        try:
            msg = client.get_shell_msg(timeout=timeout)
        except (TimeoutError, Empty):
            return {}
        if msg.get("parent_header", {}).get("msg_id") == msg_id:
            return msg.get("content", {})


def connect_client(record: SessionRecord, timeout: float) -> BlockingKernelClient:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        client = BlockingKernelClient(connection_file=record.connection_file)
        client.load_connection_file()
        client.start_channels()
        try:
            client.wait_for_ready(timeout=max(0.1, min(1.0, deadline - time.monotonic())))
            return client
        except Exception as exc:
            last_error = exc
            client.stop_channels()
            if not is_pid_alive(record.pid):
                break
            time.sleep(0.1)
    detail = f"kernel did not become ready: {last_error}"
    if record.log_file:
        detail += f"\nlog: {record.log_file}"
    raise KernError(detail, 4)


def save_artifacts(
    record: SessionRecord,
    data: dict[str, Any],
    execution_count: int | None,
    artifact_count: int,
) -> tuple[list[OutputEvent], int]:
    events: list[OutputEvent] = []
    for mime, ext, binary in [
        ("image/png", "png", True),
        ("image/jpeg", "jpg", True),
        ("image/svg+xml", "svg", False),
        ("text/html", "html", False),
        ("application/json", "json", False),
    ]:
        if mime not in data:
            continue
        artifact_count += 1
        path = artifact_path(record, execution_count, artifact_count, ext)
        if binary:
            path.write_bytes(base64.b64decode(data[mime]))
        elif mime == "application/json":
            path.write_text(json.dumps(data[mime], indent=2), encoding="utf-8")
        else:
            path.write_text(normalize_text(data[mime]), encoding="utf-8")
        events.append(OutputEvent(type="artifact", mime=mime, path=str(path)))
    return events, artifact_count


def render_result(result: ExecutionResult, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(result_to_json(result)))
        return

    for event in result.events:
        if event.type == "stream":
            target = sys.stderr if event.name == "stderr" else sys.stdout
            print(event.text or "", end="", file=target)
        elif event.type == "result":
            print(event.text or "")
        elif event.type == "artifact":
            print(f"[kern artifact] {event.mime} {event.path}")
        elif event.type == "error":
            print(event.text or "", file=sys.stderr)


def result_to_json(result: ExecutionResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "execution_count": result.execution_count,
        "events": [event_to_json(event) for event in result.events],
    }


def event_to_json(event: OutputEvent) -> dict[str, Any]:
    return {key: value for key, value in asdict(event).items() if value is not None}


def stop_record(record: SessionRecord) -> None:
    if not is_pid_alive(record.pid):
        return
    try:
        os.killpg(record.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not is_pid_alive(record.pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(record.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        os.kill(record.pid, signal.SIGKILL)


def interrupt_record(record: SessionRecord) -> None:
    if not is_pid_alive(record.pid):
        return
    try:
        os.killpg(record.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    except PermissionError:
        os.kill(record.pid, signal.SIGINT)


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_session_alive(record: SessionRecord) -> bool:
    if not is_pid_alive(record.pid) or not Path(record.connection_file).exists():
        return False
    result = subprocess.run(
        ["ps", "-ww", "-p", str(record.pid), "-o", "command="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return True
    command = result.stdout
    marker = "ipykernel" if record.kernel == "py" else "tslab"
    return marker in command and record.connection_file in command


def make_session_id(scope: Path, ident: KernelIdent, runtime: Runtime) -> str:
    raw = f"{scope.resolve()}\0{ident.kernel}\0{ident.session}\0{runtime.executable}\0{runtime.version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def session_record_from_data(data: dict[str, Any]) -> SessionRecord:
    data = dict(data)
    if "kernel" not in data:
        data["kernel"] = "py"
    if "executable" not in data:
        data["executable"] = data.pop("python", "")
    else:
        data.pop("python", None)
    if "version" not in data:
        data["version"] = ""
    if "log_file" not in data:
        data["log_file"] = ""
    return SessionRecord(**data)


@contextlib.contextmanager
def locked_registry(scope: Path):
    ensure_private_dir(kern_home(scope))
    lock_path = registry_lock_path(scope)
    if not lock_path.exists():
        lock_path.touch()
    chmod_private_file(lock_path)
    with lock_path.open("r+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        registry = load_registry(scope)
        original = json.dumps(registry, sort_keys=True)
        try:
            yield registry
        finally:
            if json.dumps(registry, sort_keys=True) != original:
                save_registry(scope, registry)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_registry(scope: Path) -> dict[str, Any]:
    path = registry_path(scope)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_registry(scope: Path, registry: dict[str, Any]) -> None:
    path = registry_path(scope)
    ensure_private_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    chmod_private_file(tmp)
    tmp.replace(path)


def touch_session(record: SessionRecord) -> None:
    scope = Path(record.scope)
    with locked_registry(scope) as registry:
        current = registry.get(record.id)
        if current is None:
            return
        current["last_used_at"] = time.time()


def kern_home(scope: Path) -> Path:
    override = os.environ.get(KERN_HOME_ENV)
    if override:
        return Path(override).expanduser().absolute()
    return scope / ".kern"


def runtime_dir(scope: Path) -> Path:
    return kern_home(scope) / "runtime"


def log_dir(scope: Path) -> Path:
    return kern_home(scope) / "logs"


def registry_path(scope: Path) -> Path:
    return kern_home(scope) / "sessions.json"


def registry_lock_path(scope: Path) -> Path:
    return kern_home(scope) / "sessions.lock"


def artifact_path(record: SessionRecord, execution_count: int | None, index: int, ext: str) -> Path:
    directory = kern_home(Path(record.scope)) / "artifacts" / record.id
    directory.mkdir(parents=True, exist_ok=True)
    count = "unknown" if execution_count is None else str(execution_count).zfill(4)
    return directory / f"cell-{count}-output-{index}-{uuid4().hex[:8]}.{ext}"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def chmod_private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def remaining_timeout(deadline: float | None) -> float:
    if deadline is None:
        return 1.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def normalize_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return str(value)


def quote_code_for_hint(code: str) -> str:
    first_line = code.strip().splitlines()[0] if code.strip() else "..."
    if len(first_line) > 40:
        first_line = first_line[:37] + "..."
    return repr(first_line)


def shorten_middle(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    keep = max(0, width - 3)
    left = keep // 2
    right = keep - left
    return f"{value[:left]}...{value[-right:]}"


def runtime_label(record: SessionRecord) -> str:
    if record.version:
        return f"{record.executable} ({record.version})"
    return record.executable
