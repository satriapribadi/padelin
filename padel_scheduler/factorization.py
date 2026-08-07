"""Konstruksi eksak pasangan lewat 1-factorization (circle / polygon method).

Ini fondasi mode Americano. Alih-alih menebak pasangan lalu berharap tidak
bentrok, kita bangun langsung struktur yang SECARA MATEMATIS menjamin setiap
kombinasi partner muncul tepat sekali.

Circle method pada graf lengkap K_m (m genap) menghasilkan m-1 ronde, tiap
ronde berisi m/2 pasangan saling lepas, dan gabungan seluruh ronde mencakup
seluruh C(m,2) pasangan tepat satu kali.

Cara kerjanya: satu pemain dipaku (fixed), sisanya diputar seperti jarum jam.

    fixed ->  o
             / \\
       0 -- o   o -- 4      tiap ronde lingkaran diputar satu langkah,
            |   |           lalu pasangan diambil dari titik yang berhadapan
       1 -- o   o -- 3
             \\ /
              o
              2

Untuk jumlah pemain ganjil, kita tambahkan "pemain hantu". Siapa pun yang
kebagian berpasangan dengan hantu otomatis dapat bye ronde itu, dan karena
rotasinya seragam, giliran bye tersebar merata dengan sendirinya.
"""

from __future__ import annotations


def one_factorization(m: int) -> list[list[tuple[int, int]]]:
    """Hasilkan m-1 ronde pasangan sempurna untuk m peserta (m genap).

    Return: list ronde; tiap ronde adalah list of (a, b) dengan a < b.
    Setiap pasangan (a, b) muncul tepat sekali di seluruh hasil.
    """
    if m < 2:
        return []
    if m % 2 != 0:
        raise ValueError("one_factorization butuh jumlah genap; pakai pad_to_even().")

    fixed = m - 1
    rotating = list(range(m - 1))
    n_rot = m - 1
    rounds: list[list[tuple[int, int]]] = []

    for r in range(m - 1):
        pairs: list[tuple[int, int]] = []
        # Pemain yang dipaku selalu berpasangan dengan satu titik rotasi.
        a, b = fixed, rotating[r % n_rot]
        pairs.append((min(a, b), max(a, b)))
        # Sisanya diambil berpasangan dari kedua sisi titik tersebut.
        for i in range(1, m // 2):
            x = rotating[(r + i) % n_rot]
            y = rotating[(r - i) % n_rot]
            pairs.append((min(x, y), max(x, y)))
        rounds.append(pairs)

    return rounds


def pad_to_even(n: int) -> tuple[int, int | None]:
    """Kembalikan (ukuran_genap, id_hantu). id_hantu None kalau n sudah genap."""
    if n % 2 == 0:
        return n, None
    return n + 1, n


def partner_rounds(n_players: int) -> list[list[tuple[int, int]]]:
    """Ronde pasangan untuk n pemain nyata (0..n-1), hantu sudah dibuang.

    Ronde hasilnya bisa berisi kurang dari n/2 pasangan kalau n ganjil —
    pemain yang hilang dari suatu ronde berarti kena bye.
    """
    m, ghost = pad_to_even(n_players)
    raw = one_factorization(m)
    if ghost is None:
        return raw

    cleaned: list[list[tuple[int, int]]] = []
    for rnd in raw:
        cleaned.append([(a, b) for (a, b) in rnd if a != ghost and b != ghost])
    return cleaned


def mixed_pair_rounds(
    group_a: list[int], group_b: list[int]
) -> list[list[tuple[int, int]]]:
    """Pasangan lintas gender lewat rotasi Latin square.

    Untuk babak mixed, partner wajib 1 putra + 1 putri, jadi 1-factorization
    tidak berlaku — yang berlaku adalah pewarnaan sisi pada graf bipartit.
    Konstruksinya: ronde r memasangkan a[i] dengan b[(i + r) mod |b|].

    Menghasilkan |b| ronde berisi |a| pasangan, dan seluruh |a| x |b| kombinasi
    putra-putri muncul tepat sekali.
    """
    if not group_a or not group_b:
        return []
    # Grup kecil jadi jangkar, grup besar yang berputar.
    small, large = (group_a, group_b) if len(group_a) <= len(group_b) else (group_b, group_a)
    n_large = len(large)

    rounds: list[list[tuple[int, int]]] = []
    for r in range(n_large):
        pairs = []
        for i, p in enumerate(small):
            q = large[(i + r) % n_large]
            pairs.append((min(p, q), max(p, q)))
        rounds.append(pairs)
    return rounds


def subset_pair_rounds(members: list[int]) -> list[list[tuple[int, int]]]:
    """1-factorization pada subset pemain sembarang (mis. hanya putra).

    Indeks internal 0..k-1 dipetakan balik ke id pemain asli.
    """
    k = len(members)
    if k < 2:
        return []
    local = partner_rounds(k)
    return [
        [(min(members[a], members[b]), max(members[a], members[b])) for a, b in rnd]
        for rnd in local
    ]


def verify_one_factorization(n_players: int) -> tuple[bool, str]:
    """Sanity check: tiap pasangan muncul tepat sekali, tiap pemain sekali/ronde."""
    rounds = partner_rounds(n_players)
    seen: dict[tuple[int, int], int] = {}

    for idx, rnd in enumerate(rounds):
        used: set[int] = set()
        for a, b in rnd:
            if a in used or b in used:
                return False, f"Pemain dobel di ronde {idx}: ({a},{b})"
            used.add(a)
            used.add(b)
            seen[(a, b)] = seen.get((a, b), 0) + 1

    expected = n_players * (n_players - 1) // 2
    dupes = [p for p, c in seen.items() if c > 1]
    if dupes:
        return False, f"Pasangan berulang: {dupes[:5]}"
    if len(seen) != expected:
        return False, f"Cakupan kurang: {len(seen)} dari {expected} pasangan"
    return True, f"OK: {len(rounds)} ronde, {len(seen)} pasangan unik"
