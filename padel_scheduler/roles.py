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

    _rebalance(assignments, byes_per_round)

    total_counts.clear()
    role_counts.clear()
    for row in assignments:
        for pid, role, _court in row:
            total_counts[pid] += 1
            role_counts[pid][role] += 1

    summary = {
        pid: {"total": total_counts[pid], **dict(role_counts[pid])}
        for pid in total_counts
    }
    return assignments, summary


def _rebalance(assignments, byes_per_round, max_steps: int = 4000) -> int:
    """Ratakan tugas setelah pembagian greedy.

    Greedy per ronde hanya melihat keadaan saat itu, jadi hasilnya bisa timpang:
    terukur wasit 0 sampai 3 kali padahal idealnya 2 rata. Ini pass perbaikannya,
    memakai dua jenis pertukaran:

      1. Pindahkan satu tugas ke peserta lain yang sedang duduk dan belum
         bertugas ronde itu - memperbaiki total sekaligus per-peran.
      2. Tukar peran antar dua petugas di ronde yang sama (wasit <-> ballboy) -
         total tidak berubah, tapi ketimpangan per-peran hilang.

    Sasarannya jumlah kuadrat: total tiap orang, ditambah hitungan tiap peran.
    Tiap pertukaran menurunkan nilai itu, jadi prosesnya pasti berhenti.
    """
    totals: dict[int, int] = defaultdict(int)
    per_role: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for row in assignments:
        for pid, role, _court in row:
            totals[pid] += 1
            per_role[role][pid] += 1

    # Keadaan terbaik yang pernah dilihat, untuk dikembalikan di akhir.
    #
    # Gerakan lokal (1 dan 2) selalu MENURUNKAN sasaran, jadi sendirian ia pasti
    # berhenti. Tapi rantai pemecah kebuntuan bisa MENAIKKANNYA - itu memang
    # gunanya, keluar dari optimum lokal - dan akibatnya keduanya bisa
    # berputar-putar. Terukur: jatah 4000 langkah habis, dan hasil yang
    # terpakai kebetulan bukan yang terbaik yang sempat dilewati; pada 26
    # peserta di 4 court itu berarti 3/4/5 tugas padahal 4 rata mungkin, di
    # separuh seed yang dicoba.
    #
    # Jadi yang terbaik disimpan, bukan yang terakhir.
    def nilai():
        """Makin kecil makin rata. Selisih total didahulukan karena itu yang
        dirasakan peserta: berapa ronde ia duduk tanpa melakukan apa-apa."""
        t = list(totals.values()) or [0]
        selisih_peran = max(
            (max(c.values()) - min(c.values()) for c in per_role.values() if c),
            default=0)
        return (max(t) - min(t), selisih_peran,
                sum(v * v for v in t)
                + sum(v * v for c in per_role.values() for v in c.values()))

    terbaik = nilai()
    salinan = [list(row) for row in assignments]

    steps = 0
    improved = True
    while improved and steps < max_steps:
        improved = False

        for r, row in enumerate(assignments):
            if not row:
                continue
            busy = {pid for pid, _, _ in row}
            free = [p for p in byes_per_round[r] if p not in busy]

            # 1. Serahkan tugas ke peserta yang lebih jarang kebagian.
            for idx, (hi, role, court) in enumerate(row):
                target = None
                for lo in free:
                    delta = (2 * (totals[lo] - totals[hi] + 1)
                             + 2 * (per_role[role][lo] - per_role[role][hi] + 1))
                    if delta < 0:
                        target = lo
                        break
                if target is None:
                    continue
                row[idx] = (target, role, court)
                totals[hi] -= 1
                totals[target] += 1
                per_role[role][hi] -= 1
                per_role[role][target] += 1
                free.remove(target)
                free.append(hi)
                improved = True
                steps += 1

            # 2. Tukar peran antar petugas di ronde ini.
            for i in range(len(row)):
                for j in range(i + 1, len(row)):
                    a, role_a, court_a = row[i]
                    b, role_b, court_b = row[j]
                    if role_a == role_b:
                        continue
                    delta = (2 * (per_role[role_a][b] - per_role[role_a][a] + 1)
                             + 2 * (per_role[role_b][a] - per_role[role_b][b] + 1))
                    if delta >= 0:
                        continue
                    # Slot tugas ditukar UTUH (peran + court). Kalau hanya
                    # perannya yang ditukar, satu court bisa berakhir punya dua
                    # ballboy dan tanpa wasit sama sekali.
                    row[i] = (a, role_b, court_b)
                    row[j] = (b, role_a, court_a)
                    per_role[role_a][a] -= 1
                    per_role[role_a][b] += 1
                    per_role[role_b][b] -= 1
                    per_role[role_b][a] += 1
                    improved = True
                    steps += 1

        sekarang = nilai()
        if sekarang < terbaik:
            terbaik = sekarang
            salinan = [list(row) for row in assignments]
        # Sudah serata yang mungkin - selisih 1 hanya muncul kalau jumlah tugas
        # memang tidak habis dibagi jumlah peserta. Tidak ada gunanya melanjutkan.
        if terbaik[0] <= 1 and terbaik[1] <= 1:
            break

        # Selisih TOTAL didahulukan. Sebelumnya rantai per-peran yang dicoba
        # lebih dulu, dan karena ia hampir selalu menemukan sesuatu, rantai
        # total tidak pernah kebagian giliran sampai jatah langkah habis -
        # ketimpangan yang paling dirasakan peserta justru yang tidak pernah
        # ditangani. Peserta merasakan "saya duduk dua ronde tanpa apa-apa",
        # bukan "wasit saya satu lebih banyak dari ballboy".
        if not improved:
            improved = _chain_total(assignments, byes_per_round, totals, per_role)
            steps += 1 if improved else 0
        if not improved:
            improved = _chain_fix(assignments, per_role)
            steps += 1 if improved else 0

    if nilai() > terbaik:
        assignments[:] = [list(row) for row in salinan]

    return steps


