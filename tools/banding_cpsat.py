"""Adu mesin: annealing, solver-sebagai-penyempurna, dan solver-sebagai-dasar.

Dipakai untuk menjawab dua pertanyaan yang tidak punya jawaban tanpa diukur:

  1. apakah angka pengulangan yang dicapai annealing itu memang batasnya, atau
     masih ada yang lebih baik dan kita cuma tidak menemukannya;
  2. berapa sebenarnya sumbangan annealing di mode CP-SAT - dijawab dengan
     menjalankan solver TANPA annealing sama sekali, di setup yang sama.

Tiga mesin yang diadu:

    SA        mode "americano"          annealing saja
    CP-SAT    mode "americano_cpsat"    annealing lalu solver di ujungnya
    DASAR     mode "americano_solver"   solver dari nol, tanpa annealing

    python tools/banding_cpsat.py
    python tools/banding_cpsat.py --detik 60 --seed 1 2 3
    python tools/banding_cpsat.py --mesin SA DASAR      # cuma dua kolom

Yang dicetak: pasang partner berulang, pasang lawan berulang, skor kualitas, dan
waktu - untuk tiap mesin, plus status solver (terbukti optimal atau tidak).
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from padel_scheduler.models import Config, Player, Segment  # noqa: E402
from padel_scheduler.scheduler import build_schedule  # noqa: E402

# Setup yang dipakai di komentar-komentar optimizer.py, plus beberapa bentuk
# lain yang menekan bagian model yang berbeda.
KASUS = [
    ("26 orang / 4 court / bebas", 26, 4, 13, None, None),
    ("16 orang / 4 court / bebas", 16, 4, 12, None, None),
    ("12 orang / 2 court / bebas", 12, 2, 10, None, None),
    ("8 orang / 2 court / bebas", 8, 2, 8, None, None),
    ("16L+10P / 4 court / sesama bentuk", 26, 4, 13, 16,
     ["LL-LL", "LP-LP", "PP-PP"]),
    ("10L+6P / 2 court / sesama bentuk", 16, 2, 12, 10,
     ["LL-LL", "LP-LP", "PP-PP"]),
]


# Mesin yang diadu. SA selalu jadi pembanding: dua kolom lainnya diukur relatif
# terhadapnya, karena itulah yang dipakai host kalau ia tidak menyalakan apa pun.
MESIN = {
    "SA": "americano",
    "CP-SAT": "americano_cpsat",
    "DASAR": "americano_solver",
}

# Awalan catatan jadwal yang memuat status solver, per mode. Dipisah karena
# kalimatnya memang berbeda - lihat _catatan_cpsat dan _catatan_dasar.
_AWALAN_STATUS = ("Mode CP-SAT", "Solver sebagai mesin dasar")


def roster(n: int, pria: int | None, rng: random.Random) -> list[Player]:
    out = []
    for i in range(n):
        gender = None if pria is None else ("M" if i < pria else "F")
        out.append(Player(id=i + 1, name=f"P{i + 1}",
                          rating=round(rng.uniform(2.0, 5.0), 1),
                          gender=gender))
    return out


def jalankan(mode: str, players, courts, rounds, matchups, seed, detik):
    cfg = Config(
        courts=courts,
        duration_minutes=rounds * 12 + 10,
        round_minutes=12,
        mode=mode,
        rounds_override=rounds,
        seed=seed,
        allowed_matchups=matchups,
        segments=[Segment(label="", rounds=rounds, rule="open")],
        cpsat_seconds=detik,
    )
    t0 = time.perf_counter()
    sch = build_schedule(players, cfg)
    return sch, time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detik", type=float, default=30.0,
                    help="batas waktu CP-SAT per jadwal")
    ap.add_argument("--seed", type=int, nargs="+", default=[42],
                    help="seed yang diuji")
    ap.add_argument("--kasus", default="",
                    help="jalankan hanya kasus yang labelnya memuat teks ini")
    ap.add_argument("--mesin", nargs="+", default=list(MESIN),
                    choices=list(MESIN),
                    help="mesin yang diadu (SA selalu jadi pembandingnya)")
    args = ap.parse_args()

    # SA adalah garis dasarnya, jadi ia ikut walau tidak diminta - tanpa itu
    # kolom lain tidak punya apa-apa untuk dibandingkan.
    mesin = ["SA"] + [m for m in args.mesin if m != "SA"]

    kasus = [k for k in KASUS if args.kasus.lower() in k[0].lower()]
    if not kasus:
        print(f"Tidak ada kasus yang cocok dengan {args.kasus!r}. Yang ada:")
        for label, *_ in KASUS:
            print("  -", label)
        return 1

    print(f"{'kasus':<38} {'mesin':<7} {'ptn':>4} {'lwn':>4} {'mutu':>6} "
          f"{'detik':>7}  status")
    print("-" * 96)

    # Per mesin: [menang, kalah, seri] terhadap SA.
    tally = {m: [0, 0, 0] for m in mesin if m != "SA"}
    for label, n, courts, rounds, pria, matchups in kasus:
        for seed in args.seed:
            rng = random.Random(seed)
            players = roster(n, pria, rng)
            baris = {}
            for nama in mesin:
                sch, wall = jalankan(MESIN[nama],
                                     [Player(**vars(p)) for p in players],
                                     courts, rounds, matchups, seed, args.detik)
                s = sch.stats
                status = next((c for c in sch.notes
                               if c.startswith(_AWALAN_STATUS)), "")
                if status:
                    if "TERBUKTI" in status:
                        status = "TERBUKTI OPTIMAL"
                    elif "TIDAK dipakai" in status or "TIDAK sampai" in status:
                        status = "jadwal solver DIBUANG"
                    else:
                        status = "terbaik dalam batas waktu"
                baris[nama] = (s.partner_repeat_pairs, s.opponent_repeat_pairs,
                               s.quality_score, wall, status)
            for nama in mesin:
                ptn, lwn, mutu, wall, status = baris[nama]
                print(f"{label + ' s' + str(seed):<38} {nama:<7} {ptn:>4} "
                      f"{lwn:>4} {mutu:>6.2f} {wall:>7.1f}  {status}")

            def kunci(nama):
                ptn, lwn, mutu, *_ = baris[nama]
                return (ptn, lwn, -mutu)

            for nama in tally:
                if kunci(nama) < kunci("SA"):
                    tally[nama][0] += 1
                elif kunci(nama) > kunci("SA"):
                    tally[nama][1] += 1
                else:
                    tally[nama][2] += 1
            print()

    for nama, (menang, kalah, seri) in tally.items():
        print(f"{nama} lebih baik dari SA di {menang}, lebih buruk di {kalah}, "
              f"sama di {seri} dari {menang + kalah + seri} kasus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
