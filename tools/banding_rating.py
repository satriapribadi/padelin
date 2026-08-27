#!/usr/bin/env python3
"""Adu mode: Americano biasa, Americano + tim sepadan, dan Mexicano.

Pertanyaan yang dijawab alat ini cuma satu, dan ia tidak bisa dijawab tanpa
diukur: apakah tahap penyeimbang rating benar-benar menyepadankan tim TANPA
membayarnya dengan keunikan atau giliran. Yang dicetak berpasangan karena itu -
angka rating di sebelah angka pengulangan, untuk setup yang sama persis.

    python tools/banding_rating.py
    python tools/banding_rating.py --seed 1 2 3 4 5
    python tools/banding_rating.py --roster elo          # skala Elo, bukan 1-7
    python tools/banding_rating.py --anggaran 400 1200   # sapu TAHAP_RATING

Kolom:
    ptn/lwn   pasang partner & lawan yang berulang (makin kecil makin baik)
    mutu      skor kualitas 0-100 keluaran penjadwal
    selisih   selisih total rating antar tim dalam satu match: rata-rata / terburuk
    sebaran   jarak rating terjauh di antara 4 orang satu court: rata2 / terburuk

Yang harus dibaca bersamaan: kolom ptn/lwn/mutu mode "sepadan" WAJIB sama
dengan Americano di baris yang sama. Kalau tidak, pagar di anneal_rating bocor
dan itu bug, bukan trade-off.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from padel_scheduler import optimizer
from padel_scheduler.models import Config, Player
from padel_scheduler.scheduler import build_schedule

# Roster uji: rentang rating yang berbeda-beda, karena bobot tahap penyeimbang
# DISETEL terhadap rentang itu. Skala Elo ada di sini bukan sebagai hiasan - ia
# yang membuktikan penyetelannya bekerja: tanpa itu, bobot yang pas untuk skala
# 1-7 akan meledak seribu kali lipat.
ROSTER = {
    "lebar": (1.5, 6.5),
    "sempit": (4.0, 5.0),
    "elo": (1200.0, 1800.0),
}

SETUP = [
    # (n_pemain, court, durasi menit, menit per ronde)
    (8, 1, 120, 12),
    (12, 2, 120, 12),
    (16, 3, 150, 12),
    (20, 4, 120, 12),
    (24, 4, 150, 12),
    (26, 4, 180, 12),
]


def buat_pemain(n: int, lo: float, hi: float, seed: int) -> list[Player]:
    rng = random.Random(1000 + seed)
    return [
        Player(id=i, name=f"P{i + 1}", rating=round(rng.uniform(lo, hi), 1))
        for i in range(n)
    ]


def ukur_rating(sch) -> tuple[float, float, float, float]:
    """(selisih rata2, selisih terburuk, sebaran rata2, sebaran terburuk)."""
    r = {p.id: p.rating for p in sch.players}
    selisih, sebaran = [], []
    for rnd in sch.rounds:
        for m in rnd.matches:
            a, b = m.team_a
            c, d = m.team_b
            selisih.append(abs((r[a] + r[b]) - (r[c] + r[d])))
            nilai = [r[a], r[b], r[c], r[d]]
            sebaran.append(max(nilai) - min(nilai))
    if not selisih:
        return 0.0, 0.0, 0.0, 0.0
    return (statistics.fmean(selisih), max(selisih),
            statistics.fmean(sebaran), max(sebaran))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--effort", type=int, default=30_000)
    ap.add_argument("--roster", nargs="+", default=["lebar"])
    ap.add_argument("--anggaran", type=float, nargs="+", default=[None],
                    help="TAHAP_RATING yang diadu; kosong = pakai bawaan")
    args = ap.parse_args()

    kolom: list[tuple[str, str, float | None]] = [("americano", "americano", None)]
    for a in args.anggaran:
        nama = "sepadan" if a is None else f"sepadan:{a:g}"
        kolom.append((nama, "americano_rating", a))
    kolom.append(("mexicano", "mexicano", None))

    total: dict[str, list] = {k[0]: [] for k in kolom}
    bawaan = optimizer.TAHAP_RATING

    for nama_roster in args.roster:
        lo, hi = ROSTER[nama_roster]
        for setup in SETUP:
            n, courts, durasi, rm = setup
            print(f"\n=== roster {nama_roster} ({lo}-{hi}) | {n} orang / "
                  f"{courts} court / {durasi} menit")
            print(f"{'mesin':<18}{'ptn':>4}{'lwn':>5}{'mutu':>7}"
                  f"{'selisih (rata/max)':>22}{'sebaran (rata/max)':>22}{'detik':>7}")
            for seed in args.seed:
                players = buat_pemain(n, lo, hi, seed)
                for label, mode, anggaran in kolom:
                    optimizer.TAHAP_RATING = (bawaan if anggaran is None
                                              else anggaran)
                    cfg = Config(courts=courts, duration_minutes=durasi,
                                 round_minutes=rm, warmup_minutes=0, mode=mode,
                                 seed=seed, effort=args.effort, attempts=1)
                    t0 = time.time()
                    sch = build_schedule(players, cfg)
                    detik = time.time() - t0
                    st = sch.stats
                    sr, sm, br, bm = ukur_rating(sch)
                    total[label].append((st.partner_repeat_pairs,
                                         st.opponent_repeat_pairs,
                                         st.quality_score, sr, sm, br, bm))
                    print(f"{label + ' s' + str(seed):<18}"
                          f"{st.partner_repeat_pairs:>4}"
                          f"{st.opponent_repeat_pairs:>5}{st.quality_score:>7.1f}"
                          f"{sr:>13.2f} /{sm:>7.2f}"
                          f"{br:>13.2f} /{bm:>7.2f}{detik:>7.1f}")

    optimizer.TAHAP_RATING = bawaan
    print("\n=== RATA-RATA SELURUH KASUS")
    print(f"{'mesin':<18}{'ptn':>6}{'lwn':>7}{'mutu':>7}"
          f"{'selisih':>10}{'max':>8}{'sebaran':>10}{'max':>8}{'membaik':>9}")
    acuan = total[kolom[0][0]]
    for label, _mode, _a in kolom:
        v = total[label]
        rata = [statistics.fmean(x[i] for x in v) for i in range(7)]
        turun = sum(1 for x, y in zip(v, acuan) if x[3] < y[3] - 1e-9)
        print(f"{label:<18}{rata[0]:>6.2f}{rata[1]:>7.2f}{rata[2]:>7.2f}"
              f"{rata[3]:>10.2f}{rata[4]:>8.2f}{rata[5]:>10.2f}{rata[6]:>8.2f}"
              f"{turun:>6}/{len(v)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
