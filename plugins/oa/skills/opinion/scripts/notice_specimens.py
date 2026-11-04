"""Extract the mark specimen images of the notice table "참고 : 상표견본이미지". Local only: no network.

Usage: python3 notice_specimens.py [--engine auto|python] [--out DIR] NOTICE.pdf

Prints JSON: status FOUND or NOT_FOUND, label_page and specimens [{role APPLICANT|CITED, cited_index, page, sha256,
width, height, mime_type, byte_length, path, role_basis LABEL|POSITION}]. The table is the image area between the
"상표견본이미지" heading and the next note or refusal heading; a role comes from the nearest column label ("본 출원"
or "인용") and, without labels, from the position in the row (leftmost = APPLICANT). JPEG (DCTDecode) and JPEG 2000
images are written as the original bytes stored in the PDF (the bytes `pdfimages -j` writes); other encodings become
PNG through poppler `pdfimages -png` when it is installed and are otherwise listed under skipped. auto finds the table
words with poppler `pdftotext -bbox` when it is installed and otherwise with the built-in reader, which is also used
for the image positions. Exit 0 with at least one specimen, 4 when there is no table or no image, 2 not a PDF,
1 unreadable.
"""
import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zlib

WHITESPACE = b' \t\r\n\x0c\x00'
DELIMITERS = b'()<>[]{}/%'
HEADING = '상표견본이미지'
END_MARKS = ('출원서에첨부', '거절이유')
OBJECT = re.compile(rb'(?<![0-9])(\d+)\s+(\d+)\s+obj\b')
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class Name(str):
    pass


class Op(str):
    pass


class Ref(tuple):
    pass


ARRAY_OPEN, ARRAY_CLOSE, DICT_OPEN, DICT_CLOSE = (object() for _ in range(4))


class Lexer:
    def __init__(self, data, pos=0):
        self.data, self.pos = data, pos

    def skip(self):
        data = self.data
        while self.pos < len(data):
            char = data[self.pos]
            if char in WHITESPACE:
                self.pos += 1
            elif char == 0x25:  # comment
                while self.pos < len(data) and data[self.pos] not in b'\r\n':
                    self.pos += 1
            else:
                break

    def token(self):
        self.skip()
        data, pos = self.data, self.pos
        if pos >= len(data):
            return None
        char = data[pos:pos + 1]
        if char == b'(':
            return self._literal()
        if data.startswith(b'<<', pos):
            self.pos += 2
            return DICT_OPEN
        if data.startswith(b'>>', pos):
            self.pos += 2
            return DICT_CLOSE
        if char == b'<':
            end = data.find(b'>', pos)
            end = len(data) if end < 0 else end
            digits = re.sub(rb'[^0-9A-Fa-f]', b'', data[pos + 1:end])
            self.pos = end + 1
            return bytes.fromhex((digits + b'0' * (len(digits) % 2)).decode())
        if char in (b'[', b']'):
            self.pos += 1
            return ARRAY_OPEN if char == b'[' else ARRAY_CLOSE
        if char in (b'{', b'}', b')', b'>'):
            self.pos += 1
            return Op(char.decode())
        end = pos + 1
        while end < len(data) and data[end] not in WHITESPACE and data[end] not in DELIMITERS:
            end += 1
        self.pos = end
        word = data[pos:end]
        if char == b'/':
            return Name(re.sub(rb'#([0-9A-Fa-f]{2})', lambda m: bytes([int(m.group(1), 16)]), word[1:]).decode('latin-1'))
        try:
            return int(word)
        except ValueError:
            pass
        try:
            return float(word)
        except ValueError:
            return Op(word.decode('latin-1'))

    def _literal(self):
        data, pos, depth, out = self.data, self.pos + 1, 1, bytearray()
        escapes = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
        while pos < len(data):
            char = data[pos]
            if char == 0x5C and pos + 1 < len(data):
                following = data[pos + 1]
                octal = re.match(rb'[0-7]{1,3}', data[pos + 1:pos + 4])
                if octal:
                    out.append(int(octal.group(), 8) & 0xFF)
                    pos += 1 + len(octal.group())
                    continue
                if following in b'\r\n':  # line continuation
                    pos += 3 if data[pos + 1:pos + 3] == b'\r\n' else 2
                    continue
                out.append(escapes.get(following, following))
                pos += 2
                continue
            if char == 0x28:
                depth += 1
            elif char == 0x29:
                depth -= 1
                if depth == 0:
                    pos += 1
                    break
            out.append(char)
            pos += 1
        self.pos = pos
        return bytes(out)

    def value(self, token=None):
        token = self.token() if token is None else token
        if token is ARRAY_OPEN:
            items = []
            while (token := self.token()) not in (ARRAY_CLOSE, None):
                items.append(self.value(token))
            return items
        if token is DICT_OPEN:
            result = {}
            while (key := self.token()) not in (DICT_CLOSE, None):
                result[key] = self.value()
            return result
        if isinstance(token, int):
            saved = self.pos
            generation = self.token()
            if isinstance(generation, int) and self.token() == 'R':
                return Ref((token, generation))
            self.pos = saved
        return token


