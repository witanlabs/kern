---
name: kern
description: Use persistent Python or JavaScript Jupyter-style kernels from shell sandboxes with the `kern` CLI. Use when Codex needs stateful execution across tool calls, exploratory data analysis with pandas, plotting or rich visual outputs, Node.js package/module exploration, incremental debugging, or REPL-like work where variables/imports/results should persist between shell commands.
---

# Kern

Use `kern` when persistent Python or JavaScript state is useful. Prefer plain `python` or `node` for one-off scripts that do not need retained variables, rich outputs, or notebook-like iteration.

## Basic Pattern

Run code in the default project-scoped Python session:

```bash
kern py 'x = 41'
kern py 'x + 1'
```

Expected output from the second command:

```text
42
```

Use stdin for multiline code:

```bash
kern py <<'PY'
values = [1, 2, 3]
sum(values)
PY
```

Use a named session when the work should be isolated:

```bash
kern py@scratch 'x = 100'
kern py@scratch 'x + 1'
```

## Bootstrap

If the selected project Python lacks `ipykernel`, run once with `--bootstrap`:

```bash
kern --bootstrap py '1 + 1'
```

`--bootstrap` installs kernel support only. It does not install user packages such as `pandas`, `numpy`, or `matplotlib`.

For JavaScript, run once with `--bootstrap` if TSLab has not been installed for the project:

```bash
kern --bootstrap js '1 + 1'
```

`kern js` uses Node.js via TSLab JS mode. It runs with the project root as `process.cwd()` and can resolve packages from the project's `node_modules`.

## Data Work

Keep imports and dataframes alive across calls:

```bash
kern py <<'PY'
import pandas as pd

df = pd.DataFrame({
    "team": ["api", "web", "infra"],
    "latency_ms": [142, 118, 167],
})
df
PY

kern py 'df.sort_values("latency_ms")'
```

Expected dataframe output includes the text form and may include an HTML artifact:

```text
[kern artifact] text/html /repo/.kern/artifacts/<session>/cell-0001-output-1-ab12cd34.html
    team  latency_ms
1    web         118
0    api         142
2  infra         167
```

Rich objects such as dataframes may print `text/plain` and also save richer artifacts such as `text/html`. Use `to_json()` or `print(...)` when you need a specific text format.

For machine-readable output, use JSON mode and print JSON from Python:

```bash
kern --json py <<'PY'
import json
print(json.dumps(df.to_dict(orient="records")))
PY
```

Expected JSON shape:

```json
{
  "ok": true,
  "execution_count": 3,
  "events": [
    {"type": "stream", "text": "[{\"team\": \"api\", \"latency_ms\": 142}, ...]\n", "name": "stdout"}
  ]
}
```

## Plots And Rich Outputs

Visual outputs are saved under the project `.kern/artifacts/` directory and printed as absolute paths:

```bash
kern py <<'PY'
import matplotlib.pyplot as plt

plt.plot([1, 2, 3], [1, 4, 9])
plt.title("growth")
plt.show()
PY
```

When `kern` prints a line like this, inspect the path with image-reading tools:

```text
[kern artifact] image/png /repo/.kern/artifacts/<session>/cell-0004-output-1-ab12cd34.png
```

## JavaScript Work

Use `kern js` for persistent Node.js exploration:

```bash
kern js 'var value = 41; value'
kern js 'value + 1'
```

Expected output from the second command:

```text
42
```

CommonJS, ESM imports, and top-level await are supported:

```bash
kern js 'const fs = require("node:fs"); fs.existsSync("package.json")'
kern js 'import path from "node:path"; path.basename("a/b.txt")'
kern js 'const mod = await import("node:path"); mod.basename("a/b.txt")'
```

Expected outputs:

```text
true
b.txt
b.txt
```

Use project packages normally after installing them in the project:

```bash
npm install left-pad
kern js 'const leftPad = require("left-pad"); leftPad("x", 3, "0")'
```

Expected output:

```text
00x
```

JavaScript mode is not TypeScript mode. Do not use TS annotations with `kern js`.

## Session Management

Use these commands to inspect or reset running sessions:

```bash
kern ls
kern restart py
kern restart js
kern stop py
kern stop py@scratch
kern stop js
```

Restart clears kernel memory but leaves `.kern/` files and artifacts. Stop terminates the running kernel session.

## Storage

By default, `kern` stores runtime files in the current project:

```text
.kern/
  sessions.json
  sessions.lock
  runtime/
  artifacts/
  logs/
  kernels/js/
```

Set `KERN_HOME` to put this state somewhere else:

```bash
KERN_HOME=/tmp/kern-state kern py 'x + 1'
```

Add `.kern/` to `.gitignore` in projects where `kern` is used.
