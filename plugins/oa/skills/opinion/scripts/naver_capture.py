"""Capture a Naver search result screen on the practitioner's PC as evidence (the way attorneys capture it by hand).

Usage: python3 naver_capture.py --case APPLICATION_NUMBER --out DIR [--channel 통합|뉴스|블로그|어학사전]
       [--engine auto|playwright|chrome] [--max-per-case N] [--again] QUERY

Opens the Naver search page for QUERY in a headless browser of this PC, waits for the render, saves a PNG and prints
one JSON record (also written next to the PNG): kind naver_capture/v1, case, query, channel, url, final_url,
captured_at {utc, kst}, png_path, sha256, byte_length, viewport, full_page, user_agent, engine, browser, page_loads,
results [{title, url, snippet}] and visible_text (the result area text for the model to read; the PNG is the evidence).
Pass the record to the server tool register_capture to get a research card.

Engines: playwright (Python package `playwright`; installed Chrome or Edge channel first, then its bundled Chromium):
full-page PNG and the rendered page text in one page load. chrome (fallback, no package): the installed Chrome, Edge
or Chromium run as `--headless --screenshot` (fixed window height, full_page false) and once more as `--dump-dom` for
the text (page_loads 2). OA_CHROME_PATH names the browser binary. auto picks playwright when importable.

Minimal use (user decision 2026-11-03, Naver forbids automated collection): at most --max-per-case captures per case
(default and maximum 6, failed attempts count) and at least 3 seconds between any two captures on this PC; otherwise
the script refuses (exit 5) without contacting Naver. The same case, query and channel already captured in --out is
returned as ALREADY_CAPTURED without a new request unless --again. Only the query goes to Naver; the case number stays
local (the per-PC ledger in OA_NAVER_CAPTURE_STATE_DIR, default ~/.oa-naver-capture, keeps only sha256 values).
Exit 0 captured or already captured, 1 capture failed, 2 bad arguments, 3 no browser, 5 refused.
"""
import argparse
import hashlib
import html.parser
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit

KIND = 'naver_capture/v1'
CHANNELS = {
    '통합': ('all', 'https://search.naver.com/search.naver?where=nexearch&query={q}'),
    '뉴스': ('news', 'https://search.naver.com/search.naver?where=news&query={q}'),
    '블로그': ('blog', 'https://search.naver.com/search.naver?where=blog&query={q}'),
    '어학사전': ('dict', 'https://{lang}.dict.naver.com/#/search?query={q}'),
}
ALIASES = {'all': '통합', 'news': '뉴스', 'blog': '블로그', 'dict': '어학사전'}
MAX_PER_CASE = 6
MIN_SPACING_SECONDS = 3.0
VIEWPORT = {'width': 1280, 'height': 900}
FALLBACK_HEIGHT = 3000
TEXT_LIMIT = 20000
RESULT_LIMIT = 30
KST = timezone(timedelta(hours=9))
HANGUL = re.compile('[가-힣]')
SKIP = {'script', 'style', 'noscript', 'template', 'svg', 'head'}
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
SCOPES = ('main_pack', 'searchPage_entry', 'content', 'container')


class Refused(Exception):
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.reason, self.details = reason, details


class CaptureFailed(Exception):
    pass


class NoBrowser(Exception):
    pass


def normalize_case(value):
    return re.sub(r'[\s-]', '', value or '')


def search_url(query, channel):
    _slug, template = CHANNELS[channel]
    lang = 'ko' if HANGUL.search(query) else 'en'
    url = template.format(q=quote(query, safe=''), lang=lang)
    base = os.environ.get('OA_NAVER_CAPTURE_BASE_URL', '').rstrip('/')
    if base:  # tests only: a local fixture server on loopback
        parts = urlsplit(base)
        if parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', 'localhost'):
            raise SystemExit('OA_NAVER_CAPTURE_BASE_URL must be http://127.0.0.1:<port>')
        rest = urlsplit(url)
        url = base + rest.path + ('?' + rest.query if rest.query else '') + ('#' + rest.fragment if rest.fragment else '')
    return url


# Text of the rendered page (both engines hand over HTML).