class Document:
    """Objects of a PDF by number (the last definition wins, so incremental updates apply), object streams included."""

    def __init__(self, data):
        self.data, self.objects = data, {}
        position = 0
        while (match := OBJECT.search(data, position)):
            lexer = Lexer(data, match.end())
            value = lexer.value()
            lexer.skip()
            stream = None
            if data.startswith(b'stream', lexer.pos):
                start = lexer.pos + 6
                start += 2 if data[start:start + 2] == b'\r\n' else 1 if data[start:start + 1] in b'\r\n' else 0
                stream = start
                end = data.find(b'endstream', start)
                position = len(data) if end < 0 else end + 9
            else:
                position = lexer.pos
            self.objects[int(match.group(1))] = (value, stream)
        for number, (value, _) in list(self.objects.items()):
            if isinstance(value, dict) and value.get('Type') == 'ObjStm':
                self._object_stream(number)

    def _object_stream(self, number):
        try:
            data = self.stream(Ref((number, 0)))
            first, count = self.resolve(self.objects[number][0].get('First')), self.resolve(self.objects[number][0].get('N'))
            header = Lexer(data)
            pairs = [(header.token(), header.token()) for _ in range(count)]
            for inner, offset in pairs:
                if isinstance(inner, int) and inner not in self.objects:
                    self.objects[inner] = (Lexer(data, first + offset).value(), None)
        except (TypeError, ValueError, zlib.error):
            return

    def resolve(self, value, depth=0):
        while isinstance(value, Ref) and depth < 32:
            value = self.objects.get(value[0], (None, None))[0]
            depth += 1
        return value

    def raw(self, ref):
        entry = self.objects.get(ref[0]) if isinstance(ref, Ref) else None
        if entry is None or entry[1] is None:
            return None, None
        header, start = entry
        length = self.resolve(header.get('Length'))
        if isinstance(length, int) and length >= 0 and \
                self.data[start + length:start + length + 32].lstrip(WHITESPACE).startswith(b'endstream'):
            return header, self.data[start:start + length]
        end = self.data.find(b'endstream', start)
        body = self.data[start:end if end >= 0 else len(self.data)]
        return header, body[:-2] if body.endswith(b'\r\n') else body[:-1] if body.endswith((b'\n', b'\r')) else body

    def stream(self, ref):
        """Decoded stream (FlateDecode applied; other filters returned as stored)."""
        header, body = self.raw(ref)
        if body is None:
            return b''
        filters = self.resolve(header.get('Filter'))
        filters = filters if isinstance(filters, list) else [filters] if filters else []
        for name in filters:
            if self.resolve(name) != 'FlateDecode':
                break
            body = zlib.decompressobj().decompress(body)
        return body

    def pages(self):
        catalog = next((v for v, _ in reversed(list(self.objects.values()))
                        if isinstance(v, dict) and v.get('Type') == 'Catalog'), None)
        found = []

        def walk(node, inherited, depth):
            node = self.resolve(node)
            if not isinstance(node, dict) or depth > 32:
                return
            inherited = {**inherited, **{k: node[k] for k in ('Resources', 'MediaBox') if k in node}}
            if node.get('Type') == 'Pages' or 'Kids' in node:
                for kid in self.resolve(node.get('Kids')) or []:
                    walk(kid, inherited, depth + 1)
            else:
                found.append({**node, **inherited})

        if catalog is not None:
            walk(catalog.get('Pages'), {}, 0)
        return found


