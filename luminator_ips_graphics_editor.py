from __future__ import annotations
import os, sys, shutil, tempfile, re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import pyodbc
except Exception:
    pyodbc = None
from PIL import Image
from PyQt6.QtCore import Qt, QSize, QTimer, QByteArray, pyqtSignal, QPoint, QEvent
from PyQt6.QtGui import QAction, QColor, QPainter, QPen, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,QMainWindow,QWidget,QFileDialog,QMessageBox,QVBoxLayout,QHBoxLayout,
    QLabel,QPushButton,QListWidget,QScrollArea,QToolBar,QStatusBar,QComboBox,
    QLineEdit,QFormLayout,QDialog,QDialogButtonBox,QSpinBox,QGroupBox,QFrame,
    QGridLayout,QSlider,QInputDialog,QPlainTextEdit,QProgressBar
)

from PyQt6.QtSvg import QSvgRenderer

_UNDO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
     stroke="#F8FAFC" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="9 14 4 9 9 4"></polyline>
  <path d="M20 20v-7a4 4 0 0 0-4-4H4"></path>
</svg>
"""

_REDO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
     stroke="#F8FAFC" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="15 14 20 9 15 4"></polyline>
  <path d="M4 20v-7a4 4 0 0 1 4-4h12"></path>
</svg>
"""

def _svg_icon(svg_text, size=18):
    renderer=QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    pixmap=QPixmap(size,size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter=QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)

COLORS = [
    ("Off", (0,0,0)), ("Red", (255,0,0)), ("Green", (0,255,0)),
    ("Amber", (255,255,0)), ("Blue", (0,0,255)), ("Magenta", (255,0,255)),
    ("Cyan", (0,255,255)), ("White", (255,255,255)),
]

@dataclass
class WrappedBlob:
    raw: bytes
    prefix: bytes
    payload: bytes
    suffix: bytes

@dataclass
class GraphicRecord:
    graphic_id: object
    name: str
    description: str
    width: int
    height: int
    stored_width: int
    ctype: object
    allow_mod: object
    pixels: list[list[tuple[int,int,int]]]
    plane_wrappers: dict[str, WrappedBlob]
    layout: str
    storage_mode: str = "rgb"