class Node:
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.children, self.parent = tag, attrs, [], parent

    def classes(self):
        return (self.attrs.get('class') or '').split()

    def text(self):
        parts = []

        def walk(node):
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                elif child.tag not in SKIP:
                    walk(child)
                    if child.tag in ('p', 'div', 'li', 'br', 'tr', 'h1', 'h2', 'h3', 'h4', 'dd', 'dt'):
                        parts.append('\n')
        walk(self)
        return ''.join(parts)

    def iter(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                if child.tag not in SKIP:
                    yield from child.iter()


class TreeBuilder(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('#root', {}, None)
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or '' for k, v in attrs}, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, {k: v or '' for k, v in attrs}, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def squash(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def extract(page_html):
    """visible_text of the result area and result titles with the snippet that follows each."""
    builder = TreeBuilder()
    builder.feed(page_html or '')
    builder.close()
    nodes = list(builder.root.iter())
    scope = next((n for name in SCOPES for n in nodes if n.attrs.get('id') == name), None) \
        or next((n for n in nodes if n.tag == 'body'), builder.root)
    lines = [squash(line) for line in scope.text().split('\n')]
    visible = '\n'.join(line for line in lines if line)[:TEXT_LIMIT]
    results, seen, last = [], set(), None
    for node in scope.iter():
        names = ' '.join(node.classes()).lower()
        link = node.tag == 'a' and node.attrs.get('data-heatmap-target') == '.link'  # 2026 layout: title, snippet
        if link or (node.tag == 'a' and ('tit' in names or 'title' in names or 'headline' in names)):
            href = node.attrs.get('href', '')
            title = squash(node.text().replace('새 창 열림', ''))
            if link and last is not None and last['url'] == href[:1000] and last['snippet'] is None:
                if title and title != last['title']:
                    last['snippet'] = title[:600]
            elif len(title) >= 2 and href.startswith(('http://', 'https://')) and (title, href) not in seen:
                seen.add((title, href))
                last = {'title': title[:300], 'url': href[:1000], 'snippet': None}
                results.append(last)
        elif last is not None and last['snippet'] is None and ('dsc' in names or 'desc' in names):
            snippet = squash(node.text())
            if snippet:
                last['snippet'] = snippet[:600]
        if len(results) >= RESULT_LIMIT:
            break
    return results, visible


# Engines: each returns {final_url, user_agent, html, browser, full_page, viewport, engine, page_loads}.

def render_playwright(url, png_path, viewport):
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
    with sync_playwright() as p:
        browser, attempts, used = None, [], None
        for channel in ('chrome', 'msedge', None):
            try:
                browser = p.chromium.launch(channel=channel, headless=True) if channel else \
                    p.chromium.launch(headless=True)
                used = channel or 'chromium'
                break
            except PlaywrightError as error:
                attempts.append(f'{channel or "chromium"}: {str(error).splitlines()[0][:120]}')
        if browser is None:
            raise NoBrowser('; '.join(attempts))
        try:
            context = browser.new_context(viewport=viewport, locale='ko-KR', timezone_id='Asia/Seoul')
            page = context.new_page()
            page.goto(url, wait_until='domcontentloaded', timeout=45000)
            try:
                page.wait_for_load_state('networkidle', timeout=8000)
            except PlaywrightError:
                pass  # ads and trackers can keep the network busy; the DOM is there
            page.evaluate('''async () => {
                for (let y = 0; y < document.body.scrollHeight && y < 20000; y += window.innerHeight) {
                    window.scrollTo(0, y); await new Promise(r => setTimeout(r, 150));
                }
                window.scrollTo(0, 0);
            }''')
            page.wait_for_timeout(1200)
            page.screenshot(path=str(png_path), full_page=True)
            text = page.evaluate("(document.getElementById('main_pack') || document.body).innerText")
            return {'final_url': page.url, 'user_agent': page.evaluate('navigator.userAgent'), 'html': page.content(),
                    'text': text,
                    'browser': f'{used} {browser.version}', 'full_page': True,
                    'viewport': viewport, 'engine': 'playwright', 'page_loads': 1}
        except PlaywrightError as error:
            raise CaptureFailed(str(error).splitlines()[0][:200]) from None
        finally:
            browser.close()


def find_chrome():
    explicit = os.environ.get('OA_CHROME_PATH', '').strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    candidates = [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
    ]
    for root in (os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'), os.environ.get('LOCALAPPDATA')):
        if root:
            candidates += [str(Path(root) / 'Google/Chrome/Application/chrome.exe'),
                           str(Path(root) / 'Microsoft/Edge/Application/msedge.exe')]
    for path in candidates:
        if Path(path).is_file():
            return path
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'microsoft-edge'):
        found = shutil.which(name)
        if found:
            return found
    return None


def render_chrome(url, png_path, viewport):
    binary = find_chrome()
    if not binary:
        raise NoBrowser('Chrome, Edge or Chromium not found (set OA_CHROME_PATH)')
    size = {'width': viewport['width'], 'height': FALLBACK_HEIGHT}
    with tempfile.TemporaryDirectory(prefix='oa-naver-capture-') as profile:
        common = [binary, '--headless', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
                  '--no-default-browser-check', '--disable-extensions', '--lang=ko-KR', f'--user-data-dir={profile}',
                  f'--window-size={size["width"]},{size["height"]}', '--virtual-time-budget=8000']
        try:
            shot = subprocess.run(common + [f'--screenshot={png_path}', url], capture_output=True, timeout=90)
            if shot.returncode != 0 or not Path(png_path).is_file():
                raise CaptureFailed(f'chrome screenshot exit {shot.returncode}')
            dump = subprocess.run(common + ['--dump-dom', url], capture_output=True, timeout=90)
            version = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise CaptureFailed('chrome timed out') from None
    return {'final_url': None, 'user_agent': None, 'html': dump.stdout.decode('utf-8', 'replace'),
            'browser': squash(version.stdout) or Path(binary).name, 'full_page': False, 'viewport': size,
            'engine': 'chrome', 'page_loads': 2}


def pick_engine(name):
    if name == 'playwright':
        return render_playwright
    if name == 'chrome':
        return render_chrome
    try:
        import playwright.sync_api  # noqa: F401
        return render_playwright
    except ImportError:
        return render_chrome


# Minimal-use ledger.

def state_dir():
    return Path(os.environ.get('OA_NAVER_CAPTURE_STATE_DIR') or Path.home() / '.oa-naver-capture')


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_ledger(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) and isinstance(data.get('captures'), list) else {'captures': []}
    except (OSError, ValueError):
        return {'captures': []}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def reserve(case, query, channel, limit, now):
    """Record one attempt or refuse (CASE_LIMIT, SPACING) before any request."""
    path = state_dir() / 'ledger.json'
    ledger = load_ledger(path)
    case_sha = sha(case)
    used = sum(1 for c in ledger['captures'] if c.get('case_sha') == case_sha)
    if used >= limit:
        raise Refused('CASE_LIMIT', used=used, limit=limit)
    last = max((c.get('at', 0) for c in ledger['captures']), default=0)
    if now - last < MIN_SPACING_SECONDS:
        raise Refused('SPACING', retry_after_seconds=round(MIN_SPACING_SECONDS - (now - last) + 0.05, 2))
    ledger['captures'].append({'case_sha': case_sha, 'request_sha': sha(f'{query}\x1f{channel}'), 'at': now})
    ledger['captures'] = ledger['captures'][-2000:]
    save_json(path, ledger)
    return used + 1


def release(at):
    path = state_dir() / 'ledger.json'
    ledger = load_ledger(path)
    ledger['captures'] = [c for c in ledger['captures'] if c.get('at') != at]
    save_json(path, ledger)


def existing(out, case, query, channel):
    for path in sorted(out.glob('*.json')):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get('kind') == KIND and record.get('case') == case \
                and record.get('query') == query and record.get('channel') == channel \
                and Path(record.get('png_path') or '').is_file():
            return record
    return None


def capture(query, channel, case, out, engine='auto', limit=MAX_PER_CASE, again=False, render=None, clock=time.time):
    out.mkdir(parents=True, exist_ok=True)
    if not again:
        found = existing(out, case, query, channel)
        if found:
            return dict(found, status='ALREADY_CAPTURED')
    render = render or pick_engine(engine)
    url = search_url(query, channel)
    clock_at = clock()
    number = reserve(case, query, channel, limit, clock_at)
    slug = CHANNELS[channel][0]
    partial = out / f'{number:02d}-{slug}-capturing.png'
    started = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        page = render(url, partial, dict(VIEWPORT))
        data = partial.read_bytes()
    except NoBrowser:
        release(clock_at)  # nothing reached Naver
        raise
    except OSError as error:
        raise CaptureFailed(f'png not written: {error.__class__.__name__}') from None
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        partial.unlink(missing_ok=True)
        raise CaptureFailed('not a PNG')
    digest = hashlib.sha256(data).hexdigest()
    png = out / f'{number:02d}-{slug}-{digest[:8]}.png'
    os.replace(partial, png)
    results, visible = extract(page['html'])
    if page.get('text'):  # the browser's own rendered text (CSS-hidden panels left out)
        visible = '\n'.join(squash(line) for line in page['text'].split('\n') if squash(line))[:TEXT_LIMIT]
    record = {
        'kind': KIND, 'status': 'CAPTURED', 'case': case, 'query': query, 'channel': channel, 'url': url,
        'final_url': page['final_url'],
        'captured_at': {'utc': started.strftime('%Y-%m-%dT%H:%M:%SZ'), 'kst': started.astimezone(KST).isoformat()},
        'png_path': str(png.resolve()), 'sha256': digest, 'byte_length': len(data), 'viewport': page['viewport'],
        'full_page': page['full_page'], 'user_agent': page['user_agent'], 'engine': page['engine'],
        'browser': page['browser'], 'page_loads': page['page_loads'], 'results': results, 'visible_text': visible,
    }
    save_json(png.with_suffix('.json'), record)
    return record


def main(argv=None, render=None, clock=time.time):
    parser = argparse.ArgumentParser(description='Capture a Naver search result screen as evidence.')
    parser.add_argument('query')
    parser.add_argument('--case', required=True, help='application number of the case (stays local)')
    parser.add_argument('--out', required=True, type=Path, help='folder for the PNG and JSON record')
    parser.add_argument('--channel', default='통합', choices=sorted(set(CHANNELS) | set(ALIASES)))
    parser.add_argument('--engine', default='auto', choices=('auto', 'playwright', 'chrome'))
    parser.add_argument('--max-per-case', type=int, default=MAX_PER_CASE)
    parser.add_argument('--again', action='store_true', help='capture again even if this query was captured')
    args = parser.parse_args(argv)
    code, payload = run(args.query, args.case, args.out, args.channel, args.engine, args.max_per_case, args.again,
                        render, clock)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if code == 0 else None))
    return code


