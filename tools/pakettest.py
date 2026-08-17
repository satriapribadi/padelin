#!/usr/bin/env python3
"""Uji PAKET hasil electron-builder, bukan repo.

Ada kelas kegagalan yang hanya muncul setelah dikemas, dan `npm run dev` tidak
akan pernah memperlihatkannya:

  - berkas yang tertinggal dari asar. Ini nyata: preload.js pernah tidak ikut
    ke paket portable, dan akibatnya bukan error - tombol cetak cuma diam-diam
    kembali ke dialog Windows tanpa pratinjau.
  - integritas asar. Nilainya ditulis electron-builder tapi DITEGAKKAN Electron;
    kalau tidak cocok, prosesnya mati sebelum jendela pertama muncul.
  - Python bundel dan berkas yang dikeluarkan dari asar (run.py, web/,
    padel_scheduler/) - salah jalur berarti server tidak pernah menyala.

Pakai:
    npm run dist:dir
    python tools/pakettest.py [dist-desktop/win-unpacked]

Catatan tentang Application Control: menjalankan Padelin.exe yang baru dibangun
bisa DITOLAK Windows (WinError 4551) karena biner tak bertanda tangan yang baru
tidak punya reputasi. Itu dilaporkan sebagai DILEWATI, bukan lulus dan bukan
gagal - pemeriksaan isi paketnya tetap berjalan, dan lapis runtime-nya harus
dicoba dengan tangan sekali lewat prompt yang mengizinkan.
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import cdp  # noqa: E402 - butuh sys.path di atas

# Yang WAJIB ada di dalam asar. Daftar ini pendek dengan sengaja: hanya berkas
# yang kalau hilang membuat fitur lenyap tanpa error.
DI_ASAR = ("main.js", "cetak.js", "preload.js", "pratinjau.html", "updater.js")

# Yang WAJIB ada di luar asar. Python tidak bisa membaca isi arsip asar, jadi
# ketiga yang pertama dikeluarkan lewat asarUnpack; yang terakhir Python bundel.
DI_LUAR_ASAR = (
    "app.asar.unpacked/run.py",
    "app.asar.unpacked/web/index.html",
    "app.asar.unpacked/padel_scheduler/html_report.py",
    "python/python.exe",
)


def isi_asar(asar: Path) -> dict:
    """Daftar isi asar, dibaca dari header JSON-nya.

    Bukan dengan menyisir potongan awal berkas sebagai teks: header di paket ini
    hampir 1 MB, dan penyisiran sepotong melaporkan berkas HILANG padahal ada -
    tepat kebalikan dari guna pemeriksaan ini.
    """
    with asar.open("rb") as f:
        _, _, _, panjang = struct.unpack("<4I", f.read(16))
        return json.loads(f.read(panjang).decode("utf-8"))


def main() -> int:
    unpacked = Path(sys.argv[1]) if len(sys.argv) > 1 \
        else REPO / "dist-desktop" / "win-unpacked"
    if not unpacked.exists():
        raise SystemExit(f"{unpacked} tidak ada. Jalankan `npm run dist:dir` dulu.")

    lap = cdp.Laporan(f"Uji paket di {unpacked}")
    res = unpacked / "resources"

    # --- 1. Isi paket ----------------------------------------------------
    asar = res / "app.asar"
    if lap.periksa("app.asar ada", asar.exists(),
                   f"{asar.stat().st_size // 1024} KB" if asar.exists() else ""):
        daftar = isi_asar(asar)["files"]["electron"]["files"]
        for berkas in DI_ASAR:
            ada = berkas in daftar
            lap.periksa(f"electron/{berkas} terkemas",
                        ada, f"{daftar[berkas].get('size')} byte" if ada else
                        "TIDAK ADA di daftar isi asar")
    for jalur in DI_LUAR_ASAR:
        lap.periksa(f"{jalur} di luar asar", (res / jalur).exists())

    # --- 2. Jalankan paketnya --------------------------------------------
    exe = unpacked / "Padelin.exe"
    port = cdp.free_port()
    userdata = Path(tempfile.mkdtemp(prefix="padelin-pakettest-"))
    try:
        proc = subprocess.Popen(
            [str(exe), f"--remote-debugging-port={port}",
             f"--user-data-dir={userdata}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except OSError as exc:
        lap.lewati(
            "paket jalan",
            f"{exc.strerror or exc} - biner baru tanpa tanda tangan bisa ditolak "
            "Application Control. Jalankan installernya sekali dengan tangan.")
        return lap.selesai()

    jendela = []
    try:
        sesi = cdp.Proses(port, proc)
        app = sesi.tempel(
            lambda u: u.startswith("http://127.0.0.1:") and "/api/" not in u,
            label="jendela aplikasi paket")
        jendela.append(app)
        lap.periksa("paket jalan dan memuat server bundel", True, app.url)
        # Integritas asar ditegakkan saat kode dimuat dari arsip: kalau nilainya
        # tidak cocok, prosesnya mati sebelum ada jendela - jadi sampai di sini
        # artinya lolos.
        lap.periksa("integritas asar diterima", True)

        app.rekam_galat()
        app.tunggu("document.querySelector('#preset option')", timeout=60,
                   label="aplikasi paket siap")
        lap.periksa("server Python bundel menjawab", True)

        roster = "\n".join(f"Pemain {i + 1}" for i in range(8))
        app.js("(() => { document.getElementById('bulk').value = "
               + json.dumps(roster)
               + "; document.getElementById('parse-bulk').click(); })(); true")
        app.tunggu("document.querySelectorAll('#ptable tbody tr').length === 8",
                   label="8 baris peserta")
        app.js("document.getElementById('generate').click(); true")
        app.tunggu("document.querySelectorAll('#rounds .round').length > 0",
                   timeout=150, label="jadwal ter-render di paket")
        lap.periksa("Generate jalan (solver Python bundel)", True,
                    f"{app.js('document.querySelectorAll(\"#rounds .round\").length')} ronde")

        app.js("document.getElementById('open-html').click(); true")
        laporan = sesi.tempel(lambda u: "/api/report" in u, label="jendela laporan")
        jendela.append(laporan)
        laporan.tunggu("document.getElementById('pdf')", label="toolbar laporan")
        lap.periksa("preload terbaca DARI asar",
                    laporan.js("typeof window.padelin") == "object")

        laporan.js("document.getElementById('pdf').click(); true")
        pratinjau = sesi.tempel(lambda u: u.startswith("padelin-pratinjau://"),
                                label="jendela pratinjau")
        jendela.append(pratinjau)
        pratinjau.tunggu("document.getElementById('halaman').textContent !== ''",
                         label="info pratinjau")
        halaman = pratinjau.js("document.getElementById('halaman').textContent")
        lap.periksa("pratinjau.html terbaca DARI asar dan terisi",
                    "halaman A4" in halaman, halaman)
        lap.periksa("konsol aplikasi paket bersih", not app.galat_konsol(),
                    "; ".join(app.galat_konsol())[:200])
    except Exception as exc:  # noqa: BLE001 - laporan uji
        lap.periksa("uji paket berjalan sampai selesai", False, str(exc))
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
