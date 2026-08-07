"""Pembagian tugas untuk peserta yang sedang tidak main.

Ini bukan sekadar pemanis. Kalau host menahan jumlah court demi biaya, akan
selalu ada yang duduk — dan duduk lama itu yang bikin peserta merasa fee-nya
mahal. Memberi peran (wasit, ballboy) mengubah "menunggu" jadi "terlibat".

Contoh nyata: 26 peserta di 4 court berarti 10 orang duduk tiap ronde. Dengan
1 wasit + 1 ballboy per court, 8 dari 10 punya tugas dan hanya 2 yang benar-benar
menganggur.

Pembagiannya dirotasi adil: yang paling jarang kebagian tugas didahulukan, dan
orang yang sama tidak ditumpuk peran yang itu-itu terus.
"""

from __future__ import annotations

import random
from collections import defaultdict


def assign_roles(
    byes_per_round: list[list[int]],
    courts_per_round: list[list[int]],
    referees_per_court: int = 1,
    ballboys_per_court: int = 0,
    rng: random.Random | None = None,
) -> tuple[list[list[tuple[int, str, int]]], dict[int, dict[str, int]]]:
    """Bagikan peran ke pemain yang istirahat.

    Args:
        byes_per_round: id pemain yang istirahat, per ronde.
        courts_per_round: nomor court yang aktif, per ronde.
        referees_per_court: berapa wasit tiap court (0 = nonaktif).
        ballboys_per_court: berapa ballboy tiap court (0 = nonaktif).

    Returns:
        (penugasan_per_ronde, rekap_per_pemain)
        penugasan_per_ronde[r] = list of (player_id, role, court)
    """
    rng = rng or random.Random(0)
    total_counts: dict[int, int] = defaultdict(int)
    role_counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    wanted: list[tuple[str, int]] = []
    assignments: list[list[tuple[int, str, int]]] = []

    for r, resting in enumerate(byes_per_round):
        courts = courts_per_round[r] if r < len(courts_per_round) else []
        # Court diurut supaya wasit dulu semua, baru ballboy: kalau orang yang
        # istirahat tidak cukup, court tetap kebagian wasit lebih dulu.
        wanted = [("wasit", c) for c in courts for _ in range(referees_per_court)]
        wanted += [("ballboy", c) for c in courts for _ in range(ballboys_per_court)]

        available = list(resting)
        round_assign: list[tuple[int, str, int]] = []

        for role, court in wanted:
            if not available:
                break
            # Prioritas: paling sedikit total tugas, lalu paling jarang dapat
            # peran ini, lalu acak (deterministik lewat seed).
            available.sort(
                key=lambda p: (
                    total_counts[p],
                    role_counts[p][role],
                    rng.random(),
                )
            )
            chosen = available.pop(0)
            round_assign.append((chosen, role, court))
            total_counts[chosen] += 1
            role_counts[chosen][role] += 1

        assignments.append(round_assign)

    summary = {
        pid: {"total": total_counts[pid], **dict(role_counts[pid])}
        for pid in total_counts
    }
    return assignments, summary


def coverage_note(
    byes_per_round: list[list[int]],
    courts_per_round: list[list[int]],
    referees_per_court: int,
    ballboys_per_court: int,
) -> str | None:
    """Peringatan kalau jumlah yang istirahat tidak cukup untuk semua tugas."""
    if referees_per_court == 0 and ballboys_per_court == 0:
        return None
    short = 0
    for r, resting in enumerate(byes_per_round):
        courts = len(courts_per_round[r]) if r < len(courts_per_round) else 0
        need = courts * (referees_per_court + ballboys_per_court)
        if len(resting) < need:
            short += 1
    if not short:
        return None
    return (
        f"{short} ronde tidak punya cukup pemain istirahat untuk mengisi semua "
        f"tugas. Wasit diisi lebih dulu, ballboy menyusul kalau ada sisa."
    )
