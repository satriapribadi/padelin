"""Penulis PDF minimal, murni stdlib.

Dibuat sendiri karena Python 3.14 belum punya wheel untuk sebagian besar
library PDF, dan tool ini dirancang tanpa dependency eksternal. Yang dipakai
hanya font bawaan PDF (Helvetica), jadi tidak ada font yang perlu ditanam dan
ukuran filenya kecil.

Cakupan sengaja dibatasi pada yang dibutuhkan laporan jadwal: teks, garis,
kotak berwarna, dan tabel sederhana dengan halaman otomatis.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

# Lebar karakter font bawaan (per 1000 unit em), untuk ASCII 32-126.
_HELV = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278,
    278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584,
    584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556,
    833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278,
    278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222,
    500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500,
    500, 334, 260, 334, 584,
)
_HELV_BOLD = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278,
    278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584,
    584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611,
    833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333,
    278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278,
    556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556,
    500, 389, 280, 389, 584,
)

A4 = (595.28, 841.89)
A4_LANDSCAPE = (841.89, 595.28)


def text_width(s: str, size: float, bold: bool = False) -> float:
    table = _HELV_BOLD if bold else _HELV
    total = 0
    for ch in s:
        o = ord(ch)
        total += table[o - 32] if 32 <= o <= 126 else 556
    return total * size / 1000.0


def truncate(s: str, max_width: float, size: float, bold: bool = False) -> str:
    """Potong teks agar muat, tambahkan elipsis kalau kepotong."""
    if text_width(s, size, bold) <= max_width:
        return s
    ell = ".."
    ew = text_width(ell, size, bold)
    out = ""
    for ch in s:
        if text_width(out + ch, size, bold) + ew > max_width:
            break
        out += ch
    return out + ell


def _esc(s: str) -> str:
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


@dataclass
class Page:
    width: float
    height: float
    ops: list[str] = field(default_factory=list)


class PDF:
    """Kanvas PDF sederhana dengan koordinat dari kiri-atas."""

    def __init__(self, size: tuple[float, float] = A4, margin: float = 36.0):
        self.size = size
        self.margin = margin
        self.pages: list[Page] = []
        self.y = 0.0
        self.new_page()

    # -- halaman ----------------------------------------------------------
    def new_page(self) -> None:
        self.pages.append(Page(width=self.size[0], height=self.size[1]))
        self.y = self.margin

    @property
    def page(self) -> Page:
        return self.pages[-1]

    @property
    def content_width(self) -> float:
        return self.size[0] - 2 * self.margin

    def space_left(self) -> float:
        return self.size[1] - self.margin - self.y

    def ensure(self, needed: float) -> bool:
        """Pindah halaman kalau sisa ruang kurang. True kalau ganti halaman."""
        if self.space_left() < needed:
            self.new_page()
            return True
        return False

    # -- primitif ---------------------------------------------------------
    def _ty(self, y: float) -> float:
        """Koordinat kiri-atas -> sistem PDF (kiri-bawah)."""
        return self.size[1] - y

    def text(self, x: float, y: float, s: str, size: float = 10,
             bold: bool = False, color: tuple[float, float, float] = (0, 0, 0)) -> None:
        font = "F2" if bold else "F1"
        r, g, b = color
        self.page.ops.append(
            f"BT /{font} {size:.2f} Tf {r:.3f} {g:.3f} {b:.3f} rg "
            f"1 0 0 1 {x:.2f} {self._ty(y) - size:.2f} Tm ({_esc(s)}) Tj ET"
        )

    def text_right(self, x_right: float, y: float, s: str, size: float = 10,
                   bold: bool = False,
                   color: tuple[float, float, float] = (0, 0, 0)) -> None:
        self.text(x_right - text_width(s, size, bold), y, s, size, bold, color)

    def text_center(self, x_center: float, y: float, s: str, size: float = 10,
                    bold: bool = False,
                    color: tuple[float, float, float] = (0, 0, 0)) -> None:
        self.text(x_center - text_width(s, size, bold) / 2, y, s, size, bold, color)

    def rect(self, x: float, y: float, w: float, h: float,
             fill: tuple[float, float, float] | None = None,
             stroke: tuple[float, float, float] | None = None,
             line_width: float = 0.5) -> None:
        ops = []
        if fill:
            ops.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg")
        if stroke:
            ops.append(f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG "
                       f"{line_width:.2f} w")
        ops.append(f"{x:.2f} {self._ty(y) - h:.2f} {w:.2f} {h:.2f} re")
        if fill and stroke:
            ops.append("B")
        elif fill:
            ops.append("f")
        else:
            ops.append("S")
        self.page.ops.append(" ".join(ops))

    def line(self, x1: float, y1: float, x2: float, y2: float,
             color: tuple[float, float, float] = (0.8, 0.8, 0.8),
             width: float = 0.5) -> None:
        self.page.ops.append(
            f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {width:.2f} w "
            f"{x1:.2f} {self._ty(y1):.2f} m {x2:.2f} {self._ty(y2):.2f} l S"
        )

    # -- serialisasi ------------------------------------------------------
    def output(self, title: str = "Jadwal Padel") -> bytes:
        objects: list[bytes] = []

        def add(obj: bytes) -> int:
            objects.append(obj)
            return len(objects)

        font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                           b"/Encoding /WinAnsiEncoding >>")
        font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                        b"/Encoding /WinAnsiEncoding >>")

        pages_id = len(objects) + 1 + 2 * len(self.pages) + 1
        page_ids: list[int] = []
        for pg in self.pages:
            stream = "\n".join(pg.ops).encode("latin-1", "replace")
            compressed = zlib.compress(stream)
            content_id = add(
                b"<< /Length " + str(len(compressed)).encode()
                + b" /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream"
            )
            page_id = add(
                f"<< /Type /Page /Parent {pages_id} 0 R "
                f"/MediaBox [0 0 {pg.width:.2f} {pg.height:.2f}] "
                f"/Resources << /Font << /F1 {font_regular} 0 R "
                f"/F2 {font_bold} 0 R >> >> "
                f"/Contents {content_id} 0 R >>".encode("latin-1")
            )
            page_ids.append(page_id)

        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        actual_pages_id = add(
            f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")
        )
        catalog_id = add(f"<< /Type /Catalog /Pages {actual_pages_id} 0 R >>"
                         .encode("latin-1"))
        info_id = add(f"<< /Title ({_esc(title)}) /Producer (padel-scheduler) >>"
                      .encode("latin-1"))

        # Perbaiki referensi /Parent kalau perkiraan id meleset.
        if actual_pages_id != pages_id:
            for i, pid in enumerate(page_ids):
                objects[pid - 1] = objects[pid - 1].replace(
                    f"/Parent {pages_id} 0 R".encode("latin-1"),
                    f"/Parent {actual_pages_id} 0 R".encode("latin-1"),
                )

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode("latin-1") + obj + b"\nendobj\n"

        xref_pos = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode("latin-1")
        out += (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R "
            f"/Info {info_id} 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
        ).encode("latin-1")
        return bytes(out)
