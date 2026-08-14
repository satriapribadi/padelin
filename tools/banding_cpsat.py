"""Adu mesin: annealing yang sekarang lawan solver CP-SAT, pada setup yang sama.

Dipakai untuk menjawab satu pertanyaan yang selama ini tidak punya jawaban:
apakah angka pengulangan yang dicapai annealing itu memang batasnya, atau masih
ada yang lebih baik dan kita cuma tidak menemukannya.

    python tools/banding_cpsat.py
    python tools/banding_cpsat.py --detik 60 --seed 1 2 3

Yang dicetak: pasang partner berulang, pasang lawan berulang, skor kualitas,
dan waktu - untuk kedua mesin, plus status solver (terbukti optimal atau tidak).
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
    args = ap.parse_args()

    print(f"{'kasus':<38} {'mesin':<7} {'ptn':>4} {'lwn':>4} {'mutu':>6} "
          f"{'detik':>7}  status")
    print("-" * 96)

    menang = kalah = seri = 0
    for label, n, courts, rounds, pria, matchups in KASUS:
        for seed in args.seed:
            rng = random.Random(seed)
            players = roster(n, pria, rng)
            baris = []
            for mode, nama in (("americano", "SA"),
                               ("americano_cpsat", "CP-SAT")):
                sch, wall = jalankan(mode, [Player(**vars(p)) for p in players],
                                     courts, rounds, matchups, seed, args.detik)
                s = sch.stats
                status = next((c for c in sch.notes if c.startswith("Mode CP-SAT")),
                              "")
                if status:
                    status = ("TERBUKTI OPTIMAL" if "TERBUKTI" in status
                              else "terbaik dalam batas waktu")
                baris.append((nama, s.partner_repeat_pairs,
                              s.opponent_repeat_pairs, s.quality_score,
                              wall, status))
            for nama, ptn, lwn, mutu, wall, status in baris:
                print(f"{label + ' s' + str(seed):<38} {nama:<7} {ptn:>4} "
                      f"{lwn:>4} {mutu:>6.2f} {wall:>7.1f}  {status}")
            a, b = baris[0], baris[1]
            kunci_a, kunci_b = (a[1], a[2], -a[3]), (b[1], b[2], -b[3])
            if kunci_b < kunci_a:
                menang += 1
            elif kunci_b > kunci_a:
                kalah += 1
            else:
                seri += 1
            print()

    print(f"CP-SAT lebih baik di {menang}, lebih buruk di {kalah}, "
          f"sama di {seri} dari {menang + kalah + seri} kasus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
