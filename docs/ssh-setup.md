# SSH Setup Guide

SSH mode connects directly to your reMarkable tablet over USB, providing:

- **10-100x faster** document access than Cloud API
- **Offline operation** — no internet required
- **No subscription needed** — works without reMarkable Connect
- **Raw file access** — get original PDFs and EPUBs

## Requirements

### 1. Enable Developer Mode

Developer mode is required to enable SSH access on your reMarkable.

> ⚠️ **Warning:** Enabling developer mode will **factory reset** your device. Make sure your documents are synced to the cloud before proceeding.

Follow the official instructions to enable developer mode:

- **[Official reMarkable Support: Developer Mode](https://support.remarkable.com/s/article/Developer-mode)** — Official guide from reMarkable
- **[reMarkable Guide: Developer Mode](https://remarkable.guide/tech/developer-mode.html)** — Community documentation with additional context

### 2. USB Connection

Connect your reMarkable to your computer via the USB-C cable.

- The tablet must be **on and unlocked**
- Default IP over USB: `10.11.99.1`
- Your SSH password is shown in **Settings → General → Software → Developer mode**

### 3. Verify SSH Access

Test the connection:

```bash
ssh root@10.11.99.1
# Enter the password shown in Developer mode settings
```

You should see a shell prompt on your reMarkable.

## Configuration

### Basic Setup

Add to your VS Code MCP config (`.vscode/mcp.json`):

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"],
      "env": {
        "GOOGLE_VISION_API_KEY": "your-api-key"
      }
    }
  }
}
```

That's it! The default connection (`root@10.11.99.1`) works for USB connections.

### Password Authentication

If you haven't set up SSH keys, you can use password authentication **(not recommended)**:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"],
      "env": {
        "REMARKABLE_SSH_PASSWORD": "your-ssh-password",
        "GOOGLE_VISION_API_KEY": "your-api-key"
      }
    }
  }
}
```

> ⚠️ **Requires sshpass:** Password authentication requires `sshpass` to be installed:
> - **Debian/Ubuntu:** `sudo apt install sshpass`
> - **macOS:** `brew install hudochenkov/sshpass/sshpass`
> - **Fedora:** `sudo dnf install sshpass`

> 🔐 **Security Recommendation:** Password authentication stores your password in plain text in your config file. For better security, set up SSH key authentication instead (see below).

### SSH Key Authentication (Recommended)

SSH keys are more secure than passwords and don't require `sshpass`:

```bash
# Generate an SSH key if you don't have one
ssh-keygen -t ed25519

# Copy your key to the tablet
ssh-copy-id root@10.11.99.1
```

Once your key is set up, you don't need to specify a password in your config.

#### Passphrase-Protected Keys

If your SSH key has a passphrase, you'll need an **SSH agent** running to cache the passphrase. Without an agent, the MCP server can't prompt for your passphrase interactively.

**Using ssh-agent:**
```bash
# Start ssh-agent (add to your shell profile)
eval "$(ssh-agent -s)"

# Add your key (will prompt for passphrase once)
ssh-add ~/.ssh/id_ed25519
```

**Password managers with SSH agent support:**

Some password managers provide built-in SSH agents, letting you use passphrase-protected keys across all your devices:

