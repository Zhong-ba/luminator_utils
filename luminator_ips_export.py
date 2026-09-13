#!/usr/bin/env python3
"""Standalone Luminator IPS (Jet 3) -> bitmap PNG exporter.

No Luminator software or Microsoft Access is required.
Tested against the supplied CMBC IPS database.

Default behavior:
  * renders every exposure/frame for every (class, sign-code, logical sign)
  * writes exact dot-resolution PNGs (1-bit for legacy signs, RGB when Spectrum color metadata is present)
  * groups by display size: OUT/160x16/1832.png, etc.
  * deduplicates identical logical signs that share the same dimensions
  * adds __SIGNNAME when same-size logical signs render differently
  * adds __classN only when the same numeric code exists in multiple message classes

All exposures are exported by default. Use --first-exposure-only for legacy single-exposure output.
"""
from __future__ import annotations

import argparse
import collections
import re
import struct
import sys
import threading
import traceback
import zipfile
from xml.sax.saxutils import escape as xml_escape
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from PIL import Image
except ImportError as e:
    raise SystemExit("Pillow is required: python -m pip install pillow") from e

PAGE_SIZE = 2048
TYPE_BOOL=0x01; TYPE_BYTE=0x02; TYPE_INT=0x03; TYPE_LONG=0x04
TYPE_MONEY=0x05; TYPE_FLOAT=0x06; TYPE_DOUBLE=0x07; TYPE_DATETIME=0x08
TYPE_BINARY=0x09; TYPE_TEXT=0x0A; TYPE_OLE=0x0B; TYPE_MEMO=0x0C; TYPE_REPID=0x0F

@dataclass
class Column:
    name: str
    typ: int
    col_num: int
    var_idx: int
    flags: int
    fixed_off: int
    length: int

@dataclass
class TableDef:
    page: int
    num_cols: int
    num_var: int
    num_rows: int
    cols: List[Column]

