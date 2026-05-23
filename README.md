## kern

`kern` is a prototype CLI for persistent Python execution from shell-based agent
sandboxes. It lets an agent submit code to a long-running Jupyter `ipykernel`
from ordinary shell commands, so later calls can reuse variables, imports, data
frames, and plotting state.

The initial scope is deliberately narrow:

- Python only, addressed as `py` or `py@session`
- project-local state under `.kern/`
- opt-in bootstrap for missing `ipykernel`
- text output to stdout/stderr
- rich outputs saved as files for later inspection

Agent usage guidance lives in [skills/kern/SKILL.md](skills/kern/SKILL.md).

## Development

This repository is managed with `uv`.

```bash
uv sync
uv run pytest -q
```

Run the local development copy:

```bash
uv run kern py '1 + 1'
uv run kern stop py
```

Example output shapes:

```bash
kern py 'x = 41'
kern py 'x + 1'
```

```text
42
```

Rich outputs are additive: `text/plain` is printed when available, and richer
MIME outputs are saved as artifacts.

```text
[kern artifact] text/html /repo/.kern/artifacts/<session>/cell-0001-output-1-ab12cd34.html
    team  latency_ms
0    api         142
1    web         118
```

Plots and images are saved as files:

```text
[kern artifact] image/png /repo/.kern/artifacts/<session>/cell-0004-output-1-ab12cd34.png
```

JSON mode emits one JSON object:

```json
{
  "ok": true,
  "execution_count": 1,
  "events": [
    {"type": "stream", "text": "hello\n", "name": "stdout"}
  ]
}
```

The test suite creates temporary projects with their own `.venv` directories and
real `ipykernel` processes. Tests should stop their kernels during cleanup; if a
run is interrupted, inspect and remove temporary `.kern/` directories or stop
leftover kernels manually.
