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

1. Go to **Settings → General → Software**
2. Tap **Developer mode**
3. Follow the prompts to enable it
4. Your device will reset and restart

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

### Passwordless SSH (Recommended)

Avoid typing your password every time:

```bash
# Generate an SSH key if you don't have one
ssh-keygen -t ed25519

# Copy your key to the tablet
ssh-copy-id root@10.11.99.1
```

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

## Troubleshooting

### "Connection refused"

- Make sure developer mode is enabled
- Verify the tablet is connected via USB and unlocked
- Check that the IP is correct (`10.11.99.1` for USB)

### "Permission denied"

- Double-check the password from Settings → Developer mode
- If using SSH keys, ensure they're set up correctly

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