class Jet3Reader:
    """Small read-only Jet 3 reader implementing only the pieces this IPS needs."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if self.data[:4] != b'\x00\x01\x00\x00' or b'Standard Jet DB' not in self.data[:32]:
            raise ValueError("Not a Microsoft Jet database")
        if self.data[0x14] != 0:
            raise ValueError("This standalone reader currently supports Jet 3 IPS files only")
        self.npages = len(self.data) // PAGE_SIZE
        self.tables: Dict[int, TableDef] = {}
        for pg in range(self.npages):
            b = self.page(pg)
            if b and b[0] == 0x02:
                try:
                    td = self._parse_tdef(pg)
                    self.tables[pg] = td
                except Exception:
                    pass

    def page(self, pg: int) -> bytes:
        return self.data[pg*PAGE_SIZE:(pg+1)*PAGE_SIZE]

    def _parse_tdef(self, pg: int) -> TableDef:
        b = self.page(pg)
        num_var = struct.unpack_from('<H', b, 23)[0]
        num_cols = struct.unpack_from('<H', b, 25)[0]
        num_real_idx = struct.unpack_from('<I', b, 31)[0]
        num_rows = struct.unpack_from('<I', b, 12)[0]
        if num_cols > 256:
            raise ValueError("implausible TDEF")
        off = 43 + num_real_idx * 8
        rawcols = []
        for i in range(num_cols):
            d = b[off+i*18:off+(i+1)*18]
            if len(d) != 18:
                raise ValueError("truncated TDEF")
            rawcols.append(dict(
                typ=d[0],
                col_num=struct.unpack_from('<H', d, 1)[0],
                var_idx=struct.unpack_from('<H', d, 3)[0],
                flags=d[13],
                fixed_off=struct.unpack_from('<H', d, 14)[0],
                length=struct.unpack_from('<H', d, 16)[0],
            ))
        off += num_cols * 18
        cols=[]
        for rc in rawcols:
            n=b[off]; off += 1
            name=b[off:off+n].decode('cp1252', 'replace'); off += n
            cols.append(Column(name=name, **rc))
        return TableDef(pg, num_cols, num_var, num_rows, cols)

    def find_table(self, *required_columns: str) -> TableDef:
        req=set(required_columns)
        candidates=[]
        for td in self.tables.values():
            names={c.name for c in td.cols}
            if req <= names:
                candidates.append(td)
        if not candidates:
            raise KeyError(f"No table containing columns {sorted(req)}")
        # Prefer populated/original tables over empty or compact/copy tables.
        return max(candidates, key=lambda t: t.num_rows)

    def _iter_raw_rows(self, tdef_page: int):
        for pg in range(self.npages):
            b=self.page(pg)
            if not b or b[0] != 0x01:
                continue
            if struct.unpack_from('<I', b, 4)[0] != tdef_page:
                continue
            n=struct.unpack_from('<H', b, 8)[0]
            offsets=[struct.unpack_from('<H', b, 10+2*i)[0] for i in range(n)]
            for rid, rawoff in enumerate(offsets):
                if rawoff & 0x8000:  # deleted
                    continue
                if rawoff & 0x4000:  # overflow indirection; not used by relevant supplied rows
                    continue
                start=rawoff & 0x1fff
                end=PAGE_SIZE if rid == 0 else (offsets[rid-1] & 0x1fff)
                if 0 <= start < end <= PAGE_SIZE:
                    yield self.page(pg)[start:end]

    @staticmethod
    def _is_null(row: bytes, colnum: int, numcols: int) -> bool:
        nmask=(numcols+7)//8
        if colnum >= numcols:
            return True
        mask=row[-nmask:]
        return not bool(mask[colnum//8] & (1 << (colnum % 8)))

    def _lval_row(self, pg_row: int) -> bytes:
        pg=pg_row >> 8; rid=pg_row & 0xff
        b=self.page(pg)
        if not b or b[0] != 0x01:
            raise ValueError(f"bad LVAL page {pg}")
        n=struct.unpack_from('<H', b, 8)[0]
        if rid >= n:
            raise IndexError(f"bad LVAL row {rid} on page {pg}")
        offsets=[struct.unpack_from('<H', b, 10+2*i)[0] for i in range(n)]
        start=offsets[rid] & 0x1fff
        end=PAGE_SIZE if rid == 0 else (offsets[rid-1] & 0x1fff)
        return b[start:end]

    def _long_value(self, raw: bytes) -> bytes:
        if len(raw) < 12:
            return b''
        desc=struct.unpack_from('<I', raw, 0)[0]
        declared=desc & 0x3fffffff
        if desc & 0x80000000:       # inline
            return raw[12:12+declared]
        pgrow=struct.unpack_from('<I', raw, 4)[0]
        if desc & 0x40000000:       # single LVAL row
            return self._lval_row(pgrow)[:declared or None]
        # multi-page LVAL chain; first 4 bytes of each chunk are next pointer
        out=bytearray(); cur=pgrow
        for _ in range(100000):
            if not cur or not (cur >> 8): break
            chunk=self._lval_row(cur)
            if len(chunk) < 4: break
            cur=struct.unpack_from('<I', chunk, 0)[0]
            out.extend(chunk[4:])
            if declared and len(out) >= declared: break
        return bytes(out[:declared] if declared else out)

    @staticmethod
    def _fixed_value(typ: int, raw: bytes):
        if typ == TYPE_BYTE: return raw[0]
        if typ == TYPE_INT: return struct.unpack('<h', raw[:2])[0]
        if typ == TYPE_LONG: return struct.unpack('<i', raw[:4])[0]
        if typ == TYPE_MONEY: return struct.unpack('<q', raw[:8])[0] / 10000
        if typ == TYPE_FLOAT: return struct.unpack('<f', raw[:4])[0]
        if typ in (TYPE_DOUBLE, TYPE_DATETIME): return struct.unpack('<d', raw[:8])[0]
        if typ == TYPE_REPID: return raw.hex()
        return raw

    def decode_row(self, td: TableDef, row: bytes) -> dict:
        numcols=row[0]
        nmask=(numcols+7)//8
        # All rows needed from this IPS are <256 bytes. Jet3's jump-table extension is
        # unnecessary here; fail loudly on a target row that would need it.
        if len(row) >= 256 and td.num_var:
            raise NotImplementedError("Jet3 variable-offset jump table required for this row")
        var_count=row[-nmask-1] if td.num_var else 0
        voff=[]
        if var_count:
            e=-nmask-1
            packed=row[e-(var_count+1):e]
            voff=list(reversed(packed))
        out={}
        for c in td.cols:
            null=self._is_null(row, c.col_num, numcols)
            if c.typ == TYPE_BOOL:
                out[c.name] = None if c.col_num >= numcols else (not null)
                continue
            if null:
                out[c.name]=None; continue
            if c.flags & 0x01:
                start=1+c.fixed_off; raw=row[start:start+c.length]
                out[c.name]=self._fixed_value(c.typ, raw)
            else:
                if c.var_idx >= var_count or c.var_idx+1 >= len(voff):
                    out[c.name]=None; continue
                raw=row[voff[c.var_idx]:voff[c.var_idx+1]]
                if c.typ == TYPE_TEXT:
                    out[c.name]=raw.decode('cp1252','replace')
                elif c.typ == TYPE_MEMO:
                    out[c.name]=self._long_value(raw).decode('cp1252','replace')
                elif c.typ == TYPE_OLE:
                    out[c.name]=self._long_value(raw)
                else:
                    out[c.name]=raw
        return out

    def rows(self, td: TableDef) -> Iterable[dict]:
        for raw in self._iter_raw_rows(td.page):
            yield self.decode_row(td, raw)

class LuminatorFont:
    """Decode the embedded legacy Luminator .fnt format used by IPS.

    This mirrors the parsing behavior of the supplied Luminator FNT editor:
      * 28-byte prefix; structural bytes 22..25
      * byte 22 = glyph height
      * byte 23 = IPS inter-character spacing
      * bytes 24/25 = contiguous first/last character codes
      * N big-endian glyph-start offsets relative to byte 28
      * optional N+1 end/sentinel offset
      * column-major pixels with reversed 8-row byte chunks
      * truncated zero bytes at the end of a glyph are implicit zeros
    """
    OFFSET_BASE = 28

    def __init__(self, blob: bytes, fallback_height: int = 0):
        if len(blob) < 40:
            raise ValueError("font blob too short")
        self.blob = bytes(blob)
        self.height = self.blob[22] or int(fallback_height or 0)
        self.spacing = self.blob[23]
        self.first = self.blob[24]
        self.last = self.blob[25]
        if not (1 <= self.height <= 32 and self.first <= self.last):
            raise ValueError("font header does not match supported Luminator FNT format")

        self.bytes_per_column = (self.height + 7) // 8
        self.count = self.last - self.first + 1
        b = self.OFFSET_BASE
        table_bytes = self.count * 2
        if b + table_bytes > len(self.blob):
            raise ValueError("font offset table runs past end of blob")

        # N start offsets, all relative to byte 28 and stored big-endian.
        self.offsets = [
            int.from_bytes(self.blob[b + 2*i:b + 2*i + 2], "big")
            for i in range(self.count)
        ]
        if not self.offsets:
            raise ValueError("font contains no glyph offsets")
        if any(self.offsets[i] > self.offsets[i+1] for i in range(len(self.offsets)-1)):
            raise ValueError("font glyph offsets are not monotonically increasing")

        # Normal files have an N+1 end/sentinel offset, but not every valid
        # Luminator font does. Detect it conservatively, exactly as the editor does.
        self.has_end_offset = False
        self.end_offset = None
        sentinel_pos = b + table_bytes
        if sentinel_pos + 2 <= len(self.blob):
            candidate_end = int.from_bytes(self.blob[sentinel_pos:sentinel_pos+2], "big")
            expected_min = table_bytes + 2
            if (
                self.offsets[0] >= expected_min
                and candidate_end >= self.offsets[-1]
                and b + candidate_end <= len(self.blob)
            ):
                self.has_end_offset = True
                self.end_offset = candidate_end

        minimum_table = table_bytes + (2 if self.has_end_offset else 0)
        if self.offsets[0] < minimum_table:
            raise ValueError("first glyph offset overlaps the offset table")

        table_end = b + table_bytes + (2 if self.has_end_offset else 0)
        data_start = b + self.offsets[0]
        if data_start > len(self.blob):
            raise ValueError("first glyph offset points outside font")
        self.table_gap = self.blob[table_end:data_start]

        # Normalize each glyph to complete columns. Some genuine fonts omit
        # trailing zero byte(s), e.g. a 16-high final column whose lower byte is 0.
        self.glyphs: List[bytes] = []
        for i, off in enumerate(self.offsets):
            start = b + off
            if i + 1 < self.count:
                end = b + self.offsets[i+1]
            elif self.has_end_offset:
                end = b + self.end_offset
            else:
                end = len(self.blob)
            if start > end or end > len(self.blob):
                raise ValueError(f"invalid glyph byte range for character {self.first+i:#x}")
            glyph = bytearray(self.blob[start:end])
            rem = len(glyph) % self.bytes_per_column
            if rem:
                glyph.extend(b"\x00" * (self.bytes_per_column - rem))
            self.glyphs.append(bytes(glyph))

    def glyph_width(self, index: int) -> int:
        return len(self.glyphs[index]) // self.bytes_per_column

    def glyph_pixel(self, index: int, x: int, y: int) -> bool:
        glyph = self.glyphs[index]
        chunk = y // 8
        stored_chunk = self.bytes_per_column - 1 - chunk
        p = x * self.bytes_per_column + stored_chunk
        return bool(glyph[p] & (1 << (y % 8)))

    def glyph_index(self, ch: str) -> Optional[int]:
        cp = ord(ch)
        if self.first <= cp <= self.last:
            return cp - self.first
        # Preserve the old exporter's pragmatic fallback for unsupported chars.
        q = ord('?')
        if self.first <= q <= self.last:
            return q - self.first
        return None

    def draw_text(self, pixels, width: int, height: int, x: int, y: int, text: str, ink=1):
        cx = x
        for ch in text:
            idx = self.glyph_index(ch)
            if idx is None:
                continue
            cols = self.glyph_width(idx)
            for dx in range(cols):
                px = cx + dx
                if not (0 <= px < width):
                    continue
                for dy in range(self.height):
                    py = y + dy
                    if 0 <= py < height and self.glyph_pixel(idx, dx, dy):
                        pixels[px, py] = ink
            # Header byte 23 is IPS inter-character spacing, not bytes/column.
            cx += cols + self.spacing
        return cx


class LuminatorGraphic:
    """Decode a Luminator Graphics-table bitmap blob.

    Graphics use the same 28-byte base as FNT files, but contain one bitmap:
    a big-endian start offset and end/sentinel at bytes 28..31, followed by
    column-major pixel data. The Graphics table's GraphicWidth value mirrors
    the blob end offset/file extent and is not the actual pixel width.
    """
    OFFSET_BASE = 28

    def __init__(self, blob: bytes):
        if not blob or len(blob) < 32:
            raise ValueError("graphic blob too short")
        self.blob = bytes(blob)
        self.height = self.blob[22]
        if not (1 <= self.height <= 64):
            raise ValueError("invalid graphic height")
        self.bytes_per_column = (self.height + 7) // 8
        start_off = int.from_bytes(self.blob[28:30], "big")
        end_off = int.from_bytes(self.blob[30:32], "big")
        start = self.OFFSET_BASE + start_off
        end = self.OFFSET_BASE + end_off
        if not (32 <= start <= end <= len(self.blob)):
            # A few utility records omit a meaningful sentinel; use EOF.
            start = 32
            end = len(self.blob)
        raw = bytearray(self.blob[start:end])
        rem = len(raw) % self.bytes_per_column
        if rem:
            raw.extend(b"\x00" * (self.bytes_per_column - rem))
        self.bitmap = bytes(raw)
        self.width = len(self.bitmap) // self.bytes_per_column

    def pixel(self, x: int, y: int) -> bool:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return False
        chunk = y // 8
        stored_chunk = self.bytes_per_column - 1 - chunk
        p = x * self.bytes_per_column + stored_chunk
        return bool(self.bitmap[p] & (1 << (y % 8)))

    def draw(self, pixels, canvas_w: int, canvas_h: int, x: int, y: int, ink=1):
        for dx in range(self.width):
            px = x + dx
            if not (0 <= px < canvas_w):
                continue
            for dy in range(self.height):
                py = y + dy
                if 0 <= py < canvas_h and self.pixel(dx, dy):
                    pixels[px, py] = ink


@dataclass
class GraphicAsset:
    graphic_id: int
    name: str
    mono: Optional[LuminatorGraphic]
    red: Optional[LuminatorGraphic]
    green: Optional[LuminatorGraphic]
    blue: Optional[LuminatorGraphic]
    rgb_levels: Tuple[int, int, int] = (255, 255, 255)

    @property
    def has_rgb(self) -> bool:
        return any((self.red, self.green, self.blue))

    @property
    def width(self) -> int:
        planes = [p for p in (self.mono, self.red, self.green, self.blue) if p]
        return max((p.width for p in planes), default=0)

    @property
    def height(self) -> int:
        planes = [p for p in (self.mono, self.red, self.green, self.blue) if p]
        return max((p.height for p in planes), default=0)

    def render(self, ink=(255,255,255)) -> Image.Image:
        if self.has_rgb:
            img = Image.new('RGB', (self.width, self.height), (0,0,0))
            px = img.load()
            rl, gl, bl = self.rgb_levels
            for x in range(self.width):
                for y in range(self.height):
                    r = rl if self.red and self.red.pixel(x,y) else 0
                    g = gl if self.green and self.green.pixel(x,y) else 0
                    b = bl if self.blue and self.blue.pixel(x,y) else 0
                    if r or g or b:
                        px[x,y] = (r,g,b)
            return img
        img = Image.new('1', (self.width, self.height), 0)
        if self.mono:
            self.mono.draw(img.load(), self.width, self.height, 0, 0, ink=1)
        return img

    def draw_onto(self, image: Image.Image, x: int, y: int, ink=(255,255,255)):
        if self.has_rgb:
            src = self.render()
            if image.mode != 'RGB':
                raise ValueError("RGB graphic requires RGB destination")
            sp = src.load(); dp = image.load()
            for sx in range(src.width):
                dx=x+sx
                if not (0 <= dx < image.width): continue
                for sy in range(src.height):
                    dy=y+sy
                    if not (0 <= dy < image.height): continue
                    c=sp[sx,sy]
                    if c != (0,0,0): dp[dx,dy]=c
        elif self.mono:
            self.mono.draw(image.load(), image.width, image.height, x, y, ink=ink if image.mode=='RGB' else 1)


def safe_name(s: str) -> str:
    s=re.sub(r'[^A-Za-z0-9._-]+','_',s.strip())
    return s or 'SIGN'


def _message_lock_map(db: Jet3Reader) -> Dict[Tuple[int, int], str]:
    """Return display-friendly lock state per (MsgClassID, MsgCode)."""
    states = collections.defaultdict(list)
    try:
        td = db.find_table('ID','MsgCode','MsgClassID','LSignID','Locked')
    except KeyError:
        return {}
    for r in db.rows(td):
        cls=r.get('MsgClassID'); code=r.get('MsgCode')
        if cls is None or code is None:
            continue
        states[(int(cls),int(code))].append(bool(r.get('Locked')))
    out={}
    for key, vals in states.items():
        if vals and all(vals):
            out[key]='Locked'
        elif any(vals):
            out[key]='Partial'
        else:
            out[key]='Unlocked'
    return out


def _message_list_sheets(db: Jet3Reader) -> List[Tuple[str,List[str],List[List[object]]]]:
    """Extract IPS message-list metadata into Excel-friendly sheets."""
    locks=_message_lock_map(db)
    sheets=[]

    try:
        route_td=db.find_table('MsgCode','MsgClassID','Route')
    except KeyError:
        route_td=None
    if route_td:
        names={c.name for c in route_td.cols}
        rows=[]
        for r in db.rows(route_td):
            cls=int(r.get('MsgClassID') or 0); code=r.get('MsgCode')
            if code is None:
                continue
            top = r.get('DestinationTop') if 'DestinationTop' in names else r.get('Destination')
            bot = r.get('DestinationBot') if 'DestinationBot' in names else r.get('Dest2')
            side = r.get('DestinationSide') if 'DestinationSide' in names else ''
            route_side = r.get('RouteSide') if 'RouteSide' in names else ''
            rows.append([int(code), locks.get((cls,int(code)), ''), r.get('Route') or '',
                         top or '', bot or '', side or '', route_side or ''])
        rows.sort(key=lambda x:x[0])
        sheets.append(('Message List', ['MsgCode','Locked','Route','DestinationTop','DestinationBot','DestinationSide','RouteSide'], rows))

    try:
        td=db.find_table('MsgCode','MsgClassID','PRText')
        names={c.name for c in td.cols}; rows=[]
        for r in db.rows(td):
            cls=int(r.get('MsgClassID') or 0); code=r.get('MsgCode')
            if code is None:
                continue
            rows.append([int(code), locks.get((cls,int(code)), ''), r.get('PRText') or '',
                         (r.get('PRBot') or '') if 'PRBot' in names else '',
                         (r.get('PRSide') or '') if 'PRSide' in names else ''])
        rows.sort(key=lambda x:x[0])
        if rows:
            sheets.append(('PR Messages',['MsgCode','Locked','PRText','PRBot','PRSide'],rows))
    except KeyError:
        pass

    try:
        td=db.find_table('MsgCode','MsgClassID','Title','Text'); rows=[]
        for r in db.rows(td):
            cls=int(r.get('MsgClassID') or 0); code=r.get('MsgCode')
            if code is None:
                continue
            rows.append([int(code), locks.get((cls,int(code)), ''), r.get('Title') or '', r.get('Text') or ''])
        rows.sort(key=lambda x:x[0])
        if rows:
            sheets.append(('Text Messages',['MsgCode','Locked','Title','Text'],rows))
    except KeyError:
        pass
    return sheets


def _xlsx_col_name(n: int) -> str:
    out=''
    while n:
        n,rem=divmod(n-1,26)
        out=chr(65+rem)+out
    return out


def _write_simple_xlsx(path: Path, sheets: List[Tuple[str,List[str],List[List[object]]]]) -> None:
    """Write a styled XLSX using only the Python standard library."""
    if not sheets:
        return
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    content_types=['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
      '<Default Extension="xml" ContentType="application/xml"/>',
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
      '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(1,len(sheets)+1):
        content_types.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    content_types.append('</Types>')
    root_rels=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
      '</Relationships>')
    wb_sheets=''.join(f'<sheet name="{xml_escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_,_) in enumerate(sheets,1))
    workbook=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
      f'<sheets>{wb_sheets}</sheets></workbook>')
    wb_rels=['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i in range(1,len(sheets)+1):
        wb_rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
    wb_rels.append(f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    styles=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
      '<fonts count="3"><font><sz val="10"/><name val="Arial"/></font><font><b/><color rgb="FF000000"/><sz val="10"/><name val="Arial"/></font><font><color rgb="FF008000"/><sz val="10"/><name val="Arial"/></font></fonts>'
      '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/><bgColor indexed="64"/></patternFill></fill></fills>'
      '<borders count="2"><border/><border><left style="thin"><color rgb="FF808080"/></left><right style="thin"><color rgb="FF808080"/></right><top style="thin"><color rgb="FF808080"/></top><bottom style="thin"><color rgb="FF808080"/></bottom></border></borders>'
      '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
      '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
      '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
      '<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
      '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf></cellXfs>'
      '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')

    def cell_xml(row,col,val,style=0):
        ref=f'{_xlsx_col_name(col)}{row}'
        if isinstance(val,(int,float)) and not isinstance(val,bool):
            return f'<c r="{ref}" s="{style}"><v>{val}</v></c>'
        txt='' if val is None else str(val)
        preserve=' xml:space="preserve"' if txt[:1].isspace() or txt[-1:].isspace() else ''
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t{preserve}>{xml_escape(txt)}</t></is></c>'

    sheet_xmls=[]
    for name,headers,rows in sheets:
        widths=[]
        for ci,h in enumerate(headers):
            longest=max([len(str(h))]+[len(str(r[ci] if ci<len(r) and r[ci] is not None else '')) for r in rows])
            widths.append(min(max(longest+2,10),42))
        cols=''.join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i,w in enumerate(widths,1))
        row_xml=['<row r="1" ht="20" customHeight="1">'+''.join(cell_xml(1,c,h,1) for c,h in enumerate(headers,1))+'</row>']
        for ri,r in enumerate(rows,2):
            cells=[]
            for ci,val in enumerate(r,1):
                style=3 if ci==1 else (2 if ci==2 else 0)
                cells.append(cell_xml(ri,ci,val,style))
            row_xml.append(f'<row r="{ri}" ht="18" customHeight="1">'+''.join(cells)+'</row>')
        last_col=_xlsx_col_name(len(headers)); last_row=len(rows)+1
        xml=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
             f'<sheetFormatPr defaultRowHeight="18"/><cols>{cols}</cols><sheetData>{"".join(row_xml)}</sheetData>'
             f'<autoFilter ref="A1:{last_col}{last_row}"/></worksheet>')
        sheet_xmls.append(xml)

    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',''.join(content_types))
        z.writestr('_rels/.rels',root_rels)
        z.writestr('xl/workbook.xml',workbook)
        z.writestr('xl/_rels/workbook.xml.rels',''.join(wb_rels))
        z.writestr('xl/styles.xml',styles)
        for i,xml in enumerate(sheet_xmls,1):
            z.writestr(f'xl/worksheets/sheet{i}.xml',xml)


def export_message_list_excel(db: Jet3Reader, path: Path) -> int:
    sheets=_message_list_sheets(db)
    _write_simple_xlsx(path,sheets)
    return sum(len(rows) for _,_,rows in sheets)

def build_export(ips: Path, outdir: Path, all_frames: bool=True, scale: int=1, export_excel: bool=True, progress_callback=None, dedupe_same_size: bool=True) -> dict:
    def _progress(value, message=''):
        if progress_callback:
            try:
                progress_callback(max(0.0, min(100.0, float(value))), message)
            except Exception:
                pass
    _progress(0, 'Opening IPS database…')
    db=Jet3Reader(ips)
    td_sign=db.find_table('LSignID','LSignName','LSignDotHeight','LSignDotWidth')
    td_font=db.find_table('FontID','FontFile','Font')
    td_msg=db.find_table('MsgFrameID','MsgCode','LSignID','Phrase','FontID','XPos','YPos','Frame')

    signs={r['LSignID']:r for r in db.rows(td_sign)}
    font_rows={r['FontID']:r for r in db.rows(td_font)}
    fonts={fid:LuminatorFont(r['Font'], r['FontSize']) for fid,r in font_rows.items() if r.get('Font')}
    msgs=list(db.rows(td_msg))
    _progress(5, f'Loaded {len(msgs):,} message elements…')

    # Decode the Graphics library. Each bitmap plane is a one-glyph FNT-like
    # blob. Spectrum-capable records may contain separate R/G/B planes.
    graphic_assets: Dict[int, GraphicAsset] = {}
    graphic_decode_errors=[]
    try:
        td_graphic=db.find_table('GraphicID','GraphicName','Graphic','RedGraphic','GreenGraphic','BlueGraphic')
        graphic_rows=list(db.rows(td_graphic))
        rgb_levels={}
        try:
            td_gattr=db.find_table('GraphicID','RedAttribute','GreenAttribute','BlueAttribute')
            for ar in db.rows(td_gattr):
                rgb_levels[ar['GraphicID']] = tuple(max(0,min(255,int(ar.get(k) or 0))) for k in ('RedAttribute','GreenAttribute','BlueAttribute'))
        except KeyError:
            pass
        for r in graphic_rows:
            gid=r.get('GraphicID')
            if gid is None:
                continue
            try:
                mono=LuminatorGraphic(r['Graphic']) if r.get('Graphic') else None
                red=LuminatorGraphic(r['RedGraphic']) if r.get('RedGraphic') else None
                green=LuminatorGraphic(r['GreenGraphic']) if r.get('GreenGraphic') else None
                blue=LuminatorGraphic(r['BlueGraphic']) if r.get('BlueGraphic') else None
                # If attribute levels are absent, use full channel intensity.
                levels=rgb_levels.get(gid, (255,255,255))
                graphic_assets[gid]=GraphicAsset(gid, r.get('GraphicName') or f'Graphic_{gid}', mono, red, green, blue, levels)
            except Exception as exc:
                graphic_decode_errors.append((gid, str(exc)))
    except KeyError:
        graphic_rows=[]

    # Spectrum/RGB-capable IPS databases carry per-element color metadata in a
    # companion table. ColorNumber is packed little-endian RGB: 0xBBGGRR.
    # Example: 255 -> red, 65280 -> green, 16711680 -> blue.
    color_queues = collections.defaultdict(list)
    try:
        td_color = db.find_table('MsgClassID','MsgCode','LSign','Frame','Phrase','ColorNumber')
        for r in db.rows(td_color):
            n = r.get('ColorNumber')
            if n is None:
                continue
            n = int(n) & 0xFFFFFF
            rgb = (n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF)
            key = (r.get('MsgClassID'), r.get('MsgCode'), r.get('LSign'),
                   r.get('Frame'), r.get('Phrase') or '', r.get('GraphicID') or 0)
            color_queues[key].append(rgb)
    except KeyError:
        td_color = None

    _progress(10, f'Decoded {len(fonts):,} fonts and {len(graphic_assets):,} graphics…')

    numeric_classes=collections.defaultdict(set)
    for r in msgs: numeric_classes[r['MsgCode']].add(r['MsgClassID'])

    # group elements by class/code/logical sign/frame
    groups=collections.defaultdict(list)
    for r in msgs:
        if r['LSignID'] not in signs: continue
        groups[(r['MsgClassID'],r['MsgCode'],r['LSignID'],r['Frame'])].append(r)

    rendered={}
    skipped=[]
    color_rows_used = 0
    color_groups = 0
    group_items=list(groups.items())
    group_total=max(1,len(group_items))
    for group_i,(key,elements) in enumerate(group_items, start=1):
        if group_i == 1 or group_i == group_total or group_i % max(1, group_total//100) == 0:
            _progress(10 + 55*(group_i/group_total), f'Rendering exposure {group_i:,} of {group_total:,}…')
        cls,code,lsid,frame=key
        sign=signs[lsid]; w=sign['LSignDotWidth']; h=sign['LSignDotHeight']
        # Preserve compact 1-bit output for legacy/monochrome signs. Upgrade to
        # RGB when Spectrum color metadata or an RGB graphic is actually used.
        has_color = any(color_queues.get((cls,code,lsid,frame,e.get('Phrase') or '',e.get('GraphicID') or 0)) for e in elements)
        has_rgb_graphic = any(graphic_assets.get(e.get('GraphicID')).has_rgb for e in elements if e.get('GraphicID') in graphic_assets)
        if has_color or has_rgb_graphic:
            img=Image.new('RGB',(w,h),(0,0,0)); color_groups += 1
        else:
            img=Image.new('1',(w,h),0)
        px=img.load()
        # Stable order. Number is usually zero; MsgFrameID preserves source ordering.
        elements=sorted(elements,key=lambda r:((r.get('Number') or 0),(r.get('MsgFrameID') or 0)))
        # Work on per-group queue copies so duplicate phrases/graphics match in order.
        local_colors = {k:list(v) for k,v in color_queues.items() if k[:4] == (cls,code,lsid,frame)}
        for e in elements:
            phrase=e.get('Phrase') or ''
            gid=e.get('GraphicID') or 0
            # IPS coordinates are 1-based: x=1/y=1 is the upper-left dot.
            x=(e.get('XPos') or 1)-1; y=(e.get('YPos') or 1)-1
            ink = 1 if img.mode == '1' else (255,255,255)
            if img.mode == 'RGB':
                ck=(cls,code,lsid,frame,phrase,gid)
                q=local_colors.get(ck)
                if q:
                    ink=q.pop(0); color_rows_used += 1
            if gid:
                asset=graphic_assets.get(gid)
                if not asset:
                    skipped.append((key,f"missing graphic {gid}")); continue
                asset.draw_onto(img,x,y,ink=ink)
                continue
            if not phrase:
                continue
            fid=e.get('FontID')
            font=fonts.get(fid)
            if not font:
                skipped.append((key,f"missing font {fid}")); continue
            font.draw_text(px,w,h,x,y,phrase,ink=ink)
        rendered[key]=(img,sign)

    # Select exposures. By default export every exposure; the first keeps the
    # traditional bare code filename and later exposures get __expNN suffixes.
    selected=[]
    by_combo=collections.defaultdict(list)
    for key in rendered: by_combo[key[:3]].append(key)
    exposure_index = {}
    for combo,keys in by_combo.items():
        keys=sorted(keys,key=lambda k:k[3])
        chosen = keys if all_frames else keys[:1]
        selected.extend(chosen)
        for idx, key in enumerate(keys, start=1):
            exposure_index[key] = idx

    # Need collision awareness among same class/code/size/frame (SIDE1/SIDE2, etc.).
    buckets=collections.defaultdict(list)
    for key in selected:
        img,sign=rendered[key]; cls,code,lsid,frame=key
        size=(sign['LSignDotWidth'],sign['LSignDotHeight'])
        buckets[(cls,code,size,frame)].append((key,img,sign))

    outdir.mkdir(parents=True,exist_ok=True)
    files=[]
    manifest_rows=[]
    deduped=0
    bucket_items=sorted(buckets.items())
    bucket_total=max(1,len(bucket_items))
    for bucket_i,((cls,code,(w,h),frame),items) in enumerate(bucket_items, start=1):
        if bucket_i == 1 or bucket_i == bucket_total or bucket_i % max(1, bucket_total//100) == 0:
            _progress(65 + 25*(bucket_i/bucket_total), f'Writing sign PNG {bucket_i:,} of {bucket_total:,}…')
        # For normal PNG exports, identical same-size logical signs can be
        # deduplicated to reduce file count.  Database converters must disable
        # this: SIDE1 and SIDE2 are distinct physical outputs even when their
        # pixels happen to be identical for a particular code/exposure.
        uniq=[]
        if dedupe_same_size:
            seen={}
            for key,img,sign in items:
                payload=img.tobytes()
                if payload in seen:
                    deduped += 1; continue
                seen[payload]=True; uniq.append((key,img,sign))
        else:
            uniq=list(items)
        folder=outdir/f'{w}x{h}'; folder.mkdir(parents=True,exist_ok=True)
        class_suffix=f'__class{cls}' if len(numeric_classes[code])>1 else ''
        # Keep exposure 1 backward-compatible (e.g. 1832.png).
        # Only subsequent exposures receive a suffix.
        exp_idx = exposure_index[items[0][0]]
        frame_suffix = '' if exp_idx == 1 else f'__exp{exp_idx:02d}'
        multi_same_size=len(uniq)>1
        for key,img,sign in uniq:
            sign_suffix=f"__{safe_name(sign['LSignName'])}" if multi_same_size else ''
            fn=f'{code}{class_suffix}{sign_suffix}{frame_suffix}.png'
            dest=folder/fn
            if scale > 1:
                img=img.resize((w*scale,h*scale),Image.Resampling.NEAREST)
            img.save(dest, optimize=False)
            files.append(dest)
            manifest_rows.append((dest.relative_to(outdir).as_posix(), cls, code, sign['LSignName'], w, h, frame, exposure_index[key]))

    # Export the database Graphics library independently of message usage.
    graphic_files=[]
    if graphic_assets:
        gfolder=outdir/'Graphics'
        gfolder.mkdir(parents=True,exist_ok=True)
        graphic_items=sorted(graphic_assets.items())
        graphic_total=max(1,len(graphic_items))
        for graphic_i,(gid,asset) in enumerate(graphic_items, start=1):
            if graphic_i == 1 or graphic_i == graphic_total or graphic_i % max(1, graphic_total//50) == 0:
                _progress(90 + 5*(graphic_i/graphic_total), f'Writing graphic {graphic_i:,} of {graphic_total:,}…')
            if asset.width <= 0 or asset.height <= 0:
                continue
            gimg=asset.render()
            if scale > 1:
                gimg=gimg.resize((gimg.width*scale,gimg.height*scale),Image.Resampling.NEAREST)
            gdest=gfolder/f'{gid:04d}_{safe_name(asset.name)}.png'
            gimg.save(gdest,optimize=False)
            graphic_files.append(gdest)

    # A machine-readable manifest is useful for code/class/sign/frame traceability.
    _progress(96, 'Writing manifest…')
    manifest=outdir/'manifest.tsv'
    with manifest.open('w',encoding='utf-8',newline='') as f:
        f.write('relative_path\tmsg_class\tmsg_code\tlogical_sign\twidth\theight\tframe\texposure\n')
        for rel,cls,code,sign_name,w,h,frame,exposure in manifest_rows:
            f.write(f"{rel}\t{cls}\t{code}\t{sign_name}\t{w}\t{h}\t{frame}\t{exposure}\n")

    message_list_rows=0
    message_list_path=None
    if export_excel:
        message_list_path=outdir/'message_list.xlsx'
        _progress(98, 'Writing message-list workbook…')
        message_list_rows=export_message_list_excel(db,message_list_path)

    _progress(100, 'IPS rendering complete')
    return dict(
        logical_signs=len(signs), fonts=len(fonts), message_rows=len(msgs),
        sign_codes=len(set((r['MsgClassID'],r['MsgCode']) for r in msgs)),
        numeric_codes=len(set(r['MsgCode'] for r in msgs)),
        sign_pngs=len(files), graphic_pngs=len(graphic_files), exported_pngs=len(files)+len(graphic_files),
        graphics_decoded=len(graphic_assets), graphic_decode_errors=graphic_decode_errors,
        color_metadata_rows=sum(len(v) for v in color_queues.values()), color_rows_used=color_rows_used, color_groups=color_groups,
        same_size_duplicates_deduped=deduped, skipped=len(skipped), files=files+graphic_files,
        signs=signs, skipped_details=skipped[:20], message_list_rows=message_list_rows, message_list_path=message_list_path
    )


def _print_stats(stats: dict):
    print(f"Logical signs: {stats['logical_signs']}")
    for s in stats['signs'].values():
        print(f"  {s['LSignName']}: {s['LSignDotWidth']}x{s['LSignDotHeight']}")
    print(f"Fonts decoded: {stats['fonts']}")
    print(f"Message rows: {stats['message_rows']}")
    print(f"Class/code pairs: {stats['sign_codes']} ({stats['numeric_codes']} numeric codes)")
    print(f"Sign PNGs written: {stats.get('sign_pngs', stats['exported_pngs'])}")
    if stats.get('graphic_pngs') is not None:
        print(f"Graphics PNGs written: {stats.get('graphic_pngs', 0)}")
    print(f"PNG files written: {stats['exported_pngs']}")
    if stats.get('color_metadata_rows'):
        print(f"Spectrum color rows: {stats['color_metadata_rows']} ({stats.get('color_rows_used', 0)} applied across {stats.get('color_groups', 0)} RGB renders)")
    print(f"Identical same-size sign renders deduplicated: {stats['same_size_duplicates_deduped']}")
    print(f"Skipped elements: {stats['skipped']}")
    if stats.get('message_list_path'):
        print(f"Message list Excel: {stats['message_list_path']} ({stats.get('message_list_rows',0)} rows)")
    if stats['skipped_details']:
        for x in stats['skipped_details']:
            print('  ', x)


def launch_gui(initial_ips: Optional[Path] = None):
    """Launch a small Tk desktop wrapper around build_export()."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as e:
        raise SystemExit(
            "Tkinter is required for the graphical interface. "
            "Use the command-line mode instead."
        ) from e

    root = tk.Tk()
    root.title("Luminator IPS → PNG Exporter")
    root.geometry("720x500")
    root.minsize(650, 430)

    ips_var = tk.StringVar(value=str(initial_ips) if initial_ips else "")
    out_var = tk.StringVar()
    all_frames_var = tk.BooleanVar(value=True)
    excel_var = tk.BooleanVar(value=True)
    scale_var = tk.IntVar(value=1)
    status_var = tk.StringVar(value="Choose an IPS database and output folder.")

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(1, weight=1)
    outer.rowconfigure(6, weight=1)

    title = ttk.Label(outer, text="Luminator IPS → PNG Exporter", font=("TkDefaultFont", 15, "bold"))
    title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))

    ttk.Label(outer, text="IPS file").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=5)
    ips_entry = ttk.Entry(outer, textvariable=ips_var)
    ips_entry.grid(row=1, column=1, sticky="ew", pady=5)

    def choose_ips():
        path = filedialog.askopenfilename(
            title="Choose Luminator IPS database",
            filetypes=[("Luminator IPS", "*.ips"), ("All files", "*.*")],
        )
        if path:
            ips_var.set(path)
            if not out_var.get().strip():
                src = Path(path)
                out_var.set(str(src.with_name(src.stem + "_png")))

    ttk.Button(outer, text="Browse…", command=choose_ips).grid(row=1, column=2, padx=(8, 0), pady=5)

    ttk.Label(outer, text="Output folder").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=5)
    out_entry = ttk.Entry(outer, textvariable=out_var)
    out_entry.grid(row=2, column=1, sticky="ew", pady=5)

    def choose_output():
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            out_var.set(path)

    ttk.Button(outer, text="Browse…", command=choose_output).grid(row=2, column=2, padx=(8, 0), pady=5)

    options = ttk.Frame(outer)
    options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 8))
    ttk.Checkbutton(options, text="Export all exposures", variable=all_frames_var).pack(side="left")
    ttk.Checkbutton(options, text="Message list Excel", variable=excel_var).pack(side="left", padx=(18,0))
    ttk.Label(options, text="Scale:").pack(side="left", padx=(24, 6))
    scale_spin = ttk.Spinbox(options, from_=1, to=20, width=5, textvariable=scale_var)
    scale_spin.pack(side="left")
    ttk.Label(options, text="×  (1 = exact dot resolution)").pack(side="left", padx=(5, 0))

    progress = ttk.Progressbar(outer, mode="indeterminate")
    progress.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(5, 5))
    ttk.Label(outer, textvariable=status_var).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))

    log = tk.Text(outer, height=12, wrap="word", state="disabled")
    log.grid(row=6, column=0, columnspan=3, sticky="nsew")
    log_scroll = ttk.Scrollbar(outer, orient="vertical", command=log.yview)
    log_scroll.grid(row=6, column=3, sticky="ns")
    log.configure(yscrollcommand=log_scroll.set)

    button_row = ttk.Frame(outer)
    button_row.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(12, 0))
    button_row.columnconfigure(0, weight=1)

    def append_log(text: str):
        log.configure(state="normal")
        log.insert("end", text.rstrip() + "\n")
        log.see("end")
        log.configure(state="disabled")

    def set_running(running: bool):
        state = "disabled" if running else "normal"
        export_btn.configure(state=state)
        ips_entry.configure(state=state)
        out_entry.configure(state=state)
        scale_spin.configure(state=state)
        if running:
            progress.start(12)
        else:
            progress.stop()

    def finished_ok(stats: dict, output: Path):
        set_running(False)
        status_var.set(f"Done — {stats['exported_pngs']} PNG files written.")
        append_log("")
        append_log(f"Logical signs: {stats['logical_signs']}")
        append_log(f"Fonts decoded: {stats['fonts']}")
        append_log(f"Message rows: {stats['message_rows']}")
        append_log(f"Sign PNGs: {stats.get('sign_pngs', stats['exported_pngs'])}")
        append_log(f"Graphics PNGs: {stats.get('graphic_pngs', 0)}")
        append_log(f"PNG files written: {stats['exported_pngs']}")
        if stats.get('color_metadata_rows'):
            append_log(f"Spectrum color rows: {stats['color_metadata_rows']} ({stats.get('color_rows_used', 0)} applied)")
        append_log(f"Duplicates deduplicated: {stats['same_size_duplicates_deduped']}")
        append_log(f"Skipped elements: {stats['skipped']}")
        append_log(f"Manifest: {output / 'manifest.tsv'}")
        if stats.get('message_list_path'):
            append_log(f"Message list: {stats['message_list_path']} ({stats.get('message_list_rows',0)} rows)")
        messagebox.showinfo(
            "Export complete",
            f"Exported {stats['exported_pngs']} PNG files to:\n{output}",
            parent=root,
        )

    def finished_error(exc: Exception):
        set_running(False)
        status_var.set("Export failed.")
        append_log(f"ERROR: {exc}")
        messagebox.showerror("Export failed", str(exc), parent=root)

    def run_export():
        ips_text = ips_var.get().strip()
        out_text = out_var.get().strip()
        if not ips_text:
            messagebox.showwarning("Missing IPS file", "Choose an IPS database first.", parent=root)
            return
        if not out_text:
            messagebox.showwarning("Missing output folder", "Choose an output folder first.", parent=root)
            return
        try:
            scale = int(scale_var.get())
            if scale < 1:
                raise ValueError
        except Exception:
            messagebox.showwarning("Invalid scale", "Scale must be an integer of 1 or greater.", parent=root)
            return

        ips = Path(ips_text)
        output = Path(out_text)
        if not ips.is_file():
            messagebox.showerror("File not found", f"IPS file does not exist:\n{ips}", parent=root)
            return

        set_running(True)
        status_var.set("Exporting…")
        append_log(f"Source: {ips}")
        append_log(f"Output: {output}")
        append_log(f"All exposures: {'yes' if all_frames_var.get() else 'no'}; scale: {scale}×; Excel list: {'yes' if excel_var.get() else 'no'}")

        all_frames = bool(all_frames_var.get())

        def worker():
            try:
                stats = build_export(ips, output, all_frames, scale, bool(excel_var.get()))
            except Exception as exc:
                root.after(0, lambda e=exc: finished_error(e))
                return
            root.after(0, lambda: finished_ok(stats, output))

        threading.Thread(target=worker, daemon=True).start()

    export_btn = ttk.Button(button_row, text="Export PNGs", command=run_export)
    export_btn.grid(row=0, column=1, padx=(8, 0))
    ttk.Button(button_row, text="Close", command=root.destroy).grid(row=0, column=2, padx=(8, 0))

    if initial_ips and not out_var.get():
        out_var.set(str(initial_ips.with_name(initial_ips.stem + "_png")))

    root.mainloop()


