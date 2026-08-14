# USB Web Interface Setup Guide

The USB web interface provides read, render, and root-folder upload over USB.

## Overview

It does not require a Connect subscription or developer mode.

## Quick Start

### 1. Enable USB Web Interface

On your reMarkable tablet:
1. Open **Settings**
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
      "args": ["remarkable-mcp", "--usb"],
      "env": {
        "GOOGLE_VISION_API_KEY": "your-api-key-if-needed"
      }
    }
  }
}
```

Or run directly:
```bash
uvx remarkable-mcp --usb
```

## Troubleshooting

### Cannot Connect to 10.11.99.1

Symptoms: the browser shows "Cannot connect" or "Connection refused".

1. Check that the tablet is on and unlocked.
2. Try a different USB port or cable.
3. Check **Settings > Storage > USB web interface**.
4. Check that a USB Ethernet interface appeared.

On Linux, verify the interface:
```bash
ip -brief address
# Look for an interface with an address in the tablet's 10.11.99.0 network.
```

On macOS, check System Settings > Network for the USB device.

On Windows, check Network Connections for "USB Ethernet/RNDIS Gadget".

### Connection Times Out

Symptoms: a request starts but does not complete.

1. Allow connections to `10.11.99.1` in the host firewall.
2. Retry. The client retries bounded tablet-generated HTTP 408 responses for safe GET requests.
3. Disable and re-enable the USB web interface.
4. Reconnect USB or restart the tablet if 408 responses continue.
5. Use a direct USB port rather than a hub.

### Documents Don't Appear

1. Make sure documents are stored on the device, not cloud-only.
2. Restart remarkable-mcp to rebuild the cached listing.
3. Check free storage on the tablet.

### Slow Performance

1. Use a reliable USB-C cable.
2. Allow the first full-library load to finish.
3. Avoid concurrent tools that scan a large library.

## How It Works

The reMarkable USB web interface provides several HTTP endpoints:

- `GET /documents/`: list root documents
- `GET /documents/{guid}`: list a folder
- `GET /download/{guid}/rmdoc`: download an `.rmdoc` archive
- `GET /download/{guid}/pdf`: download a PDF export
- `POST /upload`: upload a document

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

## Technical Details

### Network Configuration

When you enable USB web interface:
1. reMarkable creates a USB Ethernet gadget device
2. The tablet uses `10.11.99.1`
3. The host receives an address on the same USB network; the prefix varies by device and firmware
4. A web server starts on port 80 on the tablet

### Security Considerations

- There is no HTTP authentication.
- The endpoint is available only through the USB network.
- Traffic is plain HTTP.

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

- [reMarkable-Offline-Sync](https://github.com/ChrWesp/reMarkable-Offline-Sync): Python sync tool
- [rmfakecloud](https://github.com/ddvk/rmfakecloud): self-hosted cloud replacement
- Browser access at `http://10.11.99.1`

## Support

If you encounter issues:

1. Check this guide's [Troubleshooting](#troubleshooting) section
2. Verify basic connectivity: `curl http://10.11.99.1/documents/`
3. Check [reMarkable Guide](https://remarkable.guide/tech/usb-web-interface.html)
4. Open an issue on [GitHub](https://github.com/SamMorrowDrums/remarkable-mcp/issues)
