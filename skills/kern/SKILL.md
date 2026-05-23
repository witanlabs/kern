---
name: kern
description: Use persistent Python Jupyter kernels from shell sandboxes with the `kern` CLI. Use when Codex needs stateful Python execution across tool calls, exploratory data analysis with pandas, plotting or rich visual outputs, incremental debugging, or REPL-like work where variables/imports/results should persist between shell commands.
---

# Kern

Use `kern` when persistent Python state is useful. Prefer plain `python` for one-off scripts that do not need retained variables, rich outputs, or notebook-like iteration.

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

## Session Management

Use these commands to inspect or reset running sessions:

```bash
kern ls
kern restart py
kern stop py
kern stop py@scratch
```

Restart clears Python memory but leaves `.kern/` files and artifacts. Stop terminates the running kernel session.

## Storage

By default, `kern` stores runtime files in the current project:

```text
.kern/
  sessions.json
  sessions.lock
  runtime/
  artifacts/
  logs/
```

Set `KERN_HOME` to put this state somewhere else:

```bash
KERN_HOME=/tmp/kern-state kern py 'x + 1'
```

Add `.kern/` to `.gitignore` in projects where `kern` is used.
