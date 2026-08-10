#!/usr/bin/env python3
"""Sapuan regresi: bandingkan kualitas & pemerataan jatah main lintas roster.

Dipakai untuk menilai dampak _balanced_rows(). Jalankan dua kali - sekali pada
kode lama, sekali pada kode baru - lalu bandingkan file hasilnya:

    git stash && python3 -u tools/sapu_merata.py > /tmp/sapu_lama.txt
    git stash pop && python3 -u tools/sapu_merata.py > /tmp/sapu_baru.txt
    python3 -u tools/sapu_merata.py --bandingkan /tmp/sapu_lama.txt /tmp/sapu_baru.txt

Tiap kasus dicatat lengkap dengan parameternya supaya kegagalan bisa diulang
dari log tanpa menebak.
"""
from __future__ import annotations

import itertools
import json
import sys
import time
import traceback

sys.path.insert(0, ".")

from padel_scheduler.models import Config, Player


def lapor(m=""):
    print(m, flush=True)


def bandingkan(f_lama: str, f_baru: str) -> None:
    def muat(p):
        out = {}
        with open(p) as fh:
            for baris in fh:
                if baris.startswith("HASIL "):
                    d = json.loads(baris[6:])
                    out[d["kunci"]] = d
        return out

    lama, baru = muat(f_lama), muat(f_baru)
    sama = sorted(set(lama) & set(baru))
    lapor(f"kasus dibandingkan: {len(sama)} "
          f"(lama {len(lama)}, baru {len(baru)})")
    hilang = sorted((set(lama) | set(baru)) - set(sama))
    for k in hilang:
        lapor(f"  TIDAK ADA DI KEDUANYA: {k}")

    naik = turun = tetap = 0
    d_naik = d_turun = 0
    lapor("")
    lapor("kasus dengan perubahan kualitas:")
    for k in sama:
        a, b = lama[k], baru[k]
        if a.get("error") or b.get("error"):
            lapor(f"  ERROR {k}: lama={a.get('error')} baru={b.get('error')}")
            continue
        d = b["kualitas"] - a["kualitas"]
        if abs(d) < 0.05:
            tetap += 1
            continue
        if d > 0:
            naik += 1
            d_naik += d
        else:
            turun += 1
            d_turun += d
        lapor(f"  {'+' if d > 0 else ''}{d:6.1f}  {k}")
        lapor(f"          kualitas {a['kualitas']:5.1f} -> {b['kualitas']:5.1f}   "
              f"main {a['main_min']}-{a['main_max']} -> {b['main_min']}-{b['main_max']}   "
              f"partner_ulang {a['partner']} -> {b['partner']}   "
              f"lawan_ulang {a['lawan']} -> {b['lawan']}   "
              f"b2b {a['b2b']} -> {b['b2b']}")

    lapor("")
    lapor(f"ringkasan: naik {naik} (total +{d_naik:.1f}), "
          f"turun {turun} (total {d_turun:.1f}), tetap {tetap}")
    if turun:
        lapor("PERHATIAN: ada kasus yang memburuk - periksa daftar di atas.")


if len(sys.argv) > 1 and sys.argv[1] == "--bandingkan":
    bandingkan(sys.argv[2], sys.argv[3])
    sys.exit(0)


# Format yang dibatasi "sesama bentuk" - jalur yang disentuh perbaikan.
SAMA = ["LL-LL", "LP-LP", "PP-PP"]
# Beberapa pembatasan lain, untuk memastikan jalur lain tidak ikut bergeser.
VARIAN_IZIN = {
    "sesama": SAMA,
    "tanpa_LLPP": ["LL-LL", "LL-LP", "LP-LP", "LP-PP", "PP-PP"],
    "semua": None,
}

KASUS = []
for n_pria, n_wanita in [
    (5, 3), (6, 2), (4, 4), (7, 1), (9, 3), (8, 4), (10, 2), (6, 6),
    (11, 5), (13, 3), (12, 4), (14, 6), (3, 5), (2, 6), (5, 11),
]:
    for courts in (1, 2, 3):
        if (n_pria + n_wanita) // 4 < courts:
            continue
        for durasi in (90, 120, 150):
            for nama_izin in VARIAN_IZIN:
                KASUS.append((n_pria, n_wanita, courts, durasi, nama_izin))

lapor(f"total kasus: {len(KASUS)}")
lapor("")

t0 = time.time()
n_ok = n_err = 0
for idx, (n_pria, n_wanita, courts, durasi, nama_izin) in enumerate(KASUS):
    kunci = (f"L{n_pria}P{n_wanita}_c{courts}_d{durasi}_{nama_izin}")
    players = []
    for i in range(n_pria + n_wanita):
        g = "M" if i < n_pria else "F"
        players.append(Player(id=i, name=f"P{i+1}",
                              rating=float(2 + (i % 4)), gender=g))
    cfg = Config(
        courts=courts, duration_minutes=durasi, round_minutes=10,
        warmup_minutes=0, mode="americano", seed=77, effort=20_000,
        attempts=1, allowed_matchups=VARIAN_IZIN[nama_izin],
    )
    try:
        from padel_scheduler.scheduler import build_schedule
        sch = build_schedule(players, cfg)
        plays = sch.stats.plays_per_player
        rec = {
            "kunci": kunci, "pria": n_pria, "wanita": n_wanita,
            "courts": courts, "durasi": durasi, "izin": nama_izin,
            "ronde": sch.stats.rounds,
            "kualitas": sch.stats.quality_score,
            "main_min": min(plays.values()), "main_max": max(plays.values()),
            "partner": sch.stats.partner_repeat_pairs,
            "lawan": sch.stats.opponent_repeat_pairs,
            "b2b": sch.stats.back_to_back_byes,
        }
        n_ok += 1
    except Exception as e:                                  # noqa: BLE001
        rec = {"kunci": kunci, "error": f"{type(e).__name__}: {e}",
               "trace": traceback.format_exc()}
        n_err += 1
        lapor(f"GAGAL {kunci}")
        lapor(traceback.format_exc())
    lapor("HASIL " + json.dumps(rec))
    if (idx + 1) % 25 == 0:
        lapor(f"...{idx+1}/{len(KASUS)}  ok={n_ok} err={n_err}  "
              f"{time.time()-t0:.0f}s")

lapor("")
lapor(f"selesai: {n_ok} ok, {n_err} error, {time.time()-t0:.0f}s")
lapor("TIDAK diuji: mode tiered, partner terkunci, meet bersegmen, "
      "roster tanpa gender - semuanya di luar syarat pakai_kuota.")
