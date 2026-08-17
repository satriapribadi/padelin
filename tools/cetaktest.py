#!/usr/bin/env python3
"""Uji jalur cetak: laporan -> PDF -> jendela pratinjau.

Pemeriksaan statis tidak bisa menjawab pertanyaan yang penting di sini - apakah
halaman PDF-nya benar-benar terender - jadi uji ini menjalankan Electron
sungguhan, mengklik tombol di toolbar laporan, lalu MELIHAT piksel di jendela
pratinjau yang muncul.

Kenapa terpisah dari uitest.py: yang itu menguji aplikasi di browser, dan
seluruh jalur cetak justru yang TIDAK ada di browser - printToPDF(), protokol
padelin-pratinjau://, preload, penampil PDF Chromium.

Pakai:
    python tools/cetaktest.py [--tangkapan out.png]

Berkas laporan contoh dirakit di folder sementara dan dibuang setelahnya; tidak
ada database yang disentuh.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
# tools/ bukan paket (tidak ada __init__.py), dan padel_scheduler diimpor dari
# akar repo - jadi keduanya dimasukkan ke jalur modul.
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO))

import cdp  # noqa: E402 - butuh sys.path di atas


def rakit_laporan(tujuan: Path) -> int:
    """Satu laporan contoh dengan toolbar, cukup panjang untuk lebih dari 1 halaman."""
    from padel_scheduler import Config, Player, build_schedule
    from padel_scheduler.html_report import build_html

    players = [Player(id=i, name=f"Pemain {i + 1}") for i in range(12)]
    sch = build_schedule(players, Config(courts=2, duration_minutes=120))
    html = build_html(sch, title="Uji Cetak", venue="Lapangan Uji")
    tujuan.write_text(html, encoding="utf-8")
    return len(html)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tangkapan", metavar="PNG",
                    help="simpan tangkapan layar jendela pratinjau ke berkas ini")
    args = ap.parse_args()

    exe = cdp.electron_exe()
    with tempfile.TemporaryDirectory(prefix="padelin-cetaktest-") as tmp:
        laporan = Path(tmp) / "laporan.html"
        ukuran = rakit_laporan(laporan)
        print(f"Uji jalur cetak di {exe.name} "
              f"(laporan contoh {ukuran // 1024} KB)\n")

        env = None
        if args.tangkapan:
            import os
            env = {**os.environ,
                   "PADELIN_UJI_TANGKAPAN": str(Path(args.tangkapan).resolve())}

        # Keluarannya DITANGKAP lalu dicetak ulang, bukan diwarisi. Electron di
        # Windows adalah aplikasi subsistem GUI: ia tidak menulis apa pun ke
        # handle stdout yang diwarisi dari proses lain, jadi tanpa pipa ini uji
        # ini melapor "sukses" tanpa satu baris hasil - persis kegagalan yang
        # paling mudah salah dibaca sebagai lulus.
        proc = subprocess.run(
            [str(exe), str(TOOLS / "cetak_e2e.js"), str(REPO), str(laporan)],
            text=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        for baris in (proc.stdout or "").splitlines():
            # Peringatan internal Electron (deprecation, GPU, dsb) tidak
            # menambah apa pun ke laporan uji.
            if baris.startswith("(node:") or baris.startswith("(Use `electron"):
                continue
            print(baris)
        if not (proc.stdout or "").strip():
            print("  [GAGAL] harness tidak mengeluarkan apa pun "
                  f"(kode keluar {proc.returncode})")
            return 1
        return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
