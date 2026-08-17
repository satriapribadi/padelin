#!/usr/bin/env python3
"""Menempel ke aplikasi Electron yang sudah jalan, lewat DevTools Protocol.

Bedanya dengan Browser di uitest.py: yang itu MENJALANKAN Edge/Chrome sendiri
lalu membuka satu URL, sementara yang ini menempel ke proses yang sudah ada dan
bisa berpindah antar JENDELA. Itu yang dibutuhkan untuk menguji aplikasi
desktop: satu proses, tiga jendela (aplikasi, laporan, pratinjau), dan yang
diuji justru perpindahan di antaranya.

Klien WebSocket-nya TIDAK ditulis ulang - diambil dari uitest.py, yang sudah
memuat satu-satunya implementasi di repo ini (CDP jalan di atas WebSocket, dan
WebSocket tidak ada di stdlib). Arah impornya sengaja begini, bukan sebaliknya:
uitest.py adalah 28 uji yang sudah berjalan, dan tidak ada alasan mengusiknya
demi berkas baru.

Dipakai oleh tools/apptest.py dan tools/pakettest.py.
"""
from __future__ import annotations

import importlib.util
import json
import time
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent


def _muat_uitest():
    spec = importlib.util.spec_from_file_location("uitest", TOOLS / "uitest.py")
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


_uitest = _muat_uitest()
WS = _uitest.WS
free_port = _uitest._free_port


def electron_exe() -> Path:
    """Biner Electron dari node_modules.

    Namanya dibaca dari path.txt yang ditulis `install-electron`, bukan ditebak
    per platform. Kalau berkasnya belum ada, itu gejala yang sudah dikenal:
    sejak Electron 43 paket npm-nya tidak punya postinstall, jadi binernya harus
    diunduh terpisah - `npm run postinstall` melakukannya.
    """
    dasar = REPO / "node_modules" / "electron"
    nama = (dasar / "path.txt").read_text(encoding="utf-8").strip() \
        if (dasar / "path.txt").exists() else "electron.exe"
    exe = dasar / "dist" / nama
    if not exe.exists():
        raise SystemExit(
            f"Biner Electron tidak ada di {exe}.\n"
            "Jalankan: npm run postinstall")
    return exe


class Jendela:
    """Satu target CDP - satu jendela atau satu webContents."""

    def __init__(self, ws_url: str):
        self.ws = WS(ws_url)
        self.msg_id = 0
        self.events: list[dict] = []

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
            # Peristiwa yang datang di antara jawaban disimpan, tidak dibuang:
            # error konsol dan exception justru tiba sebagai peristiwa.
            if "method" in msg:
                self.events.append(msg)

    def js(self, expr: str):
        """Jalankan JS di jendela ini; Promise ditunggu sampai selesai."""
        res = self.call("Runtime.evaluate", expression=expr,
                        awaitPromise=True, returnByValue=True)
        if res.get("exceptionDetails"):
            d = res["exceptionDetails"]
            pesan = d.get("exception", {}).get("description") or d.get("text", "")
            raise RuntimeError(f"JS gagal: {pesan}")
        return res.get("result", {}).get("value")

    def tunggu(self, expr: str, timeout: float = 30.0, label: str = ""):
        batas = time.time() + timeout
        while time.time() < batas:
            if self.js(f"!!({expr})"):
                return True
            time.sleep(0.25)
        raise AssertionError(f"Timeout menunggu: {label or expr}")

    def galat_konsol(self) -> list[str]:
        """Error konsol dan exception yang tercatat sejak Runtime/Log dinyalakan."""
        pesan = []
        for e in self.events:
            if e.get("method") == "Runtime.exceptionThrown":
                d = e["params"]["exceptionDetails"]
                teks = d.get("exception", {}).get("description") or d.get("text", "")
                pesan.append("exception: " + teks[:160])
            elif e.get("method") == "Log.entryAdded":
                entri = e["params"]["entry"]
                if entri.get("level") == "error":
                    pesan.append("log: " + entri.get("text", "")[:160])
        return pesan

    def rekam_galat(self) -> None:
        """Nyalakan pengumpul error. Panggil segera setelah menempel."""
        self.call("Runtime.enable")
        self.call("Log.enable")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001 - penutupan, bukan kode produksi
            pass


class Proses:
    """Daftar jendela pada satu endpoint DevTools."""

    def __init__(self, port: int, proc=None):
        self.port = port
        self.proc = proc

    def targets(self) -> list[dict]:
        raw = urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/json", timeout=3).read()
        return json.loads(raw)

    def tunggu_jendela(self, cocok, timeout: float = 60.0, label: str = "") -> dict:
        batas = time.time() + timeout
        while time.time() < batas:
            # Kalau prosesnya sudah mati, tidak ada gunanya menunggu sampai
            # timeout - dan pesan "keluar dengan kode N" jauh lebih berguna
            # daripada "target tidak muncul".
            if self.proc is not None and self.proc.poll() is not None:
                raise AssertionError(
                    f"aplikasi keluar dengan kode {self.proc.returncode} "
                    f"sebelum {label or 'jendela muncul'}")
            try:
                for t in self.targets():
                    if t.get("webSocketDebuggerUrl") and cocok(t.get("url", "")):
                        return t
            except Exception:  # noqa: BLE001 - endpoint belum siap
                pass
            time.sleep(0.4)
        raise AssertionError(f"Timeout: {label or 'jendela tidak muncul'}")

    def tempel(self, cocok, timeout: float = 60.0, label: str = "") -> Jendela:
        t = self.tunggu_jendela(cocok, timeout, label)
        j = Jendela(t["webSocketDebuggerUrl"])
        j.url = t.get("url", "")
        return j


# --------------------------------------------------------------------------
# Pelaporan, seragam dengan uitest.py
# --------------------------------------------------------------------------
class Laporan:
    def __init__(self, judul: str):
        self.judul = judul
        self.lulus: list[str] = []
        self.gagal: list[tuple[str, str]] = []
        self.dilewati: list[tuple[str, str]] = []
        print(f"{judul}\n")

    def periksa(self, nama: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.lulus.append(nama)
            print(f"  [OK ] {nama}" + (f" - {detail}" if detail else ""))
        else:
            self.gagal.append((nama, detail))
            print(f"  [GAGAL] {nama}" + (f"\n         {detail}" if detail else ""))
        return ok

    def coba(self, nama: str, fn) -> None:
        try:
            self.periksa(nama, True, fn() or "")
        except Exception as exc:  # noqa: BLE001 - laporan uji
            self.gagal.append((nama, str(exc)))
            print(f"  [GAGAL] {nama}\n         {exc}")

    def lewati(self, nama: str, alasan: str) -> None:
        """Dilewati BUKAN lulus: ia disebut di ringkasan supaya tidak terbaca hijau."""
        self.dilewati.append((nama, alasan))
        print(f"  [LEWAT] {nama}\n          {alasan}")

    def selesai(self) -> int:
        print(f"\n{len(self.lulus)} lulus, {len(self.gagal)} gagal"
              + (f", {len(self.dilewati)} dilewati" if self.dilewati else ""))
        for nama, alasan in self.dilewati:
            print(f"  dilewati: {nama} - {alasan}")
        return 1 if self.gagal else 0
