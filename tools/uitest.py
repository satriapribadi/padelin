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

        # --- 2b. court berkurang di tengah acara --------------------------
        # Kotak centang ini mengubah jumlah match seluruh acara, jadi yang
        # diperiksa bukan cuma "kotaknya muncul": kalimat bantuannya harus
        # menyebut blok ronde dan court-jam yang benar, kartu biaya harus ikut
        # turun, dan angkanya harus benar-benar terkirim. Yang terakhir dibaca
        # lewat info debug - app.js module, jadi buildPayload tidak global, dan
        # yang perlu diuji memang apa yang dikirim.
        def court_drop():
            semula = b.js(
                "JSON.stringify(['courts','duration','round_min','warmup',"
                "'court_price'].map(i => document.getElementById(i).value))")
            b.js("""(() => {
              const set = (id, v) => { const e = document.getElementById(id);
                e.value = v; e.dispatchEvent(new Event('input', {bubbles:true})); };
              set('courts', 2); set('duration', 180); set('warmup', 0);
              set('round_min', 12); set('court_price', 200000);
              return true; })()""")
            tersembunyi = b.js("getComputedStyle(document.getElementById("
                               "'courts-drop-row')).display")
            assert tersembunyi == "none", \
                f"baris court berkurang tampil padahal belum dicentang: {tersembunyi}"

            b.js("""(() => {
              const c = document.getElementById('courts_drop');
              c.checked = true;
              c.dispatchEvent(new Event('input', {bubbles:true}));
              const set = (id, v) => { const e = document.getElementById(id);
                e.value = v; e.dispatchEvent(new Event('input', {bubbles:true})); };
              set('courts_after', 1); set('courts_from_round', 11);
              return true; })()""")
            assert b.js("getComputedStyle(document.getElementById("
                        "'courts-drop-row')).display") != "none", \
                "baris court berkurang tetap tersembunyi setelah dicentang"

            b.wait_for("document.getElementById('courts-drop-hint')"
                       ".textContent.includes('court-jam')",
                       timeout=5, label="kalimat court berkurang")
            hint = b.js("document.getElementById('courts-drop-hint').textContent")
            # 180 menit / 12 = 15 ronde; turun di ronde 11 berarti 5 ronde
            # terakhir 1 court, dan sewanya 2x120m + 1x60m = 5 court-jam.
            for perlu in ("Ronde 1-10 pakai 2 court",
                          "ronde 11-15 pakai 1 court", "5 ronde",
                          "5 court-jam", "hemat"):
                assert perlu in hint, f"kalimat tanpa '{perlu}': {hint!r}"

            # Kartu biaya memakai court-jam nyata, bukan court x jam. 6 court-jam
            # akan memberi 1.200.000; yang benar 5 court-jam = 1.000.000.
            b.wait_for("document.getElementById('setup-econ')"
                       ".textContent.includes('court berkurang')",
                       timeout=8, label="kartu biaya court berkurang")
            econ = b.js("document.getElementById('setup-econ').textContent")
            assert "1.000.000" in econ, \
                f"biaya tidak memakai court-jam nyata: {econ[:160]!r}"
            assert "1.200.000" not in econ, \
                f"biaya masih menagih court penuh: {econ[:160]!r}"

            b.js("document.getElementById('copy-debug').click(); true")
            b.wait_for("document.getElementById('debug-out').value.length > 50",
                       timeout=5, label="teks debug")
            dbg = b.js("document.getElementById('debug-out').value")
            assert "(jadi 1 dari ronde 11)" in dbg, \
                f"info debug tidak menyebut court berkurang: {dbg[:120]!r}"

            # Dimatikan lagi + nilai semula dipulihkan, supaya skenario
            # berikutnya tidak mewarisi acara 3 jam yang court-nya berkurang.
            b.js("""(() => {
              const c = document.getElementById('courts_drop');
              c.checked = false;
              c.dispatchEvent(new Event('input', {bubbles:true}));
              document.getElementById('debug-out').style.display = 'none';
              return true; })()""")
            b.js("(() => { const v = " + semula + ";"
                 "['courts','duration','round_min','warmup','court_price']"
                 ".forEach((id, i) => { const e = document.getElementById(id);"
                 " e.value = v[i];"
                 " e.dispatchEvent(new Event('input', {bubbles:true})); });"
                 "return true; })()")
            assert b.js("document.getElementById('courts-drop-hint')"
                        ".textContent") == "", \
                "kalimat court berkurang tertinggal setelah centang dilepas"
            return "15 ronde -> 5 court-jam, biaya Rp 1.000.000, terkirim"
        check("Court berkurang di tengah acara", court_drop)

        # --- 2b. court BERTAMBAH di tengah acara --------------------------
        # Arah yang lain dari fitur yang sama, dan yang paling mudah rusak:
        # kotak "jadi berapa court" dulu dijepit ke jumlah court awal, jadi
        # mengetik 3 pada setup 1 court diam-diam terkirim sebagai 1 - host
        # menekan Generate lalu tidak terjadi apa-apa.
        def court_naik():
            semula = b.js(
                "JSON.stringify(['courts','duration','round_min','warmup',"
                "'court_price'].map(i => document.getElementById(i).value))")
            b.js("""(() => {
              const set = (id, v) => { const e = document.getElementById(id);
                e.value = v; e.dispatchEvent(new Event('input', {bubbles:true})); };
              set('courts', 1); set('duration', 180); set('warmup', 0);
              set('round_min', 12); set('court_price', 200000);
              const c = document.getElementById('courts_drop');
              c.checked = true;
              c.dispatchEvent(new Event('input', {bubbles:true}));
              set('courts_after', 3); set('courts_from_round', 5);
              return true; })()""")
            b.wait_for("document.getElementById('courts-drop-hint')"
                       ".textContent.includes('court-jam')",
                       timeout=5, label="kalimat court bertambah")
            hint = b.js("document.getElementById('courts-drop-hint').textContent")
            # 180 menit / 12 = 15 ronde; naik di ronde 5 berarti 11 ronde
            # terakhir 3 court, dan sewanya 1x48m + 3x132m = 7,4 court-jam.
            for perlu in ("Ronde 1-4 pakai 1 court",
                          "ronde 5-15 pakai 3 court", "11 ronde", "7.4 court-jam"):
                assert perlu in hint, f"kalimat tanpa '{perlu}': {hint!r}"

            b.wait_for("document.getElementById('setup-econ')"
                       ".textContent.includes('court bertambah')",
                       timeout=8, label="kartu biaya court bertambah")

            b.js("document.getElementById('copy-debug').click(); true")
            b.wait_for("document.getElementById('debug-out').value.length > 50",
                       timeout=5, label="teks debug")
            dbg = b.js("document.getElementById('debug-out').value")
            assert "(jadi 3 dari ronde 5)" in dbg, \
                f"kotak 'jadi berapa court' masih dijepit ke court awal: {dbg[:160]!r}"

            b.js("""(() => {
              const c = document.getElementById('courts_drop');
              c.checked = false;
              c.dispatchEvent(new Event('input', {bubbles:true}));
              document.getElementById('debug-out').style.display = 'none';
              return true; })()""")
            b.js("(() => { const v = " + semula + ";"
                 "['courts','duration','round_min','warmup','court_price']"
                 ".forEach((id, i) => { const e = document.getElementById(id);"
                 " e.value = v[i];"
                 " e.dispatchEvent(new Event('input', {bubbles:true})); });"
                 "return true; })()")
            return "15 ronde -> 7,4 court-jam, terkirim sebagai 3 dari ronde 5"
        check("Court bertambah di tengah acara", court_naik)

        # --- 2c. rentang ronde per peserta --------------------------------
        # Kolomnya disembunyikan sampai diminta, jadi yang diuji dua hal: ia
        # MUNCUL saat dicentang, dan angkanya benar-benar sampai ke payload.
        # Rentang yang tersimpan tapi tidak terkirim adalah kegagalan yang
        # tidak kelihatan di mana pun sampai jadwalnya salah.
        def rentang_ronde():
            kolom = ("document.querySelectorAll("
                     "'#ptable input.rw').length")
            assert b.js(kolom) == 0, "kolom rentang ronde tampil tanpa dicentang"
            b.js("""(() => {
              const c = document.getElementById('round_windows');
              c.checked = true;
              c.dispatchEvent(new Event('change', {bubbles:true}));
              return true; })()""")
            b.wait_for(kolom + " > 0", timeout=5, label="kolom rentang ronde")

            # Baris pertama: ikut mulai ronde 3. Baris kedua: sampai ronde 5.
            b.js("""(() => {
              const isi = (i, f, v) => {
                const e = document.querySelector(
                  `#ptable input.rw[data-i="${i}"][data-f="${f}"]`);
                e.value = v; e.dispatchEvent(new Event('input', {bubbles:true}));
              };
              isi(0, 'from_round', 3);
              isi(1, 'until_round', 5);
              return true; })()""")
            b.wait_for("document.getElementById('counts')"
                       ".textContent.includes('Ikut sebagian')",
                       timeout=5, label="hitungan ikut sebagian")

            b.js("document.getElementById('copy-debug').click(); true")
            b.wait_for("document.getElementById('debug-out').value.length > 50",
                       timeout=5, label="teks debug")
            dbg = b.js("document.getElementById('debug-out').value")
            for perlu in ("ikut=3-akhir", "ikut=1-5"):
                assert perlu in dbg, \
                    f"info debug tanpa '{perlu}': {dbg[:400]!r}"

            # Dimatikan lagi: angkanya harus IKUT terhapus, bukan cuma
            # tersembunyi - rentang yang tidak terlihat tapi tetap dikirim
            # adalah cara tercepat membuat host mengira penjadwalnya rusak.
            b.js("""(() => {
              const c = document.getElementById('round_windows');
              c.checked = false;
              c.dispatchEvent(new Event('change', {bubbles:true}));
              document.getElementById('debug-out').style.display = 'none';
              return true; })()""")
            assert b.js(kolom) == 0, "kolom rentang ronde tertinggal"
            b.js("document.getElementById('copy-debug').click(); true")
            time.sleep(0.4)
            dbg2 = b.js("document.getElementById('debug-out').value")
            assert "ikut=" not in dbg2, \
                f"rentang masih terkirim setelah dimatikan: {dbg2[:400]!r}"
            b.js("document.getElementById('debug-out').style.display = 'none';"
                 " true")
            return "kolom muncul, 2 rentang terkirim, dibersihkan saat dimatikan"
        check("Rentang ronde per peserta", rentang_ronde)

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

        # --- 3a. ganti nama court -----------------------------------------
        # Nama court muncul di empat tempat: kartu ronde di layar, teks
        # WhatsApp, jadwal per pemain, dan CSV. Tiga yang terakhir dibuat
        # server, jadi yang diuji bukan cuma labelnya berubah di layar - tapi
        # bahwa teks yang disalin host ikut berubah.
        def ganti_nama_court():
            jml_court = b.js("document.querySelectorAll("
                             "'#court-names input[data-court]').length")
            assert jml_court > 0, "kotak isian nama court tidak muncul"

            # Rekam jawaban endpoint penulisan ulang teks.
            b.js("""(() => {
              window.__cn = [];
              window.__cnHook = true;
              const asli = window.fetch;
              window.fetch = async (u, o) => {
                const res = await asli(u, o);
                if (String(u).includes('/api/schedule/text')) {
                  res.clone().json().then(d => window.__cn.push(d))
                     .catch(() => {});
                }
                return res;
              };
            })(); true""")

            b.js("""(() => {
              const i = document.querySelector(
                '#court-names input[data-court="1"]');
              i.value = 'Indoor A';
              i.dispatchEvent(new Event('input', {bubbles:true}));
              i.dispatchEvent(new Event('change', {bubbles:true}));
            })(); true""")

            label = b.js("document.querySelector('#rounds .match .c')"
                         ".textContent")
            assert label == "Indoor A", f"kartu ronde masih menyebut '{label}'"

            # Kolom court melebar mengikuti nama - kalau tidak, nama terpanjang
            # menabrak nama tim di sebelahnya.
            lebar = b.js("getComputedStyle(document.querySelector('#rounds "
                         ".rounds')).getPropertyValue('--courtw').trim()")
            assert lebar and int(lebar.replace("px", "")) > 24, \
                f"kolom court tidak melebar untuk nama panjang: '{lebar}'"

            b.wait_for("window.__cn.length > 0", timeout=15,
                       label="teks ditulis ulang server")
            d = b.js("window.__cn[0]")
            for kunci in ("text", "personal_text", "csv"):
                assert "Indoor A" in (d.get(kunci) or ""), \
                    f"nama court tidak masuk ke {kunci}"
            assert "C2" in d["text"], "court yang tidak diganti kehilangan namanya"

            # Dikosongkan lagi harus kembali ke nama bawaan, bukan jadi kosong.
            b.js("""(() => {
              const i = document.querySelector(
                '#court-names input[data-court="1"]');
              i.value = '';
              i.dispatchEvent(new Event('input', {bubbles:true}));
            })(); true""")
            balik = b.js("document.querySelector('#rounds .match .c')"
                         ".textContent")
            assert balik == "C1", f"tidak kembali ke nama bawaan: '{balik}'"
            return f"{jml_court} court bisa dinamai, teks server ikut berubah"
        check("Ganti nama court", ganti_nama_court)

        # --- 3a2. ganti nama peserta --------------------------------------
        # Nama peserta muncul di jauh lebih banyak tempat daripada nama court:
        # kartu ronde, rekap, dua matriks pertemuan (yang BARISNYA diurutkan
        # menurut nama, jadi urutannya ikut bergeser), daftar peserta di tab
        # Setup, dan tiga teks yang dibuat server. Yang diuji bukan cuma nama
        # barunya muncul - tapi nama lamanya tidak tertinggal di satu pun.
        def ganti_nama_pemain():
            b.js("""(() => {
              window.__cn = [];
              if (window.__cnHook) return;
              window.__cnHook = true;
              const asli = window.fetch;
              window.fetch = async (u, o) => {
                const res = await asli(u, o);
                if (String(u).includes('/api/schedule/text')) {
                  res.clone().json().then(d => window.__cn.push(d))
                     .catch(() => {});
                }
                return res;
              };
            })(); true""")

            # Yang diganti adalah nama yang BUKAN bagian dari nama lain, supaya
            # "nama lama sudah hilang" tidak gagal palsu gara-gara "Pemain 1"
            # yang memang hidup di dalam "Pemain 12".
            target = b.js("""(() => {
              const kotak = [...document.querySelectorAll(
                '#recap input[data-rename]')];
              const nama = kotak.map((i) => i.value);
              const pilih = kotak.find(
                (i) => !nama.some((n) => n !== i.value && n.includes(i.value)));
              return pilih ? {id: pilih.dataset.rename, nama: pilih.value} : null;
            })()""")
            assert target, "kotak nama di rekap tidak muncul"
            lama, pid = target["nama"], target["id"]
            baru = "Zulaikha Uji"
            sel = json.dumps(f'#recap input[data-rename="{pid}"]')

            def isi(nilai):
                b.js("(() => { const i = document.querySelector(" + sel + ");"
                     " i.value = " + json.dumps(nilai) + ";"
                     " i.dispatchEvent(new Event('change', {bubbles:true}));"
                     " })(); true")

            def nama_rekap():
                return b.js("[...document.querySelectorAll("
                            "'#recap input[data-rename]')].map((i) => i.value)")

            isi(baru)
            rekap = nama_rekap()
            assert baru in rekap, f"rekap belum memakai nama baru: {rekap[:3]}"
            assert lama not in rekap, "nama lama masih ada di rekap"

            # Matriks: isinya harus menyebut nama baru (termasuk di title tiap
            # sel), dan urutan barisnya harus tetap sama dengan urutan rekap -
            # keduanya diurutkan menurut nama, jadi kalau matriksnya tidak ikut
            # digambar ulang, urutannya langsung berbeda.
            for panel in ("rounds", "matrix"):
                html = b.js(f"document.getElementById('{panel}').innerHTML")
                assert baru in html, f"nama baru tidak muncul di #{panel}"
                assert lama not in html, f"nama lama tertinggal di #{panel}"
            baris_mx = b.js("""(() => {
              return [...document.querySelectorAll('#mx-partner .mx-row')]
                .map((e) => {
                  const no = e.querySelector('.mx-no');
                  return e.textContent.slice(no ? no.textContent.length : 0);
                });
            })()""")
            assert baris_mx == rekap, (
                f"urutan baris matriks tidak ikut nama baru: {baris_mx[:3]} "
                f"vs {rekap[:3]}")

            # Daftar peserta di tab Setup ikut, kalau tidak Generate berikutnya
            # menghidupkan lagi nama yang barusan dibuang.
            setup = b.js("[...document.querySelectorAll('#ptable .nm')]"
                         ".map((i) => i.value)")
            assert baru in setup, "tab Setup masih memakai nama lama"
            assert lama not in setup, "nama lama masih ada di tab Setup"

            b.wait_for("window.__cn.length > 0", timeout=15,
                       label="teks ditulis ulang server")
            d = b.js("window.__cn[window.__cn.length - 1]")
            for kunci in ("text", "personal_text", "csv"):
                teks = d.get(kunci) or ""
                assert baru in teks, f"nama baru tidak masuk ke {kunci}"
                assert lama not in teks, f"nama lama masih ada di {kunci}"

            # Nama kembar ditolak, tanpa membedakan huruf besar-kecil: dua
            # peserta bernama sama akan lebur jadi satu baris di database.
            lain = [n for n in nama_rekap() if n != baru][0]
            isi(lain.lower())
            sesudah = nama_rekap()
            assert sesudah.count(lain) == 1, f"nama kembar lolos: {lain}"
            assert baru in sesudah, "penolakan ikut menghapus nama yang sah"

            # Nama kosong juga ditolak, dan isiannya kembali ke nama semula.
            isi("")
            assert b.js("document.querySelector(" + sel + ").value") == baru, \
                "nama kosong tidak dikembalikan"

            isi(lama)
            assert lama in nama_rekap(), "tidak bisa dikembalikan ke nama semula"
            return (f"'{lama}' -> '{baru}' menyeret rekap, matriks, kartu ronde, "
                    f"tab Setup, dan tiga teks server")
        check("Ganti nama peserta", ganti_nama_pemain)

        # --- 3a4. ganti L/P di rekap --------------------------------------
        # Ganti nama di rekap sebenarnya dipakai untuk SUBSTITUSI peserta, dan
        # pengganti yang beda gender meninggalkan jadwal yang diam-diam
        # memperlakukan dia sebagai gender orang yang digantikan. Yang diuji:
        # L/P bisa diubah di baris yang sama, dan perubahannya menyeret warna
        # nama di kartu ronde, tab Setup, serta penilaian aturan di server.
        def ganti_gender():
            target = b.js("""(() => {
              const sel = [...document.querySelectorAll('#recap select[data-gender]')]
                .find((s) => s.value === 'M' || s.value === 'F');
              if (!sel) return null;
              const id = +sel.dataset.gender;
              const nama = sel.closest('tr').querySelector('input').value;
              return {id, nama, semula: sel.value,
                      tujuan: sel.value === 'M' ? 'F' : 'M'};
            })()""")
            assert target, "tidak ada peserta bergender di rekap"
            pid, semula, tujuan = target["id"], target["semula"], target["tujuan"]
            kelas_awal = "g-m" if semula == "M" else "g-f"
            kelas_baru = "g-m" if tujuan == "M" else "g-f"

            warna = (f"[...document.querySelectorAll("
                     f"'#rounds .nm-swap[data-tukar$=\\\":{pid}\\\"] span')]"
                     f".every((s) => s.classList.contains('{kelas_baru}'))")
            jumlah = b.js(f"document.querySelectorAll("
                          f"'#rounds .nm-swap[data-tukar$=\":{pid}\"]').length")
            assert jumlah > 0, "nama peserta ini tidak muncul di kartu ronde"

            b.js(f"""(() => {{
              const s = document.querySelector(
                '#recap select[data-gender="{pid}"]');
              s.value = '{tujuan}';
              s.dispatchEvent(new Event('change', {{bubbles:true}}));
            }})(); true""")
            b.wait_for(warna, timeout=25,
                       label="warna nama di kartu ronde ikut berubah")

            pil = b.js(f"""document.querySelector(
              '#recap select[data-gender="{pid}"]').className""")
            assert pil.strip().endswith("f" if tujuan == "F" else "m"), \
                f"pil L/P di rekap tidak ikut berubah: '{pil}'"
            di_setup = b.js(f"""(() => {{
              const baris = [...document.querySelectorAll('#ptable tbody tr')]
                .find((r) => r.querySelector('.nm').value === """
                            + json.dumps(target["nama"]) + """);
              return baris ? baris.querySelector('[data-f="gender"]').value : null;
            })()""")
            assert di_setup == tujuan, \
                f"tab Setup masih menyebut L/P lama: {di_setup}"

            # Dikembalikan: angkanya harus pulih, termasuk catatan pelanggaran
            # yang mungkin lahir dari perubahan tadi.
            b.js(f"""(() => {{
              const s = document.querySelector(
                '#recap select[data-gender="{pid}"]');
              s.value = '{semula}';
              s.dispatchEvent(new Event('change', {{bubbles:true}}));
            }})(); true""")
            b.wait_for(
                f"[...document.querySelectorAll("
                f"'#rounds .nm-swap[data-tukar$=\":{pid}\"] span')]"
                f".every((s) => s.classList.contains('{kelas_awal}'))",
                timeout=25, label="warna nama kembali")
            return (f"{target['nama']}: {semula} -> {tujuan} menyeret {jumlah} "
                    f"nama di kartu ronde, pil rekap, dan tab Setup")
        check("Ganti L/P peserta di rekap", ganti_gender)

        # --- 3a5. ganti rating di rekap -----------------------------------
        # Bagian ketiga dari substitusi peserta: pengganti bisa beda kekuatan.
        # Rating tidak menyentuh skor kualitas, tapi ia menggerakkan selisih
        # rating antar tim yang tercetak di laporan, dan di mode pool rating ia
        # yang MENENTUKAN pool tiap orang - jadi ia harus lewat hitung ulang
        # server, bukan cuma diubah di layar.
        def ganti_rating():
            b.js("""(() => {
              window.__rc = [];
              if (window.__rcHook) return;
              window.__rcHook = true;
              const asli = window.fetch;
              window.fetch = async (u, o) => {
                const res = await asli(u, o);
                if (String(u).includes('/api/schedule/recalc')) {
                  res.clone().json().then((d) => window.__rc.push(d))
                     .catch(() => {});
                }
                return res;
              };
            })(); true""")

            target = b.js("""(() => {
              const inp = document.querySelector('#recap input[data-rating]');
              return inp ? {id: +inp.dataset.rating, nilai: inp.value,
                            nama: inp.closest('tr').querySelector('input').value}
                : null;
            })()""")
            assert target, "kolom rating tidak muncul di rekap"
            pid, semula, nama = target["id"], target["nilai"], target["nama"]
            baru = "6.5" if float(semula) != 6.5 else "2.5"

            def isi(nilai):
                b.js("(() => { const i = document.querySelector("
                     + json.dumps(f'#recap input[data-rating="{pid}"]')
                     + "); i.value = " + json.dumps(nilai) + ";"
                     " i.dispatchEvent(new Event('change', {bubbles:true}));"
                     " })(); true")

            isi(baru)
            b.wait_for("window.__rc.length > 0", timeout=25,
                       label="statistik dihitung ulang server")
            b.wait_for(f"""document.querySelector(
              '#recap input[data-rating="{pid}"]').value === {json.dumps(baru)}""",
                       timeout=10, label="rating baru bertahan setelah digambar ulang")
            di_setup = b.js("""(() => {
              const baris = [...document.querySelectorAll('#ptable tbody tr')]
                .find((r) => r.querySelector('.nm').value === """
                            + json.dumps(nama) + """);
              return baris ? baris.querySelector('[data-f="rating"]').value : null;
            })()""")
            assert float(di_setup) == float(baru), \
                f"tab Setup masih menyebut rating lama: {di_setup}"

            # Di luar rentang dijepit SEKETIKA, bukan menunggu jawaban server.
            isi("99")
            jepit = b.js(f"""document.querySelector(
              '#recap input[data-rating="{pid}"]').value""")
            assert float(jepit) == 7, f"rating 99 tidak dijepit ke 7: {jepit}"

            # Bukan angka ditolak, dan isiannya kembali ke nilai sebelumnya.
            b.wait_for("window.__rc.length >= 2", timeout=25, label="jepitan dihitung")
            isi("bukan angka")
            tolak = b.js(f"""document.querySelector(
              '#recap input[data-rating="{pid}"]').value""")
            assert float(tolak) == 7, f"isian tidak sah tidak dikembalikan: {tolak}"

            isi(semula)
            b.wait_for(f"""document.querySelector(
              '#recap input[data-rating="{pid}"]').value === {json.dumps(semula)}""",
                       timeout=25, label="rating kembali ke semula")
            return (f"{nama}: rating {semula} -> {baru} sampai ke tab Setup, "
                    f"99 dijepit ke 7, bukan-angka ditolak")
        check("Ganti rating peserta di rekap", ganti_rating)

        # --- 3a6. ganti nama mengambil L/P & rating dari master ------------
        # Substitusi peserta sebenarnya tiga perubahan sekaligus: nama, gender,
        # kekuatan. Host yang cuma mengganti namanya meninggalkan jadwal yang
        # memperlakukan pengganti sebagai orang yang digantikan - dan dua field
        # yang tertinggal itu dibaca aturan babak, permintaan court, dan pool
        # rating. Yang diuji: kalau namanya ada di master klub, keduanya ikut
        # tanpa diketik.
        def nama_dari_master():
            # Peserta master yang BELUM ada di jadwal, dengan L/P dan rating
            # yang berbeda dari orang yang akan digantikan - kalau sama, tidak
            # ada yang bisa dibuktikan ikut berubah.
            umpan = {"nama": f"Master Uji {stamp}", "rating": 6.5, "gender": "F"}
            # club_id diambil dari halaman, bukan dikirim null: loadMaster()
            # menyaring per klub di SERVER, jadi peserta tanpa klub tidak akan
            # pernah ikut terkirim balik dan sarannya tidak mungkin muncul.
            # Klubnya diambil dari /api/master, bukan dari kotak di halaman:
            # server memakai klub BAWAAN kalau club_id kosong, jadi peserta
            # tanpa klub tidak pernah ikut terkirim balik dan sarannya mustahil
            # muncul. Yang dipakai harus klub yang sama dengan yang dibaca UI.
            b.js("""(async () => {
              const m = await (await fetch('/api/master')).json();
              window.__mu = await (await fetch('/api/players/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(Object.assign(""" + json.dumps({
                  "name": umpan["nama"], "rating": umpan["rating"],
                  "gender": umpan["gender"]}) + """,
                  {club_id: m.club_id})),
              })).json();
            })(); true""")
            b.wait_for("window.__mu && window.__mu.id", timeout=15,
                       label="peserta master uji tersimpan")
            mid = b.js("window.__mu.id")

            # Master dimuat ulang lalu jadwal digambar ulang, seperti yang
            # terjadi kalau host menambah pemain lalu kembali ke tab Jadwal.
            b.js("document.querySelector('.tabs button[data-view=\"master\"]')"
                 ".click(); true")
            time.sleep(1.2)
            b.js("document.querySelector('.tabs button[data-view=\"jadwal\"]')"
                 ".click(); true")
            time.sleep(0.5)

            # Saran disegarkan saat kotak nama disentuh, bukan saat tab dibuka -
            # jadi fokus dulu, seperti host yang hendak mengetik.
            b.js("document.querySelector('#recap input[data-rename]')"
                 ".dispatchEvent(new Event('focusin', {bubbles:true})); true")
            saran = b.js("[...document.querySelectorAll('#master-nama option')]"
                         ".map((o) => o.value)")
            assert umpan["nama"] in (saran or []), (
                f"peserta master tidak muncul di {len(saran or [])} saran; "
                f"contoh: {(saran or [])[:4]}")
            # Yang sudah main di jadwal ini tidak boleh ikut disarankan.
            dipakai = b.js("[...document.querySelectorAll("
                           "'#recap input[data-rename]')].map((i) => i.value)")
            tumpang = [n for n in (saran or []) if n in dipakai]
            assert not tumpang, f"peserta yang sudah main ikut disarankan: {tumpang}"

            # Korban: peserta yang L/P dan ratingnya berbeda dari umpan.
            korban = b.js("""(() => {
              const tr = [...document.querySelectorAll('#recap table.data tbody tr')];
              for (const r of tr) {
                const g = r.querySelector('select[data-gender]');
                const rt = r.querySelector('input[data-rating]');
                if (g && g.value !== 'F' && parseFloat(rt.value) !== 6.5) {
                  return {id: +g.dataset.gender, nama: r.querySelector('input').value,
                          gender: g.value, rating: rt.value};
                }
              }
              return null;
            })()""")
            assert korban, "tidak ada peserta yang L/P & ratingnya berbeda"
            pid = korban["id"]

            b.js("(() => { const i = document.querySelector("
                 + json.dumps(f'#recap input[data-rename="{pid}"]')
                 + "); i.value = " + json.dumps(umpan["nama"]) + ";"
                 " i.dispatchEvent(new Event('change', {bubbles:true}));"
                 " })(); true")
            b.wait_for(f"""(() => {{
              const g = document.querySelector('#recap select[data-gender="{pid}"]');
              const r = document.querySelector('#recap input[data-rating="{pid}"]');
              return g && r && g.value === 'F' && parseFloat(r.value) === 6.5;
            }})()""", timeout=25, label="L/P & rating ikut dari master")

            # Tab Setup ikut, kalau tidak Generate berikutnya menghidupkan lagi
            # angka orang yang sudah digantikan.
            di_setup = b.js("""(() => {
              const baris = [...document.querySelectorAll('#ptable tbody tr')]
                .find((r) => r.querySelector('.nm').value === """
                            + json.dumps(umpan["nama"]) + """);
              if (!baris) return null;
              return {g: baris.querySelector('[data-f="gender"]').value,
                      r: baris.querySelector('[data-f="rating"]').value};
            })()""")
            assert di_setup and di_setup["g"] == "F" \
                and float(di_setup["r"]) == 6.5, \
                f"tab Setup tidak ikut: {di_setup}"

            # Nama yang TIDAK ada di master tidak membawa apa pun - L/P dan
            # rating yang barusan diambil harus tetap, bukan ikut terhapus.
            b.js("(() => { const i = document.querySelector("
                 + json.dumps(f'#recap input[data-rename="{pid}"]')
                 + "); i.value = " + json.dumps(f"Bukan Master {stamp}") + ";"
                 " i.dispatchEvent(new Event('change', {bubbles:true}));"
                 " })(); true")
            time.sleep(1.0)
            tetap = b.js(f"""(() => {{
              const g = document.querySelector('#recap select[data-gender="{pid}"]');
              const r = document.querySelector('#recap input[data-rating="{pid}"]');
              return g.value + '/' + r.value;
            }})()""")
            assert tetap.startswith("F/") and float(tetap.split("/")[1]) == 6.5, \
                f"nama di luar master ikut mengubah L/P atau rating: {tetap}"

            b.js(f"fetch('/api/players/delete', {{method:'POST',"
                 f" headers:{{'Content-Type':'application/json'}},"
                 f" body: JSON.stringify({{id: {mid}}})}}); true")
            return (f"'{korban['nama']}' ({korban['gender']}/{korban['rating']}) "
                    f"-> master: F/6.5 ikut ke rekap & tab Setup; nama di luar "
                    f"master tidak membawa apa pun")
        check("Ganti nama mengambil L/P & rating dari master", nama_dari_master)

        # --- 3a7. quick-add peserta rekap ke master pemain -----------------
        # Pengganti yang datang di tengah acara justru yang paling sering belum
        # jadi anggota klub. Tanpa tombol ini host harus pindah ke tab Master,
        # mengetik ulang nama + L/P + rating yang baru saja ia isi di rekap,
        # lalu kembali. Yang diuji: satu klik menyimpannya dengan angka dari
        # barisnya sendiri, tombolnya lenyap, dan namanya berbalik jadi saran.
        def quickadd_master():
            nama = f"Tamu Uji {stamp}"
            target = b.js("""(() => {
              const inp = document.querySelector('#recap input[data-rename]');
              return inp ? +inp.dataset.rename : null;
            })()""")
            assert target is not None, "rekap tidak punya kotak nama"

            def isi(sel, nilai, ev="change"):
                b.js("(() => { const i = document.querySelector("
                     + json.dumps(sel) + "); i.value = " + json.dumps(nilai)
                     + f"; i.dispatchEvent(new Event('{ev}', {{bubbles:true}}));"
                     " })(); true")

            # Nama di luar master, dengan L/P dan rating yang khas supaya bisa
            # dibuktikan angka itulah yang tersimpan - bukan bawaan 3.0.
            isi(f'#recap input[data-rename="{target}"]', nama)
            b.wait_for(f"""document.querySelector(
              '#recap input[data-rename="{target}"]').value === {json.dumps(nama)}""",
                       timeout=20, label="nama pengganti terpasang")
            isi(f'#recap select[data-gender="{target}"]', "F")
            b.wait_for(f"""document.querySelector(
              '#recap select[data-gender="{target}"]').value === 'F'""",
                       timeout=20, label="L/P terpasang")
            isi(f'#recap input[data-rating="{target}"]', "5.5")
            b.wait_for(f"""parseFloat(document.querySelector(
              '#recap input[data-rating="{target}"]').value) === 5.5""",
                       timeout=20, label="rating terpasang")

            tombol = f'#recap .rk-add-btn[data-simpan-master="{target}"]'
            b.wait_for(f"document.querySelector({json.dumps(tombol)})", timeout=10,
                       label="tawaran simpan ke master muncul")
            label = b.js(f"document.querySelector({json.dumps(tombol)}).textContent")
            assert nama in label, f"tombolnya tidak menyebut namanya: {label}"

            b.js(f"document.querySelector({json.dumps(tombol)}).click(); true")
            # Tombolnya lenyap sendiri begitu orangnya terdaftar - itu sekaligus
            # bukti master sudah dimuat ulang, bukan cuma request terkirim.
            b.wait_for(f"!document.querySelector({json.dumps(tombol)})", timeout=25,
                       label="tawaran lenyap setelah tersimpan")

            # Yang tersimpan harus angka dari barisnya, bukan bawaan.
            tersimpan = b.js("""(async () => {
              const m = await (await fetch('/api/master')).json();
              return (m.players || []).find(
                (p) => p.name === """ + json.dumps(nama) + """) || null;
            })()""")
            assert tersimpan, "peserta tidak ada di master setelah disimpan"
            assert tersimpan["gender"] == "F", \
                f"L/P tidak ikut tersimpan: {tersimpan['gender']}"
            assert float(tersimpan["rating"]) == 5.5, \
                f"rating tidak ikut tersimpan: {tersimpan['rating']}"

            # Sekarang ia anggota klub, jadi ia TIDAK boleh disarankan lagi -
            # ia sudah main di jadwal ini, dan memilihnya cuma nama kembar.
            b.js("document.querySelector('#recap input[data-rename]')"
                 ".dispatchEvent(new Event('focusin', {bubbles:true})); true")
            saran = b.js("[...document.querySelectorAll('#master-nama option')]"
                         ".map((o) => o.value)")
            assert nama not in (saran or []), \
                "peserta yang sedang main ikut disarankan setelah terdaftar"

            b.js(f"""fetch('/api/players/delete', {{method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{id: {tersimpan['id']}}})}}); true""")
            return (f"'{nama}' F/5.5 tersimpan dari rekap dengan satu klik, "
                    f"tombolnya lenyap, dan tidak disarankan lagi")
        check("Quick-add peserta rekap ke master pemain", quickadd_master)

        # --- 3a3. kalibrasi manual: tukar orang di dalam satu ronde -------
        # Yang diuji bukan namanya pindah di kartu ronde - itu bagian yang
        # gampang. Yang diuji adalah seluruh TURUNANNYA ikut bergeser: jumlah
        # main di rekap pemain, skor kualitas, dan teks salinan. Angka itu
        # lahir di server dari kode penilaian yang sama dengan saat generate,
        # jadi kalau round-trip-nya putus yang tampil adalah angka jadwal yang
        # sudah tidak ada - dan itu tidak kelihatan dari layar.
        #
        # Semuanya dibaca dari DOM, bukan dari variabel app.js: app.js dimuat
        # sebagai module, jadi state-nya memang tidak bisa dijangkau dari luar -
        # dan yang ingin dijamin justru apa yang benar-benar dibaca host.
        def kalibrasi_tukar():
            # Kolom "Main" dicari lewat kepala tabel, bukan dihitung dari kiri:
            # kolom L/P dan kolom tugas muncul-hilang mengikuti setup.
            def main_di_rekap(nama):
                return b.js("""(() => {
                  const th = [...document.querySelectorAll(
                    '#recap table.data thead th')].map((x) => x.textContent.trim());
                  const i = th.indexOf('Main');
                  const row = [...document.querySelectorAll(
                    '#recap table.data tbody tr')].find(
                      (x) => x.querySelector('input').value === """
                            + json.dumps(nama) + """);
                  return row ? +row.children[i].textContent : null;
                })()""")

            def kualitas():
                return b.js("""(() => {
                  const k = [...document.querySelectorAll('#sched-stats .stat')]
                    .find((s) => s.querySelector('.k').textContent === 'Kualitas');
                  // Angkanya saja: kartu berstatus membawa glif di depan
                  // nilainya, dan glif itu tidak bisa dicetak di konsol Windows.
                  return k ? k.querySelector('.v').textContent
                    .replace(/[^0-9.]/g, '') : null;
                })()""")

            # Ronde yang punya orang menganggur DAN orang main: hanya di situ
            # pertukaran menggeser jumlah main.
            target = b.js("""(() => {
              const per = {};
              document.querySelectorAll('#rounds .nm-swap').forEach((t) => {
                const [ri, pid] = t.dataset.tukar.split(':');
                const posisi = t.closest('.resting') ? 'duduk'
                  : t.closest('.duty') ? 'tugas' : 'main';
                (per[ri] = per[ri] || []).push(
                  {pid: +pid, nama: t.textContent.trim(), posisi});
              });
              for (const ri of Object.keys(per)) {
                const duduk = per[ri].find((o) => o.posisi === 'duduk');
                const main = per[ri].find((o) => o.posisi === 'main');
                if (duduk && main) return {ri: +ri, duduk, main};
              }
              return null;
            })()""")
            assert target, "tidak ada ronde dengan peserta menganggur"
            ri, duduk, main = target["ri"], target["duduk"], target["main"]
            m_duduk, m_main = main_di_rekap(duduk["nama"]), main_di_rekap(main["nama"])
            skor_awal = kualitas()
            assert m_duduk is not None and m_main is not None, "rekap tidak terbaca"

            # Menu dibuka lewat klik pada nama, persis seperti host.
            b.js(f"""document.querySelector(
              '#rounds .nm-swap[data-tukar="{ri}:{main['pid']}"]').click(); true""")
            b.wait_for("document.querySelector('.swapmenu')", timeout=5,
                       label="menu tukar")
            baris = b.js("document.querySelectorAll('.swapmenu .sm-row').length")
            assert baris >= 3, f"pilihan tukar terlalu sedikit: {baris}"
            kepala = b.js("document.querySelector('.swapmenu .sm-h').textContent")
            assert "main" in kepala, f"kepala menu tidak menyebut posisinya: {kepala}"
            akibat = b.js(f"""document.querySelector(
              '.swapmenu .sm-row[data-pilih="{duduk['pid']}"] .sm-e').textContent""")
            assert f"{m_duduk}" in akibat and f"{m_duduk + 1}" in akibat, \
                f"akibat pertukaran tidak disebut dengan benar: '{akibat}'"

            b.js(f"""document.querySelector(
              '.swapmenu .sm-row[data-pilih="{duduk['pid']}"]').click(); true""")
            # Angkanya baru berubah setelah server menjawab; menunggu di rekap
            # berarti yang ditunggu adalah yang benar-benar dibaca host.
            b.wait_for(f"""(() => {{
              const th = [...document.querySelectorAll(
                '#recap table.data thead th')].map((x) => x.textContent.trim());
              const row = [...document.querySelectorAll(
                '#recap table.data tbody tr')].find(
                  (x) => x.querySelector('input').value === {json.dumps(duduk['nama'])});
              return row && +row.children[th.indexOf('Main')].textContent
                === {m_duduk + 1};
            }})()""", timeout=25, label="rekap dihitung ulang server")

            assert not b.js("!!document.querySelector('.swapmenu')"), \
                "menu tidak tertutup setelah memilih"
            assert main_di_rekap(main["nama"]) == m_main - 1, \
                "yang keluar tidak berkurang jumlah mainnya di rekap"
            # Namanya benar-benar bertukar di kartu ronde.
            posisi_baru = b.js(f"""(() => {{
              const t = document.querySelector(
                '#rounds .nm-swap[data-tukar="{ri}:{duduk['pid']}"]');
              return t ? (t.closest('.resting') ? 'duduk'
                : t.closest('.duty') ? 'tugas' : 'main') : null;
            }})()""")
            assert posisi_baru == "main", \
                f"yang masuk tidak jadi main di kartu ronde: {posisi_baru}"

            skor_baru = kualitas()
            catatan = b.js("document.getElementById('notes').textContent")
            assert "Kalibrasi manual" in catatan, \
                "catatan tidak menyebut jadwal ini sudah dikalibrasi"
            assert "1 pertukaran" in catatan, f"jumlah kalibrasi salah: {catatan[:80]}"

            # Urungkan harus mengembalikan ANGKANYA, bukan cuma susunannya.
            b.js("document.getElementById('kal-urung').click(); true")
            b.wait_for(f"""(() => {{
              const th = [...document.querySelectorAll(
                '#recap table.data thead th')].map((x) => x.textContent.trim());
              const row = [...document.querySelectorAll(
                '#recap table.data tbody tr')].find(
                  (x) => x.querySelector('input').value === {json.dumps(duduk['nama'])});
              return row && +row.children[th.indexOf('Main')].textContent === {m_duduk};
            }})()""", timeout=25, label="urung dihitung ulang")
            assert kualitas() == skor_awal, \
                f"skor tidak kembali setelah diurungkan: {skor_awal} -> {kualitas()}"
            assert not b.js("!!document.getElementById('kal-urung')"), \
                "tombol urung masih ada padahal tidak ada lagi yang bisa diurungkan"
            assert "Kalibrasi manual" not in b.js(
                "document.getElementById('notes').textContent"), \
                "catatan kalibrasi tidak ikut hilang saat diurungkan"
            return (f"R{ri + 1}: {duduk['nama']} main {m_duduk}->{m_duduk + 1}, "
                    f"{main['nama']} {m_main}->{m_main - 1}, kualitas "
                    f"{skor_awal}->{skor_baru}, urung mengembalikan semuanya")
        check("Kalibrasi manual: tukar orang di satu ronde", kalibrasi_tukar)

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

        # --- 3c. blok tawaran penyempurnaan tidak menempel kartu di atasnya
        # Kelas bug yang lolos dari pemeriksaan kode: dua kotak berbingkai yang
        # jaraknya nol tetap "benar" menurut DOM, tapi terbaca sebagai bertumpuk.
        # Pernah terjadi - .stat-grid tidak punya margin bawah dan .issue tidak
        # punya margin atas, jadi keduanya bersentuhan persis di 244px.
        def jarak_tawaran():
            d = b.js("""(() => {
              const s = document.getElementById('sched-stats');
              const box = document.getElementById('lns-box');
              const row = document.querySelector('#view-jadwal .btn-row');
              if (!s || !box || !row) return null;
              const rs = s.getBoundingClientRect();
              const rb = box.getBoundingClientRect();
              return {bawah: Math.round(rs.bottom), atas: Math.round(rb.top),
                      tinggi: Math.round(rb.height),
                      tombol: Math.round(row.getBoundingClientRect().top)};
            })()""")
            if d is None:
                return "elemen tidak ada - dilewati"
            # Wadah kosong tidak boleh menyisakan celah hantu.
            kosong = d["tinggi"] == 0
            celah_kosong = d["tombol"] - d["bawah"] if kosong else None
            if kosong and celah_kosong > 20:
                raise AssertionError(
                    f"wadah kosong menyisakan celah {celah_kosong}px")
            # Dan yang berisi harus punya jarak. Kalau app tidak sedang
            # menawarkannya, anak boneka disisipkan sebentar - yang diuji aturan
            # CSS-nya, dan itu berlaku sama saja isinya sungguhan atau bukan.
            jarak = b.js("""(() => {
              const box = document.getElementById('lns-box');
              const simpan = box.innerHTML;
              const semula = box.getBoundingClientRect().height;
              if (!semula) box.innerHTML = '<div class="issue info">x</div>';
              const s = document.getElementById('sched-stats')
                          .getBoundingClientRect();
              const r = box.getBoundingClientRect();
              const j = Math.round(r.top - s.bottom);
              if (!semula) box.innerHTML = simpan;
              return j;
            })()""")
            if jarak < 6:
                raise AssertionError(
                    f"blok tawaran menempel kartu statistik (jarak {jarak}px)")
            return (f"jarak {jarak}px"
                    + (f", wadah kosong tanpa celah hantu ({celah_kosong}px)"
                       if kosong else " (sedang ditawarkan)"))
        check("Blok penyempurnaan tidak menempel kartu statistik", jarak_tawaran)

        # Cacat yang sama satu tingkat di bawah: pesan hasil simpan mendarat
        # 0px di bawah barisan tombol, dan karena tombolnya bersudut bulat,
        # kotak yang menempel rata terbaca seperti menindihnya. Dilaporkan host
        # dari layar sungguhan, jadi diuji dari geometri sungguhan.
        def jarak_pesan_simpan():
            d = b.js("""(() => {
              const box = document.getElementById('save-msg');
              const row = document.querySelector('#view-jadwal .btn-row');
              if (!box || !row) return null;
              // Wadahnya biasanya kosong - anak boneka disisipkan sebentar,
              // karena yang diuji aturan CSS-nya.
              const simpan = box.innerHTML;
              const semula = box.getBoundingClientRect().height;
              const celah_kosong = Math.round(
                box.getBoundingClientRect().top - row.getBoundingClientRect().bottom);
              if (!semula) box.innerHTML = '<div class="msg ok">x</div>';
              const pesan = box.querySelector('.msg').getBoundingClientRect();
              const j = Math.round(pesan.top - row.getBoundingClientRect().bottom);
              // Pesan terakhir tidak boleh menambah jarak di atas padding kartu.
              const kartu = box.closest('.card').getBoundingClientRect();
              const bawah = Math.round(kartu.bottom - pesan.bottom);
              if (!semula) box.innerHTML = simpan;
              return {jarak: j, kosong: !semula, celah_kosong: celah_kosong,
                      bawah: bawah};
            })()""")
            if d is None:
                return "elemen tidak ada - dilewati"
            if d["kosong"] and d["celah_kosong"] > 6:
                raise AssertionError(
                    f"wadah kosong menyisakan celah {d['celah_kosong']}px")
            if d["jarak"] < 6:
                raise AssertionError(
                    f"pesan simpan menempel barisan tombol (jarak {d['jarak']}px)")
            if d["bawah"] > 24:
                raise AssertionError(
                    f"pesan terakhir menyisakan {d['bawah']}px di bawah, "
                    "lebih longgar daripada padding kartu")
            return (f"jarak {d['jarak']}px, sisa bawah kartu {d['bawah']}px"
                    + (", wadah kosong tanpa celah hantu" if d["kosong"] else ""))
        check("Pesan simpan tidak menempel barisan tombol", jarak_pesan_simpan)

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

        # --- 12c. mode CP-SAT: sakelar, batas waktu, dan penyembunyiannya --
        # Modenya bergantung pada paket Python yang bisa saja tidak ada, jadi
        # yang diuji bukan cuma "kotaknya muncul" melainkan seluruh rantainya:
        # kolom batas waktu ikut muncul, angkanya benar-benar terkirim ke
        # server, dan kalau OR-Tools tidak ada modenya hilang dari daftar -
        # bukan tinggal di situ menunggu dipilih lalu gagal.
        # Sama seperti effort di atas: dibaca lewat tombol info debug, bukan
        # dengan memanggil fungsi internal app.js.
        def cpsat_toggle():
            ada = json.loads(b.js("""JSON.stringify((() => {
              const o = document.getElementById('mode')
                .querySelector('option[value="americano_cpsat"]');
              return o ? {ada: true, tersembunyi: o.hidden} : {ada: false};
            })())"""))
            if not ada["ada"]:
                return "opsi tidak ada di daftar HTML"
            if ada["tersembunyi"]:
                # Sah kalau OR-Tools memang tidak terpasang di server.
                return "opsi disembunyikan (server melaporkan OR-Tools tidak ada)"

            def debug_untuk(mode, detik=None):
                b.js(f"""(() => {{
                  const sel = document.getElementById('mode');
                  sel.value = {json.dumps(mode)};
                  sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                  {f"document.getElementById('cpsat_seconds').value = '{detik}';"
                   if detik else ""}
                  document.getElementById('debug-out').value = '';
                  document.getElementById('copy-debug').click();
                }})(); true""")
                b.wait_for("document.getElementById('debug-out').value"
                           ".includes('percobaan=')",
                           timeout=5, label=f"teks debug {mode}")
                return b.js("""JSON.stringify({
                  teks: document.getElementById('debug-out').value,
                  blok: document.getElementById('cpsat-block').style.display,
                })""")

            eksak = json.loads(debug_untuk("americano_cpsat", 45))
            assert eksak["blok"] == "", "kolom batas waktu tidak muncul"
            assert "batas_solver=45s" in eksak["teks"], \
                "batas waktu solver tidak ikut terkirim ke server"

            # Mode kedua yang memakai solver: mesin dasar. Kotak batas waktu
            # yang dipakainya SAMA, jadi kalau pemeriksaan mode di app.js
            # ditulis per-nama alih-alih lewat daftar, mode inilah yang lupa -
            # dan gejalanya cuma "kotaknya tidak muncul", tanpa error apa pun.
            dasar = json.loads(debug_untuk("americano_solver", 20))
            assert dasar["blok"] == "", (
                "kolom batas waktu tidak muncul di mode solver-sebagai-dasar")
            assert "batas_solver=20s" in dasar["teks"], (
                "batas waktu solver tidak terkirim di mode solver-sebagai-dasar")

            # Sakelar "hasil bisa diulang". Yang diuji bukan determinismenya
            # (itu milik tes unit) melainkan bahwa centangnya benar-benar sampai
            # ke server: kalau ia berhenti di browser, host mengira jadwalnya
            # bisa dipanggil ulang padahal tidak, dan tidak ada yang gagal.
            b.js("""document.getElementById('cpsat_deterministic')
                    .checked = true; true""")
            ulang = json.loads(debug_untuk("americano_solver", 20))
            assert "bisa_diulang=true" in ulang["teks"], (
                "sakelar 'hasil bisa diulang' tidak terkirim ke server")
            b.js("""document.getElementById('cpsat_deterministic')
                    .checked = false; true""")

            biasa = json.loads(debug_untuk("americano"))
            assert biasa["blok"] == "none", "kolom batas waktu tidak ikut sembunyi"
            # Batas waktu solver tidak berarti apa-apa di mode lain, jadi ia juga
            # tidak boleh muncul di info debug - laporan yang menyebut angka yang
            # tidak dipakai membuat orang mengejar sebab yang salah.
            assert "batas_solver" not in biasa["teks"], \
                "batas solver ikut dilaporkan padahal modenya tidak memakainya"
            return ("blok muncul/hilang, batas 45s & 20s terkirim di dua mode "
                    "solver, sakelar bisa-diulang terkirim, Americano bersih")
        check("Mode CP-SAT: sakelar & batas waktu", cpsat_toggle)

        # --- 12c-bis. mode tim sepadan: kalimat penjelasnya -----------------
        # Modenya tidak punya kotak pengaturan sendiri, jadi satu-satunya yang
        # dilihat host sebelum menekan Generate adalah kalimat ini - dan
        # kalimat itu yang memberi tahu bahwa yang disepadankan KEKUATAN TIM,
        # bukan level lawan. Tanpa dia, mode ini terbaca seperti "pool rating"
        # dan host memilih yang salah.
        def mode_sepadan():
            hasil = json.loads(b.js("""JSON.stringify((() => {
              const sel = document.getElementById('mode');
              const box = document.getElementById('mode-peran');
              const pilih = (v) => {
                sel.value = v;
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                return {teks: box.textContent, tampil: box.style.display};
              };
              const ada = !!sel.querySelector('option[value="americano_rating"]');
              const sepadan = pilih('americano_rating');
              const biasa = pilih('americano');
              return {ada: ada, sepadan: sepadan, biasa: biasa};
            })())"""))
            assert hasil["ada"], "opsi mode tim sepadan tidak ada di daftar"
            teks = hasil["sepadan"]["teks"]
            # Dua kalimat yang mungkin - "rating semua sama" atau penjelasan
            # penuh - dan dua-duanya menyebut Americano. Yang diuji: ADA
            # kalimatnya, dan ia bicara soal Americano.
            assert "Americano" in teks and len(teks) > 80, (
                f"kalimat mode tim sepadan kosong atau terlalu pendek: {teks!r}")
            assert hasil["biasa"]["teks"] == "", (
                "kalimat mode masih tertinggal setelah pindah ke Americano")
            assert hasil["biasa"]["tampil"] == "none", (
                "kotak kalimat mode tidak ikut sembunyi di Americano")
            return f"kalimat {len(teks)} huruf muncul, hilang di Americano"
        check("Mode tim sepadan: kalimat penjelas", mode_sepadan)

        # --- 12d. tombol Buka laporan membawa jadwal yang tampil ----------
        # Dulu yang dikirim cuma setup, jadi server menyusun ulang seluruh
        # jadwal sebelum mengirim satu byte pun - sementara jendela laporan
        # sudah telanjur terbuka putih. Host yang menekan Ctrl+P di situ
        # mencetak halaman kosong, dan tidak ada log apa pun yang menjelaskan.
        #
        # submit() ditambal supaya harness tidak benar-benar membuka tab baru;
        # yang diperiksa isi payload-nya, karena di situlah bug-nya hidup.
        def laporan_bawa_jadwal():
            hasil = b.js("""JSON.stringify((() => {
              const asli = HTMLFormElement.prototype.submit;
              let terkirim = null;
              HTMLFormElement.prototype.submit = function () {
                const f = this.querySelector('input[name=payload]');
                terkirim = f ? f.value : null;
              };
              try { document.getElementById('open-html').click(); }
              finally { HTMLFormElement.prototype.submit = asli; }
              if (!terkirim) return {ada: false};
              const p = JSON.parse(terkirim);
              const s = p.schedule || {};
              return {
                ada: true,
                ronde: (s.rounds || []).length,
                pemain: (s.players || []).length,
                stats: !!s.stats,
                kb: Math.round(terkirim.length / 1024),
              };
            })())""")
            d = json.loads(hasil)
            assert d["ada"], "form laporan tidak pernah disubmit"
            assert d["ronde"] > 0, \
                "jadwal tidak ikut dikirim - server akan generate ulang"
            assert d["stats"], \
                "statistik tidak ikut - rekap per pemain di laporan jadi nol"
            return (f"{d['ronde']} ronde + {d['pemain']} pemain ikut terkirim "
                    f"({d['kb']} KB)")
        check("Buka laporan membawa jadwal yang tampil", laporan_bawa_jadwal)

        # --- 12e. laporan laba/rugi -------------------------------------
        # Tombolnya membuka halaman lewat window.open, jadi yang diperiksa dua
        # hal: alamat yang dibangun (penyaring benar-benar ikut) dan halaman
        # yang keluar dari alamat itu. Memeriksa alamatnya saja pernah tidak
        # cukup di proyek ini - lihat "Grafik trade-off memuat pilihan aktif".
        def laporan_laba_rugi():
            b.js("document.querySelector('.tabs button[data-view=\"riwayat\"]')"
                 ".click(); true")
            b.wait_for("document.getElementById('open-ledger')", timeout=8,
                       label="tombol laporan laba/rugi")
            # Promise-nya harus jadi nilai yang di-await, bukan objek yang
            # di-JSON.stringify - JSON.stringify(Promise) menghasilkan "{}" dan
            # tesnya lalu gagal di tempat yang tidak menjelaskan apa pun.
            hasil = b.js("""(async () => {
              const asli = window.open;
              let alamat = null;
              window.open = (u) => { alamat = u; return null; };
              const set = (id, v) => { document.getElementById(id).value = v; };
              try {
                set('ledger-since', '2026-01-01');
                set('ledger-until', '2026-12-31');
                document.getElementById('open-ledger').click();
              } finally { window.open = asli; }
              if (!alamat) return JSON.stringify({ada: false});

              // Rentang terbalik harus ditolak sebelum jendela dibuka.
              let ditolak = true;
              window.open = () => { ditolak = false; return null; };
              try {
                set('ledger-since', '2026-12-31');
                set('ledger-until', '2026-01-01');
                document.getElementById('open-ledger').click();
              } finally { window.open = asli; }
              set('ledger-since', ''); set('ledger-until', '');

              const r = await fetch(alamat);
              const h = await r.text();
              return JSON.stringify({
                ada: true, alamat, ditolak, status: r.status,
                seksi: [...new DOMParser().parseFromString(h, 'text/html')
                  .querySelectorAll('h2')].map(x => x.firstChild.textContent),
                hint: document.getElementById('ledger-hint').textContent,
              });
            })()""")
            d = json.loads(hasil)
            assert d["ada"], "tombol laporan laba/rugi tidak membuka apa pun"
            assert "/api/host-report" in d["alamat"], d["alamat"]
            for perlu in ("since=2026-01-01", "until=2026-12-31"):
                assert perlu in d["alamat"], \
                    f"penyaring tidak ikut ke alamat: {d['alamat']}"
            assert d["ditolak"], \
                "rentang tanggal terbalik tetap membuka laporan"
            assert d["status"] == 200, f"laporan menjawab {d['status']}"
            # Halamannya harus benar-benar berisi buku besarnya, bukan cuma
            # terbuka: acara yang tersimpan di database uji ini nyata.
            assert "Ringkasan" in d["seksi"], d["seksi"]
            assert any("Laba / rugi per acara" in x for x in d["seksi"]) \
                or not d["seksi"], f"tabel per acara tidak ada: {d['seksi']}"
            assert "acara tersimpan" in d["hint"], d["hint"]
            return (f"{len(d['seksi'])} seksi, penyaring tanggal ikut, "
                    f"rentang terbalik ditolak")
        check("Laporan laba/rugi terbuka dengan penyaringnya", laporan_laba_rugi)

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
