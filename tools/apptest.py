#!/usr/bin/env python3
"""Uji APLIKASI DESKTOP sungguhan, dari main.js, lewat DevTools Protocol.

uitest.py menguji halaman web di browser; ini menguji hal-hal yang cuma ada di
aplikasi desktop dan tidak pernah tersentuh di sana:

  - main.js menyalakan server Python-nya sendiri dan menunggunya siap
  - "Buka laporan" membuka JENDELA baru (window.open dengan target=_blank), dan
    penangannya harus memasang preload di jendela itu
  - tombol di laporan harus menemukan jembatan window.padelin, lalu membuka
    jendela pratinjau cetak

Ketiganya pernah gagal dengan cara yang tidak terlihat di uji lain: preload yang
tidak sampai ke jendela anak membuat tombolnya jatuh diam-diam ke dialog cetak
Windows tanpa pratinjau.

Yang dilakukan hanya menempel peserta dan Generate - keduanya tidak menulis ke
database. Tetap begitu: kalau uji ini nanti perlu menyimpan acara atau menambah
master data, arahkan PADELIN_DB ke berkas sementara lebih dulu.

Pakai:
    python tools/apptest.py

Aplikasi lain yang sedang terbuka harus DITUTUP dulu: main.js hanya mengizinkan
satu instance, jadi instance kedua langsung keluar.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import cdp  # noqa: E402 - butuh sys.path di atas

# Dipasang SEBELUM skrip halaman, lalu halamannya dimuat ulang - supaya error
# saat boot ikut tertangkap, bukan cuma yang terjadi setelah kita menempel.
PENGUMPUL = """
  window.__errs = [];
  addEventListener('error', e => window.__errs.push(e.message));
  addEventListener('unhandledrejection', e => window.__errs.push('promise: ' + e.reason));
"""


def main() -> int:
    exe = cdp.electron_exe()
    port = cdp.free_port()
    lap = cdp.Laporan(f"Uji aplikasi desktop ({exe.name}, DevTools di {port})")

    userdata = Path(tempfile.mkdtemp(prefix="padelin-apptest-"))
    proc = subprocess.Popen(
        [str(exe), ".", f"--remote-debugging-port={port}",
         f"--user-data-dir={userdata}"],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    sesi = cdp.Proses(port, proc)
    jendela = []

    try:
        # --- 1. Aplikasi -------------------------------------------------
        app = sesi.tempel(
            lambda u: u.startswith("http://127.0.0.1:") and "/api/" not in u,
            label="jendela aplikasi")
        jendela.append(app)
        lap.periksa("jendela aplikasi terbuka", True, app.url)

        app.rekam_galat()
        app.call("Page.enable")
        app.call("Page.addScriptToEvaluateOnNewDocument", source=PENGUMPUL)
        app.call("Page.reload")
        app.tunggu("document.querySelector('#preset option')", timeout=40,
                   label="aplikasi siap")
        lap.periksa("server Python menjawab (preset terisi)", True)

        # --- 2. Peserta dan Generate --------------------------------------
        roster = "\n".join(f"Pemain {i + 1}" for i in range(12))
        app.js("(() => { document.getElementById('bulk').value = "
               + json.dumps(roster)
               + "; document.getElementById('parse-bulk').click(); })(); true")
        app.tunggu("document.querySelectorAll('#ptable tbody tr').length === 12",
                   label="12 baris peserta")
        lap.periksa("tempel massal 12 peserta", True,
                    app.js("document.getElementById('counts').textContent.trim()"))

        app.js("document.getElementById('generate').click(); true")
        app.tunggu("document.querySelectorAll('#rounds .round').length > 0",
                   timeout=120, label="jadwal ter-render")
        ronde = app.js("document.querySelectorAll('#rounds .round').length")
        lap.periksa("Generate jadwal", ronde > 0, f"{ronde} ronde")
        lap.periksa("log kemajuan terisi dari server",
                    app.js("document.querySelectorAll('#prog-log div').length") > 0)
        simpul = app.js("document.querySelectorAll('#engagement .viz-plot *').length")
        lap.periksa("grafik keterlibatan ter-render", simpul > 20, f"{simpul} simpul svg")

        # --- 3. Buka laporan: jendela anak, dengan preload ----------------
        app.js("document.getElementById('open-html').click(); true")
        laporan = sesi.tempel(lambda u: "/api/report" in u, label="jendela laporan")
        jendela.append(laporan)
        laporan.rekam_galat()
        laporan.tunggu("document.getElementById('pdf')", label="toolbar laporan")
        lap.periksa("preload sampai ke jendela laporan",
                    laporan.js("typeof window.padelin") == "object")
        label = laporan.js("document.getElementById('pdf').textContent")
        lap.periksa("tombol memakai jalur pratinjau", label == "Pratinjau cetak",
                    f'label="{label}"')

        # --- 4. Pratinjau cetak -------------------------------------------
        laporan.js("document.getElementById('pdf').click(); true")
        pratinjau = sesi.tempel(lambda u: u.startswith("padelin-pratinjau://"),
                                label="jendela pratinjau")
        jendela.append(pratinjau)
        pratinjau.rekam_galat()
        pratinjau.tunggu("document.getElementById('halaman').textContent !== ''",
                         label="info pratinjau terisi")
        halaman = pratinjau.js("document.getElementById('halaman').textContent")
        judul = pratinjau.js("document.title")
        lap.periksa("pratinjau cetak terbuka", True, pratinjau.url)
        lap.periksa("jumlah halaman disebut", "halaman A4" in halaman, f'"{halaman}"')
        lap.periksa("judul laporan ada di bilah jendela",
                    judul.startswith("Pratinjau cetak - "), f'"{judul}"')
        lap.periksa("penampil PDF terpasang",
                    pratinjau.js("!!document.getElementById('dokumen')"))
        kabar = pratinjau.js("document.getElementById('kabar').textContent")
        lap.periksa("tidak ada pesan galat di pratinjau", kabar == "", f'"{kabar}"')
        lap.periksa("tombol Simpan/Cetak hidup",
                    pratinjau.js("!document.getElementById('simpan').disabled"))

        # --- 5. Konsol ketiga jendela -------------------------------------
        errs = app.js("window.__errs") or []
        lap.periksa("tidak ada error JS di aplikasi", not errs, "; ".join(errs)[:200])
        for nama, j in (("aplikasi", app), ("laporan", laporan), ("pratinjau", pratinjau)):
            galat = j.galat_konsol()
            lap.periksa(f"konsol {nama} bersih", not galat, "; ".join(galat)[:200])
    except Exception as exc:  # noqa: BLE001 - laporan uji
        lap.periksa("uji berjalan sampai selesai", False, str(exc))
    finally:
        for j in jendela:
            j.close()
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    return lap.selesai()


if __name__ == "__main__":
    sys.exit(main())
