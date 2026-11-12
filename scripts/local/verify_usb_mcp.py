"""Read-only protocol checks against the running local Streamable HTTP server."""
import argparse
import base64
import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'verification'
URL = 'http://127.0.0.1:8000/mcp'


class MCP:
    def __init__(self):
        self.http = requests.Session()
        self.http.trust_env = False
        self.headers = {'Accept': 'application/json, text/event-stream'}
        self.next_id = 0

    def request(self, method, params=None, notify=False):
        body = {'jsonrpc': '2.0', 'method': method}
        if not notify:
            self.next_id += 1
            body['id'] = self.next_id
        if params is not None:
            body['params'] = params
        with self.http.post(URL, json=body, headers=self.headers, timeout=(5, 240), stream=True) as response:
            response.raise_for_status()
            session = response.headers.get('mcp-session-id')
            if session:
                self.headers['Mcp-Session-Id'] = session
            if notify or response.status_code == 202:
                return None
            if 'application/json' in response.headers.get('Content-Type', ''):
                result = response.json()
            else:
                result = None
                for line in response.iter_lines(decode_unicode=True):
                    if line and line.startswith('data:'):
                        message = json.loads(line[5:].strip())
                        if message.get('id') == body['id']:
                            result = message
                            break
            if result is None:
                raise RuntimeError(f'No JSON-RPC response for {method}')
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result['result']

    def connect(self):
        result = self.request('initialize', {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'remarkable-usb-verification', 'version': '1.0'},
        })
        self.headers['MCP-Protocol-Version'] = result['protocolVersion']
        self.request('notifications/initialized', notify=True)
        save('initialize.json', result)
        print('MCP initialized:', result['serverInfo'], 'protocol', result['protocolVersion'], flush=True)

    def call(self, name, arguments):
        result = self.request('tools/call', {'name': name, 'arguments': arguments})
        save(name + '.json', result)
        if result.get('isError'):
            raise RuntimeError(f'{name}: {result}')
        return result


def save(name, result):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')


def payload(result):
    for item in result.get('content', []):
        if item.get('type') == 'text':
            try:
                value = json.loads(item['text'])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                if value.get('error') or value.get('_error'):
                    raise RuntimeError(value)
                return value
    return result.get('structuredContent', result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--document', help='Read and render this notebook path; omit to check status and browse.')
    args = parser.parse_args()
    client = MCP()
    client.connect()
    if args.document:
        reading = payload(client.call('remarkable_read', {'document': args.document, 'include_ocr': False}))
        print('Read result:', json.dumps(reading, ensure_ascii=True), flush=True)
        result = client.call('remarkable_image', {'document': args.document, 'page': 1, 'output_format': 'png', 'include_ocr': False})
        for item in result.get('content', []):
            resource = item.get('resource', {})
            data = resource.get('blob') if resource.get('mimeType') == 'image/png' else None
            if item.get('type') == 'image' and item.get('mimeType') == 'image/png':
                data = item.get('data')
            if data:
                png = base64.b64decode(data, validate=True)
                assert png.startswith(b'\x89PNG\r\n\x1a\n'), 'Invalid PNG'
                path = OUT / 'notebook-page-1.png'
                path.write_bytes(png)
                from PIL import Image
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    print('Rendered PNG:', str(path), image.size, len(png), 'bytes', flush=True)
                break
        else:
            raise RuntimeError(f'No embedded PNG returned: {payload(result)}')
    else:
        tools = client.request('tools/list', {})
        save('tools.json', tools)
        names = {tool['name'] for tool in tools['tools']}
        forbidden = {'remarkable_upload', 'remarkable_mkdir', 'remarkable_move', 'remarkable_rename', 'remarkable_delete', 'remarkable_author', 'remarkable_refresh'}
        assert not names & forbidden, f'Write tools exposed: {names & forbidden}'
        print('Tools:', ', '.join(sorted(names)), flush=True)
        status = payload(client.call('remarkable_status', {}))
        assert status.get('status') == 'connected', status
        assert status.get('transport') == 'usb-web', status
        assert status.get('write_enabled') is False, status
        assert not status.get('fell_back_to_cloud', False), status
        print('Status:', json.dumps(status, ensure_ascii=True), flush=True)
        browse = payload(client.call('remarkable_browse', {'path': '/'}))
        print('Root browse:', json.dumps(browse, ensure_ascii=True), flush=True)
    client.http.close()


if __name__ == '__main__':
    main()
