# USB Web Interface Setup Guide

The USB web interface is the **easiest way** to connect remarkable-mcp to your tablet. It requires no developer mode, no subscription, and works entirely offline.

## Overview

The USB web interface is an official reMarkable feature that provides HTTP API access when your tablet is connected via USB. This gives you:

- ✅ **No developer mode required** — Keep your warranty and avoid factory reset
- ✅ **No subscription needed** — Works without reMarkable Connect
- ✅ **Fast offline access** — Direct USB connection
- ✅ **Officially supported** — Part of the standard reMarkable OS
- ✅ **Simple setup** — Just enable in Settings and connect

## Quick Start

### 1. Enable USB Web Interface

On your reMarkable tablet:
1. Go to **Settings** (gear icon)
2. Tap **Storage**
3. Toggle **USB web interface** to **On**

### 2. Connect via USB

1. Connect your reMarkable to your computer using the USB-C cable
2. Make sure the tablet is **on and unlocked**
3. Your computer should recognize it as a USB Ethernet device

### 3. Verify Connection

Open a web browser and go to: [http://10.11.99.1](http://10.11.99.1)

You should see the reMarkable web interface showing your documents.

### 4. Configure MCP Server

Add to your `.vscode/mcp.json`:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--usb-web"],
      "env": {
        "GOOGLE_VISION_API_KEY": "your-api-key-if-needed"
      }
    }
  }
}
```

Or run directly:
```bash
uvx remarkable-mcp --usb-web
```

## Troubleshooting

### Cannot Connect to 10.11.99.1

**Symptoms:** Browser shows "Cannot connect" or "Connection refused"

**Solutions:**
1. **Check tablet is on and unlocked** — The web interface only works when the tablet is active
2. **Verify USB connection** — Try a different USB port or cable
3. **Check USB web interface is enabled** — Settings → Storage → USB web interface
4. **Check network interface** — The tablet creates a USB Ethernet device (usually `usb0` on Linux)

On Linux, verify the interface:
```bash
ip addr show usb0
# Should show: inet 10.11.99.2/29
```

On macOS, check System Preferences → Network for the USB device.

On Windows, check Network Connections for "USB Ethernet/RNDIS Gadget".

### Connection Times Out

**Symptoms:** Request starts but never completes

**Solutions:**
1. **Check firewall settings** — Allow connections to 10.11.99.1
2. **Restart the tablet** — Turn off and on again
3. **Reconnect USB** — Unplug and plug back in
4. **Try a different USB port** — Preferably a direct port, not a hub

### Documents Don't Appear

**Symptoms:** Connection works but no documents are listed

**Solutions:**
1. **Check sync status** — Make sure documents are synced to the device (not cloud-only)
2. **Restart remarkable-mcp** — The server caches document listings
3. **Check tablet storage** — Settings → Storage → check free space

### Slow Performance

**Symptoms:** Listing/downloading documents takes a long time

**Solutions:**
1. **USB connection quality** — Use a high-quality USB-C cable
2. **Large library** — First load with many documents is slower, subsequent loads are cached
3. **USB 2.0 vs 3.0** — USB 3.0 ports are faster

## How It Works

The reMarkable USB web interface provides several HTTP endpoints:

- `GET /documents/` — List all documents
- `GET /documents/{guid}` — List documents in a folder
- `GET /download/{guid}/rmdoc` — Download document as `.rmdoc` archive
- `GET /download/{guid}/pdf` — Download document as PDF
- `POST /upload` — Upload new documents

The remarkable-mcp USB web client:
1. Recursively fetches document listings from all folders
2. Builds a complete document tree
3. Downloads documents using the `/download/{guid}/rmdoc` endpoint
4. Extracts text, annotations, and metadata

## Environment Variables

Customize USB web interface behavior:

```bash
# Change the USB host (default: http://10.11.99.1)
export REMARKABLE_USB_HOST="http://192.168.1.100:8080"

# Adjust timeout in seconds (default: 10)
export REMARKABLE_USB_TIMEOUT="30"
```

## Comparison: USB Web vs SSH vs Cloud

| Feature | USB Web | SSH | Cloud |
|---------|---------|-----|-------|
| **Setup** | ✅ Easy | ⚠️ Complex | ✅ Easy |
| **Developer Mode** | ✅ Not required | ❌ Required | ✅ Not required |
| **Factory Reset** | ✅ No | ❌ Yes | ✅ No |
| **Subscription** | ✅ Not required | ✅ Not required | ❌ Required |
| **Speed** | ✅ Fast | ✅✅ Very Fast | ⚠️ Slow |
| **Offline** | ✅ Yes | ✅ Yes | ❌ No |
| **Connection** | USB only | USB only | Internet |
| **Stability** | ✅ Good | ✅ Good | ⚠️ Variable |

**Recommendation:**
- **Most users:** USB Web Interface — Best balance of ease and performance
- **Advanced users:** SSH — Fastest, but requires developer mode
- **Remote access:** Cloud API — Works anywhere, but slower and needs subscription

## Technical Details

### Network Configuration

When you enable USB web interface:
1. reMarkable creates a USB Ethernet gadget device
2. The tablet assigns itself `10.11.99.1/29`
3. Your computer gets `10.11.99.2/29` (via DHCP or auto-config)
4. A web server starts on port 80 on the tablet

### Security Considerations

- **No authentication** — Anyone with USB access can read/write documents
- **Local only** — Only accessible over USB, not remotely
- **HTTP, not HTTPS** — Data is not encrypted (but local to USB)

For better security:
- Keep your computer locked when connected
- Disable USB web interface when not in use
- Consider SSH mode for encrypted connection

### API Endpoint Reference

Complete list of available endpoints:

```
GET  /documents/              - List root documents
GET  /documents/{guid}        - List folder contents
GET  /download/{guid}/pdf     - Download as PDF
GET  /download/{guid}/rmdoc   - Download as .rmdoc (firmware 3.9+)
POST /upload                  - Upload document (multipart form)
GET  /thumbnail/{guid}        - Get document thumbnail
GET  /log.txt                 - Download system logs
```

See [reMarkable Guide](https://remarkable.guide/tech/usb-web-interface.html) for more details.

## Alternative Tools

Other tools that use the USB web interface:

- [reMarkable-Offline-Sync](https://github.com/ChrWesp/reMarkable-Offline-Sync) — Python sync tool
- [rmfakecloud](https://github.com/ddvk/rmfakecloud) — Self-hosted cloud replacement
- Browser — Direct access at `http://10.11.99.1` for manual file management

## Support

If you encounter issues:

1. Check this guide's [Troubleshooting](#troubleshooting) section
2. Verify basic connectivity: `curl http://10.11.99.1/documents/`
3. Check [reMarkable Guide](https://remarkable.guide/tech/usb-web-interface.html)
4. Open an issue on [GitHub](https://github.com/SamMorrowDrums/remarkable-mcp/issues)
