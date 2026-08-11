#!/usr/bin/env python3
"""Uji interaksi UI lewat Chrome DevTools Protocol, tanpa dependency.

Pemeriksaan statis dan render sekali jalan tidak bisa menjawab "apakah tombolnya
benar-benar bekerja". Skrip ini menjalankan Edge/Chrome headless, menyambung ke
DevTools Protocol, lalu mengeksekusi JavaScript di dalam halaman untuk mengklik,
mengetik, dan memeriksa hasilnya.

CDP berjalan di atas WebSocket, yang tidak ada di stdlib, jadi klien WebSocket
minimalnya ditulis di sini - hanya yang dibutuhkan: handshake, kirim frame teks
bertopeng, terima frame teks.

Pakai:
    python tools/uitest.py [--url http://127.0.0.1:8770] [--keep]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    # macOS. Tanpa baris ini skripnya berhenti dengan "Tidak menemukan Edge
    # atau Chrome" di mesin yang browsernya jelas terpasang, dan uji UI-nya
    # dilewati diam-diam - padahal justru di sinilah bug yang lolos dari
    # pemeriksaan statis ketahuan.
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


# ---------------------------------------------------------------------------
# Klien WebSocket minimal (RFC 6455) - hanya frame teks, tanpa ekstensi
# ---------------------------------------------------------------------------

class WS:
    def __init__(self, url: str):
        assert url.startswith("ws://"), f"hanya ws:// yang didukung: {url}"
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake WebSocket terputus")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise ConnectionError(f"handshake gagal: {buf.split(b'0d0a')[0]!r}")
        self.buf = buf.split(b"\r\n\r\n", 1)[1]

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("koneksi ditutup")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])          # FIN + opcode teks
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)                # klien wajib menopengi payload
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        while True:
            b0, b1 = self._read(2)
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n)            # frame dari server tidak bertopeng
            if opcode == 0x8:
                raise ConnectionError("server menutup koneksi")
            if opcode == 0x9:               # ping -> balas pong
                continue
            if opcode in (0x1, 0x2):
                return data.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Pembungkus CDP
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Port kosong yang dipilih OS.

    Port tetap bikin harness ini rapuh: sisa proses browser dari run sebelumnya
    masih memegang portnya, sehingga klien menyambung ke browser lama yang
    menampilkan halaman basi.
    """
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


