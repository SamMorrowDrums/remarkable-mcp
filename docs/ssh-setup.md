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

## Troubleshooting

### "Connection refused"

- Make sure developer mode is enabled
- Verify the tablet is connected via USB and unlocked
- Check that the IP is correct (`10.11.99.1` for USB)

### "Permission denied"

- Double-check the password from Settings → Developer mode
- If using SSH keys, ensure they're set up correctly
- If your key has a passphrase, make sure ssh-agent is running and your key is added (`ssh-add -l` to check)

### "Connection timed out"

- The tablet may be asleep — tap the screen to wake it
- Try unplugging and reconnecting the USB cable
- Restart the tablet if issues persist

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