class PlaneCodec:
    """Codec for the native IPS v3.8 graphic plane record.

    A database long-binary value contains a 32-byte IPS record header followed
    by the 1-bit pixel plane. Pixels are stored by column. For heights above
    eight pixels, the highest 8-row chunk is stored first, while bits within
    each byte are LSB-first from top to bottom.  For a 16-pixel-high graphic:

        y 0..7   -> second byte of each column, bits 0..7
        y 8..15  -> first byte of each column,  bits 0..7

    This is the layout reproduced by IPS v3.8 for the supplied Xmascane/Turkey
    records. The previous heuristic layout/offset selection is intentionally
    gone: it could make random bytes look like valid artwork.
    """
    HEADER_SIZE = 32
    LAYOUT = "ips-column"

    @staticmethod
    def bytes_per_column(h):
        return max(1, (int(h) + 7) // 8)

    @classmethod
    def payload_size(cls, w, h):
        return int(w) * cls.bytes_per_column(h)

    @classmethod
    def visible_width_from_blob(cls, value, h):
        raw = bytes(value or b"")
        bpc = cls.bytes_per_column(h)
        n = len(raw) - cls.HEADER_SIZE
        if n >= 0 and n % bpc == 0:
            return n // bpc
        return None

    @classmethod
    def unwrap(cls, value, w, h):
        raw = bytes(value or b"")
        need = cls.payload_size(w, h)
        if len(raw) >= cls.HEADER_SIZE:
            prefix = raw[:cls.HEADER_SIZE]
            available = raw[cls.HEADER_SIZE:]
            payload = available[:need].ljust(need, b"\0")
            suffix = available[need:]
            return WrappedBlob(raw, prefix, payload, suffix)
        # Corrupt/atypical record fallback: keep the bytes, but never scan for an
        # arbitrary 'best-looking' offset. The payload starts at byte zero.
        payload = raw[:need].ljust(need, b"\0")
        return WrappedBlob(raw, b"", payload, raw[need:])

    @classmethod
    def decode(cls, payload, w, h):
        out = [[False] * w for _ in range(h)]
        bpc = cls.bytes_per_column(h)
        for x in range(w):
            for y in range(h):
                # IPS stores vertical byte chunks in reverse chunk order.
                stored_chunk = bpc - 1 - (y // 8)
                pos = x * bpc + stored_chunk
                if pos < len(payload):
                    out[y][x] = bool(payload[pos] & (1 << (y % 8)))
        return out

    @classmethod
    def encode(cls, bits, w, h):
        out = bytearray(cls.payload_size(w, h))
        bpc = cls.bytes_per_column(h)
        for y in range(h):
            for x in range(w):
                if bits[y][x]:
                    stored_chunk = bpc - 1 - (y // 8)
                    pos = x * bpc + stored_chunk
                    out[pos] |= 1 << (y % 8)
        return bytes(out)

    @classmethod
    def rebuild(cls, wrap, payload, name="", description="", *, width=None, height=None, stored_width=None):
        """Rebuild a native IPS plane and keep its dimension metadata in sync.

        In the supplied IPS v3.8 database the 32-byte plane header repeats the
        dimensions independently of the Graphics table fields:

            header[22] = visible height
            header[31] = packed GraphicWidth value

        IPS reads these bytes when it opens the graphic. All other header bytes
        are preserved byte-for-byte.
        """
        if wrap:
            prefix=bytearray(wrap.prefix)
            if len(prefix)>=cls.HEADER_SIZE and width is not None and height is not None:
                width=int(width);height=int(height)
                if stored_width is None:
                    stored_width=4+width*cls.bytes_per_column(height)
                stored_width=int(stored_width)
                if not 1<=width<=255:
                    raise ValueError("IPS graphic width must be between 1 and 255 pixels.")
                if not 1<=height<=255:
                    raise ValueError("IPS graphic height must be between 1 and 255 pixels.")
                if not 0<=stored_width<=255:
                    raise ValueError("This canvas is too wide for the IPS native graphic header at this height.")
                prefix[22]=height & 0xFF
                prefix[31]=stored_width & 0xFF
            return bytes(prefix)+payload+wrap.suffix
        return payload

class IPSDatabase:
    FIELDS=("GraphicID","GraphicName","GraphicDescription","GraphicHeight","GraphicWidth",
            "GraphicAllowMod","GraphicCType","Graphic","RedGraphic","GreenGraphic","BlueGraphic")
    def __init__(self,path):
        self.path=Path(path); self.conn=None; self.backend=None

    @staticmethod
    def driver():
        if pyodbc is None: return None
        for d in reversed(pyodbc.drivers()):
            if "Access Driver" in d and ("*.mdb" in d or "*.accdb" in d): return d
        return None

    @staticmethod
    def _legacy_powershell():
        if os.name != "nt": return None
        windir=os.environ.get("WINDIR", r"C:\Windows")
        candidates=[Path(windir)/"SysWOW64"/"WindowsPowerShell"/"v1.0"/"powershell.exe",
                    Path(windir)/"System32"/"WindowsPowerShell"/"v1.0"/"powershell.exe"]
        return next((str(x) for x in candidates if x.exists()), None)

    def _run_jet(self, operation, payload=None):
        import json, subprocess
        ps=self._legacy_powershell()
        if not ps: raise RuntimeError("Legacy Jet fallback is only available on Windows.")
        data={"path":str(self.path),"operation":operation,"payload":payload or {}}
        bridge=r'''param([string]$InputJson,[string]$OutputJson)
$ErrorActionPreference='Stop'
$d=Get-Content -Raw -LiteralPath $InputJson | ConvertFrom-Json
$c=New-Object -ComObject ADODB.Connection
try {
  $c.Open("Provider=Microsoft.Jet.OLEDB.4.0;Data Source=" + $d.path + ";Persist Security Info=False;")
  if($d.operation -eq 'list') {
    $rs=New-Object -ComObject ADODB.Recordset
    $rs.Open("SELECT [GraphicID],[GraphicName],[GraphicDescription],[GraphicHeight],[GraphicWidth],[GraphicAllowMod],[GraphicCType],[Graphic],[RedGraphic],[GreenGraphic],[BlueGraphic] FROM [Graphics] ORDER BY [GraphicName]",$c,0,1)
    $rows=@()
    while(-not $rs.EOF) {
      $a=@()
      for($i=0;$i -lt 11;$i++) {
        $v=$rs.Fields.Item($i).Value
        if($i -ge 7) {
          if($null -eq $v -or $v -is [System.DBNull]) {$a += $null}
          else {$a += @{__bytes__=[Convert]::ToBase64String([byte[]]$v)}}
        } else {
          if($null -eq $v -or $v -is [System.DBNull]) {$a += $null} else {$a += $v}
        }
      }
      $rows += ,$a; $rs.MoveNext()
    }
    $rs.Close()
    @{ok=$true;rows=$rows} | ConvertTo-Json -Depth 8 -Compress | Set-Content -Encoding UTF8 -LiteralPath $OutputJson
  } elseif($d.operation -eq 'save') {
    $p=$d.payload
    $cmd=New-Object -ComObject ADODB.Command; $cmd.ActiveConnection=$c
    $cmd.CommandText='UPDATE [Graphics] SET [GraphicName]=?,[GraphicDescription]=?,[GraphicHeight]=?,[GraphicWidth]=?,[Graphic]=?,[RedGraphic]=?,[GreenGraphic]=?,[BlueGraphic]=? WHERE [GraphicID]=?'
    [void]$cmd.Parameters.Append($cmd.CreateParameter('n',202,1,255,[string]$p.name))
    [string]$desc=[string]$p.description
    [void]$cmd.Parameters.Append($cmd.CreateParameter('d',203,1,[Math]::Max(1,$desc.Length),$desc))
    [void]$cmd.Parameters.Append($cmd.CreateParameter('h',3,1,0,[int]$p.height))
    [void]$cmd.Parameters.Append($cmd.CreateParameter('w',3,1,0,[int]$p.width))
    foreach($key in @('graphic','red','green','blue')) {
      [byte[]]$b=[Convert]::FromBase64String([string]$p.$key)
      $par=$cmd.CreateParameter($key,205,1,$b.Length); $par.AppendChunk($b); [void]$cmd.Parameters.Append($par)
    }
    [void]$cmd.Parameters.Append($cmd.CreateParameter('id',3,1,0,[int]$p.id))
    [void]$cmd.Execute()
    @{ok=$true} | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath $OutputJson
  } elseif($d.operation -eq 'save_mono') {
    $p=$d.payload
    $cmd=New-Object -ComObject ADODB.Command; $cmd.ActiveConnection=$c
    $cmd.CommandText='UPDATE [Graphics] SET [GraphicName]=?,[GraphicDescription]=?,[GraphicHeight]=?,[GraphicWidth]=?,[Graphic]=? WHERE [GraphicID]=?'
    [void]$cmd.Parameters.Append($cmd.CreateParameter('n',202,1,255,[string]$p.name))
    [string]$desc=[string]$p.description
    [void]$cmd.Parameters.Append($cmd.CreateParameter('d',203,1,[Math]::Max(1,$desc.Length),$desc))
    [void]$cmd.Parameters.Append($cmd.CreateParameter('h',3,1,0,[int]$p.height))
    [void]$cmd.Parameters.Append($cmd.CreateParameter('w',3,1,0,[int]$p.width))
    [byte[]]$b=[Convert]::FromBase64String([string]$p.graphic)
    $par=$cmd.CreateParameter('graphic',205,1,$b.Length); $par.AppendChunk($b); [void]$cmd.Parameters.Append($par)
    [void]$cmd.Parameters.Append($cmd.CreateParameter('id',3,1,0,[int]$p.id))
    [void]$cmd.Execute()
    @{ok=$true} | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath $OutputJson
  } elseif($d.operation -eq 'duplicate') {
    $p=$d.payload
    $cmd=New-Object -ComObject ADODB.Command; $cmd.ActiveConnection=$c
    $cmd.CommandText="INSERT INTO [Graphics] ([GraphicName],[GraphicDescription],[GraphicHeight],[GraphicWidth],[GraphicAllowMod],[GraphicCType],[Graphic],[RedGraphic],[GreenGraphic],[BlueGraphic]) SELECT ?, '', [GraphicHeight],[GraphicWidth],[GraphicAllowMod],[GraphicCType],[Graphic],[RedGraphic],[GreenGraphic],[BlueGraphic] FROM [Graphics] WHERE [GraphicID]=?"
    [void]$cmd.Parameters.Append($cmd.CreateParameter('n',202,1,255,[string]$p.name))
    [void]$cmd.Parameters.Append($cmd.CreateParameter('id',3,1,0,[int]$p.source_id))
    [void]$cmd.Execute()
    # Jet/ADO's @@IDENTITY is unreliable for INSERT ... SELECT in some old IPS
    # databases. GraphicName is unique in the editor, so resolve the inserted
    # row explicitly by name instead of trusting @@IDENTITY.
    $lookup=New-Object -ComObject ADODB.Command; $lookup.ActiveConnection=$c
    $lookup.CommandText='SELECT TOP 1 [GraphicID] FROM [Graphics] WHERE [GraphicName]=? ORDER BY [GraphicID] DESC'
    [void]$lookup.Parameters.Append($lookup.CreateParameter('n',202,1,255,[string]$p.name))
    $rs=$lookup.Execute()
    if($rs.EOF) { throw 'The cloned graphic was inserted, but its GraphicID could not be resolved.' }
    $newid=[int]$rs.Fields.Item(0).Value
    $rs.Close()
    @{ok=$true;id=$newid} | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath $OutputJson
  } elseif($d.operation -eq 'rename') {
    $p=$d.payload
    $cmd=New-Object -ComObject ADODB.Command; $cmd.ActiveConnection=$c
    $cmd.CommandText='UPDATE [Graphics] SET [GraphicName]=? WHERE [GraphicID]=?'
    [void]$cmd.Parameters.Append($cmd.CreateParameter('n',202,1,255,[string]$p.name))
    [void]$cmd.Parameters.Append($cmd.CreateParameter('id',3,1,0,[int]$p.id))
    [void]$cmd.Execute()
    @{ok=$true} | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath $OutputJson
  } elseif($d.operation -eq 'delete') {
    $p=$d.payload
    $cmd=New-Object -ComObject ADODB.Command; $cmd.ActiveConnection=$c
    $cmd.CommandText='DELETE FROM [Graphics] WHERE [GraphicID]=?'
    [void]$cmd.Parameters.Append($cmd.CreateParameter('id',3,1,0,[int]$p.id))
    [void]$cmd.Execute()
    @{ok=$true} | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath $OutputJson
  } else { throw 'Unknown bridge operation' }
} catch {
  @{ok=$false;error=$_.Exception.Message} | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath $OutputJson
  exit 2
} finally { if($c.State -ne 0){$c.Close()} }
'''
        with tempfile.TemporaryDirectory(prefix="ipsjet_") as td:
            td=Path(td); inp=td/"in.json"; out=td/"out.json"; script=td/"bridge.ps1"
            inp.write_text(json.dumps(data),encoding="utf-8"); script.write_text(bridge,encoding="utf-8")
            cp=subprocess.run([ps,"-NoProfile","-ExecutionPolicy","Bypass","-File",str(script),str(inp),str(out)],capture_output=True,text=True)
            if not out.exists(): raise RuntimeError("Legacy Jet bridge failed: "+(cp.stderr.strip() or cp.stdout.strip() or "unknown error"))
            result=json.loads(out.read_text(encoding="utf-8-sig"))
            if not result.get("ok"): raise RuntimeError(result.get("error","Legacy Jet bridge failed."))
            return result

    def open(self):
        d=self.driver(); odbc_error=None
        if d:
            try:
                self.conn=pyodbc.connect(f"DRIVER={{{d}}};DBQ={self.path};", autocommit=False)
                names={r.table_name.lower():r.table_name for r in self.conn.cursor().tables(tableType="TABLE")}
                if "graphics" not in names: raise RuntimeError("This database has no Graphics table.")
                self.backend="odbc"; return
            except Exception as e:
                odbc_error=e
                try:
                    if self.conn:self.conn.close()
                except: pass
                self.conn=None
        try:
            self._run_jet("list"); self.backend="jet4"
        except Exception as jet_error:
            extra=f"\n\nODBC error: {odbc_error}" if odbc_error else ""
            raise RuntimeError(f"Could not open this legacy IPS database.\nJet 4.0 fallback error: {jet_error}{extra}")

    def close(self):
        if self.conn:
            try:self.conn.close()
            except:pass
        self.conn=None

    def list_graphics(self):
        if self.backend=="jet4":
            import base64
            rows=self._run_jet("list")["rows"]; fixed=[]
            for row in rows:
                fixed.append([base64.b64decode(v["__bytes__"]) if isinstance(v,dict) and "__bytes__" in v else v for v in row])
            return fixed
        cur=self.conn.cursor(); q=",".join(f"[{x}]" for x in self.FIELDS)
        return cur.execute(f"SELECT {q} FROM [Graphics] ORDER BY [GraphicName]").fetchall()

    def load_record(self,row):
        d = dict(zip(self.FIELDS, row))
        h = max(1, int(d["GraphicHeight"] or 1))
        stored_w = max(1, int(d["GraphicWidth"] or 1))
        bpc = PlaneCodec.bytes_per_column(h)

        # IPS stores GraphicWidth as the packed pixel-byte width plus four.  The
        # long-binary records independently confirm the visible width because a
        # valid plane is exactly 32-byte header + width*bytes_per_column bytes.
        width_from_field = None
        if stored_w >= 4 and (stored_w - 4) % bpc == 0:
            width_from_field = (stored_w - 4) // bpc

        try:
            ctype = int(d["GraphicCType"] or 0)
        except Exception:
            ctype = 0
        color_mode = (ctype == 1)

        source_keys = ("RedGraphic", "GreenGraphic", "BlueGraphic") if color_mode else ("Graphic",)
        blob_widths = []
        for key in source_keys:
            bw = PlaneCodec.visible_width_from_blob(d.get(key), h)
            if bw:
                blob_widths.append(bw)
        # If CType says color but the RGB values are absent, allow Graphic to
        # provide the width; likewise for malformed old records.
        if not blob_widths:
            for key in ("Graphic", "RedGraphic", "GreenGraphic", "BlueGraphic"):
                bw = PlaneCodec.visible_width_from_blob(d.get(key), h)
                if bw:
                    blob_widths.append(bw)

        # GraphicWidth is the canvas width used by IPS.  Do NOT shrink the
        # canvas to the binary plane length: some IPS graphics intentionally
        # contain blank columns on the left/right (for example Xmascane and
        # WEST), and some individual colour planes may be shorter/empty even
        # though the graphic canvas is wider.
        #
        # For the supplied IPS v3.8 database the field is encoded as:
        #     GraphicWidth = 4 + visible_width * bytes_per_column
        # (e.g. 44 -> 20 pixels at height 16, 84 -> 40 pixels at height 14).
        # Plane/blob width is only a fallback for malformed records where the
        # GraphicWidth value cannot be decoded.
        if width_from_field:
            w = width_from_field
        elif blob_widths:
            w = max(set(blob_widths), key=blob_widths.count)
        else:
            w = max(1, stored_w)

        wrappers = {}
        # Always retain Graphic as well as RGB. For color graphics IPS keeps a
        # composite on/off bitmap in Graphic alongside the three color planes.
        for key in ("Graphic", "RedGraphic", "GreenGraphic", "BlueGraphic"):
            wrappers[key] = PlaneCodec.unwrap(d.get(key), w, h)

        if color_mode:
            planes = {
                key: PlaneCodec.decode(wrappers[key].payload, w, h)
                for key in ("RedGraphic", "GreenGraphic", "BlueGraphic")
            }
            px = []
            for y in range(h):
                px.append([
                    (255 if planes["RedGraphic"][y][x] else 0,
                     255 if planes["GreenGraphic"][y][x] else 0,
                     255 if planes["BlueGraphic"][y][x] else 0)
                    for x in range(w)
                ])
            storage_mode = "rgb"
        else:
            mono = PlaneCodec.decode(wrappers["Graphic"].payload, w, h)
            # IPS v3.8 displays legacy one-bit graphics as amber/yellow LEDs.
            px = [[(255,255,0) if mono[y][x] else (0,0,0) for x in range(w)] for y in range(h)]
            storage_mode = "mono"

        return GraphicRecord(
            d["GraphicID"], str(d["GraphicName"] or ""), str(d["GraphicDescription"] or ""),
            w, h, stored_w, d["GraphicCType"], d["GraphicAllowMod"], px, wrappers,
            PlaneCodec.LAYOUT, storage_mode
        )

    def save_record(self,g):
        import base64
        if getattr(g, "storage_mode", "rgb") == "mono":
            bits = [[any(g.pixels[y][x]) for x in range(g.width)] for y in range(g.height)]
            payload = PlaneCodec.encode(bits, g.width, g.height)
            mono = PlaneCodec.rebuild(g.plane_wrappers.get("Graphic"), payload, g.name, g.description, width=g.width, height=g.height, stored_width=g.stored_width)
            if self.backend == "jet4":
                self._run_jet("save_mono", {
                    "id": int(g.graphic_id), "name": g.name, "description": g.description,
                    "height": g.height, "width": g.stored_width,
                    "graphic": base64.b64encode(mono).decode()
                })
                return
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE [Graphics] SET [GraphicName]=?,[GraphicDescription]=?,[GraphicHeight]=?,[GraphicWidth]=?,[Graphic]=? WHERE [GraphicID]=?",
                g.name, g.description, g.height, g.stored_width, pyodbc.Binary(mono), g.graphic_id
            )
            self.conn.commit()
            return

        built = {}
        channel_bits = []
        for key, chan in (("RedGraphic",0),("GreenGraphic",1),("BlueGraphic",2)):
            bits = [[g.pixels[y][x][chan] > 0 for x in range(g.width)] for y in range(g.height)]
            channel_bits.append(bits)
            payload = PlaneCodec.encode(bits, g.width, g.height)
            built[key] = PlaneCodec.rebuild(g.plane_wrappers.get(key), payload, g.name, g.description, width=g.width, height=g.height, stored_width=g.stored_width)

        # Color records also carry Graphic as a monochrome composite. The supplied
        # 250A record has Graphic == (Red OR Green OR Blue), so keep it synchronized.
        composite = [[channel_bits[0][y][x] or channel_bits[1][y][x] or channel_bits[2][y][x]
                      for x in range(g.width)] for y in range(g.height)]
        composite_payload = PlaneCodec.encode(composite, g.width, g.height)
        built["Graphic"] = PlaneCodec.rebuild(g.plane_wrappers.get("Graphic"), composite_payload, g.name, g.description, width=g.width, height=g.height, stored_width=g.stored_width)

        if self.backend == "jet4":
            self._run_jet("save", {
                "id": int(g.graphic_id), "name": g.name, "description": g.description,
                "height": g.height, "width": g.stored_width,
                "graphic": base64.b64encode(built["Graphic"]).decode(),
                "red": base64.b64encode(built["RedGraphic"]).decode(),
                "green": base64.b64encode(built["GreenGraphic"]).decode(),
                "blue": base64.b64encode(built["BlueGraphic"]).decode()
            })
            return
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE [Graphics] SET [GraphicName]=?,[GraphicDescription]=?,[GraphicHeight]=?,[GraphicWidth]=?,[Graphic]=?,[RedGraphic]=?,[GreenGraphic]=?,[BlueGraphic]=? WHERE [GraphicID]=?",
            g.name, g.description, g.height, g.stored_width,
            pyodbc.Binary(built["Graphic"]), pyodbc.Binary(built["RedGraphic"]),
            pyodbc.Binary(built["GreenGraphic"]), pyodbc.Binary(built["BlueGraphic"]), g.graphic_id
        )
        self.conn.commit()

    def duplicate_graphic(self, source_id, new_name):
        """Create a new graphic by cloning an existing IPS record.

        Cloning is deliberate: the selected record supplies IPS's native binary
        headers, storage mode and dimensions. The UI blanks the cloned pixels
        immediately after insertion, so we never invent unknown header bytes.
        """
        if self.backend == "jet4":
            return int(self._run_jet("duplicate", {"source_id": int(source_id), "name": new_name})["id"])
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO [Graphics] ([GraphicName],[GraphicDescription],[GraphicHeight],[GraphicWidth],[GraphicAllowMod],[GraphicCType],[Graphic],[RedGraphic],[GreenGraphic],[BlueGraphic]) "
            "SELECT ?, '', [GraphicHeight],[GraphicWidth],[GraphicAllowMod],[GraphicCType],[Graphic],[RedGraphic],[GreenGraphic],[BlueGraphic] "
            "FROM [Graphics] WHERE [GraphicID]=?",
            new_name, source_id
        )
        # Do not rely on @@IDENTITY here. Older Jet databases can return the
        # wrong value after INSERT ... SELECT. Names are checked for uniqueness
        # before insertion, so resolve the new row directly.
        row = cur.execute(
            "SELECT TOP 1 [GraphicID] FROM [Graphics] WHERE [GraphicName]=? ORDER BY [GraphicID] DESC",
            new_name
        ).fetchone()
        self.conn.commit()
        if not row:
            raise RuntimeError("The cloned graphic was inserted, but its GraphicID could not be resolved.")
        return int(row[0])

    def rename_graphic(self, graphic_id, new_name):
        if self.backend == "jet4":
            self._run_jet("rename", {"id": int(graphic_id), "name": new_name})
            return
        cur = self.conn.cursor()
        cur.execute("UPDATE [Graphics] SET [GraphicName]=? WHERE [GraphicID]=?", new_name, graphic_id)
        self.conn.commit()

    def delete_graphic(self, graphic_id):
        if self.backend == "jet4":
            self._run_jet("delete", {"id": int(graphic_id)})
            return
        cur = self.conn.cursor()
        cur.execute("DELETE FROM [Graphics] WHERE [GraphicID]=?", graphic_id)
        self.conn.commit()