def run(query, case, out, channel='통합', engine='auto', limit=MAX_PER_CASE, again=False, render=None,
        clock=time.time):
    """(exit code, JSON payload): the record, or {status INVALID|REFUSED|NO_BROWSER|FAILED, ...}. Shared by the
    command line and the plugin's local MCP server (mcp/oa_capture.py)."""
    query = squash(query)
    case = normalize_case(case)
    if not query or len(query) > 200 or not re.fullmatch(r'4[015][0-9]{11}', case) \
            or not 1 <= limit <= MAX_PER_CASE or ALIASES.get(channel, channel) not in CHANNELS:
        return 2, {'status': 'INVALID', 'expected': 'query 1-200 chars, 13-digit case number, '
                   f'max per case 1-{MAX_PER_CASE}, channel {"|".join(CHANNELS)}'}
    channel = ALIASES.get(channel, channel)
    try:
        return 0, capture(query, channel, case, Path(out), engine, limit, again, render, clock)
    except Refused as error:
        return 5, {'status': 'REFUSED', 'reason': error.reason, **error.details}
    except NoBrowser as error:
        return 3, {'status': 'NO_BROWSER', 'detail': str(error),
                   'hint': 'pip install playwright (uses installed Chrome) or install Google Chrome'}
    except CaptureFailed as error:
        return 1, {'status': 'FAILED', 'detail': str(error)}


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