class Browser:
    def __init__(self, url: str, port: int | None = None):
        port = port or _free_port()
        exe = next((b for b in BROWSERS if Path(b).exists()), None)
        if not exe:
            raise SystemExit("Tidak menemukan Edge atau Chrome.")
        self.url = url
        self.profile = tempfile.mkdtemp(prefix="uitest-")
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--hide-scrollbars",
             f"--remote-debugging-port={port}",
             f"--user-data-dir={self.profile}", "--window-size=1400,1200", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.msg_id = 0
        target = self._wait_target(port)
        self.ws = WS(target)

    def _wait_target(self, port: int, timeout: float = 25.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json", timeout=2).read()
                pages = [t for t in json.loads(raw)
                         if t.get("type") == "page"
                         and t.get("webSocketDebuggerUrl")]
                # Utamakan tab yang sudah memuat URL yang diminta.
                for t in pages:
                    if t.get("url", "").startswith(self.url):
                        return t["webSocketDebuggerUrl"]
                if pages:
                    return pages[0]["webSocketDebuggerUrl"]
            except Exception:
                pass
            time.sleep(0.4)
        raise SystemExit("DevTools tidak merespons.")

    def call(self, method: str, **params):
        self.msg_id += 1
        mid = self.msg_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expression: str):
        """Jalankan JS di halaman; Promise ditunggu sampai selesai."""
        res = self.call("Runtime.evaluate", expression=expression,
                        awaitPromise=True, returnByValue=True)
        result = res.get("result", {})
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise RuntimeError(f"JS gagal: {text}")
        return result.get("value")

    def wait_for(self, expression: str, timeout: float = 20.0, label: str = ""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js(f"!!({expression})"):
                return True
            time.sleep(0.25)
        raise AssertionError(f"Timeout menunggu: {label or expression}")

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# Skenario
# ---------------------------------------------------------------------------

PASS, FAIL = [], []


def load_roster(path: str | None) -> list[str]:
    """Baca "Nama, rating, L/P" per baris; kalau tidak ada, pakai contoh.

    Nama anggota klub itu data pribadi - biarkan di file lokal yang
    di-gitignore, bukan di dalam berkas sumber.
    """
    if path:
        raw = Path(path).read_text(encoding="utf-8").splitlines()
        rows = [l.strip() for l in raw
                if l.strip() and not l.lstrip().startswith("#")]
        if len(rows) < 4:
            raise SystemExit(f"{path}: butuh minimal 4 peserta, ada {len(rows)}")
        return rows
    return [f"Pemain {i + 1}, {2 + (i % 6) * 0.5}, {'L' if i < 13 else 'P'}"
            for i in range(26)]


def check(name: str, fn) -> None:
    try:
        detail = fn()
        PASS.append(name)
        print(f"  [OK ] {name}" + (f" - {detail}" if detail else ""))
    except Exception as exc:  # noqa: BLE001 - laporan uji, bukan kode produksi
        FAIL.append((name, str(exc)))
        print(f"  [GAGAL] {name}\n         {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    ap.add_argument("--roster", metavar="FILE",
                    help="daftar peserta 'Nama, rating, L/P' per baris. "
                         "Tanpa ini dipakai nama contoh Pemain 1..26. "
                         "Simpan roster asli di file yang di-gitignore, "
                         "jangan di dalam kode.")
    args = ap.parse_args()

    roster = load_roster(args.roster)

    # Nama unik per run supaya baris quick-add memang selalu "baru".
    stamp = time.strftime('%H%M%S')
    new_venue = f'Venue Uji {stamp}'
    created_venue_id = None

    print(f"Uji interaksi UI di {args.url}\n")
    b = Browser(args.url)
    try:
        # Tangkap error JS apa pun yang muncul selama sesi.
        b.js("""
          window.__errs = [];
          window.addEventListener('error', e => window.__errs.push(e.message));
          window.addEventListener('unhandledrejection',
            e => window.__errs.push('promise: ' + e.reason));
          true
        """)
        b.wait_for("document.querySelector('#preset option')", label="app siap")

        # --- 1. isi peserta lewat tempel massal ---------------------------
        def bulk():
            b.js("(() => { document.getElementById('bulk').value = "
                 + json.dumps("\n".join(roster))
                 + "; document.getElementById('parse-bulk').click(); })(); true")
            b.wait_for("document.querySelectorAll('#ptable tbody tr').length === "
                       f"{len(roster)}", label=f"{len(roster)} baris peserta")
            return b.js("document.getElementById('counts').textContent.trim()")
        check(f"Tempel massal {len(roster)} peserta", bulk)

        # --- 1b. autocomplete peserta dari master -------------------------
        pick_combo = "document.getElementById('pick_player').closest('.combo')"

        def picker():
            # Simpan dulu roster ke master supaya ada yang bisa disarankan.
            b.js("document.getElementById('save-roster').click(); true")
            time.sleep(1.2)
            before = b.js("document.querySelectorAll('#ptable tbody tr').length")

            # Hapus satu peserta, lalu tambahkan lagi lewat autocomplete.
            removed = b.js("(() => { const t = document.querySelector("
                           "'#ptable tbody tr:last-child .nm').value;"
                           " document.querySelector('#ptable tbody tr:last-child "
                           ".x').click(); return t; })()")
            b.wait_for(f"document.querySelectorAll('#ptable tbody tr').length === "
                       f"{before - 1}", timeout=5, label="peserta terhapus")

            b.js("(() => { const i = document.getElementById('pick_player');"
                 " i.focus(); i.value = " + json.dumps(removed[:3]) + ";"
                 " i.dispatchEvent(new Event('input', {bubbles:true})); })(); true")
            b.wait_for(pick_combo + ".querySelector('.combo-row')", timeout=5,
                       label="saran peserta")
            suggestions = b.js(pick_combo + ".querySelectorAll('.combo-row').length")
            b.js(pick_combo + ".querySelector('.combo-row').dispatchEvent("
                 "new MouseEvent('mousedown', {bubbles:true})); true")
            b.wait_for(f"document.querySelectorAll('#ptable tbody tr').length === "
                       f"{before}", timeout=5, label="peserta kembali")
            cleared = b.js("document.getElementById('pick_player').value === ''")
            assert cleared, "kotak tidak dikosongkan setelah memilih"
            return f"{suggestions} saran, '{removed}' dikembalikan"
        check("Autocomplete peserta dari master", picker)

        def no_duplicate():
            before = b.js("document.querySelectorAll('#ptable tbody tr').length")
            name = b.js("document.querySelector('#ptable tbody tr .nm').value")
            # Nama yang sudah ada tidak boleh muncul sebagai saran.
            b.js("(() => { const i = document.getElementById('pick_player');"
                 " i.focus(); i.value = " + json.dumps("") + ";"
                 " i.dispatchEvent(new Event('input', {bubbles:true})); })(); true")
            time.sleep(0.4)
            listed = b.js(pick_combo + ".querySelectorAll('.combo-row')"
                          ".length && [..." + pick_combo + ".querySelectorAll('.combo-row')]"
                          ".map(e => e.textContent)")
            if listed:
                assert not any(name in t for t in listed), (
                    f"'{name}' masih ditawarkan padahal sudah di daftar")
            # Tempel ulang seluruh roster: tidak boleh menggandakan.
            b.js("(() => { document.getElementById('bulk').value = "
                 + json.dumps("\n".join(roster))
                 + "; document.getElementById('parse-bulk').click(); })(); true")
            time.sleep(0.6)
            after = b.js("document.querySelectorAll('#ptable tbody tr').length")
            assert after == before, f"tempel ulang menggandakan: {before} -> {after}"
            return f"tetap {after} peserta"
        check("Duplikat peserta ditolak", no_duplicate)

        # --- 1c. editor babak: preset, duplikat, urutan -------------------
        def preset_no_wipe():
            b.js("(() => { document.getElementById('segments').innerHTML = '';"
                 " document.getElementById('add-seg').click();"
                 " const r = document.querySelector('.seg-editor');"
                 " r.children[1].value = 'Babak Saya';"
                 " r.children[2].value = 5; })(); true")
            b.wait_for("document.querySelectorAll('.seg-editor').length === 1",
                       timeout=5, label="satu babak")

            # Memilih preset TIDAK boleh menghapus apa pun.
            b.js("(() => { const s = document.getElementById('preset');"
                 " s.value = 'gender_3_3_6';"
                 " s.dispatchEvent(new Event('change', {bubbles:true})); })(); true")
            time.sleep(0.3)
            after = b.js("document.querySelectorAll('.seg-editor').length")
            assert after == 1, f"memilih preset menghapus babak: {after}"
            name = b.js("document.querySelector('.seg-editor').children[1].value")
            assert name == 'Babak Saya', f"isian ikut hilang: {name}"

            # Tombol tambah menyambung, bukan mengganti.
            b.js("document.getElementById('preset-append').click(); true")
            b.wait_for("document.querySelectorAll('.seg-editor').length === 4",
                       timeout=5, label="preset ditambahkan")
            first = b.js("document.querySelector('.seg-editor').children[1].value")
            assert first == 'Babak Saya', "babak lama tergeser/hilang"
            return "1 babak dipertahankan, 3 ditambahkan"
        check("Preset tidak menghapus babak yang ada", preset_no_wipe)

        def duplicate_seg():
            before = b.js("document.querySelectorAll('.seg-editor').length")
            b.js("document.querySelector('.seg-editor .seg-dup').click(); true")
            b.wait_for(f"document.querySelectorAll('.seg-editor').length === "
                       f"{before + 1}", timeout=5, label="babak digandakan")
            pair = b.js("[...document.querySelectorAll('.seg-editor')].slice(0,2)"
                        ".map(r => r.children[1].value + '/' + r.children[2].value"
                        " + '/' + r.children[3].value)")
            assert pair[0] == pair[1], f"salinan tidak sama: {pair}"
            return f"'{pair[0]}' digandakan tepat di bawahnya"
        check("Gandakan babak", duplicate_seg)

        def reorder_seg():
            # Nama dibuat berbeda dulu. Tanpa ini semua baris bernama sama dan
            # assertion-nya benar secara otomatis - tes yang tidak bisa gagal.
            b.js("(() => { [...document.querySelectorAll('.seg-editor')]"
                 ".forEach((r, i) => { r.children[1].value = 'Babak ' + i; });"
                 " })(); true")
            before = b.js("[...document.querySelectorAll('.seg-editor')]"
                          ".map(r => r.children[1].value)")
            assert len(set(before)) == len(before), f"nama masih sama: {before}"

            # Jalur keyboard: panah bawah pada gagang baris pertama.
            b.js("(() => { const g = document.querySelector('.seg-grip');"
                 " g.focus();"
                 " g.dispatchEvent(new KeyboardEvent('keydown',"
                 "   {key: 'ArrowDown', bubbles: true})); })(); true")
            time.sleep(0.3)
            after = b.js("[...document.querySelectorAll('.seg-editor')]"
                         ".map(r => r.children[1].value)")
            assert after[0] == before[1] and after[1] == before[0], (
                f"panah bawah tidak menukar: {before} -> {after}")

            # Jalur seret: baris terakhir dijatuhkan ke posisi pertama.
            b.js("""(() => {
              const host = document.getElementById('segments');
              const rows = [...host.querySelectorAll('.seg-editor')];
              const moving = rows[rows.length - 1];
              const target = rows[0].getBoundingClientRect();
              moving.draggable = true;
              moving.dispatchEvent(new DragEvent('dragstart', {bubbles: true}));
              host.dispatchEvent(new DragEvent('dragover',
                {bubbles: true, clientY: target.top + 2}));
              moving.dispatchEvent(new DragEvent('dragend', {bubbles: true}));
            })(); true""")
            time.sleep(0.3)
            dragged = b.js("[...document.querySelectorAll('.seg-editor')]"
                           ".map(r => r.children[1].value)")
            assert dragged[0] == after[-1], (
                f"seret tidak memindahkan ke atas: {after} -> {dragged}")
            assert sorted(dragged) == sorted(before), (
                f"ada babak hilang saat diseret: {dragged}")
            return f"panah: {before[0]}<->{before[1]}, seret: {after[-1]} ke atas"
        check("Urutkan babak (panah & seret)", reorder_seg)

        def seg_total():
            txt = b.js("document.getElementById('seg-total').textContent")
            assert 'babak' in txt and 'ronde' in txt, f"ringkasan kosong: {txt}"
            # Bersihkan supaya skenario berikutnya kembali ke satu babak biasa.
            b.js("(() => { document.getElementById('segments').innerHTML = '';"
                 " document.getElementById('duration').dispatchEvent("
                 "   new Event('input', {bubbles:true})); })(); true")
            time.sleep(0.3)
            return txt[:52]
        check("Ringkasan total ronde", seg_total)

        # --- 2. analisa kelayakan otomatis --------------------------------
        def analyze():
            b.js("document.getElementById('courts').value = 4;"
                 "document.getElementById('courts').dispatchEvent("
                 "new Event('change')); true")
            b.wait_for("document.querySelectorAll('#analysis .stat').length >= 5",
                       label="kartu analisa")
            return b.js("document.querySelectorAll('#analysis .issue').length"
                        " + ' peringatan'")
        check("Analisa kelayakan terisi otomatis", analyze)

        # --- 3. generate jadwal -------------------------------------------
        def generate():
            b.js("document.getElementById('referees').value='1';"
                 "document.getElementById('ballboys').value='1';"
                 "document.getElementById('generate').click(); true")
            b.wait_for("document.querySelectorAll('#rounds .round').length > 0",
                       timeout=40, label="jadwal ter-render")
            return b.js("document.querySelectorAll('#rounds .round').length"
                        " + ' ronde'")
        check("Generate jadwal", generate)

        # --- 3b. log kemajuan terisi dari server --------------------------
        def proglog():
            lines = b.js("document.querySelectorAll('#prog-log div').length")
            assert lines >= 5, f"log terlalu sedikit: {lines} baris"
            pct = b.js("document.getElementById('prog-pct').textContent")
            assert pct == "100%", f"progres tidak selesai: {pct}"
            stage = b.js("document.getElementById('prog-stage').textContent")
            first = b.js("document.querySelector('#prog-log div').textContent")
            assert "Memeriksa setup" in first, f"baris pertama tak terduga: {first}"
            return f"{lines} baris, akhir '{stage}'"
        check("Log kemajuan terisi dari server", proglog)

        # --- 4. grafik keterlibatan ---------------------------------------
        def engagement():
            b.wait_for("document.querySelector('#engagement svg')",
                       label="grafik keterlibatan")
            bars = b.js("document.querySelectorAll('#engagement rect:not(.viz-hit)')"
                        ".length")
            legend = b.js("[...document.querySelectorAll('#engagement "
                          ".viz-legend-item')].map(e=>e.textContent).join(', ')")
            return f"{bars} segmen, legend: {legend}"
        check("Grafik keterlibatan ter-render", engagement)

        # --- 5. tombol Tabel benar-benar menukar tampilan ------------------
        def toggle():
            before = b.js("getComputedStyle(document.querySelector"
                          "('#engagement .viz-plot')).display")
            b.js("document.querySelector('#engagement .viz-toggle').click(); true")
            after = b.js("getComputedStyle(document.querySelector"
                         "('#engagement .viz-plot')).display")
            table = b.js("getComputedStyle(document.querySelector"
                         "('#engagement .viz-table')).display")
            label = b.js("document.querySelector('#engagement .viz-toggle')"
                         ".textContent")
            rows = b.js("document.querySelectorAll('#engagement .viz-table tbody tr')"
                        ".length")
            assert before == "block" and after == "none", \
                f"plot tidak disembunyikan ({before} -> {after})"
            assert table != "none", "tabel tidak muncul"
            assert label == "Grafik", f"label tombol tidak berubah: {label}"
            b.js("document.querySelector('#engagement .viz-toggle').click(); true")
            return f"tabel {rows} baris, tombol kembali ke Grafik"
        check("Tombol Tabel menukar grafik <-> tabel", toggle)

        # --- 6. tooltip muncul saat hover ---------------------------------
        def tooltip():
            b.js("""(() => {
              const hit = document.querySelector('#engagement .viz-hit');
              const r = hit.getBoundingClientRect();
              hit.dispatchEvent(new PointerEvent('pointermove', {
                bubbles: true, clientX: r.left + r.width/2, clientY: r.top + 2 }));
            })(); true""")
            b.wait_for("document.querySelector('.viz-tip') && "
                       "getComputedStyle(document.querySelector('.viz-tip'))"
                       ".display === 'block'", timeout=5, label="tooltip tampil")
            rows = b.js("document.querySelectorAll('.viz-tip .viz-tip-row').length")
            title = b.js("document.querySelector('.viz-tip .viz-tip-title')"
                         ".textContent")
            assert rows >= 2, f"isi tooltip terlalu sedikit: {rows}"
            return f"'{title}', {rows} baris"
        check("Tooltip muncul saat hover", tooltip)

        nonlocal_created = []

        # Halaman punya DUA combobox (klub & venue). querySelector polos
        # mengambil yang klub, jadi setiap selektor dilingkupi ke wadah venue.
        venue_combo = "document.getElementById('venue_name').closest('.combo')"

        # --- 7. combobox: autocomplete ------------------------------------
        def combo_filter():
            b.js("""(() => {
              const i = document.getElementById('venue_name');
              i.focus(); i.value = 'Arena';
              i.dispatchEvent(new Event('input', {bubbles:true}));
            })(); true""")
            b.wait_for(venue_combo + ".querySelector('.combo-list').style.display === 'block'",
                       timeout=5, label="daftar saran venue")
            return b.js(venue_combo + ".querySelectorAll('.combo-row').length + ' saran'")
        check("Combobox menampilkan saran", combo_filter)

        # --- 8. combobox: quick-add untuk nama yang belum ada -------------
        def combo_add():
            b.js("(() => { const i = document.getElementById('venue_name');"
                 " i.focus(); i.value = " + json.dumps(new_venue) + ";"
                 " i.dispatchEvent(new Event('input', {bubbles:true})); })(); true")
            b.wait_for(venue_combo + ".querySelector('.combo-add')", timeout=5,
                       label="baris quick-add")
            text = b.js(venue_combo + ".querySelector('.combo-add').textContent")
            b.js(venue_combo + ".querySelector('.combo-add').dispatchEvent("
                 "new MouseEvent('mousedown', {bubbles:true})); true")
            b.wait_for(venue_combo + ".querySelector('.combo-form')", timeout=5,
                       label="formulir quick-add")
            fields = b.js(venue_combo + ".querySelectorAll('.combo-form input').length")
            assert fields == 2, f"jumlah field tak terduga: {fields}"
            return f"{text.strip()}, {fields} field"
        check("Quick-add muncul untuk nama baru", combo_add)

        # --- 9. validasi quick-add menolak masukan tak sah ----------------
        def combo_validate():
            b.js("(() => { const c = " + venue_combo + ";"
                 " const ins = c.querySelectorAll('.combo-form input');"
                 " ins[0].value = '0';"
                 " c.querySelector('.combo-form .btn').dispatchEvent("
                 "   new MouseEvent('mousedown', {bubbles:true})); })(); true")
            b.wait_for(venue_combo + ".querySelector('.combo-err').textContent.length > 0",
                       timeout=5, label="pesan validasi")
            msg = b.js(venue_combo + ".querySelector('.combo-err').textContent")
            assert "court" in msg.lower(), f"pesan tak terduga: {msg}"
            return msg
        check("Validasi quick-add menolak court = 0", combo_validate)

        # --- 10. quick-add benar-benar menyimpan ke master -----------------
        def combo_save():
            b.js("(() => { const c = " + venue_combo + ";"
                 " const ins = c.querySelectorAll('.combo-form input');"
                 " ins[0].value = '3'; ins[1].value = '175000';"
                 " c.querySelector('.combo-form .btn').dispatchEvent("
                 "   new MouseEvent('mousedown', {bubbles:true})); })(); true")
            b.wait_for("document.getElementById('venue_id').value !== ''",
                       timeout=10, label="venue tersimpan & terpilih")
            vid = b.js("document.getElementById('venue_id').value")
            nonlocal_created.append(vid)
            courts = b.js("document.getElementById('courts').value")
            price = b.js("document.getElementById('court_price').value")
            assert courts == "3", f"jumlah court tidak ikut terisi: {courts}"
            assert price == "175000", f"harga tidak ikut terisi: {price}"
            return f"id={vid}, court->{courts}, harga->{price}"
        check("Quick-add menyimpan & mengisi setup", combo_save)

        # --- 11. panel biaya + grafik trade-off ---------------------------
        def econ():
            b.js("document.querySelector('.tabs button[data-view=\"biaya\"]')"
                 ".click(); document.getElementById('calc-econ').click(); true")
            b.wait_for("document.querySelector('#econ-chart svg')", timeout=30,
                       label="grafik trade-off")
            pts = b.js("document.querySelectorAll('#econ-chart .viz-pt').length")
            cur = b.js("[...document.querySelectorAll('#econ-chart text')]"
                       ".some(t => t.textContent.includes('court'))")
            assert cur, "label pilihan sekarang tidak ada di grafik"
            return f"{pts} skenario, titik aktif berlabel"
        check("Grafik trade-off memuat pilihan aktif", econ)

        # --- 12. paging master data ---------------------------------------
        def paging():
            b.js("document.querySelector('.tabs button[data-view=\"master\"]')"
                 ".click(); true")
            b.wait_for("document.querySelector('#players-table table') || "
                       "document.querySelector('#players-table .empty')",
                       timeout=15, label="tabel pemain")
            has_pager = b.js("!!document.querySelector('#players-pager .pager')")
            info = b.js("(document.querySelector('#players-pager')||{})"
                        ".textContent || ''")
            return f"pager={'ada' if has_pager else 'satu halaman'}, {info.strip()[:60]}"
        check("Master data memuat tabel berhalaman", paging)

        # --- 12b. tombol info debug ---------------------------------------
        def debug_button():
            b.js("document.getElementById('copy-debug').click(); true")
            b.wait_for("document.getElementById('debug-out').value.length > 50",
                       timeout=5, label="teks debug")
            txt = b.js("document.getElementById('debug-out').value")
            for needed in ("INFO DEBUG PADELIN", "court=", "seed=", "peserta="):
                assert needed in txt, f"bagian '{needed}' hilang"
            # Nama asli TIDAK boleh ikut - itu inti penyamarannya.
            #
            # Diperiksa SEMUA nama, bukan cuma yang pertama. Catatan jadwal
            # sekarang memuat nama ("yang dilewati Budi 2x", "tidak kebagian
            # main sama sekali: Sari") dan disisipkan ke teks ini; memeriksa
            # satu nama saja membuat uji ini lolos kebetulan justru saat
            # catatan menyebut orang lain.
            nama = [r.split(",")[0].strip() for r in roster]
            bocor = [n for n in nama if n and n in txt]
            assert not bocor, f"nama asli bocor ke info debug: {bocor[:3]}"
            assert "P1 " in txt, "nama samaran tidak dipakai"
            # Bagian catatan harus benar-benar ada, kalau tidak pemeriksaan di
            # atas cuma menguji teks yang memang tidak pernah memuat nama.
            assert "catatan:" in txt, "info debug tanpa bagian catatan"
            return (f"{len(txt.splitlines())} baris, {len(nama)} nama "
                    f"disamarkan")
        check("Tombol info debug", debug_button)

        # --- 12b. selector kualitas mengirim DUA angka --------------------
        # Pilihannya membawa "effort:percobaan", bukan effort saja. Kalau
        # pemisahannya rusak, <select> yang disetel ke nilai asing jadi kosong
        # dan buildPayload mengirim effort 0 - jadwal tetap keluar, cuma
        # optimasinya nyaris tidak jalan, dan tidak ada yang mengeluh. Persis
        # kelas kegagalan yang tidak terlihat dari pemeriksaan statis.
        # Dibaca lewat tombol info debug, bukan dengan memanggil buildPayload:
        # app.js dimuat sebagai module jadi fungsinya tidak global, dan itu
        # justru benar - yang perlu diuji apa yang benar-benar dikirim, bukan
        # apa yang dihitung fungsi internal.
        def effort_pair():
            harap = {"Cepat": (10000, 3), "Normal": (30000, 3),
                     "Teliti": (80000, 3), "Sangat teliti": (80000, 6)}
            dapat = {}
            labels = b.js("""JSON.stringify(
              [...document.getElementById('effort').options]
                .map((o) => [o.value, o.textContent.trim()]))""")
            for nilai, label in json.loads(labels):
                b.js(f"""(() => {{
                  const sel = document.getElementById('effort');
                  sel.value = {json.dumps(nilai)};
                  sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                  document.getElementById('debug-out').value = '';
                  document.getElementById('copy-debug').click();
                }})(); true""")
                b.wait_for("document.getElementById('debug-out').value"
                           ".includes('percobaan=')",
                           timeout=5, label=f"teks debug {label}")
                txt = b.js("document.getElementById('debug-out').value")
                eff = int(txt.split("effort=")[1].split()[0])
                att = int(txt.split("percobaan=")[1].split()[0])
                dapat[label] = (eff, att)
            assert dapat == harap, f"yang dikirim {dapat} != {harap}"
            return "4 pilihan -> " + ", ".join(
                f"{k} {v[0] // 1000}k/att{v[1]}" for k, v in dapat.items())
        check("Kualitas optimasi mengirim effort + percobaan", effort_pair)

        # Dua pilihan berbagi effort 80.000 dan hanya percobaannya yang beda,
        # jadi memulihkan jadwal lama harus mencocokkan pasangannya. Jadwal yang
        # tersimpan dengan effort 160.000 - pilihan yang sudah tidak ada - harus
        # jatuh ke cocokan effort saja dan tidak meninggalkan selector kosong.
        def effort_restore():
            # Aturan pencocokan yang sama seperti di restore() app.js. Ditulis
            # ulang di sini dengan sengaja: yang diuji apakah ATURANNYA menutup
            # keempat bentuk permintaan yang mungkin, termasuk jadwal lama yang
            # tidak menyimpan percobaan sama sekali.
            hasil = b.js("""(() => {
              const sel = document.getElementById('effort');
              const semula = sel.value;
              const coba = (req) => {
                sel.value = '10000:3';
                const opts = [...sel.options].map((o) => {
                  const [eff, att] = o.value.split(':').map(Number);
                  return {value: o.value, eff, att: att || 3};
                });
                const pas = opts.find((o) => o.eff === +req.effort
                    && o.att === +(req.attempts ?? 3))
                  || opts.slice().sort((a, b) =>
                    Math.abs(a.eff - req.effort) - Math.abs(b.eff - req.effort)
                    || b.att - a.att)[0];
                if (pas) sel.value = pas.value;
                return sel.value;
              };
              const out = {
                teliti: coba({effort: 80000, attempts: 3}),
                sangat: coba({effort: 80000, attempts: 6}),
                lama160: coba({effort: 160000}),
                tanpa_att: coba({effort: 30000}),
              };
              sel.value = semula;
              return JSON.stringify(out);
            })()""")
            d = json.loads(hasil)
            assert d["teliti"] == "80000:3", f"Teliti salah pulih: {d}"
            assert d["sangat"] == "80000:6", f"Sangat teliti salah pulih: {d}"
            assert d["tanpa_att"] == "30000:3", f"tanpa percobaan: {d}"
            # effort 160.000 sudah tidak ada pilihannya. Ia harus mendarat di
            # setelan paling teliti yang tersisa - bukan di "Cepat", yang
            # membalik maksud host sepenuhnya.
            assert d["lama160"] == "80000:6", \
                f"jadwal effort 160k tidak pulih ke setelan paling teliti: {d}"
            return "pasangan cocok, jadwal effort 160k -> Sangat teliti"
        check("Pulihkan pilihan kualitas dari jadwal tersimpan", effort_restore)

        # --- 13. tidak ada error JS sepanjang sesi ------------------------
        def no_errors():
            errs = b.js("window.__errs")
            assert not errs, f"{len(errs)} error: {errs[:3]}"
            return "bersih"
        check("Tidak ada error JS selama sesi", no_errors)

        # --- 13b. kartu "Main / orang" tidak boleh berbohong --------------
        # Sengaja dijalankan PALING AKHIR dan di atas halaman yang dimuat
        # ulang. Dua belas langkah sebelumnya meninggalkan court, durasi, dan
        # babak dalam keadaan yang tidak bisa ditebak, dan ketika langkah ini
        # dicoba di tengah, kegagalannya bukan tentang kartunya sama sekali -
        # angkanya keluar dari setup lain. Memuat ulang lebih murah daripada
        # membereskan keadaan satu per satu, dan aman di sini karena uji error
        # JS sudah lewat (window.__errs ikut hilang saat reload).
        # Rata-rata seluruh peserta menggambarkan nol orang begitu ada babak
        # putra/putri. Diuji lewat kartu yang benar-benar ter-render, bukan
        # lewat respons API: yang salah selama ini bukan angkanya di server,
        # melainkan angka mana yang sampai ke mata host.
        def kartu_main_per_orang():
            b.js("location.reload(); true")
            b.wait_for("document.querySelector('#preset option')",
                       label="halaman dimuat ulang")
            # Roster sengaja dibuat TIMPANG, 20 putra + 4 putri, dan bukan
            # memakai roster uji standar yang seimbang. Di roster seimbang
            # kedua kelompok memang dapat jatah yang sama dan jumlah duduknya
            # tidak berayun - server benar kalau menyajikannya sebagai satu
            # angka, jadi tidak ada yang bisa diuji. Menempelnya aman di sini
            # karena halaman baru saja dimuat ulang dan daftarnya kosong.
            timpang = [f"Uji {i + 1}, 3, {'L' if i < 20 else 'P'}"
                       for i in range(24)]
            b.js("(() => { document.getElementById('bulk').value = "
                 + json.dumps("\n".join(timpang))
                 + "; document.getElementById('parse-bulk').click(); })(); true")
            b.wait_for("document.querySelectorAll('#ptable tbody tr').length === 24",
                       label="roster timpang 20L+4P")

            #
            # JANGAN pakai tombol "Kosongkan semua": ia memanggil confirm(),
            # dan dialog native memblokir renderer sehingga Runtime.evaluate
            # tidak pernah kembali - seluruh sesi CDP ikut mati, bukan cuma
            # langkah ini. Tombol hapus per-baris tidak berdialog.
            b.js("""(() => {
              const set = (id, v) => {
                const e = document.getElementById(id);
                e.value = v;
                e.dispatchEvent(new Event('change', {bubbles: true}));
              };
              set('courts', 2); set('duration', 120);
              document.querySelectorAll('#segments .seg-editor .x')
                .forEach((x) => x.click());
              for (const [lab, rn, rule] of [['Putra', 5, 'men'],
                                             ['Putri', 5, 'women'],
                                             ['Mixed', 5, 'mixed']]) {
                document.getElementById('add-seg').click();
                const baris = document.querySelectorAll('#segments .seg-editor');
                const s = baris[baris.length - 1];
                // Urutan anak baris babak: gagang, nama, ronde, aturan, ...
                s.children[1].value = lab;
                s.children[2].value = rn;
                s.children[3].value = rule;
                s.children[3].dispatchEvent(
                    new Event('change', {bubbles: true}));
              }
              // Panel dikosongkan supaya penantian di bawah menunggu analisa
              // BARU, bukan yang sudah terpampang dari langkah sebelumnya.
              // Tanpa ini penantiannya lolos seketika dan yang terbaca panel
              // basi - analisa ulangnya ter-debounce 250ms.
              document.getElementById('analysis').innerHTML = '';
            })(); true""")
            b.wait_for("document.querySelectorAll('#analysis .stat').length >= 5",
                       timeout=8, label="panel analisa terisi ulang")
            kartu = b.js("""(() => {
              const out = {};
              for (const s of document.querySelectorAll('#analysis .stat')) {
                out[s.querySelector('.k').textContent.trim()] =
                  [s.querySelector('.v').textContent.trim(),
                   s.querySelector('.s').textContent.trim()];
              }
              return JSON.stringify(out);
            })()""")
            semua_kartu = json.loads(kartu)
            nilai, satuan = semua_kartu.get("Main / orang",
                                            ["(tidak ketemu)", ""])
            duduk = semua_kartu.get("Duduk / ronde", ["(tidak ketemu)", ""])
            # Bersihkan babak sebelum menilai, supaya kegagalan assert di bawah
            # tidak meninggalkan halaman dalam keadaan lain.
            b.js("""document.querySelectorAll('#segments .seg-editor .x')
                 .forEach((x) => x.click()); true""")

            # 20 putra + 4 putri: babak putri cuma bisa mengisi satu court, dan
            # babak mixed butuh satu putri per tim, jadi para putri main jauh
            # lebih banyak. Menyajikannya sebagai satu rata-rata tunggal memberi
            # angka yang tidak berlaku bagi siapa pun.
            assert satuan, f"kartu tanpa satuan/konteks: {nilai!r}"
            assert "putra" in satuan and "putri" in satuan, \
                f"kelompoknya tidak disebut: {nilai!r} / {satuan!r}"
            assert "-" in nilai, f"masih satu angka tunggal: {nilai!r}"
            # Babak putra dan babak putri mengisi court sebanyak orangnya
            # masing-masing, jadi jumlah yang duduk ikut berayun. Satu angka di
            # situ selalu ujung yang paling ramai.
            assert "-" in duduk[0], \
                f"Duduk / ronde masih satu angka tunggal: {duduk!r}"
            assert "berayun" in duduk[1], f"rentangnya tidak dijelaskan: {duduk!r}"
            return f"main '{nilai}' ({satuan}) · duduk '{duduk[0]}'"
        check("Kartu Main / orang memisah per kelompok babak",
              kartu_main_per_orang)

        # --- 14. bersih-bersih -------------------------------------------
        def cleanup():
            if not nonlocal_created:
                return "tidak ada yang perlu dihapus"
            vid = nonlocal_created[0]
            ok = b.js(f"""fetch('/api/venues/delete', {{
              method: 'POST', headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{id: {vid}}}) }}).then(r => r.ok)""")
            assert ok, "gagal menghapus venue uji"
            return f"venue uji #{vid} dihapus"
        check("Bersih-bersih data uji", cleanup)

    finally:
        b.close()

    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
