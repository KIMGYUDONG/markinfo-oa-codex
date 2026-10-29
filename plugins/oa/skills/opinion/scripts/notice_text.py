"""Print the text of a notice PDF to stdout. Local only: no network, standard library fallback.

Usage: python3 notice_text.py [--engine auto|pdftotext|python] NOTICE.pdf

auto uses pdftotext (poppler) when it is installed and otherwise a small built-in reader that
handles FlateDecode content streams and ToUnicode maps. Scanned (image only) notices yield no text;
the skill then asks the practitioner for the facts instead.
"""
import argparse
import re
import shutil
import subprocess
import sys
import zlib


def with_pdftotext(path):
    result = subprocess.run(['pdftotext', '-layout', '-enc', 'UTF-8', path, '-'],
                            capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace').strip() or 'pdftotext failed')
    return result.stdout.decode('utf-8', 'replace')


STREAM = re.compile(rb'<<(.*?)>>\s*stream\r?\n(.*?)\r?\nendstream', re.DOTALL)


def streams(data):
    for match in STREAM.finditer(data):
        header, raw = match.groups()
        if b'/FlateDecode' in header:
            try:
                yield zlib.decompress(raw)
            except zlib.error:
                continue
        elif b'/Filter' not in header:
            yield raw


def unicode_map(cmaps):
    mapping = {}
    for cmap in cmaps:
        for block in re.findall(rb'beginbfchar(.*?)endbfchar', cmap, re.DOTALL):
            for source, target in re.findall(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', block):
                mapping[source.decode().upper()] = bytes.fromhex(target.decode()).decode('utf-16-be', 'replace')
        for block in re.findall(rb'beginbfrange(.*?)endbfrange', cmap, re.DOTALL):
            for low, high, target in re.findall(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', block):
                width, start, base = len(low), int(low, 16), int(target, 16)
                for offset in range(int(high, 16) - start + 1):
                    code = format(start + offset, f'0{width}X')
                    mapping[code] = chr(base + offset) if base + offset < 0x110000 else ''
    return mapping


def literal(raw):
    out, index = bytearray(), 0
    escapes = {b'n': b'\n', b'r': b'\r', b't': b'\t', b'b': b'\b', b'f': b'\f'}
    while index < len(raw):
        char = raw[index:index + 1]
        if char == b'\\' and index + 1 < len(raw):
            following = raw[index + 1:index + 2]
            octal = re.match(rb'[0-7]{1,3}', raw[index + 1:index + 4])
            if octal:
                out.append(int(octal.group(), 8) & 0xFF)
                index += 1 + len(octal.group())
                continue
            out += escapes.get(following, following)
            index += 2
            continue
        out += char
        index += 1
    return bytes(out)


def decode_hex(hex_text, mapping):
    hex_text = re.sub(r'\s', '', hex_text).upper()
    for width in (4, 2):
        if len(hex_text) % width == 0:
            codes = [hex_text[i:i + width] for i in range(0, len(hex_text), width)]
            if mapping and all(code in mapping for code in codes):
                return ''.join(mapping[code] for code in codes)
    return bytes.fromhex(hex_text if len(hex_text) % 2 == 0 else hex_text + '0').decode('latin-1')


TOKEN = re.compile(rb'\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]+>|(?<![A-Za-z])(?:T\*|TJ|Tj|Td|TD|ET|\'|")(?![A-Za-z])',
                   re.DOTALL)


def with_python(path):
    data = open(path, 'rb').read()
    decoded = list(streams(data))
    mapping = unicode_map([s for s in decoded if b'beginbfchar' in s or b'beginbfrange' in s])
    lines = []
    for content in decoded:
        if b'BT' not in content:
            continue
        line, pending = [], []
        for token in TOKEN.findall(content):
            if token.startswith(b'('):
                pending.append(literal(token[1:-1]).decode('latin-1'))
            elif token.startswith(b'<'):
                pending.append(decode_hex(token[1:-1].decode(), mapping))
            elif token in (b'Tj', b'TJ', b"'", b'"'):
                line.extend(pending)
                pending = []
            else:  # T*, Td, TD, ET start a new line
                if line:
                    lines.append(''.join(line))
                line, pending = [], []
        if line:
            lines.append(''.join(line))
    return '\n'.join(lines) + ('\n' if lines else '')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--engine', choices=('auto', 'pdftotext', 'python'), default='auto')
    parser.add_argument('pdf')
    args = parser.parse_args(argv)
    try:
        with open(args.pdf, 'rb') as stream:
            if not stream.read(5).startswith(b'%PDF-'):
                print(f'Not a PDF file: {args.pdf}', file=sys.stderr)
                return 2
        if args.engine == 'pdftotext':
            text = with_pdftotext(args.pdf)
        elif args.engine == 'python':
            text = with_python(args.pdf)
        else:  # auto: poppler first, built-in reader when it is missing, fails or finds nothing
            try:
                text = with_pdftotext(args.pdf) if shutil.which('pdftotext') else ''
            except (RuntimeError, subprocess.TimeoutExpired):
                text = ''
            if not text.strip():
                text = with_python(args.pdf)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'Could not read the PDF: {error}', file=sys.stderr)
        return 1
    if not text.strip():
        print('No text layer found (scanned notice?). Ask the practitioner for the facts.', file=sys.stderr)
        return 3
    sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