- **[1Password SSH Agent](https://developer.1password.com/docs/ssh/)** — Stores SSH keys in your vault, prompts via 1Password GUI when needed
- **[Secretive](https://github.com/maxgoedjen/secretive)** (macOS) — Stores keys in Secure Enclave with Touch ID
- **[KeePassXC](https://keepassxc.org/docs/KeePassXC_UserGuide#_ssh_agent_integration)** — Open-source with SSH agent integration

These integrate seamlessly — the agent handles authentication automatically, and you get the security benefits of passphrase-protected keys without manual setup.

> ⚠️ **Headless servers and interactive agents:** Agents like 1Password sign keys only after **interactive approval** (a GUI prompt or biometric). When the MCP server runs somewhere that approval can't be shown — a remote box, CI, or an automated agent — that prompt never appears and SSH calls **hang** until they time out. In that case, pin an unencrypted on-disk key instead:
>
> ```json
> {
>   "servers": {
>     "remarkable": {
>       "command": "uvx",
>       "args": ["remarkable-mcp", "--ssh", "--ssh-key", "~/.ssh/id_ed25519"],
>       "env": { "GOOGLE_VISION_API_KEY": "your-api-key" }
>     }
>   }
> }
> ```
>
> `--ssh-key` (or the `REMARKABLE_SSH_KEY` env var) makes ssh use **only** that key (`IdentitiesOnly=yes`) and ignore the agent, so authentication never blocks. Make sure that key's public half is in the tablet's `~/.ssh/authorized_keys` (e.g. via `ssh-copy-id`).

### SSH Config Alias

For convenience, add to `~/.ssh/config`:

```
Host remarkable
    HostName 10.11.99.1
    User root
    # Optional: specify your key
    IdentityFile ~/.ssh/id_ed25519
```

Then use the alias in your MCP config:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"],
      "env": {
        "REMARKABLE_SSH_HOST": "remarkable",
        "GOOGLE_VISION_API_KEY": "your-api-key"
      }
    }
  }
}
```

### WiFi Connection

You can also connect over WiFi if your tablet and computer are on the same network:

1. Find your tablet's IP in **Settings → General → About → IP address**
2. Use that IP as `REMARKABLE_SSH_HOST`

Note: WiFi is slower than USB but works from anywhere on your network.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REMARKABLE_SSH_HOST` | `10.11.99.1` | SSH hostname or IP address |
| `REMARKABLE_SSH_USER` | `root` | SSH username |
| `REMARKABLE_SSH_PORT` | `22` | SSH port |
| `REMARKABLE_SSH_PASSWORD` | *(none)* | SSH password (requires `sshpass`, key auth recommended) |
| `REMARKABLE_SSH_KEY` | *(none)* | Path to a private key for key auth. Pins this on-disk identity (`IdentitiesOnly`) and ignores any ssh-agent. |
| `REMARKABLE_SSH_MAX_ATTEMPTS` | `4` | Maximum attempts for failures proven to occur before remote execution |
| `REMARKABLE_SSH_BACKOFF_INITIAL` | `0.5` | Initial retry delay in seconds |
| `REMARKABLE_SSH_BACKOFF_MULTIPLIER` | `2` | Exponential retry multiplier |
| `REMARKABLE_SSH_BACKOFF_MAX` | `2` | Maximum delay between SSH attempts |
| `REMARKABLE_SSH_WAKE_GRACE` | `8` | Total seconds allowed for bounded wake/reconnect retries |
| `REMARKABLE_SSH_REFRESH_DEBOUNCE` | `0.15` | Stable window used to coalesce concurrent writes into one refresh |
| `REMARKABLE_SSH_REFRESH_MAX_WAIT` | `1` | Hard maximum seconds before a write generation closes |
| `REMARKABLE_SSH_SHUTDOWN_TIMEOUT` | `5` | Maximum seconds to drain/terminate SSH work during server shutdown |
| `REMARKABLE_RESTART_TIMEOUT` | `60` | Max seconds to confirm stable fresh SSH sessions, unchanged boot ID, and any required USB web recovery after restart |
| `REMARKABLE_RESTART_POLL_INTERVAL` | `1` | Seconds between fresh SSH readiness probes while waiting for `xochitl` |
| `REMARKABLE_RESTART_SETTLE` | `3` | Stable interval between the first ready probe and the required confirmation probe |
| `REMARKABLE_DEFER_RESTART` | *(unset)* | When set (`1`/`true`/`yes`), write tools skip their per-write `xochitl` restart; call `remarkable_refresh` once to apply a whole batch with a single restart |
| `REMARKABLE_USB_MAX_CONCURRENCY` | `2` | Maximum concurrent requests to the USB HTTP interface |

Direct SSH writes bypass the live library control plane used by cloud sync and
the USB web upload service. Stock firmware exposes no external library reload
API, and xochitl does not automatically ingest raw metadata/filesystem changes.
That is why a refresh restart is still required after an SSH write batch. The
server serializes all SSH subprocesses through one FIFO dispatcher. Concurrent
non-deferred writes form a bounded generation: every mutation completes, one
participant restarts `xochitl`, and all callers wait for that same refresh
before returning success.