class RawBitmapDialog(QDialog):
    """Create a graphic from human-readable one-bit bitmap rows.

    Each non-empty input line is one horizontal row of pixels. Bytes on that
    row are read left-to-right, and bits are MSB-first (bit 7 is the leftmost
    pixel of each byte). The resulting bitmap is converted to IPS's native
    column-packed plane format when it is saved.
    """
    def __init__(self, parent=None, suggested_name="New Graphic", default_height=16, storage_note="",
                 show_name=True, trim_blank_borders=True, window_title="Add Graphic from Raw Bitmap",
                 intro_text=None):
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.resize(560, 500)
        self._input_byte_count = 0
        self.trim_blank_borders = bool(trim_blank_borders)
        self.show_name = bool(show_name)

        layout=QVBoxLayout(self)
        layout.setContentsMargins(18,18,18,18)
        layout.setSpacing(12)

        intro=QLabel(
            intro_text or
            "Paste one bitmap row per line. Bytes are read left-to-right; within each byte, "
            "the most-significant bit is the leftmost pixel. The editor converts the rows "
            "to IPS's native column format automatically."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form=QFormLayout()
        self.name_edit=None
        if self.show_name:
            self.name_edit=QLineEdit(suggested_name)
            form.addRow("Graphic name", self.name_edit)

        if storage_note:
            note=QLabel(storage_note)
            note.setWordWrap(True)
            form.addRow("Storage", note)
        layout.addLayout(form)

        raw_label=QLabel("Raw bitmap values — one row per line")
        raw_label.setObjectName("RawBitmapLabel")
        layout.addWidget(raw_label)

        self.raw_edit=QPlainTextEdit()
        self.raw_edit.setObjectName("RawBitmapEdit")
        self.raw_edit.setPlaceholderText(
            "08 00\n"
            "04 00\n"
            "FF E0\n"
            "80 20\n"
            "04 00\n"
            "04 00\n"
            "FF E0\n"
            "0E 00\n"
            "15 00\n"
            "24 80\n"
            "C4 60\n"
            "04 00"
        )
        layout.addWidget(self.raw_edit,1)

        self.summary=QLabel("Enter bitmap rows to calculate the dimensions.")
        self.summary.setObjectName("HintLabel")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._accept_checked)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.raw_edit.textChanged.connect(self._update_summary)
        self._update_summary()

    @staticmethod
    def _row_tokens(line):
        parts=re.split(r"[\s,;]+", line.strip()) if line.strip() else []
        values=[]
        for token in parts:
            if not token:
                continue
            t=token[2:] if token.lower().startswith("0x") else token
            if not re.fullmatch(r"[0-9A-Fa-f]{2}", t):
                raise ValueError(f"'{token}' is not a two-digit hexadecimal byte")
            values.append(int(t,16))
        return values

    def bitmap_data(self):
        lines=[line for line in self.raw_edit.toPlainText().splitlines() if line.strip()]
        if not lines:
            raise ValueError("Enter at least one bitmap row.")

        rows=[self._row_tokens(line) for line in lines]
        if any(not row for row in rows):
            raise ValueError("Each bitmap row must contain at least one byte.")

        row_bytes=len(rows[0])
        for i,row in enumerate(rows,1):
            if len(row)!=row_bytes:
                raise ValueError(
                    f"Row {i} contains {len(row)} byte(s); every row must contain {row_bytes}."
                )

        raw_width=row_bytes*8
        height=len(rows)
        if height>64:
            raise ValueError("Raw bitmap height cannot exceed 64 pixels.")
        if raw_width>512:
            raise ValueError("Raw bitmap width cannot exceed 512 pixels.")

        bits=[]
        for row in rows:
            pixel_row=[]
            for value in row:
                # Conventional bitmap notation: 0x80 is the leftmost pixel and
                # 0x01 is the rightmost pixel within each byte.
                pixel_row.extend(bool(value & (0x80 >> bit)) for bit in range(8))
            bits.append(pixel_row)

        # New-graphic import trims fully blank byte-padding columns at the two
        # horizontal edges. Selection paste deliberately keeps the raw bitmap's
        # exact dimensions so its size can be compared with the selection box.
        left=0
        right=raw_width-1
        if self.trim_blank_borders:
            while left < right and all(not bits[y][left] for y in range(height)):
                left += 1
            while right > left and all(not bits[y][right] for y in range(height)):
                right -= 1
            bits=[row[left:right+1] for row in bits]
        width=right-left+1
        self._trimmed_left=left if self.trim_blank_borders else 0
        self._trimmed_right=(raw_width-1-right) if self.trim_blank_borders else 0
        self._raw_width=raw_width

        self._input_byte_count=sum(len(row) for row in rows)
        payload=PlaneCodec.encode(bits,width,height)
        return width,height,payload

    def input_byte_count(self):
        return self._input_byte_count

    def _update_summary(self):
        try:
            width,height,payload=self.bitmap_data()
            trimmed=getattr(self,"_trimmed_left",0)+getattr(self,"_trimmed_right",0)
            trim_text=(
                f" • trimmed {trimmed} blank border column(s)"
                if trimmed else ""
            )
            self.summary.setText(
                f"Result: {width} × {height} pixels • {self._input_byte_count} input byte(s)"
                f"{trim_text}."
            )
        except ValueError as e:
            self.summary.setText(str(e))

    def _accept_checked(self):
        if self.name_edit is not None:
            name=self.name_edit.text().strip()
            if not name:
                QMessageBox.warning(self,"Add Graphic","Graphic name cannot be blank.")
                return
        try:
            self.bitmap_data()
        except ValueError as e:
            QMessageBox.warning(self,"Raw bitmap",str(e))
            return
        self.accept()

class EdgeResizeButton(QPushButton):
    """Circular canvas-edge arrow with hold-to-choose-amount feedback.

    A quick click performs one resize step. Holding fills a radial pie overlay;
    releasing after the ring is full emits longPressed instead of clicked.
    """
    longPressed=pyqtSignal()
    HOLD_MS=650

    def __init__(self,text,parent=None,fill_color="#60A5FA"):
        super().__init__(text,parent)
        self._hold_progress=0.0
        self._hold_armed=False
        self._hold_elapsed=0
        self._hold_fill=QColor(fill_color)
        self._hold_fill.setAlpha(105)
        self._hold_timer=QTimer(self)
        self._hold_timer.setInterval(25)
        self._hold_timer.timeout.connect(self._advance_hold)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _advance_hold(self):
        self._hold_elapsed+=self._hold_timer.interval()
        self._hold_progress=min(1.0,self._hold_elapsed/self.HOLD_MS)
        if self._hold_progress>=1.0:
            self._hold_armed=True
            self._hold_timer.stop()
        self.update()

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            self._hold_elapsed=0
            self._hold_progress=0.0
            self._hold_armed=False
            self._hold_timer.start()
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            self._hold_timer.stop()
            armed=self._hold_armed
            self._hold_progress=0.0
            self._hold_armed=False
            self.update()
            if armed:
                # Suppress QPushButton.clicked: this gesture belongs to the
                # quantity dialog instead of the one-step action.
                self.setDown(False)
                event.accept()
                self.longPressed.emit()
                return
        super().mouseReleaseEvent(event)

    def paintEvent(self,event):
        super().paintEvent(event)
        if self._hold_progress<=0.0:
            return
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing,True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._hold_fill)
        r=self.rect().adjusted(3,3,-3,-3)
        # Qt angles are in sixteenths of a degree. Start at 12 o'clock and
        # fill clockwise until a full circle indicates a long press.
        painter.drawPie(r,90*16,-int(360*16*self._hold_progress))
        painter.end()