def multiply(a, b):
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2],
            a[2] * b[1] + a[3] * b[3], a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def apply(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def numbers(values, count):
    values = values[-count:]
    return [float(v) for v in values] if len(values) == count and all(isinstance(v, (int, float)) for v in values) \
        else None


class Font:
    def __init__(self, document, font):
        font = document.resolve(font) or {}
        self.two_byte = font.get('Subtype') == 'Type0'
        self.map = {}
        cmap = document.stream(font.get('ToUnicode')) if isinstance(font.get('ToUnicode'), Ref) else b''
        for block in re.findall(rb'beginbfchar(.*?)endbfchar', cmap, re.DOTALL):
            for source, target in re.findall(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', block):
                self.map[int(source, 16)] = bytes.fromhex(target.decode()).decode('utf-16-be', 'replace')
        for block in re.findall(rb'beginbfrange(.*?)endbfrange', cmap, re.DOTALL):
            for low, high, target in re.findall(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', block):
                base = int(target, 16)
                for offset in range(min(int(high, 16) - int(low, 16) + 1, 65536)):
                    self.map[int(low, 16) + offset] = chr(base + offset) if base + offset < 0x110000 else ''
        self.widths, self.default = {}, 1000.0 if self.two_byte else 500.0
        if self.two_byte:
            descendant = document.resolve((document.resolve(font.get('DescendantFonts')) or [None])[0]) or {}
            self.default = float(document.resolve(descendant.get('DW')) or 1000)
            spec, index = document.resolve(descendant.get('W')) or [], 0
            while index + 1 < len(spec):
                start, following = document.resolve(spec[index]), document.resolve(spec[index + 1])
                if isinstance(following, list):
                    for offset, width in enumerate(following):
                        self.widths[start + offset] = float(document.resolve(width))
                    index += 2
                elif index + 2 < len(spec):
                    for code in range(start, following + 1):
                        self.widths[code] = float(document.resolve(spec[index + 2]))
                    index += 3
                else:
                    break
        else:
            first = document.resolve(font.get('FirstChar')) or 0
            for offset, width in enumerate(document.resolve(font.get('Widths')) or []):
                self.widths[first + offset] = float(document.resolve(width))

    def glyphs(self, raw):
        step = 2 if self.two_byte else 1
        for index in range(0, len(raw) - step + 1, step):
            code = int.from_bytes(raw[index:index + step], 'big')
            text = self.map.get(code, '' if self.two_byte else chr(code))
            yield code, text, self.widths.get(code, self.default) / 1000.0


class Page:
    """Glyph and image positions of one page, top-down coordinates from the page's top left corner."""

    def __init__(self, document, page, number):
        self.document, self.number, self.glyphs, self.images = document, number, [], []
        box = numbers(document.resolve(page.get('MediaBox')) or [0, 0, 612, 792], 4) or [0, 0, 612, 792]
        self.left, self.top = box[0], box[3]
        contents = document.resolve(page.get('Contents'))
        refs = contents if isinstance(contents, list) else [page.get('Contents')]
        data = b'\n'.join(document.stream(ref) for ref in refs if isinstance(ref, Ref))
        self.fonts = {}
        self.run(data, document.resolve(page.get('Resources')) or {}, IDENTITY, 0)

    def point(self, matrix, x, y):
        px, py = apply(matrix, x, y)
        return px - self.left, self.top - py

    def font(self, resources, name):
        fonts = self.document.resolve(resources.get('Font')) or {}
        ref = fonts.get(name)
        key = ref if isinstance(ref, Ref) else id(ref)
        if key not in self.fonts:
            self.fonts[key] = Font(self.document, ref)
        return self.fonts[key]

    def run(self, data, resources, ctm, depth):
        state = {'ctm': ctm, 'font': None, 'size': 0.0, 'tc': 0.0, 'tw': 0.0, 'th': 1.0, 'tl': 0.0, 'rise': 0.0}
        stack, operands = [], []
        tm = tlm = IDENTITY
        lexer = Lexer(data)
        while (token := lexer.token()) is not None:
            if not isinstance(token, Op) or token in ('true', 'false', 'null'):
                operands.append(lexer.value(token))
                continue
            op = str(token)
            if op == 'BI':  # inline image: skip its binary data
                found = re.compile(rb'\sEI(?=[\s]|$)').search(data, data.find(b'ID', lexer.pos) + 3)
                lexer.pos = found.end() if found else len(data)
            elif op == 'q':
                stack.append(dict(state))
            elif op == 'Q' and stack:
                state = stack.pop()
            elif op == 'cm' and (m := numbers(operands, 6)):
                state['ctm'] = multiply(tuple(m), state['ctm'])
            elif op == 'BT':
                tm = tlm = IDENTITY
            elif op == 'Tf' and len(operands) >= 2 and isinstance(operands[-1], (int, float)):
                state['font'], state['size'] = self.font(resources, operands[-2]), float(operands[-1])
            elif op in ('Tc', 'Tw', 'TL', 'Ts') and (v := numbers(operands, 1)):
                state[{'Tc': 'tc', 'Tw': 'tw', 'TL': 'tl', 'Ts': 'rise'}[op]] = v[0]
            elif op == 'Tz' and (v := numbers(operands, 1)):
                state['th'] = v[0] / 100.0
            elif op in ('Td', 'TD') and (v := numbers(operands, 2)):
                if op == 'TD':
                    state['tl'] = -v[1]
                tm = tlm = multiply((1, 0, 0, 1, v[0], v[1]), tlm)
            elif op == 'Tm' and (m := numbers(operands, 6)):
                tm = tlm = tuple(m)
            elif op == 'T*':
                tm = tlm = multiply((1, 0, 0, 1, 0, -state['tl']), tlm)
            elif op in ('Tj', "'", '"', 'TJ') and operands:
                if op in ("'", '"'):
                    if op == '"' and (v := numbers(operands[:-1], 2)):
                        state['tw'], state['tc'] = v
                    tm = tlm = multiply((1, 0, 0, 1, 0, -state['tl']), tlm)
                shown = operands[-1]
                for part in shown if isinstance(shown, list) else [shown]:
                    if isinstance(part, (int, float)):
                        tm = multiply((1, 0, 0, 1, -part / 1000.0 * state['size'] * state['th'], 0), tm)
                    elif isinstance(part, bytes) and state['font'] is not None:
                        tm = self.show(part, state, tm)
            elif op == 'Do' and operands and depth < 8:
                self.draw(operands[-1], resources, state['ctm'], depth)
            operands = []

    def show(self, raw, state, tm):
        size, th = state['size'], state['th']
        for code, text, width in state['font'].glyphs(raw):
            render = multiply((size * th, 0, 0, size, 0, state['rise']), multiply(tm, state['ctm']))
            x0, y = self.point(render, 0, 0)
            x1, _ = self.point(render, width, 0)
            if text.strip():
                self.glyphs.append((text, min(x0, x1), max(x0, x1), y))
            space = state['tw'] if not state['font'].two_byte and code == 32 else 0.0
            tm = multiply((1, 0, 0, 1, (width * size + state['tc'] + space) * th, 0), tm)
        return tm

    def draw(self, name, resources, ctm, depth):
        ref = (self.document.resolve(resources.get('XObject')) or {}).get(name)
        header = self.document.resolve(ref)
        if not isinstance(header, dict) or not isinstance(ref, Ref):
            return
        if header.get('Subtype') == 'Image':
            corners = [self.point(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
            xs, ys = [c[0] for c in corners], [c[1] for c in corners]
            self.images.append({'ref': ref, 'page': self.number, 'x0': min(xs), 'x1': max(xs), 'y0': min(ys),
                                'y1': max(ys)})
        elif header.get('Subtype') == 'Form':
            matrix = numbers(self.document.resolve(header.get('Matrix')) or list(IDENTITY), 6) or list(IDENTITY)
            inner = self.document.resolve(header.get('Resources')) or resources
            self.run(self.document.stream(ref), inner, multiply(tuple(matrix), ctm), depth + 1)


def poppler_words(path):
    """Words as glyph lists per page from `pdftotext -bbox` (top-down page coordinates), or None."""
    if not shutil.which('pdftotext'):
        return None
    try:
        result = subprocess.run(['pdftotext', '-bbox', path, '-'], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    pages = []
    for page in re.split(r'<page\b', result.stdout.decode('utf-8', 'replace'))[1:]:
        glyphs = []
        for x0, y0, x1, y1, word in re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" '
                                               r'yMax="([\d.]+)">(.*?)</word>', page):
            text = html.unescape(word)
            step = (float(x1) - float(x0)) / max(len(text), 1)
            glyphs += [(char, float(x0) + i * step, float(x0) + (i + 1) * step, float(y1))
                       for i, char in enumerate(text) if char.strip()]
        pages.append(glyphs)
    return pages


def lines(glyphs, page):
    """Glyphs grouped into lines (same baseline within 3 pt), each (page, y, text, x centers per character)."""
    grouped = []
    for glyph in sorted(glyphs, key=lambda g: (g[3], g[1])):
        if grouped and abs(grouped[-1][0] - glyph[3]) <= 3:
            grouped[-1][1].append(glyph)
        else:
            grouped.append((glyph[3], [glyph]))
    result = []
    for y, members in grouped:
        members.sort(key=lambda g: g[1])
        text, centers = '', []
        for char, x0, x1, _ in members:
            text += char
            centers += [(x0 + x1) / 2] * len(char)
        result.append((page, y, text, centers))
    return result


def label_positions(line):
    _, _, text, centers = line
    found = []
    for word, role in (('본출원', 'APPLICANT'), ('인용', 'CITED')):
        for match in re.finditer(word, text):
            found.append((sum(centers[match.start():match.end()]) / len(word), role))
    return sorted(found)


def find_table(all_lines):
    """(heading line, column label line or None, end (page, y)) of the specimen table, or None."""
    for index, line in enumerate(all_lines):
        if HEADING in line[2] and HEADING + '는' not in line[2]:
            labels, start = None, index + 1
            if start < len(all_lines) and all_lines[start][0] <= line[0] + 1 and label_positions(all_lines[start]):
                labels, start = all_lines[start], start + 1
            end = next(((l[0], l[1]) for l in all_lines[start:] if any(m in l[2] for m in END_MARKS)),
                       (line[0] + 1, float('inf')))
            return line, labels, end
    return None


def roles(images, labels):
    """Role and cited index per image from the nearest column label, or from the row position without labels."""
    if labels:
        columns = label_positions(labels)
        cited = [x for x, role in columns if role == 'CITED']
        for image in images:
            center = (image['x0'] + image['x1']) / 2
            x, role = min(columns, key=lambda column: abs(column[0] - center))
            image.update(role=role, cited_index=cited.index(x) + 1 if role == 'CITED' else None, role_basis='LABEL')
        return
    rows = []
    for image in sorted(images, key=lambda i: (i['page'], i['y0'])):
        if rows and rows[-1][0]['page'] == image['page'] and image['y0'] < rows[-1][0]['y1']:
            rows[-1].append(image)
        else:
            rows.append([image])
    for row in rows:
        for position, image in enumerate(sorted(row, key=lambda i: i['x0'])):
            image.update(role='APPLICANT' if position == 0 else 'CITED', cited_index=position or None,
                         role_basis='POSITION')


def image_bytes(document, path, image, scratch):
    """(bytes, mime type, extension) of an image: the stored JPEG or JPEG 2000 bytes, else PNG from poppler."""
    header, body = document.raw(image['ref'])
    filters = document.resolve(header.get('Filter'))
    filters = [document.resolve(f) for f in (filters if isinstance(filters, list) else [filters] if filters else [])]
    if filters and filters[-1] in ('DCTDecode', 'JPXDecode') and all(f == 'FlateDecode' for f in filters[:-1]):
        for _ in filters[:-1]:
            body = zlib.decompressobj().decompress(body)
        return (body, 'image/jpeg', 'jpg') if filters[-1] == 'DCTDecode' else (body, 'image/jp2', 'jp2')
    if not shutil.which('pdfimages'):
        return None
    page = str(image['page'])
    listing = subprocess.run(['pdfimages', '-list', '-f', page, '-l', page, path], capture_output=True, text=True,
                             timeout=60)
    index = next((int(row.split()[1]) for row in listing.stdout.splitlines()[2:]
                  if len(row.split()) > 10 and row.split()[10] == str(image['ref'][0])), None)
    if index is None:
        return None
    prefix = Path(scratch) / f'p{page}'
    subprocess.run(['pdfimages', '-png', '-f', page, '-l', page, path, str(prefix)], capture_output=True, timeout=60)
    target = Path(f'{prefix}-{index:03d}.png')
    return (target.read_bytes(), 'image/png', 'png') if target.exists() else None


def extract(path, out, engine):
    document = Document(open(path, 'rb').read())
    pages = [Page(document, page, number) for number, page in enumerate(document.pages(), 1)]
    words = poppler_words(path) if engine == 'auto' else None
    table = None
    if words and len(words) == len(pages):
        table = find_table([l for number, glyphs in enumerate(words, 1) for l in lines(glyphs, number)])
    if table is None:
        table = find_table([l for page in pages for l in lines(page.glyphs, page.number)])
    if table is None:
        return {'status': 'NOT_FOUND', 'reason': 'NO_TABLE', 'specimens': [], 'skipped': []}
    heading, labels, end = table
    start = (labels or heading)[:2]
    inside = [dict(i) for page in pages for i in page.images
              if start < (i['page'], (i['y0'] + i['y1']) / 2) < end and min(i['x1'] - i['x0'], i['y1'] - i['y0']) >= 4]
    roles(inside, labels)
    specimens, skipped, seen = [], [], set()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        for image in sorted(inside, key=lambda i: (i['page'], i['y0'], i['x0'])):
            header = document.resolve(image['ref'])
            extracted = image_bytes(document, path, image, scratch)
            if extracted is None:
                skipped.append({'page': image['page'], 'role': image['role'], 'reason': 'UNSUPPORTED_ENCODING'})
                continue
            data, mime, extension = extracted
            digest = hashlib.sha256(data).hexdigest()
            if (image['role'], digest) in seen:
                continue
            seen.add((image['role'], digest))
            name = f"p{image['page']}-{image['role'].lower()}{image['cited_index'] or ''}-{digest[:12]}.{extension}"
            (out / name).write_bytes(data)
            specimens.append({'role': image['role'], 'cited_index': image['cited_index'], 'page': image['page'],
                              'sha256': digest, 'width': document.resolve(header.get('Width')),
                              'height': document.resolve(header.get('Height')), 'mime_type': mime,
                              'byte_length': len(data), 'path': str((out / name).resolve()),
                              'role_basis': image['role_basis']})
    result = {'status': 'FOUND' if specimens else 'NOT_FOUND', 'label_page': heading[0],
              'applicant_found': any(s['role'] == 'APPLICANT' for s in specimens), 'specimens': specimens,
              'skipped': skipped}
    if not specimens:
        result['reason'] = 'NO_IMAGE'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--engine', choices=('auto', 'python'), default='auto')
    parser.add_argument('--out', default='notice-specimens', help='folder for the images (default ./notice-specimens)')
    parser.add_argument('pdf')
    args = parser.parse_args(argv)
    try:
        with open(args.pdf, 'rb') as stream:
            if not stream.read(5).startswith(b'%PDF-'):
                print(f'Not a PDF file: {args.pdf}', file=sys.stderr)
                return 2
        result = extract(args.pdf, Path(args.out), args.engine)
    except (OSError, ValueError, zlib.error, subprocess.TimeoutExpired) as error:
        print(f'Could not read the PDF: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['specimens'] else 4


if __name__ == '__main__':
    raise SystemExit(main())
