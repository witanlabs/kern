## kern

`kern` is a prototype CLI for persistent kernel execution from shell-based agent
sandboxes. It lets an agent submit code to a long-running Jupyter-style kernel
from ordinary shell commands, so later calls can reuse variables, imports, data
frames, plotting state, and JavaScript module state.

The initial scope is deliberately narrow:

- Python, addressed as `py` or `py@session`
- JavaScript, addressed as `js` or `js@session`, backed by TSLab JS mode
- project-local state under `.kern/`
- opt-in bootstrap for missing runtime support
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
uv run kern --bootstrap js '1 + 1'
uv run kern stop py
uv run kern stop js
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

JavaScript sessions run in the project root and can use project `node_modules`:

```bash
kern js 'var value = 41; value'
kern js 'value + 1'
```

```text
42
```

TSLab JS mode supports both CommonJS and ESM syntax:

```bash
kern js 'const fs = require("node:fs"); fs.existsSync("package.json")'
kern js 'import path from "node:path"; path.basename("a/b.txt")'
kern js 'const mod = await import("node:path"); mod.basename("a/b.txt")'
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

The test suite creates temporary projects with their own `.venv` directories,
real `ipykernel` processes, and TSLab-backed JS kernels. Tests should stop their
kernels during cleanup; if a run is interrupted, inspect and remove temporary
`.kern/` directories or stop leftover kernels manually.