class PixelCanvas(QWidget):
    changeStarted=pyqtSignal(); changed=pyqtSignal(); selectionChanged=pyqtSignal()
    def __init__(self,parent=None):
        super().__init__(parent); self.model=None; self.base_cell_size=36; self.scale_factor=1.; self.cell_size=36
        self.paint_color=(255,255,255); self.last_cell=None; self.hover_cell=None; self.selection=None; self.anchor=None; self.selecting=False
        self.moving=False; self.move_start=None; self.move_rect=None; self.move_bitmap=None; self.move_delta=(0,0); self.setMouseTracking(True)
    def set_model(self,m):
        self.model=m
        self.clear_selection()
        self.update_geometry()
        self.update()
    def set_scale_factor(self,f): self.scale_factor=max(.15,min(4,float(f))); self.cell_size=max(6,round(self.base_cell_size*self.scale_factor)); self.update_geometry(); self.update()
    def update_geometry(self):
        if not self.model:self.setFixedSize(500,300)
        else:self.setFixedSize(self.model.width*self.cell_size+1,self.model.height*self.cell_size+1)
    def paintEvent(self,e):
        p=QPainter(self); p.fillRect(self.rect(),QColor("#070B12"))
        if not self.model:
            p.setPen(QColor("#94A3B8")); p.drawText(self.rect(),Qt.AlignmentFlag.AlignCenter,"Open a Luminator .ips database") ; return
        m=max(2,int(self.cell_size*.16)); d=max(2,self.cell_size-2*m); p.setRenderHint(QPainter.RenderHint.Antialiasing,True)
        for y in range(self.model.height):
            for x in range(self.model.width):
                cx=x*self.cell_size; cy=y*self.cell_size; p.setPen(QPen(QColor("#172033"),1)); p.setBrush(Qt.BrushStyle.NoBrush); p.drawRect(cx,cy,self.cell_size,self.cell_size)
                rgb=self.model.pixels[y][x]; c=QColor(*rgb) if any(rgb) else QColor("#202938")
                if (x,y)==self.hover_cell and self.selection is None: p.fillRect(cx+1,cy+1,self.cell_size-2,self.cell_size-2,QColor(148,163,184,45))
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(c); p.drawEllipse(cx+m,cy+m,d,d)
        if self.selection:
            l,t,r,b=self.selection; p.setBrush(QColor(59,130,246,28)); pen=QPen(QColor("#60A5FA"),2); pen.setStyle(Qt.PenStyle.DashLine); p.setPen(pen)
            p.drawRect(l*self.cell_size+1,t*self.cell_size+1,(r-l+1)*self.cell_size-2,(b-t+1)*self.cell_size-2)
    def cell(self,pos):
        if not self.model:return None
        x=int(pos.x()//self.cell_size); y=int(pos.y()//self.cell_size)
        return (x,y) if 0<=x<self.model.width and 0<=y<self.model.height else None
    def inside(self,c):
        if not c or not self.selection:return False
        x,y=c;l,t,r,b=self.selection;return l<=x<=r and t<=y<=b
    def clear_selection(self):
        had=self.selection is not None
        self.selection=None
        self.anchor=None
        self.selecting=False
        self.moving=False
        self.move_start=None
        self.move_rect=None
        self.move_bitmap=None
        self.move_delta=(0,0)
        self.update()
        if had:self.selectionChanged.emit()
    def mousePressEvent(self,e):
        c=self.cell(e.position())
        if e.button()==Qt.MouseButton.RightButton:
            if c:
                self.anchor=c
                self.selection=(c[0],c[1],c[0],c[1])
                self.selecting=True
                self.update()
                self.selectionChanged.emit()
            else:
                self.clear_selection()
            e.accept()
            return
        if e.button()!=Qt.MouseButton.LeftButton:return
        if not c:
            if self.selection:self.clear_selection()
            e.accept()
            return
        if self.inside(c):
            self.changeStarted.emit();self.moving=True;self.move_start=c;self.move_rect=self.selection
            l,t,r,b=self.selection;self.move_bitmap=[[self.model.pixels[y][x] for x in range(l,r+1)] for y in range(t,b+1)];self.move_delta=(0,0);return
        if self.selection:
            self.clear_selection();e.accept();return
        self.changeStarted.emit(); old=self.model.pixels[c[1]][c[0]]; self.paint_color=(0,0,0) if old==self.paint_color else self.paint_color; self.last_cell=None; self.paint(c)
    def paint(self,c):
        if c and c!=self.last_cell:self.model.pixels[c[1]][c[0]]=self.paint_color;self.last_cell=c;self.update();self.changed.emit()
    def mouseMoveEvent(self,e):
        c=self.cell(e.position());self.hover_cell=c;self.update()
        if self.selecting and e.buttons()&Qt.MouseButton.RightButton and c:
            a=self.anchor;self.selection=(min(a[0],c[0]),min(a[1],c[1]),max(a[0],c[0]),max(a[1],c[1]));self.update();self.selectionChanged.emit();return
        if self.moving and e.buttons()&Qt.MouseButton.LeftButton and c:
            dx=c[0]-self.move_start[0];dy=c[1]-self.move_start[1]
            if (dx,dy)!=self.move_delta:self.apply_move(dx,dy)
            return
        if e.buttons()&Qt.MouseButton.LeftButton:self.paint(c)
    def apply_move(self,dx,dy):
        l,t,r,b=self.move_rect; olddx,olddy=self.move_delta
        # Clear previous destination, restore source, then place at new delta.
        for yy,row in enumerate(self.move_bitmap):
            for xx,_ in enumerate(row):
                px=l+xx+olddx;py=t+yy+olddy
                if 0<=px<self.model.width and 0<=py<self.model.height:self.model.pixels[py][px]=(0,0,0)
        for yy,row in enumerate(self.move_bitmap):
            for xx,v in enumerate(row): self.model.pixels[t+yy][l+xx]=v
        for yy,row in enumerate(self.move_bitmap):
            for xx,v in enumerate(row):
                px=l+xx+dx;py=t+yy+dy
                if 0<=px<self.model.width and 0<=py<self.model.height:self.model.pixels[py][px]=v
        nl=max(0,l+dx);nt=max(0,t+dy);nr=min(self.model.width-1,r+dx);nb=min(self.model.height-1,b+dy);self.selection=(nl,nt,nr,nb) if nl<=nr and nt<=nb else None;self.move_delta=(dx,dy);self.update();self.changed.emit();self.selectionChanged.emit()
    def mouseReleaseEvent(self,e):
        if e.button()==Qt.MouseButton.RightButton:self.selecting=False
        if e.button()==Qt.MouseButton.LeftButton:self.moving=False;self.last_cell=None

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Luminator IPS Graphics Editor")
        self.resize(1180, 780)
        self.db=None
        self.rows=[]
        self.current=None
        # Keep every viewed/edited graphic alive in memory, matching the FNT
        # editor's model-first workflow. Switching the list selection never
        # reloads over unsaved work; Ctrl+S writes all dirty cached graphics.
        self.graphic_cache={}
        self.history_cache={}
        self.dirty_ids=set()
        self.undo_stack=[]
        self.redo_stack=[]
        self.dirty=False
        self.manual_scale_override=False
        self.clipboard=None

        self._build_toolbar()
        self._build_ui()
        self._apply_style()
        self.setStatusBar(QStatusBar())
        self._task_status_label = QLabel("")
        self._task_status_label.setObjectName("TaskStatusLabel")
        self._task_status_label.setVisible(False)
        self._task_progress = QProgressBar()
        self._task_progress.setObjectName("TaskProgress")
        self._task_progress.setRange(0, 100)
        self._task_progress.setValue(0)
        self._task_progress.setTextVisible(True)
        self._task_progress.setFixedWidth(190)
        self._task_progress.setVisible(False)
        self.statusBar().addPermanentWidget(self._task_status_label)
        self.statusBar().addPermanentWidget(self._task_progress)
        self.statusBar().showMessage("Ready")
        self.set_color()
        self._update_undo_redo_buttons()
        self._update_graphic_actions()

    def _build_toolbar(self):
        toolbar=QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18,18))
        self.addToolBar(toolbar)

        open_action=QAction("Open IPS",self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self.open_ips)
        toolbar.addAction(open_action)

        self.save_action=QAction("Save",self)
        self.save_action.setShortcut("Ctrl+S")
        self.save_action.triggered.connect(self.save)
        self.save_action.setEnabled(False)
        toolbar.addAction(self.save_action)

        self.save_as_action=QAction("Save As",self)
        self.save_as_action.setShortcut("Ctrl+Shift+S")
        self.save_as_action.triggered.connect(self.save_as)
        self.save_as_action.setEnabled(False)
        toolbar.addAction(self.save_as_action)

        toolbar.addSeparator()

        import_action=QAction("Import PNG",self)
        import_action.triggered.connect(self.import_png)
        toolbar.addAction(import_action)

        export_action=QAction("Export PNG",self)
        export_action.triggered.connect(self.export_png)
        toolbar.addAction(export_action)

        toolbar.addSeparator()
        self.filename_label=QLabel("No IPS loaded")
        self.filename_label.setObjectName("FilenameLabel")
        toolbar.addWidget(self.filename_label)

    def _build_ui(self):
        root=QWidget()
        self.setCentralWidget(root)

        main=QHBoxLayout(root)
        main.setContentsMargins(20,20,20,20)
        main.setSpacing(18)

        # Left control card -- mirrors luminator_fnt_editor.py.
        side=QFrame()
        side.setObjectName("SidePanel")
        side.setFixedWidth(280)
        side_layout=QVBoxLayout(side)
        side_layout.setContentsMargins(18,18,18,18)
        side_layout.setSpacing(14)

        title=QLabel("Graphic Controls")
        title.setObjectName("PanelTitle")
        side_layout.addWidget(title)

        graphic_group=QGroupBox("Graphic")
        graphic_layout=QGridLayout(graphic_group)
        graphic_layout.addWidget(QLabel("Selected"),0,0)
        self.selected_graphic_label=QLabel("—")
        self.selected_graphic_label.setObjectName("SelectedGraphicLabel")
        graphic_layout.addWidget(self.selected_graphic_label,0,1)
        graphic_layout.addWidget(QLabel("Name"),1,0)
        self.name=QLineEdit()
        self.name.editingFinished.connect(self.meta_changed)
        graphic_layout.addWidget(self.name,1,1)
        graphic_layout.addWidget(QLabel("Description"),2,0)
        self.desc=QLineEdit()
        self.desc.editingFinished.connect(self.meta_changed)
        graphic_layout.addWidget(self.desc,2,1)
        side_layout.addWidget(graphic_group)

        list_group=QGroupBox("Graphics")
        list_layout=QVBoxLayout(list_group)
        self.list=QListWidget()
        self.list.setObjectName("GraphicList")
        self.list.currentRowChanged.connect(self.select_graphic)
        self.list.setFixedHeight(285)
        list_layout.addWidget(self.list)
        side_layout.addWidget(list_group)

        metrics_group=QGroupBox("Metrics")
        metrics_layout=QGridLayout(metrics_group)
        self.width_value=QLabel("—")
        self.height_value=QLabel("—")
        self.storage_value=QLabel("—")
        self.storage_value.setWordWrap(True)
        metrics_layout.addWidget(QLabel("Width"),0,0)
        metrics_layout.addWidget(self.width_value,0,1)
        metrics_layout.addWidget(QLabel("Height"),1,0)
        metrics_layout.addWidget(self.height_value,1,1)
        metrics_layout.addWidget(QLabel("Storage"),2,0)
        metrics_layout.addWidget(self.storage_value,2,1)
        side_layout.addWidget(metrics_group)

        edit_group=QGroupBox("Edit Graphic")
        edit_layout=QVBoxLayout(edit_group)

        graphic_row=QHBoxLayout()
        self.add_graphic_btn=QPushButton("Add Graphic")
        self.add_graphic_btn.setToolTip("Create a blank graphic using the selected graphic's IPS format and dimensions")
        self.add_graphic_btn.clicked.connect(self.add_graphic)
        graphic_row.addWidget(self.add_graphic_btn)

        self.delete_graphic_btn=QPushButton("Delete Graphic")
        self.delete_graphic_btn.setObjectName("DangerButton")
        self.delete_graphic_btn.setToolTip("Permanently delete the selected graphic from the IPS database")
        self.delete_graphic_btn.clicked.connect(self.delete_graphic)
        graphic_row.addWidget(self.delete_graphic_btn)
        edit_layout.addLayout(graphic_row)

        self.add_raw_graphic_btn=QPushButton("Add from Raw Bitmap")
        self.add_raw_graphic_btn.setToolTip("Create a graphic from raw IPS one-bit bitmap bytes")
        self.add_raw_graphic_btn.clicked.connect(self.add_raw_graphic)
        edit_layout.addWidget(self.add_raw_graphic_btn)

        self.rename_graphic_btn=QPushButton("Rename Graphic")
        self.rename_graphic_btn.setToolTip("Rename the selected graphic")
        self.rename_graphic_btn.clicked.connect(self.rename_graphic)
        edit_layout.addWidget(self.rename_graphic_btn)

        self.clear_btn=QPushButton("Clear Graphic")
        self.clear_btn.setObjectName("DangerButton")
        self.clear_btn.setToolTip("Turn every pixel in the current graphic off")
        self.clear_btn.clicked.connect(self.clear_graphic)
        edit_layout.addWidget(self.clear_btn)
        side_layout.addWidget(edit_group)
        side_layout.addStretch(1)

        hint=QLabel("Tip: left-drag paints or erases. Right-drag selects a rectangle; then left-drag inside the selection to move those pixels as a group. Ctrl + mouse wheel adjusts scale.")
        hint.setWordWrap(True)
        hint.setObjectName("HintLabel")
        side_layout.addWidget(hint)
        main.addWidget(side)

        # Right editor card -- same structure as the FNT editor.
        editor_panel=QFrame()
        editor_panel.setObjectName("EditorPanel")
        editor_layout=QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(18,18,18,18)
        editor_layout.setSpacing(12)

        header_row=QHBoxLayout()
        self.editor_title=QLabel("Pixel Editor")
        self.editor_title.setObjectName("EditorTitle")
        header_row.addWidget(self.editor_title)

        self.undo_btn=QPushButton()
        self.undo_btn.setObjectName("CompactButton")
        self.undo_btn.setIcon(_svg_icon(_UNDO_SVG))
        self.undo_btn.setIconSize(QSize(18,18))
        self.undo_btn.setToolTip("Undo (Ctrl+Z)")
        self.undo_btn.clicked.connect(self.undo)
        header_row.addWidget(self.undo_btn)

        self.redo_btn=QPushButton()
        self.redo_btn.setObjectName("CompactButton")
        self.redo_btn.setIcon(_svg_icon(_REDO_SVG))
        self.redo_btn.setIconSize(QSize(18,18))
        self.redo_btn.setToolTip("Redo (Ctrl+Y)")
        self.redo_btn.clicked.connect(self.redo)
        header_row.addWidget(self.redo_btn)

        header_row.addStretch(1)

        color_label=QLabel("Color")
        color_label.setObjectName("ScaleLabel")
        header_row.addWidget(color_label)
        self.color=QComboBox()
        self.color.setObjectName("ColorCombo")
        for n,rgb in COLORS:
            self.color.addItem(n,rgb)
        self.color.setCurrentIndex(7)
        self.color.currentIndexChanged.connect(self.set_color)
        header_row.addWidget(self.color)

        scale_label=QLabel("Scale")
        scale_label.setObjectName("ScaleLabel")
        header_row.addWidget(scale_label)

        self.scale_slider=QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(15,400)
        self.scale_slider.setValue(100)
        self.scale_slider.setFixedWidth(180)
        self.scale_slider.setToolTip("Zoom the pixel editor")
        self.scale_slider.valueChanged.connect(self.manual_scale_changed)
        header_row.addWidget(self.scale_slider)

        self.scale_value_label=QLabel("100%")
        self.scale_value_label.setObjectName("ScaleValueLabel")
        self.scale_value_label.setFixedWidth(46)
        self.scale_value_label.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.scale_value_label)

        self.fit_btn=QPushButton("Fit")
        self.fit_btn.setObjectName("CompactButton")
        self.fit_btn.clicked.connect(self.enable_auto_fit)
        header_row.addWidget(self.fit_btn)

        editor_layout.addLayout(header_row)

        self.canvas=PixelCanvas()
        self.canvas.changeStarted.connect(self.snapshot)
        self.canvas.changed.connect(self.mark_dirty)
        self.canvas.selectionChanged.connect(self._update_selection_actions_bar)
        self.canvas.installEventFilter(self)

        self.scroll=QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.canvas)
        self.scroll.setObjectName("CanvasScroll")
        self.scroll.viewport().installEventFilter(self)
        self.scroll.installEventFilter(self)
        self.scroll.horizontalScrollBar().valueChanged.connect(lambda _v: self._position_editor_overlays())
        self.scroll.verticalScrollBar().valueChanged.connect(lambda _v: self._position_editor_overlays())
        editor_layout.addWidget(self.scroll,1)

        # Canvas resizing belongs next to the pixels, not in the side panel.
        # The arrow itself describes how that canvas edge will move:
        # outward = add space, inward = remove space. Hold an arrow until the
        # radial fill completes to choose a multi-row / multi-column amount.
        def make_edge_button(text, tooltip, callback, hold_callback, danger=False):
            fill="#F87171" if danger else "#60A5FA"
            button=EdgeResizeButton(text,self.scroll,fill)
            button.setObjectName("CanvasEdgeDangerButton" if danger else "CanvasEdgeButton")
            button.setFixedSize(36,36)
            button.setToolTip(tooltip+"\nHold: choose how many.")
            button.clicked.connect(callback)
            button.longPressed.connect(hold_callback)
            button.setVisible(False)
            return button

        # Left edge: outward is left, inward is right.
        self.add_left_edge_btn=make_edge_button(
            "←","Add one blank column on the left",
            lambda:self.add_column("left"),
            lambda:self._choose_edge_resize_amount("column","left",True))
        self.remove_left_edge_btn=make_edge_button(
            "→","Remove one column from the left",
            lambda:self.remove_column("left"),
            lambda:self._choose_edge_resize_amount("column","left",False),True)

        # Right edge: outward is right, inward is left.
        self.add_right_edge_btn=make_edge_button(
            "→","Add one blank column on the right",
            lambda:self.add_column("right"),
            lambda:self._choose_edge_resize_amount("column","right",True))
        self.remove_right_edge_btn=make_edge_button(
            "←","Remove one column from the right",
            lambda:self.remove_column("right"),
            lambda:self._choose_edge_resize_amount("column","right",False),True)

        # Top edge: outward is up, inward is down.
        self.add_top_edge_btn=make_edge_button(
            "↑","Add one blank row on the top",
            lambda:self.add_row("top"),
            lambda:self._choose_edge_resize_amount("row","top",True))
        self.remove_top_edge_btn=make_edge_button(
            "↓","Remove one row from the top",
            lambda:self.remove_row("top"),
            lambda:self._choose_edge_resize_amount("row","top",False),True)

        # Bottom edge: outward is down, inward is up.
        self.add_bottom_edge_btn=make_edge_button(
            "↓","Add one blank row on the bottom",
            lambda:self.add_row("bottom"),
            lambda:self._choose_edge_resize_amount("row","bottom",True))
        self.remove_bottom_edge_btn=make_edge_button(
            "↑","Remove one row from the bottom",
            lambda:self.remove_row("bottom"),
            lambda:self._choose_edge_resize_amount("row","bottom",False),True)

        # Same floating pixel action bar used by luminator_fnt_editor.py.
        self.selection_actions_bar=QWidget(self.scroll)
        self.selection_actions_bar.setObjectName("SelectionActionsBar")
        self.selection_actions_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        selection_actions_layout=QHBoxLayout(self.selection_actions_bar)
        selection_actions_layout.setContentsMargins(10,6,10,6)
        selection_actions_layout.setSpacing(8)

        self.copy_btn=QPushButton("Copy")
        self.copy_btn.setObjectName("SelectionActionButton")
        self.copy_btn.setToolTip("Copy the selection (or the whole graphic if nothing is selected)")
        self.copy_btn.clicked.connect(self.copy_pixels)
        selection_actions_layout.addWidget(self.copy_btn)

        self.paste_btn=QPushButton("Paste")
        self.paste_btn.setObjectName("SelectionActionButton")
        self.paste_btn.setToolTip("Paste into the selection (or from the top-left if nothing is selected)")
        self.paste_btn.clicked.connect(self.paste_pixels)
        selection_actions_layout.addWidget(self.paste_btn)

        self.paste_raw_btn=QPushButton("Paste Raw")
        self.paste_raw_btn.setObjectName("SelectionActionButton")
        self.paste_raw_btn.setToolTip("Paste hexadecimal bitmap rows starting at the selection's top-left corner")
        self.paste_raw_btn.clicked.connect(self.paste_raw_bitmap)
        selection_actions_layout.addWidget(self.paste_raw_btn)

        self.mirror_h_btn=QPushButton("Mirror H")
        self.mirror_h_btn.setObjectName("SelectionActionButton")
        self.mirror_h_btn.setToolTip("Flip the selection (or whole graphic) left-to-right")
        self.mirror_h_btn.clicked.connect(lambda:self.mirror_pixels("horizontal"))
        selection_actions_layout.addWidget(self.mirror_h_btn)

        self.mirror_v_btn=QPushButton("Mirror V")
        self.mirror_v_btn.setObjectName("SelectionActionButton")
        self.mirror_v_btn.setToolTip("Flip the selection (or whole graphic) top-to-bottom")
        self.mirror_v_btn.clicked.connect(lambda:self.mirror_pixels("vertical"))
        selection_actions_layout.addWidget(self.mirror_v_btn)

        self.set_lit_btn=QPushButton("Set Lit")
        self.set_lit_btn.setObjectName("SelectionActionButton")
        self.set_lit_btn.setToolTip("Set every pixel in the selection to the current paint color")
        self.set_lit_btn.clicked.connect(lambda:self.set_selection_pixels(True))
        selection_actions_layout.addWidget(self.set_lit_btn)

        self.set_unlit_btn=QPushButton("Set Unlit")
        self.set_unlit_btn.setObjectName("SelectionActionButton")
        self.set_unlit_btn.setToolTip("Turn every pixel in the selection off")
        self.set_unlit_btn.clicked.connect(lambda:self.set_selection_pixels(False))
        selection_actions_layout.addWidget(self.set_unlit_btn)
        self.selection_actions_bar.setVisible(False)

        main.addWidget(editor_panel,1)

    def _apply_style(self):
        # Deliberately follows the supplied luminator_fnt_editor.py palette,
        # spacing, rounded panels, controls, slider, scrollbars and toolbar.
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #0B1220;
                color: #E5E7EB;
            }

            QToolBar {
                background: #111827;
                color: #F8FAFC;
                border: none;
                border-bottom: 1px solid #253047;
                padding: 8px 12px;
                spacing: 8px;
            }

            QToolButton {
                background: #172033;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 7px 12px;
            }
            QToolButton:hover { background: #22304A; }
            QToolButton:pressed { background: #2B3B58; }
            QToolButton:disabled {
                background: #111827;
                color: #64748B;
                border-color: #253047;
            }

            #FilenameLabel {
                background: transparent;
                color: #CBD5E1;
                padding-left: 8px;
            }

            #SidePanel, #EditorPanel {
                background: #111827;
                color: #E5E7EB;
                border: 1px solid #253047;
                border-radius: 14px;
            }

            #PanelTitle, #EditorTitle {
                background: transparent;
                color: #F8FAFC;
                font-size: 18px;
                font-weight: 700;
            }

            QGroupBox {
                background: #111827;
                color: #CBD5E1;
                font-weight: 600;
                border: 1px solid #253047;
                border-radius: 10px;
                margin-top: 12px;
                padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                background: #111827;
                color: #CBD5E1;
            }

            QLabel {
                background: transparent;
                color: #CBD5E1;
            }
            #SelectedGraphicLabel {
                color: #F8FAFC;
                font-weight: 700;
            }

            QLineEdit, QComboBox, QPlainTextEdit {
                background: #0F172A;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 7px;
                padding: 6px 8px;
                min-height: 24px;
                selection-background-color: #2563EB;
                selection-color: #FFFFFF;
            }
            QLineEdit:hover, QComboBox:hover, QPlainTextEdit:hover { border-color: #475569; }
            QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid #3B82F6; }
            QComboBox QAbstractItemView {
                background: #111827;
                color: #F8FAFC;
                border: 1px solid #334155;
                selection-background-color: #22304A;
            }

            QPushButton {
                background: #172033;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 8px 10px;
                text-align: left;
            }
            QPushButton:hover { background: #22304A; }
            QPushButton:pressed { background: #2B3B58; }
            QPushButton:disabled {
                background: #111827;
                color: #64748B;
                border-color: #253047;
            }

            #ScaleLabel { color: #94A3B8; }
            #ScaleValueLabel { color: #E5E7EB; font-weight: 600; }

            QSlider::groove:horizontal {
                height: 6px;
                background: #253047;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #3B82F6;
                border-radius: 3px;
            }
            QSlider::add-page:horizontal {
                background: #253047;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #F8FAFC;
                border: 1px solid #64748B;
                width: 16px;
                height: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
            QSlider::handle:horizontal:hover {
                background: #DBEAFE;
                border-color: #60A5FA;
            }

            #CompactButton {
                min-width: 40px;
                max-width: 54px;
                padding: 6px 8px;
                text-align: center;
            }
            #DangerButton { color: #FCA5A5; }
            #DangerButton:hover { color: #FECACA; }
            #HintLabel { color: #94A3B8; font-size: 12px; }

            #SelectionActionsBar {
                background: rgba(17, 24, 39, 235);
                border: 1px solid #334155;
                border-radius: 10px;
            }
            #SelectionActionButton {
                text-align: center;
                padding: 6px 12px;
            }
            #CanvasEdgeButton {
                min-width: 36px;
                max-width: 36px;
                min-height: 36px;
                max-height: 36px;
                padding: 0px;
                text-align: center;
                border-radius: 18px;
                background: rgba(23, 32, 51, 238);
                border: 1px solid #475569;
                color: #F8FAFC;
                font-size: 20px;
                font-weight: 700;
            }
            #CanvasEdgeButton:hover {
                background: #2563EB;
                border-color: #60A5FA;
            }
            #CanvasEdgeButton:pressed { background: #1D4ED8; }
            #CanvasEdgeDangerButton {
                min-width: 36px;
                max-width: 36px;
                min-height: 36px;
                max-height: 36px;
                padding: 0px;
                text-align: center;
                border-radius: 18px;
                background: rgba(23, 32, 51, 238);
                border: 1px solid #475569;
                color: #FCA5A5;
                font-size: 20px;
                font-weight: 700;
            }
            #CanvasEdgeDangerButton:hover {
                background: #7F1D1D;
                border-color: #F87171;
                color: #FECACA;
            }
            #CanvasEdgeDangerButton:pressed { background: #991B1B; }

            #GraphicList {
                background: #0F172A;
                color: #E5E7EB;
                border: 1px solid #253047;
                border-radius: 8px;
                padding: 4px;
                outline: 0;
            }
            #GraphicList::item {
                min-height: 26px;
                padding: 3px 6px;
                border-radius: 5px;
            }
            #GraphicList::item:selected {
                background: #2563EB;
                color: #FFFFFF;
            }
            #GraphicList::item:hover:!selected { background: #172033; }

            #CanvasScroll {
                background: #0B1220;
                color: #E5E7EB;
                border: 1px solid #253047;
                border-radius: 10px;
            }

            QScrollBar:vertical {
                background: #0F172A;
                width: 12px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #334155;
                min-height: 24px;
                border-radius: 6px;
            }
            QScrollBar:horizontal {
                background: #0F172A;
                height: 12px;
                margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #334155;
                min-width: 24px;
                border-radius: 6px;
            }

            QStatusBar {
                background: #111827;
                color: #CBD5E1;
                border-top: 1px solid #253047;
            }
            #TaskStatusLabel {
                background: transparent;
                color: #CBD5E1;
                padding: 0 4px 0 8px;
            }
            #TaskProgress {
                background: #0F172A;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 6px;
                text-align: center;
                min-height: 16px;
                max-height: 16px;
            }
            #TaskProgress::chunk {
                background: #2563EB;
                border-radius: 5px;
            }
            QMessageBox { background: #111827; color: #F8FAFC; }
            QMessageBox QLabel { color: #F8FAFC; }
        """)

    def _begin_task(self, message, value=0):
        """Show a persistent bottom-right progress indicator for slow database work."""
        self._task_status_label.setText(message)
        self._task_status_label.setVisible(True)
        self._task_progress.setRange(0, 100)
        self._task_progress.setValue(max(0, min(100, int(value))))
        self._task_progress.setVisible(True)
        self._task_progress.repaint()
        self._task_status_label.repaint()
        QApplication.processEvents()

    def _task_step(self, value, message=None):
        if message is not None:
            self._task_status_label.setText(message)
        self._task_progress.setValue(max(0, min(100, int(value))))
        self._task_progress.repaint()
        self._task_status_label.repaint()
        QApplication.processEvents()

    def _end_task(self):
        self._task_progress.setValue(100)
        self._task_progress.repaint()
        QApplication.processEvents()
        self._task_progress.setVisible(False)
        self._task_status_label.setVisible(False)

    def set_color(self):
        if self.current and getattr(self.current,"storage_mode","")=="mono":
            self.canvas.paint_color=(255,255,0)
        else:
            data=self.color.currentData()
            self.canvas.paint_color=tuple(data) if data is not None else (255,255,255)

    def _update_undo_redo_buttons(self):
        self.undo_btn.setEnabled(bool(self.current and self.undo_stack))
        self.redo_btn.setEnabled(bool(self.current and self.redo_stack))

    def _update_graphic_actions(self):
        has_db = self.db is not None
        has_current = has_db and self.current is not None
        for button_name in ("add_graphic_btn", "add_raw_graphic_btn", "rename_graphic_btn", "delete_graphic_btn", "clear_btn"):
            button = getattr(self, button_name, None)
            if button is not None:
                button.setEnabled(has_current)
        self._update_selection_actions_bar()

    @staticmethod
    def _graphic_key(graphic_id):
        return str(graphic_id)

    def _stash_current(self):
        """Store the live editor state without touching the database."""
        if not self.current:
            return
        # Read metadata directly from the controls so a click on another
        # graphic (or Ctrl+S while a line edit still has focus) cannot outrun
        # QLineEdit.editingFinished.
        new_name=self.name.text()
        new_description=self.desc.text()
        metadata_changed=(new_name!=self.current.name or new_description!=self.current.description)
        self.current.name=new_name
        self.current.description=new_description
        key = self._graphic_key(self.current.graphic_id)
        self.graphic_cache[key] = self.current
        self.history_cache[key] = (list(self.undo_stack), list(self.redo_stack))
        if metadata_changed:
            self.dirty_ids.add(key)
            self.dirty=True

    def _reset_memory_state(self):
        self.graphic_cache.clear()
        self.history_cache.clear()
        self.dirty_ids.clear()
        self.undo_stack=[]
        self.redo_stack=[]
        self.dirty=False

    def _discard_unsaved_changes(self):
        """Drop dirty cached records so the next view reloads them from IPS."""
        dirty = set(self.dirty_ids)
        for key in dirty:
            self.graphic_cache.pop(key, None)
            self.history_cache.pop(key, None)
        self.dirty_ids.clear()
        self.dirty=False
        # If the current record was dirty, reload it from the database now.
        if self.current and self._graphic_key(self.current.graphic_id) in dirty:
            current_id=self.current.graphic_id
            row_index=next((i for i,row in enumerate(self.rows) if str(row[0])==str(current_id)), -1)
            self.current=None
            if row_index>=0:
                self.select_graphic(row_index)

    def _clear_current_view(self):
        self.current = None
        self.canvas.set_model(None)
        self.name.clear()
        self.desc.clear()
        self.selected_graphic_label.setText("—")
        self.width_value.setText("—")
        self.height_value.setText("—")
        self.storage_value.setText("—")
        self.editor_title.setText("Pixel Editor")
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.dirty = bool(self.dirty_ids)
        self._update_undo_redo_buttons()
        self._update_graphic_actions()

    def refresh_graphics(self, select_id=None):
        if not self.db:
            return
        self.rows = self.db.list_graphics()
        self.list.blockSignals(True)
        self.list.clear()
        target = -1
        for i, row in enumerate(self.rows):
            self.list.addItem(str(row[1] or f"Graphic {row[0]}"))
            if select_id is not None and str(row[0]) == str(select_id):
                target = i
        if target < 0 and self.rows:
            target = 0
        self.list.setCurrentRow(target)
        self.list.blockSignals(False)
        if target >= 0:
            self.select_graphic(target)
        else:
            self._clear_current_view()

    def _name_exists(self, name, exclude_id=None):
        wanted = name.strip().casefold()
        for row in self.rows:
            if exclude_id is not None and str(row[0]) == str(exclude_id):
                continue
            cached=self.graphic_cache.get(self._graphic_key(row[0]))
            row_name=cached.name if cached is not None else str(row[1] or "")
            if row_name.strip().casefold() == wanted:
                return True
        return False

    def _next_new_graphic_name(self):
        base = "New Graphic"
        if not self._name_exists(base):
            return base
        n = 2
        while self._name_exists(f"{base} {n}"):
            n += 1
        return f"{base} {n}"

    def add_graphic(self):
        if not self.db or not self.current:
            return
        if not self.maybe_save():
            return
        suggested = self._next_new_graphic_name()
        name, ok = QInputDialog.getText(self, "Add Graphic", "Graphic name:", text=suggested)
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Add Graphic", "Graphic name cannot be blank.")
            return
        if self._name_exists(name):
            QMessageBox.warning(self, "Add Graphic", "A graphic with that name already exists.")
            return
        source_id = self.current.graphic_id
        self._begin_task("Adding graphic…", 5)
        try:
            self._task_step(15, "Cloning IPS record…")
            new_id = self.db.duplicate_graphic(source_id, name)
            self._task_step(45, "Reloading graphics…")
            self.refresh_graphics(new_id)
            if self.current and str(self.current.graphic_id) == str(new_id):
                self._task_step(65, "Clearing pixels…")
                self.current.description = ""
                self.desc.setText("")
                self.current.pixels = [[(0,0,0) for _ in range(self.current.width)] for _ in range(self.current.height)]
                self._task_step(75, "Writing new graphic…")
                self.db.save_record(self.current)
                self.canvas.update()
                self.dirty_ids.discard(self._graphic_key(self.current.graphic_id))
                self.dirty = bool(self.dirty_ids)
            self._task_step(100, "Done")
            self.statusBar().showMessage(f"Added {name}", 4000)
        except Exception as e:
            QMessageBox.critical(self, "Add failed", str(e))
        finally:
            self._end_task()


    def add_raw_graphic(self):
        if not self.db or not self.current:
            return
        if not self.maybe_save():
            return

        suggested=self._next_new_graphic_name()
        mode_note=(
            "Legacy monochrome / amber (same IPS storage type as the selected graphic)"
            if self.current.storage_mode=="mono"
            else "RGB color using the current paint color (same IPS storage type as the selected graphic)"
        )
        dlg=RawBitmapDialog(
            self, suggested_name=suggested, default_height=self.current.height, storage_note=mode_note
        )
        if dlg.exec()!=QDialog.DialogCode.Accepted:
            return

        name=dlg.name_edit.text().strip()
        if self._name_exists(name):
            QMessageBox.warning(self,"Add Graphic","A graphic with that name already exists.")
            return
        try:
            width,height,payload=dlg.bitmap_data()
        except ValueError as e:
            QMessageBox.warning(self,"Raw bitmap",str(e))
            return

        source_id=self.current.graphic_id
        # Raw bytes describe the one-bit shape.  On an RGB template the shape
        # is written using the currently selected editor color.
        paint_color=tuple(self.canvas.paint_color) if any(self.canvas.paint_color) else (255,255,255)

        self._begin_task("Adding raw graphic…", 5)
        try:
            self._task_step(15, "Cloning IPS record…")
            new_id=self.db.duplicate_graphic(source_id,name)
            self._task_step(35, "Reloading new record…")
            self.refresh_graphics(new_id)
            if not self.current or str(self.current.graphic_id)!=str(new_id):
                # A stale/odd Jet ID should no longer occur, but recover by the
                # unique graphic name rather than leaving a successfully inserted
                # record unusable.
                match = next((row for row in self.rows if str(row[1] or "").strip().casefold() == name.casefold()), None)
                if match is not None:
                    self.refresh_graphics(match[0])
            if not self.current or self.current.name.strip().casefold()!=name.casefold():
                raise RuntimeError("The new graphic was inserted but could not be reloaded.")

            bits=PlaneCodec.decode(payload,width,height)
            self.current.description=""
            self.current.width=width
            self.current.height=height
            # In IPS v3.8 GraphicWidth is four plus the packed payload width.
            self.current.stored_width=4 + width*PlaneCodec.bytes_per_column(height)

            if self.current.storage_mode=="mono":
                on=(255,255,0)
            else:
                on=paint_color
            self.current.pixels=[
                [on if bits[y][x] else (0,0,0) for x in range(width)]
                for y in range(height)
            ]

            self._task_step(70, "Writing bitmap data…")
            self.db.save_record(self.current)
            # Reload from the database so the editor is showing the exact bytes
            # that were written, not an in-memory approximation. Evict this one
            # cache entry first; ordinary graphic switching never does this.
            self._task_step(90, "Verifying saved graphic…")
            new_key=self._graphic_key(new_id)
            self.graphic_cache.pop(new_key,None)
            self.history_cache.pop(new_key,None)
            self.current=None
            self.refresh_graphics(new_id)
            self._task_step(100, "Done")
            self.statusBar().showMessage(
                f"Added {name} from {dlg.input_byte_count()} raw bitmap bytes ({width}×{height})", 5000
            )
        except Exception as e:
            QMessageBox.critical(self,"Add from raw failed",str(e))
        finally:
            self._end_task()

    def rename_graphic(self):
        if not self.db or not self.current:
            return
        if not self.maybe_save():
            return
        old_name = self.current.name
        name, ok = QInputDialog.getText(self, "Rename Graphic", "New name:", text=old_name)
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Rename Graphic", "Graphic name cannot be blank.")
            return
        if name == old_name:
            return
        if self._name_exists(name, self.current.graphic_id):
            QMessageBox.warning(self, "Rename Graphic", "A graphic with that name already exists.")
            return
        graphic_id = self.current.graphic_id
        try:
            self.db.rename_graphic(graphic_id, name)
            self.current.name=name
            self.name.setText(name)
            self.graphic_cache[self._graphic_key(graphic_id)]=self.current
            self.refresh_graphics(graphic_id)
            self.statusBar().showMessage(f"Renamed {old_name} to {name}", 4000)
        except Exception as e:
            QMessageBox.critical(self, "Rename failed", str(e))

    def delete_graphic(self):
        if not self.db or not self.current:
            return
        if not self.maybe_save():
            return
        graphic_id = self.current.graphic_id
        name = self.current.name or f"Graphic {graphic_id}"
        r = QMessageBox.warning(
            self, "Delete Graphic",
            f"Permanently delete '{name}' from this IPS database?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        old_row = self.list.currentRow()
        self._begin_task("Deleting graphic…", 10)
        try:
            self._task_step(20, "Deleting IPS record…")
            self.db.delete_graphic(graphic_id)
            deleted_key=self._graphic_key(graphic_id)
            self.graphic_cache.pop(deleted_key,None)
            self.history_cache.pop(deleted_key,None)
            self.dirty_ids.discard(deleted_key)
            self.current=None
            self._task_step(55, "Refreshing graphic list…")
            self.rows = self.db.list_graphics()
            next_id = None
            if self.rows:
                idx = min(max(0, old_row), len(self.rows)-1)
                next_id = self.rows[idx][0]
            self._task_step(75, "Loading next graphic…")
            self.refresh_graphics(next_id)
            self._task_step(100, "Done")
            self.statusBar().showMessage(f"Deleted {name}", 4000)
        except Exception as e:
            QMessageBox.critical(self, "Delete failed", str(e))
        finally:
            self._end_task()


    def maybe_save(self):
        self._stash_current()
        if not self.dirty_ids:return True
        count=len(self.dirty_ids)
        noun="graphic" if count==1 else "graphics"
        r=QMessageBox.question(
            self,"Unsaved changes",f"Save changes to {count} modified {noun}?",
            QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel
        )
        if r==QMessageBox.StandardButton.Cancel:return False
        if r==QMessageBox.StandardButton.Save:
            try:self.save()
            except Exception:return False
        else:
            self._discard_unsaved_changes()
        return True

    def open_ips(self):
        if not self.maybe_save():return
        p,_=QFileDialog.getOpenFileName(self,"Open Luminator IPS","","Luminator IPS (*.ips);;All files (*)")
        if not p:return
        try:
            if self.db:self.db.close()
            self.db=IPSDatabase(p);self.db.open()
            self._reset_memory_state()
            self.setWindowTitle(f"Luminator IPS Graphics Editor — {Path(p).name}")
            self.filename_label.setText(Path(p).name)
            self.save_action.setEnabled(True)
            self.save_as_action.setEnabled(True)
            self.manual_scale_override=False
            self.refresh_graphics()
            self.statusBar().showMessage(f"Opened {p} — {len(self.rows)} graphics")
            self._update_graphic_actions()
        except Exception as e:QMessageBox.critical(self,"Open failed",str(e))

    def select_graphic(self,i):
        if i<0 or i>=len(self.rows) or not self.db:return
        try:
            # Switching graphics is memory-only. The outgoing graphic remains
            # cached exactly as edited until the user explicitly saves the IPS.
            self._stash_current()
            target_id=self.rows[i][0]
            key=self._graphic_key(target_id)
            if key in self.graphic_cache:
                self.current=self.graphic_cache[key]
            else:
                self.current=self.db.load_record(self.rows[i])
                self.graphic_cache[key]=self.current
            hist=self.history_cache.get(key, ([], []))
            self.undo_stack=list(hist[0]);self.redo_stack=list(hist[1])
            self.canvas.set_model(self.current)
            self.name.setText(self.current.name)
            self.desc.setText(self.current.description)
            if self.list.item(i) is not None:
                self.list.item(i).setText(self.current.name or f"Graphic {self.current.graphic_id}")
            self.selected_graphic_label.setText(self.current.name or f"Graphic {self.current.graphic_id}")
            self._refresh_metrics()
            mode="Legacy mono / amber" if self.current.storage_mode=="mono" else "RGB color"
            self.storage_value.setText(f"{mode}\n{self.current.layout}")
            self.editor_title.setText(f"Pixel Editor — {self.current.name or 'Graphic'}")
            self.color.setEnabled(self.current.storage_mode!="mono")
            if self.current.storage_mode=="mono":self.color.setCurrentText("Amber")
            self.set_color()
            self.dirty=bool(self.dirty_ids)
            self._update_undo_redo_buttons()
            self._update_graphic_actions()
            self._update_selection_actions_bar()
            self.manual_scale_override=False
            QTimer.singleShot(0,self.auto_fit_canvas)
            QTimer.singleShot(0,self._position_selection_actions_bar)
        except Exception as e:QMessageBox.critical(self,"Graphic decode failed",str(e))

    def meta_changed(self):
        if self.current:
            self.current.name=self.name.text();self.current.description=self.desc.text();self.mark_dirty()
            self.selected_graphic_label.setText(self.current.name or f"Graphic {self.current.graphic_id}")
            self.editor_title.setText(f"Pixel Editor — {self.current.name or 'Graphic'}")
            row=self.list.currentRow()
            if row>=0:self.list.item(row).setText(self.current.name or f"Graphic {self.current.graphic_id}")

    def _history_state(self):
        if not self.current:return None
        return {
            "width":self.current.width,
            "height":self.current.height,
            "stored_width":self.current.stored_width,
            "pixels":[[p for p in row] for row in self.current.pixels],
        }

    def _restore_history_state(self,state):
        if not self.current or not state:return
        self.current.width=int(state["width"])
        self.current.height=int(state["height"])
        self.current.stored_width=int(state["stored_width"])
        self.current.pixels=[[p for p in row] for row in state["pixels"]]
        self.canvas.clear_selection()
        self.canvas.update_geometry()
        self.canvas.update()
        self._refresh_metrics()
        if not self.manual_scale_override:QTimer.singleShot(0,self.auto_fit_canvas)

    def snapshot(self):
        state=self._history_state()
        if state:
            self.undo_stack.append(state)
            self.undo_stack=self.undo_stack[-100:]
            self.redo_stack.clear()
            self._update_undo_redo_buttons()

    def mark_dirty(self):
        if self.current:
            key=self._graphic_key(self.current.graphic_id)
            self.graphic_cache[key]=self.current
            self.dirty_ids.add(key)
        self.dirty=bool(self.dirty_ids)
        self._update_undo_redo_buttons()

    def undo(self):
        if not self.current or not self.undo_stack:return
        current=self._history_state()
        if current:self.redo_stack.append(current)
        self._restore_history_state(self.undo_stack.pop())
        self.mark_dirty()

    def redo(self):
        if not self.current or not self.redo_stack:return
        current=self._history_state()
        if current:self.undo_stack.append(current)
        self._restore_history_state(self.redo_stack.pop())
        self.mark_dirty()

    def _refresh_metrics(self):
        if not self.current:
            self.width_value.setText("—");self.height_value.setText("—")
            return
        self.width_value.setText(f"{self.current.width} px")
        self.height_value.setText(f"{self.current.height} px")

    def _sync_stored_width(self):
        if self.current:
            self.current.stored_width=4+self.current.width*PlaneCodec.bytes_per_column(self.current.height)

    @staticmethod
    def _max_ips_width_for_height(height):
        # The packed GraphicWidth is repeated as one byte in the native plane
        # header. Four of those units are IPS overhead rather than pixel data.
        return max(1,(255-4)//PlaneCodec.bytes_per_column(height))

    def _confirm_edge_data_loss(self,kind,side,has_pixels):
        if not has_pixels:return True
        label=f"{side}most {kind}"
        r=QMessageBox.warning(
            self,"Discard Pixel Data?",
            f"The {label} contains lit pixels. Removing it will permanently discard those pixels.\n\nContinue?",
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return r==QMessageBox.StandardButton.Yes

    def _finish_edge_resize(self,message):
        self._sync_stored_width()
        self.canvas.clear_selection()
        self.canvas.update_geometry()
        self.canvas.update()
        self._refresh_metrics()
        self.mark_dirty()
        if not self.manual_scale_override:
            QTimer.singleShot(0,self.auto_fit_canvas)
        QTimer.singleShot(0,self._position_editor_overlays)
        self.statusBar().showMessage(message,2500)

    def _choose_edge_resize_amount(self,kind,side,adding):
        """Long-press action: ask how far this canvas edge should move."""
        if not self.current:return
        noun="column" if kind=="column" else "row"
        plural=noun+"s"
        if adding:
            if kind=="column":
                maximum=self._max_ips_width_for_height(self.current.height)-self.current.width
            else:
                maximum=0
                for candidate in range(self.current.height+1,256):
                    if self.current.width<=self._max_ips_width_for_height(candidate):
                        maximum=candidate-self.current.height
                    else:
                        break
            verb="Add"
            action="add"
            if maximum<1:
                self.statusBar().showMessage("This graphic is already at the IPS native size limit.",2500)
                return
        else:
            dimension=self.current.width if kind=="column" else self.current.height
            maximum=dimension-1
            verb="Remove"
            action="remove"
            if maximum<1:
                self.statusBar().showMessage(f"A graphic must keep at least one {noun}.",2500)
                return
        count,ok=QInputDialog.getInt(
            self,f"{verb} {plural.title()}",
            f"How many {plural} should I {action} at the {side} edge?",
            2,1,maximum,1)
        if not ok:return
        if kind=="column":
            self._resize_columns(side,count if adding else -count)
        else:
            self._resize_rows(side,count if adding else -count)

    def _resize_columns(self,side,delta):
        if not self.current or side not in ("left","right") or delta==0:return
        blank=(0,0,0)
        if delta>0:
            count=int(delta)
            max_width=self._max_ips_width_for_height(self.current.height)
            if self.current.width+count>max_width:
                QMessageBox.warning(self,"IPS Size Limit",
                    f"At {self.current.height} pixels high, IPS can store at most {max_width} columns.")
                return
            self.snapshot()
            padding=[blank]*count
            if side=="left":
                self.current.pixels=[padding+list(row) for row in self.current.pixels]
            else:
                self.current.pixels=[list(row)+padding for row in self.current.pixels]
            self.current.width+=count
            self._finish_edge_resize(
                f"Added {count} blank {side} column"+("s" if count!=1 else ""))
            return

        count=min(int(-delta),self.current.width-1)
        if count<1:
            self.statusBar().showMessage("A graphic must keep at least one column.",2500)
            return
        if side=="left":
            has_pixels=any(any(pixel) for row in self.current.pixels for pixel in row[:count])
        else:
            has_pixels=any(any(pixel) for row in self.current.pixels for pixel in row[-count:])
        if has_pixels:
            label=f"{count} {side}most column"+("s" if count!=1 else "")
            r=QMessageBox.warning(
                self,"Discard Pixel Data?",
                f"The {label} contain lit pixels. Removing them will permanently discard those pixels.\n\nContinue?",
                QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if r!=QMessageBox.StandardButton.Yes:return
        self.snapshot()
        if side=="left":
            self.current.pixels=[row[count:] for row in self.current.pixels]
        else:
            self.current.pixels=[row[:-count] for row in self.current.pixels]
        self.current.width-=count
        self._finish_edge_resize(
            f"Removed {count} {side} column"+("s" if count!=1 else ""))

    def _resize_rows(self,side,delta):
        if not self.current or side not in ("top","bottom") or delta==0:return
        if delta>0:
            count=int(delta)
            new_height=self.current.height+count
            max_width=self._max_ips_width_for_height(new_height) if new_height<=255 else 0
            if new_height>255 or self.current.width>max_width:
                QMessageBox.warning(self,"IPS Size Limit",
                    "That height would make this graphic too large for the IPS native graphic header.")
                return
            self.snapshot()
            rows=[[(0,0,0) for _ in range(self.current.width)] for _ in range(count)]
            if side=="top":
                self.current.pixels=rows+[list(row) for row in self.current.pixels]
            else:
                self.current.pixels=[list(row) for row in self.current.pixels]+rows
            self.current.height+=count
            self._finish_edge_resize(
                f"Added {count} blank {side} row"+("s" if count!=1 else ""))
            return

        count=min(int(-delta),self.current.height-1)
        if count<1:
            self.statusBar().showMessage("A graphic must keep at least one row.",2500)
            return
        removed=self.current.pixels[:count] if side=="top" else self.current.pixels[-count:]
        has_pixels=any(any(pixel) for row in removed for pixel in row)
        if has_pixels:
            label=f"{count} {side}most row"+("s" if count!=1 else "")
            r=QMessageBox.warning(
                self,"Discard Pixel Data?",
                f"The {label} contain lit pixels. Removing them will permanently discard those pixels.\n\nContinue?",
                QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if r!=QMessageBox.StandardButton.Yes:return
        self.snapshot()
        if side=="top":self.current.pixels=self.current.pixels[count:]
        else:self.current.pixels=self.current.pixels[:-count]
        self.current.height-=count
        self._finish_edge_resize(
            f"Removed {count} {side} row"+("s" if count!=1 else ""))

    def add_column(self,side):
        self._resize_columns(side,1)

    def add_row(self,side):
        self._resize_rows(side,1)

    def remove_column(self,side):
        self._resize_columns(side,-1)

    def remove_row(self,side):
        self._resize_rows(side,-1)

    def set_selection_pixels(self,value):
        if not self.current or self.canvas.selection is None:return
        l,t,r,b=self.canvas.selection
        self.snapshot()
        if self.current.storage_mode=="mono":
            on=(255,255,0)
        else:
            data=self.color.currentData()
            on=tuple(data) if data is not None and any(data) else (255,255,255)
        target=on if value else (0,0,0)
        for y in range(t,b+1):
            for x in range(l,r+1):self.current.pixels[y][x]=target
        self.canvas.update();self.mark_dirty()

    def copy_pixels(self):
        if not self.current:return
        if self.canvas.selection is not None:l,t,r,b=self.canvas.selection
        else:l,t,r,b=0,0,self.current.width-1,self.current.height-1
        rows=[[self.current.pixels[y][x] for x in range(l,r+1)] for y in range(t,b+1)]
        self.clipboard={"width":r-l+1,"height":b-t+1,"rows":rows}
        self._update_selection_actions_bar()
        self.statusBar().showMessage(f"Copied {r-l+1}x{b-t+1} pixels",2500)

    def _paste_rows(self,rows,max_w,max_h,origin_x=0,origin_y=0):
        for sy,row in enumerate(rows):
            if sy>=max_h:break
            for sx,value in enumerate(row):
                if sx>=max_w:break
                tx=origin_x+sx;ty=origin_y+sy
                if 0<=tx<self.current.width and 0<=ty<self.current.height:
                    self.current.pixels[ty][tx]=value

    def paste_pixels(self):
        if not self.current or not self.clipboard:return
        rows=self.clipboard["rows"]
        self.snapshot()
        if self.canvas.selection is not None:
            l,t,r,b=self.canvas.selection
            self._paste_rows(rows,r-l+1,b-t+1,l,t)
            msg="Pasted into selection"
        else:
            self._paste_rows(rows,self.current.width,self.current.height,0,0)
            msg="Pasted"
        self.canvas.update();self.mark_dirty();self.statusBar().showMessage(msg,2500)

    def paste_raw_bitmap(self):
        """Paste human-readable bitmap rows into the current selection.

        Raw dimensions are preserved for this operation (unlike Add from Raw
        Bitmap, which trims blank border columns). If the bitmap exceeds the
        selection, the user can either crop it to the selection or paste the
        full bitmap from the selection's top-left corner.
        """
        if not self.current or self.canvas.selection is None:
            return

        l,t,r,b=self.canvas.selection
        sel_w=r-l+1
        sel_h=b-t+1
        dlg=RawBitmapDialog(
            self,
            show_name=False,
            trim_blank_borders=False,
            window_title="Paste Raw Bitmap into Selection",
            storage_note=f"Selection: {sel_w} × {sel_h} pixels",
            intro_text=(
                "Paste one bitmap row per line. Bytes are read left-to-right and bits are MSB-first. "
                "The bitmap will start at the top-left pixel of the current selection."
            ),
        )
        if dlg.exec()!=QDialog.DialogCode.Accepted:
            return
        try:
            width,height,payload=dlg.bitmap_data()
        except ValueError as e:
            QMessageBox.warning(self,"Raw bitmap",str(e))
            return

        paste_full=True
        exceeds=width>sel_w or height>sel_h
        if exceeds:
            box=QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Raw bitmap exceeds selection")
            box.setText(
                f"The raw bitmap is {width} × {height} pixels, but the selection is "
                f"{sel_w} × {sel_h}."
            )
            box.setInformativeText(
                "Trim limits the paste to the selection box. Paste Full starts at the "
                "selection's top-left corner and may extend outside the selection."
            )
            trim_btn=box.addButton("Trim to Selection",QMessageBox.ButtonRole.AcceptRole)
            full_btn=box.addButton("Paste Full from Top Left",QMessageBox.ButtonRole.ActionRole)
            cancel_btn=box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(trim_btn)
            box.exec()
            clicked=box.clickedButton()
            if clicked is cancel_btn or clicked is None:
                return
            paste_full=(clicked is full_btn)

        bits=PlaneCodec.decode(payload,width,height)
        if self.current.storage_mode=="mono":
            on=(255,255,0)
        else:
            data=self.color.currentData()
            on=tuple(data) if data is not None and any(data) else (255,255,255)
        rows=[
            [on if bits[y][x] else (0,0,0) for x in range(width)]
            for y in range(height)
        ]

        self.snapshot()
        if exceeds and not paste_full:
            max_w=sel_w
            max_h=sel_h
            msg=f"Pasted raw bitmap trimmed to {sel_w}×{sel_h} selection"
        else:
            # Full paste may leave the selection but can never write beyond the
            # graphic itself. _paste_rows clips against the current canvas.
            max_w=max(0,self.current.width-l)
            max_h=max(0,self.current.height-t)
            clipped=(width>max_w or height>max_h)
            msg=f"Pasted raw bitmap {width}×{height} from selection top-left"
            if clipped:
                msg += " (clipped at graphic edge)"
        self._paste_rows(rows,max_w,max_h,l,t)
        self.canvas.update()
        self.mark_dirty()
        self.statusBar().showMessage(msg,4000)

    def mirror_pixels(self,axis):
        if not self.current:return
        if self.canvas.selection is not None:l,t,r,b=self.canvas.selection
        else:l,t,r,b=0,0,self.current.width-1,self.current.height-1
        rows=[[self.current.pixels[y][x] for x in range(l,r+1)] for y in range(t,b+1)]
        flipped=[row[::-1] for row in rows] if axis=="horizontal" else rows[::-1]
        self.snapshot()
        self._paste_rows(flipped,r-l+1,b-t+1,l,t)
        self.canvas.update();self.mark_dirty()
        self.statusBar().showMessage("Mirrored left-right" if axis=="horizontal" else "Mirrored top-bottom",2500)

    def _position_editor_overlays(self):
        self._position_selection_actions_bar()
        self._position_canvas_edge_buttons()

    def _position_selection_actions_bar(self):
        if not hasattr(self,"selection_actions_bar"):return
        bar=self.selection_actions_bar
        bar.adjustSize()
        viewport=self.scroll.viewport()
        viewport_origin=viewport.mapTo(self.scroll,QPoint(0,0))
        canvas_origin=self.canvas.mapTo(self.scroll,QPoint(0,0))
        canvas_center_x=canvas_origin.x()+self.canvas.width()//2
        x=canvas_center_x-bar.width()//2
        min_x=viewport_origin.x()
        max_x=max(min_x,viewport_origin.x()+viewport.width()-bar.width())
        x=max(min_x,min(x,max_x))
        bar.move(x,viewport_origin.y()+10)
        bar.raise_()

    def _position_canvas_edge_buttons(self):
        required=(
            "add_left_edge_btn","remove_left_edge_btn",
            "add_right_edge_btn","remove_right_edge_btn",
            "add_top_edge_btn","remove_top_edge_btn",
            "add_bottom_edge_btn","remove_bottom_edge_btn",
        )
        if not all(hasattr(self,name) for name in required):
            return
        buttons=tuple(getattr(self,name) for name in required)
        visible=bool(self.current)
        for button in buttons:
            button.setVisible(visible)
        if not visible:
            return

        viewport=self.scroll.viewport()
        viewport_origin=viewport.mapTo(self.scroll,QPoint(0,0))
        canvas_origin=self.canvas.mapTo(self.scroll,QPoint(0,0))
        pad=7
        gap=4
        bw=self.add_left_edge_btn.width()
        bh=self.add_left_edge_btn.height()

        vx0=viewport_origin.x()+pad
        vy0=viewport_origin.y()+pad
        vx1=viewport_origin.x()+viewport.width()-pad
        vy1=viewport_origin.y()+viewport.height()-pad

        # Left/right pairs are stacked vertically and centered on their edge.
        side_h=bh*2+gap
        side_y=canvas_origin.y()+(self.canvas.height()-side_h)//2
        side_y=max(vy0,min(side_y,max(vy0,vy1-side_h)))
        left_x=canvas_origin.x()-bw-pad
        right_x=canvas_origin.x()+self.canvas.width()+pad
        left_x=max(vx0,min(left_x,max(vx0,vx1-bw)))
        right_x=max(vx0,min(right_x,max(vx0,vx1-bw)))

        self.add_left_edge_btn.move(left_x,side_y)
        self.remove_left_edge_btn.move(left_x,side_y+bh+gap)
        self.add_right_edge_btn.move(right_x,side_y)
        self.remove_right_edge_btn.move(right_x,side_y+bh+gap)

        # Top/bottom pairs sit horizontally centered on their edge.
        pair_w=bw*2+gap
        pair_x=canvas_origin.x()+(self.canvas.width()-pair_w)//2
        pair_x=max(vx0,min(pair_x,max(vx0,vx1-pair_w)))
        top_y=canvas_origin.y()-bh-pad
        bottom_y=canvas_origin.y()+self.canvas.height()+pad
        top_y=max(vy0,min(top_y,max(vy0,vy1-bh)))
        bottom_y=max(vy0,min(bottom_y,max(vy0,vy1-bh)))

        self.add_top_edge_btn.move(pair_x,top_y)
        self.remove_top_edge_btn.move(pair_x+bw+gap,top_y)
        self.add_bottom_edge_btn.move(pair_x,bottom_y)
        self.remove_bottom_edge_btn.move(pair_x+bw+gap,bottom_y)

        for button in buttons:
            button.raise_()

    def _update_selection_actions_bar(self):
        if not hasattr(self,"selection_actions_bar"):return
        has_model=bool(self.current)
        self.selection_actions_bar.setVisible(has_model)
        if has_model:self._position_selection_actions_bar()
        selection_active=has_model and self.canvas.selection is not None
        self.set_lit_btn.setEnabled(selection_active)
        self.set_unlit_btn.setEnabled(selection_active)
        self.copy_btn.setEnabled(has_model)
        self.paste_btn.setEnabled(has_model and self.clipboard is not None)
        self.paste_raw_btn.setEnabled(selection_active)
        self.mirror_h_btn.setEnabled(has_model)
        self.mirror_v_btn.setEnabled(has_model)

    def eventFilter(self,obj,event):
        if hasattr(self,"scroll") and obj is self.scroll.viewport() and event.type()==QEvent.Type.MouseButtonPress:
            if self.canvas.selection is not None:self.canvas.clear_selection()
        if hasattr(self,"scroll") and event.type()==QEvent.Type.Resize and obj in (self.scroll,self.scroll.viewport(),self.canvas):
            QTimer.singleShot(0,self._position_editor_overlays)
        return super().eventFilter(obj,event)

    def clear_graphic(self):
        if not self.current:return
        self.snapshot()
        self.current.pixels=[[(0,0,0) for _ in range(self.current.width)] for _ in range(self.current.height)]
        self.canvas.update();self.mark_dirty()

    def save(self):
        if not self.db:return
        self._stash_current()
        if not self.dirty_ids:
            self.statusBar().showMessage("No unsaved graphic changes",2500)
            return
        dirty_keys=list(self.dirty_ids)
        selected_id=self.current.graphic_id if self.current else None
        self._begin_task("Saving graphics…",0)
        try:
            total=len(dirty_keys)
            saved=0
            for n,key in enumerate(dirty_keys,1):
                g=self.graphic_cache.get(key)
                if g is None:
                    continue
                pct=int(((n-1)/max(1,total))*90)
                self._task_step(pct,f"Saving {g.name or 'graphic'}…")
                self.db.save_record(g)
                saved+=1
            self.dirty_ids.clear()
            self.dirty=False
            self._task_step(92,"Refreshing graphic list…")
            # Re-read only the lightweight list so renamed graphics sort the
            # same way IPS does. Cached GraphicRecord objects stay intact.
            self.refresh_graphics(selected_id)
            self._task_step(100,"Done")
            noun="graphic" if saved==1 else "graphics"
            self.statusBar().showMessage(f"Saved {saved} {noun}",4000)
        except Exception as e:
            QMessageBox.critical(self,"Save failed",str(e));raise
        finally:
            self._end_task()

    def save_as(self):
        if not self.db:return
        p,_=QFileDialog.getSaveFileName(self,"Save IPS Copy",str(self.db.path.with_name(self.db.path.stem+"_edited.ips")),"Luminator IPS (*.ips)")
        if not p:return
        try:
            selected_id=self.current.graphic_id if self.current else None
            self.save();self.db.close();shutil.copy2(self.db.path,p);self.db=IPSDatabase(p);self.db.open()
            self._reset_memory_state()
            self.filename_label.setText(Path(p).name)
            self.setWindowTitle(f"Luminator IPS Graphics Editor — {Path(p).name}")
            self.refresh_graphics(selected_id)
            self.statusBar().showMessage(f"Now editing {p}",5000)
        except Exception as e:QMessageBox.critical(self,"Save As failed",str(e))

    def export_png(self):
        if not self.current:return
        p,_=QFileDialog.getSaveFileName(self,"Export PNG",f"{self.current.name or 'graphic'}.png","PNG (*.png)")
        if not p:return
        im=Image.new("RGB",(self.current.width,self.current.height));im.putdata([p for row in self.current.pixels for p in row]);im.save(p)

    def import_png(self):
        if not self.current:return
        p,_=QFileDialog.getOpenFileName(self,"Import PNG","","Images (*.png *.bmp *.gif *.jpg *.jpeg)")
        if not p:return
        try:
            im=Image.open(p).convert("RGB")
            if im.size!=(self.current.width,self.current.height):
                r=QMessageBox.question(self,"Resize image",f"Image is {im.width}×{im.height}; graphic is {self.current.width}×{self.current.height}. Resize with nearest-neighbor?")
                if r!=QMessageBox.StandardButton.Yes:return
                im=im.resize((self.current.width,self.current.height),Image.Resampling.NEAREST)
            self.snapshot();data=list(im.getdata())
            if self.current.storage_mode=="mono":
                self.current.pixels=[[((255,255,0) if max(data[y*self.current.width+x])>=128 else (0,0,0)) for x in range(self.current.width)] for y in range(self.current.height)]
            else:
                palette=[rgb for _,rgb in COLORS]
                def nearest(c):return min(palette,key=lambda q:sum((c[i]-q[i])**2 for i in range(3)))
                self.current.pixels=[[nearest(data[y*self.current.width+x]) for x in range(self.current.width)] for y in range(self.current.height)]
            self.canvas.update();self.mark_dirty()
        except Exception as e:QMessageBox.critical(self,"Import failed",str(e))

    def manual_scale_changed(self,value):
        self.scale_value_label.setText(f"{value}%")
        if not self.current:return
        self.manual_scale_override=True
        self.canvas.set_scale_factor(value/100.0)
        self.statusBar().showMessage(f"Scale: {value}% (manual)",2500)

    def enable_auto_fit(self):
        self.manual_scale_override=False
        self.auto_fit_canvas()
        self.statusBar().showMessage("Scale: automatic fit",2500)

    def auto_fit_canvas(self):
        if not self.current or self.manual_scale_override:return
        cols=max(1,self.current.width);rows=max(1,self.current.height)
        viewport=self.scroll.viewport().size()
        available_w=max(1,viewport.width()-28);available_h=max(1,viewport.height()-28)
        natural_w=cols*self.canvas.base_cell_size+1;natural_h=rows*self.canvas.base_cell_size+1
        fit_factor=min(1.0,available_w/natural_w,available_h/natural_h)
        fit_factor=max(0.15,fit_factor)
        self.canvas.set_scale_factor(fit_factor)
        pct=int(round(fit_factor*100))
        self.scale_slider.blockSignals(True);self.scale_slider.setValue(pct);self.scale_slider.blockSignals(False)
        self.scale_value_label.setText(f"{pct}%")

    def keyPressEvent(self,event):
        if event.modifiers()&Qt.KeyboardModifier.ControlModifier:
            if event.key()==Qt.Key.Key_Z:self.undo();event.accept();return
            if event.key()==Qt.Key.Key_Y:self.redo();event.accept();return
            if event.key()==Qt.Key.Key_C:self.copy_pixels();event.accept();return
            if event.key()==Qt.Key.Key_V:self.paste_pixels();event.accept();return
        super().keyPressEvent(event)

    def wheelEvent(self,event):
        if event.modifiers()&Qt.KeyboardModifier.ControlModifier:
            delta=event.angleDelta().y()
            if delta:
                steps=delta/120.0;current=self.scale_slider.value();new_value=int(round(current+steps*10))
                new_value=max(self.scale_slider.minimum(),min(self.scale_slider.maximum(),new_value))
                if new_value!=current:self.scale_slider.setValue(new_value)
            event.accept();return
        super().wheelEvent(event)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if not self.manual_scale_override:QTimer.singleShot(0,self.auto_fit_canvas)
        QTimer.singleShot(0,self._position_selection_actions_bar)

    def closeEvent(self,e):
        if self.maybe_save():
            if self.db:self.db.close()
            e.accept()
        else:e.ignore()

def main():
    app=QApplication(sys.argv);app.setStyle("Fusion");w=MainWindow();w.show();sys.exit(app.exec())
if __name__=="__main__":main()