`remarkable_refresh` performs one restart for all writes explicitly deferred
with `defer_restart=True`; it remains preferable for a known large batch. It
does not restart `xochitl` when the current server process has already confirmed
that no deferred work is pending. A newly started process performs one
conservative explicit refresh because process-local memory cannot prove that a
previous process left no pending disk changes.

The refresh runs entirely inside the serialized SSH dispatcher. Immediately
before restart it clears only `xochitl`'s systemd start counter; this avoids the
Paper Pro firmware's four-starts-per-ten-minutes limit, whose failure target
reboots the tablet. Success requires two fresh SSH sessions separated by the
settle interval and the same kernel boot ID. These probes explicitly disable
OpenSSH connection multiplexing, so an existing control socket cannot mask a
failed new session. OpenSSH aliases are resolved with `ssh -G`; port 80 at the
effective `HostName` is also required after restart only when it was reachable
before it. A failure is reported as persisted-but-unrefreshed with
`refresh_pending: true`, so do not repeat the mutation automatically. Wake and
unlock a rebooted or sleeping tablet, then retry only the refresh.

If a deferred write completes while a refresh is already in progress, that
newer dirty epoch remains pending and the response requests one additional
refresh. Multi-step writes also retain cumulative dirty state: cancellation or
an error after an earlier successful mutation cannot turn the operation clean.
Those failures return `write_partially_persisted` with do-not-repeat guidance
and the actual pending state. A shared refresh failure does not replace the
original result of a participant whose own mutation remained clean.

Only failures that prove no remote command started are retried. Generic SSH
exit 255, authentication failures, connection resets after session start,
remote command failures, and local process timeouts are not replayed because a
write may already be on disk. `remarkable_status` exposes queue depth, the
active operation, retry counts, refresh generation state, and refresh count.

## Troubleshooting

### "Connection refused"

- Make sure developer mode is enabled
- Verify the tablet is connected via USB and unlocked
- Check that the IP is correct (`10.11.99.1` for USB)
- The server waits briefly with bounded exponential backoff so a sleeping tablet
  can wake. The final error reports the attempts and elapsed grace window.

### "Permission denied"

- Double-check the password from Settings → Developer mode
- If using SSH keys, ensure they're set up correctly
- If your key has a passphrase, make sure ssh-agent is running and your key is added (`ssh-add -l` to check)

### "Connection timed out"

- The tablet may be asleep — tap the screen to wake it
- Try unplugging and reconnecting the USB cable
- Restart the tablet if issues persist

### Commands hang, then time out

If SSH connects but every command stalls for the full timeout, an **interactive ssh-agent** (e.g. 1Password) is likely holding the authorized key and waiting for approval the server can't show. Pin an unencrypted on-disk key with `--ssh-key ~/.ssh/id_ed25519` (or `REMARKABLE_SSH_KEY`) to bypass the agent. See [SSH Key Authentication](#ssh-key-authentication-recommended).

### Slow Performance

- USB is always faster than WiFi
- Make sure you're not running other heavy SSH sessions
- Check that your tablet isn't in the middle of a sync

## SSH vs Cloud API Comparison

| Feature | SSH Mode | Cloud API |
|---------|----------|-----------|
| Speed | ⚡ 10-100x faster | Slower |
| Offline | ✅ Yes | ❌ No |
| Subscription | ✅ Not required | ❌ Connect required |
| Raw files | ✅ PDFs, EPUBs | ❌ Not available |
| Setup | Developer mode | One-time code |

## Security Notes

- SSH access gives full root access to your tablet
- The default password is visible in settings — change it if concerned
- USB connection is local-only; WiFi exposes SSH on your network
- Consider firewall rules if using WiFi SSH

## Further Reading

- [Remarkable Guide: SSH Access](https://remarkable.guide/guide/access/ssh.html) — Comprehensive community guide
- [reMarkable Wiki](https://remarkablewiki.com/) — Community knowledge base
