"""Local stdio MCP server of the OA plugin: the Naver search screen capture as the tool `naver_capture`.

Why a local server (2026-11-03 smoke run): the skill ran scripts/naver_capture.py as a shell command inside the Codex
command sandbox (macOS Seatbelt, workspace-write), where network is off by default and Chrome aborts at start
(SIGABRT in HIServices _RegisterApplication, the Mach services it needs are denied), so every capture failed with
"chrome screenshot exit -6". Codex starts plugin MCP servers itself, outside the command sandbox, so the same
capture code runs here with the practitioner's normal browser and network, and nobody has to loosen the sandbox.

The tool runs naver_capture.run from the skill folder: the same per-case cap (6, failed attempts count), 3 s spacing
and per-PC ledger, only the query goes to Naver, and the PNG and JSON record are written to out_dir (an absolute
`web-captures` folder of the case). It returns the record (png_path, sha256, results, visible_text); registration
stays with the remote server tool register_capture, which never fetches the page.

Protocol: MCP over stdio, newline-delimited JSON-RPC 2.0, standard library only (python3 on the practitioner's PC).
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/opinion/scripts'))
import naver_capture  # noqa: E402

NAME, VERSION = 'oa-capture', '1.0.0'
PROTOCOL = '2025-06-18'
OUT_NAME = 'web-captures'
TOOL = {
    'name': 'naver_capture',
    'title': '네이버 검색 화면 캡처',
    'description': (
        "Capture the Naver search result screen for a query on this PC (headless Chrome/Edge, or Playwright when "
        "installed) as evidence, the way attorneys capture it by hand. At most 6 captures per case (failed attempts "
        "count) and 3 s apart; the same case, query and channel already in out_dir is returned as ALREADY_CAPTURED "
        "without a new request unless again. Only the query goes to Naver. out_dir must be the absolute path of the "
        "case's 'web-captures' folder. Returns the capture record (kind naver_capture/v1, png_path, sha256, "
        "captured_at, results [{title, url, snippet}], visible_text); pass it unchanged as capture_json to the oa "
        "server tool register_capture to get a research card. Failures: status INVALID, REFUSED (CASE_LIMIT, SPACING),"
        " NO_BROWSER or FAILED."),
    'inputSchema': {
        'type': 'object',
        'properties': {
            'application_number': {'type': 'string', 'description': 'case application number (stays on this PC)'},
            'query': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'out_dir': {'type': 'string', 'description': "absolute path of the case's web-captures folder"},
            'channel': {'type': 'string', 'enum': list(naver_capture.CHANNELS), 'default': '통합'},
            'again': {'type': 'boolean', 'default': False},
        },
        'required': ['application_number', 'query', 'out_dir'],
        'additionalProperties': False,
    },
    'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': True},
}


def call(arguments, render=None, clock=None):
    """(isError, payload) of one naver_capture call."""
    arguments = arguments if isinstance(arguments, dict) else {}
    out = Path(str(arguments.get('out_dir') or ''))
    if not out.is_absolute() or out.name != OUT_NAME or '..' in out.parts:
        return True, {'status': 'INVALID', 'expected': f"out_dir: absolute path of the case's {OUT_NAME} folder"}
    extra = {'clock': clock} if clock else {}
    code, payload = naver_capture.run(str(arguments.get('query') or ''), str(arguments.get('application_number') or ''),
                                      out, arguments.get('channel') or '통합', again=bool(arguments.get('again')),
                                      render=render, **extra)
    return code != 0, payload


def handle(message, render=None, clock=None):
    """The JSON-RPC response to one message, or None for a notification."""
    method, ident = message.get('method'), message.get('id')
    if ident is None:
        return None
    if method == 'initialize':
        requested = (message.get('params') or {}).get('protocolVersion')
        result = {'protocolVersion': requested or PROTOCOL, 'capabilities': {'tools': {'listChanged': False}},
                  'serverInfo': {'name': NAME, 'version': VERSION}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [TOOL]}
    elif method == 'tools/call':
        params = message.get('params') or {}
        if params.get('name') != TOOL['name']:
            return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32602, 'message': 'unknown tool'}}
        failed, payload = call(params.get('arguments'), render, clock)
        result = {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}],
                  'structuredContent': payload, 'isError': failed}
    else:
        return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': f'method not found: {method}'}}
    return {'jsonrpc': '2.0', 'id': ident, 'result': result}


def serve(stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            if not isinstance(message, dict):
                continue
            try:
                response = handle(message)
            except Exception as error:  # one bad call must not end the server
                response = {'jsonrpc': '2.0', 'id': message.get('id'),
                            'error': {'code': -32603, 'message': error.__class__.__name__}}
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + '\n')
            stdout.flush()


if __name__ == '__main__':
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    serve()