def _is_frozen_executable() -> bool:
    """True when running from a bundler such as PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def _show_fatal_gui_error(exc: BaseException):
    """Best-effort error dialog for windowed/frozen builds with no console."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Luminator IPS Exporter",
            f"The application could not start:\n\n{exc}",
            parent=root,
        )
        root.destroy()
    except Exception:
        # A console build will still get the traceback from the outer handler.
        pass


def main():
    frozen = _is_frozen_executable()

    # A packaged Windows build is GUI-first. This also makes Windows
    # "Open with..." / drag-and-drop useful: an IPS path passed by Explorer
    # pre-populates the GUI instead of launching an invisible CLI process.
    # Use --cli explicitly if command-line behavior is desired from the EXE.
    if frozen and '--cli' not in sys.argv[1:]:
        initial_ips = None
        for arg in sys.argv[1:]:
            if not arg.startswith('-'):
                candidate = Path(arg)
                if candidate.suffix.lower() == '.ips':
                    initial_ips = candidate
                    break
        launch_gui(initial_ips)
        return

    # In normal Python use, no arguments still opens the desktop UI.
    if len(sys.argv) == 1:
        launch_gui()
        return

    ap = argparse.ArgumentParser(
        description='Export Luminator IPS sign codes as bitmap PNGs without Luminator software.'
    )
    ap.add_argument('ips', type=Path, nargs='?', help='Luminator .ips database')
    ap.add_argument('-o', '--out', type=Path, default=None, help='output folder (default: <IPS name>_png beside the source)')
    ap.add_argument('--all-frames', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--first-exposure-only', action='store_true', help='export only the first exposure for each sign code')
    ap.add_argument('--scale', type=int, default=1, help='nearest-neighbor PNG scale factor (default 1 = exact dot dimensions)')
    ap.add_argument('--no-message-list', action='store_true', help='do not create message_list.xlsx')
    ap.add_argument('--gui', action='store_true', help='open the graphical interface')
    ap.add_argument('--cli', action='store_true', help='force command-line mode (mainly useful for packaged EXEs)')
    args = ap.parse_args()

    if args.gui:
        launch_gui(args.ips)
        return
    if args.ips is None:
        ap.error('IPS path is required unless --gui is used')
    if args.scale < 1:
        ap.error('--scale must be >= 1')

    out = args.out or args.ips.with_name(args.ips.stem + '_png')

    # All exposures are now the default. --all-frames remains accepted for
    # compatibility with older command lines; --first-exposure-only opts out.
    stats = build_export(args.ips, out, not args.first_exposure_only, args.scale, not args.no_message_list)
    _print_stats(stats)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if _is_frozen_executable():
            _show_fatal_gui_error(exc)
        raise
