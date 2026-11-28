# Claude Desktop Setup Guide (macOS)

Step-by-step cloud mode setup for the Claude desktop app on macOS, written for
people who do not edit JSON configuration files often.

## Overview

Cloud mode needs a reMarkable Connect subscription and no cable or developer
mode. For the other modes, follow this guide and change the `args` line as
described in [Connection Modes](../README.md#connection-modes).

Write tools are enabled by default. To let Claude read your library without
changing it, see [read-only mode](../README.md#disabling-write-tools-read-only-mode).

## Quick Start

### 1. Install uv and Find uvx

In Terminal:

```bash
brew install uv
which uvx
```

Note the path that `which uvx` prints. You need it in step 3.

| Installed with | Typical path |
|----------------|--------------|
| Homebrew, Apple silicon | `/opt/homebrew/bin/uvx` |
| Homebrew, Intel | `/usr/local/bin/uvx` |
| Standalone installer | `/Users/yourname/.local/bin/uvx` |

### 2. Register with reMarkable

1. Go to [my.remarkable.com/device/desktop/connect](https://my.remarkable.com/device/desktop/connect) and generate a one-time code.
2. Register with it:

```bash
uvx remarkable-mcp --register YOUR_CODE
```

Registration saves the credential to `~/.rmapi`. Confirm it exists:

```bash
ls -l ~/.rmapi
```

### 3. Add the Server to Claude Desktop

Back up the configuration file, then open it in TextEdit:

```bash
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
touch "$CONFIG"
cp "$CONFIG" "$CONFIG.bak"
open -e "$CONFIG"
```

`touch` creates the file if Claude Desktop has not created it yet.

Add a `remarkable` entry, using the path from step 1 as `command`. If the file
is empty, paste this whole block:

```json
{
  "mcpServers": {
    "remarkable": {
      "command": "/opt/homebrew/bin/uvx",
      "args": ["remarkable-mcp"]
    }
  }
}
```

If the file already has an `mcpServers` object, add the `remarkable` entry
inside it, separated from the other entries by a comma.

Use the absolute path. Claude Desktop does not start from your shell, so it may
not have the same `PATH` and a bare `uvx` can fail to launch.

Check that the file is still valid JSON:

```bash
uv run --no-project python -m json.tool \
  "$HOME/Library/Application Support/Claude/claude_desktop_config.json" \
  > /dev/null && echo "Config OK"
```

### 4. Restart Claude Desktop

Quit with **Cmd+Q**, then reopen the app. Closing the window does not reload
the configuration.

### 5. Verify

In a new chat, ask:

> Check my reMarkable connection status.

Claude calls `remarkable_status`, which should report the `cloud` transport and
a document count.

## Troubleshooting

### Tools Do Not Appear

1. Quit with **Cmd+Q** and reopen. Closing the window is not enough.
2. Check that `command` is the absolute path from `which uvx`, not `uvx`.
3. Read the server log at `~/Library/Logs/Claude/mcp-server-remarkable.log`.
   `spawn uvx ENOENT` means Claude Desktop could not find `uvx`.
4. Run the configured command in Terminal. If it starts and waits for input, the
   server works; press **Ctrl+C** to stop it.

### All MCP Servers Disappeared

The configuration file is not valid JSON, usually because of a missing or extra
comma. Fix it or restore the backup from step 3, then restart Claude Desktop.

### Authentication Errors

1. Check that `~/.rmapi` exists.
2. Generate a new one-time code and register again.
3. Confirm the account has a Connect subscription, which cloud mode requires.

### Stopped Working After Moving to Another Mac

The configuration entry and the `uvx` path are per machine. Repeat steps 1, 3,
and 4 on the new Mac. Repeat step 2 only if `~/.rmapi` did not carry over.

## Security Considerations

- `~/.rmapi` grants read and write access to your reMarkable library. Do not
  share it or commit it to a repository.
- The server runs on your Mac. Claude can reach your library only while the
  Claude desktop app is open on that machine.
