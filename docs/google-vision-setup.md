# Google Cloud Vision Setup

Google Cloud Vision provides **far superior handwriting recognition** compared to Tesseract. Unless your handwriting is exceptionally clear and print-like, we strongly recommend using Google Vision.

## Why Google Vision?

| Feature | Google Vision | Tesseract |
|---------|---------------|-----------|
| Handwriting | ✅ Excellent | ❌ Poor |
| Cursive | ✅ Handles well | ❌ Fails |
| Mixed content | ✅ Text + drawings | ❌ Confused |
| Languages | ✅ Auto-detect | Manual config |
| Setup | API key | System install |

## Quick Setup (API Key)

The easiest way to get started:

### 1. Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** → **New Project**
3. Name it (e.g., "remarkable-ocr") and click **Create**

### 2. Enable the Vision API

1. Go to [Cloud Vision API](https://console.cloud.google.com/apis/library/vision.googleapis.com)
2. Click **Enable**

### 3. Create an API Key

1. Go to [Credentials](https://console.cloud.google.com/apis/credentials)
2. Click **Create Credentials** → **API Key**
3. Copy the key

### 4. (Optional) Restrict the Key

For security, restrict the API key:

1. Click on the API key you just created
2. Under **API restrictions**, select **Restrict key**
3. Select only **Cloud Vision API**
4. Click **Save**

### 5. Configure MCP

Add the key to your MCP configuration:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"],
      "env": {
        "GOOGLE_VISION_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

## Cost

Google Cloud Vision pricing:

| Tier | Price |
|------|-------|
| First 1,000 requests/month | **Free** |
| 1,001 - 5,000,000 requests | $1.50 per 1,000 |

For personal use, you'll likely stay within the free tier. Each notebook page counts as one request when OCR is enabled.

## Alternative: Service Account Credentials

For production use or tighter security controls:

### 1. Create a Service Account

1. Go to [Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Click **Create Service Account**
3. Name it and click **Create**
4. Grant the role **Cloud Vision API User**
5. Click **Done**

### 2. Download Credentials

1. Click on the service account
2. Go to **Keys** tab
3. Click **Add Key** → **Create new key**
4. Select **JSON** and click **Create**
5. Save the downloaded file securely

### 3. Configure MCP

Point to your credentials file:

```json
{
  "servers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/your/credentials.json"
      }
    }
  }
}
```

Or install the SDK with default credentials:

```bash
# Install gcloud CLI and authenticate
gcloud auth application-default login

# Install SDK dependency
pip install remarkable-mcp[ocr]
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GOOGLE_VISION_API_KEY` | API key (simplest setup) |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to service account JSON |
| `REMARKABLE_OCR_BACKEND` | Force backend: `auto`, `google`, `tesseract` |

## Troubleshooting

### "API key not valid"

- Double-check the key is copied correctly
- Ensure the Vision API is enabled for your project
- Check that API restrictions (if any) include Vision API

### "Permission denied"

- For service accounts, verify the Cloud Vision API User role
- Check that the credentials file path is correct

### "Quota exceeded"

- You've exceeded the free tier
- Enable billing or wait until next month
- Consider caching results (remarkable-mcp does this automatically)

### OCR Not Running

- Check `remarkable_status()` to see which OCR backend is configured
- Set `REMARKABLE_OCR_BACKEND=google` to force Google Vision
- Ensure `include_ocr=True` when calling `remarkable_read()`

## Performance Tips

1. **Caching**: OCR results are cached per document — reading multiple pages doesn't re-run OCR
2. **Selective OCR**: Only enable `include_ocr` when you need handwritten content
3. **Typed text**: Notebooks with Type Folio extract text without OCR
4. **PDFs**: Text is extracted directly, OCR only needed for scanned documents

## Privacy Considerations

When using Google Vision:
- Your handwritten content is sent to Google's servers for processing
- Google's [Cloud Vision Terms](https://cloud.google.com/terms/) apply
- Consider this when processing sensitive documents

For fully offline OCR, use Tesseract (with significantly reduced accuracy for handwriting).
