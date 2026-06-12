# Multi-transport MCP smoke test

`run_smoke.py` is the **first port of call for a support request**. It is a
deterministic, no-AI diagnostic that drives the *real* `remarkable-mcp` server
over the MCP protocol (stdio, via the official `mcp` client SDK — no models, no
mocks) and exercises **every available tool in every available transport**.

It answers one question quickly: *"does each mode this machine can reach actually
work, tool by tool?"*

## Running it

```bash
# All modes this machine can reach (cloud, then usb-web, then ssh)
uv run python smoke/run_smoke.py

# Just one mode
uv run python smoke/run_smoke.py --modes cloud
uv run python smoke/run_smoke.py --modes usb
uv run python smoke/run_smoke.py --modes ssh

# Connectivity + read checks only — no writes at all
uv run python smoke/run_smoke.py --read-only
```

Modes run in the order **cloud → usb-web → ssh** on purpose: pushing files over
SSH can reset the USB web interface, so the only filesystem-writing transport
runs last.

## Reading the result

Each `(mode, tool)` cell is one of:

| Result | Meaning |
| ------ | ------- |
| **PASS** ✅ | The tool ran and returned a sensible result for this transport. |
| **N/A** ⛔ | The transport does not expose this tool — it is *correctly hidden* (e.g. `mkdir`/`move`/`rename`/`delete` over USB web, or `remarkable_author` outside SSH). This is expected, **not** a failure. |
| **SKIP** | The tool could not be run (mode unreachable, or no target document to read). |
| **FAIL** ❌ | The tool is exposed but errored or returned the wrong thing. **This is the only result that means something is broken.** |

`OVERALL: PASS` means no exposed tool misbehaved. A mode you don't have (no cloud
token, device unplugged, SSH off) is reported as *unavailable* and all of its
tools are SKIPped, so a single-mode user still gets a clean report.

The harness is **`tools/list`-driven**: it asks each running server which tools it
actually exposes and only exercises those, so it stays correct as per-transport
tool visibility changes.

## Per-mode expectations

| Tool | cloud | usb-web | ssh |
| ---- | :---: | :-----: | :-: |
| `remarkable_status` / `browse` / `recent` / `search` / `read` / `image` / `canvas` | PASS | PASS | PASS |
| `remarkable_upload` | PASS | PASS | PASS |
| `remarkable_mkdir` / `rename` / `move` / `delete` | PASS | **N/A** | PASS |
| `remarkable_author` | **N/A** | **N/A** | PASS |

USB web is read + render + upload-to-root only — the device firmware HTTP server
exposes no folder/move/rename/delete endpoints, so those tools are not registered
in that mode (shown as N/A). `remarkable_author` requires native `.rm` write-back
and is SSH-only today.

## Caveats

- **USB upload leaves a file at the device root.** The USB web interface ignores
  the requested name/folder and has no delete endpoint, so a full (non-`--read-only`)
  USB run leaves one clearly-named `smoke-usb-<ts>-…` document on the tablet. If
  SSH is also available in the same run it is swept automatically; otherwise delete
  it from the tablet by hand. Use `--read-only` to avoid it entirely.
- **SSH requires working key auth.** With `--ssh`/`--usb` the harness disables the
  cloud startup fallback (`REMARKABLE_DISABLE_CLOUD_FALLBACK=1`) so a dead device
  can't silently "pass" via cloud. If SSH key auth isn't set up, the SSH mode waits
  for its connect timeout and is then reported as unavailable.
- Writes are confined to a unique per-run folder and cleaned up afterwards
  (cloud/SSH). The only exception is the USB-root upload noted above.

Run artifacts (`smoke/snapshots/*.json`) are written on every run and are
git-ignored.