def _chain_total(assignments, byes_per_round, totals, per_role) -> bool:
    """Ratakan TOTAL tugas lewat perantara.

    _chain_fix mengurus ketimpangan per-peran; ini mengurus ketimpangan
    totalnya, dan keduanya tidak saling menggantikan. Terukur pada 26 peserta di
    4 court: 13 ronde x 4 court x 2 peran = 104 tugas untuk 26 orang, yaitu
    tepat 4 masing-masing - tapi hasilnya 3, 4, dan 5. Satu orang jadi punya dua
    ronde menganggur sementara yang lain nol.

    Pemindahan langsung sering mustahil di situ: orang yang kelebihan tugas
    justru bertugas di SETIAP ronde ia duduk, jadi tidak pernah ada ronde tempat
    ia bertugas sementara si kekurangan sedang menganggur. Perantara memutus
    kebuntuan itu - hi menyerahkan tugas ke m di satu ronde, m menyerahkan tugas
    ke lo di ronde lain. Hitungan m kembali seperti semula, hi berkurang satu,
    lo bertambah satu.
    """
    pemain = list(totals)
    if not pemain:
        return False
    hi = max(pemain, key=lambda p: totals[p])
    lo = min(pemain, key=lambda p: totals[p])
    if totals[hi] - totals[lo] < 2:
        return False

    for r1, row1 in enumerate(assignments):
        hi_idx = next((i for i, (p, _, _) in enumerate(row1) if p == hi), None)
        if hi_idx is None:
            continue
        sibuk1 = {p for p, _, _ in row1}
        for m in byes_per_round[r1]:
            # m harus benar-benar menganggur di r1, dan bukan hi/lo sendiri -
            # kalau m = lo, tambahannya di r1 dibatalkan lagi di r2.
            if m in sibuk1 or m == hi or m == lo:
                continue
            for r2, row2 in enumerate(assignments):
                if r2 == r1:
                    continue
                m_idx = next((i for i, (p, _, _) in enumerate(row2) if p == m), None)
                if m_idx is None:
                    continue
                sibuk2 = {p for p, _, _ in row2}
                if lo in sibuk2 or lo not in byes_per_round[r2]:
                    continue

                _, peran1, court1 = row1[hi_idx]
                row1[hi_idx] = (m, peran1, court1)
                _, peran2, court2 = row2[m_idx]
                row2[m_idx] = (lo, peran2, court2)

                totals[hi] -= 1
                totals[lo] += 1
                per_role[peran1][hi] -= 1
                per_role[peran1][m] += 1
                per_role[peran2][m] -= 1
                per_role[peran2][lo] += 1
                return True
    return False


def _chain_fix(assignments, per_role) -> bool:
    """Keluar dari optimum lokal lewat rantai dua langkah.

    Kalau semua yang istirahat selalu bertugas, satu-satunya gerakan adalah
    tukar peran di ronde yang sama - dan itu mentok kalau pemain yang kelebihan
    peran X tidak pernah seronde dengan pemain yang kekurangan peran X.

    Jalan keluarnya lewat perantara: hi menyerahkan X ke m di satu ronde, lalu m
    menyerahkan X ke lo di ronde lain. Hitungan m kembali seperti semula, hi
    berkurang satu, lo bertambah satu.
    """
    roles = list(per_role)
    for role in roles:
        counts = per_role[role]
        if not counts:
            continue
        players = list(counts)
        hi = max(players, key=lambda p: counts[p])
        lo = min(players, key=lambda p: counts[p])
        if counts[hi] - counts[lo] < 2:
            continue
        other = [x for x in roles if x != role]
        if not other:
            continue

        # Langkah 1: ronde tempat hi memegang `role` dan m memegang peran lain.
        for r1, row1 in enumerate(assignments):
            hi_idx = next((i for i, (p, ro, _) in enumerate(row1)
                           if p == hi and ro == role), None)
            if hi_idx is None:
                continue
            for m_idx, (m, m_role, m_court) in enumerate(row1):
                if m == hi or m_role == role:
                    continue
                # Langkah 2: ronde tempat m memegang `role` dan lo memegang lainnya.
                for r2, row2 in enumerate(assignments):
                    if r2 == r1:
                        continue
                    m2 = next((i for i, (p, ro, _) in enumerate(row2)
                               if p == m and ro == role), None)
                    lo2 = next((i for i, (p, ro, _) in enumerate(row2)
                                if p == lo and ro != role), None)
                    if m2 is None or lo2 is None:
                        continue

                    # Sama seperti di atas: yang berpindah adalah slot tugas
                    # utuh, supaya tiap court tetap punya satu wasit satu ballboy.
                    hp, _hr, hc = row1[hi_idx]
                    row1[hi_idx] = (hp, m_role, m_court)
                    row1[m_idx] = (m, role, hc)
                    mp, _mr, mc = row2[m2]
                    lp, lr, lc = row2[lo2]
                    row2[m2] = (mp, lr, lc)
                    row2[lo2] = (lp, role, mc)

                    per_role[role][hi] -= 1
                    per_role[m_role][hi] += 1
                    per_role[m_role][m] -= 1
                    per_role[role][lo] += 1
                    per_role[lr][lo] -= 1
                    per_role[lr][m] += 1
                    return True
    return False


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
