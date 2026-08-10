"""Perakit jadwal: menyatukan konstruksi eksak, aturan segmen, dan optimizer.

Pembagian tanggung jawab:

  Segmen  -> menentukan SIAPA yang boleh turun & batasan pasangan (putra/putri/mixed)
  Mode    -> menentukan BAGAIMANA pasangan dibentuk & dinilai (americano/tiered/
             mexicano/team)
  Optimizer -> merapikan lawan, istirahat, dan keseimbangan rating

Keunikan partner & lawan dihitung LINTAS segmen: orang yang sudah jadi lawanmu
di babak putra akan dihindari lagi di babak mixed selama masih memungkinkan.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from itertools import combinations

from .capacity import analyze, rounds_from_duration, shape_budget, shape_totals
from .factorization import mixed_pair_rounds, subset_pair_rounds
from .models import (
    MATCHUP_LABELS,
    MATCHUPS,
    TEAM_SHAPES,
    Config,
    Match,
    PairStat,
    Player,
    PreferenceViolation,
    RoleAssignment,
    Round,
    Schedule,
    ScheduleStats,
    Segment,
    matchup_code,
    team_shape,
)
from .optimizer import (
    Rules,
    ScheduleState,
    Weights,
    anneal,
    play_counts,
    polish_pairs,
    rebalance_plays,
)
from .roles import assign_roles, coverage_note


class ScheduleError(ValueError):
    """Setup yang mustahil dijadwalkan (bukan sekadar kurang optimal)."""


# ---------------------------------------------------------------------------
# Persiapan
# ---------------------------------------------------------------------------

def _eligible_for(rule: str, players: list[Player]) -> list[int]:
    if rule == "men":
        return [p.id for p in players if p.gender == "M"]
    if rule == "women":
        return [p.id for p in players if p.gender == "F"]
    return [p.id for p in players]


def _validate(players: list[Player], config: Config, segments: list[Segment]) -> None:
    if len(players) < 4:
        raise ScheduleError(
            f"Butuh minimal 4 pemain, sekarang {len(players)}."
        )
    if len(players) != len({p.id for p in players}):
        raise ScheduleError("Ada id pemain yang dobel.")

    men = [p for p in players if p.gender == "M"]
    women = [p for p in players if p.gender == "F"]

    for seg in segments:
        if seg.rounds == 0:
            continue
        if seg.rule == "men" and len(men) < 4:
            raise ScheduleError(
                f"Segmen '{seg.label}' butuh minimal 4 pemain putra, "
                f"yang terdaftar {len(men)}."
            )
        if seg.rule == "women" and len(women) < 4:
            raise ScheduleError(
                f"Segmen '{seg.label}' butuh minimal 4 pemain putri, "
                f"yang terdaftar {len(women)}."
            )
        if seg.rule == "mixed" and (len(men) < 2 or len(women) < 2):
            raise ScheduleError(
                f"Segmen '{seg.label}' (mixed) butuh minimal 2 putra dan 2 putri, "
                f"yang terdaftar {len(men)} putra dan {len(women)} putri."
            )
        if seg.rule == "same_gender" and (len(men) < 4 and len(women) < 4):
            raise ScheduleError(
                f"Segmen '{seg.label}' butuh minimal satu gender dengan 4 pemain."
            )

    if config.mode == "team":
        unpaired = [p.name for p in players if p.partner_id is None]
        if unpaired:
            raise ScheduleError(
                "Mode team butuh semua pemain punya rekan tetap. Belum berpasangan: "
                + ", ".join(unpaired[:6])
            )


def _resolve_segments(config: Config) -> list[Segment]:
    """Segmen eksplisit, atau satu babak tunggal dari hitungan durasi."""
    segs = [s for s in config.segments if s.rounds > 0]
    if segs:
        return segs
    n_rounds = (
        config.rounds_override
        if config.rounds_override is not None
        else rounds_from_duration(
            config.duration_minutes, config.round_minutes, config.warmup_minutes
        )
    )
    return [Segment(label="", rounds=max(0, n_rounds), rule="open")]


def _resolve_round_minutes(config: Config, total_rounds: int) -> int:
    """Kalau host menetapkan jumlah ronde, durasi per ronde menyesuaikan jam sewa."""
    if not config.fit_rounds_to_duration or total_rounds <= 0:
        return config.round_minutes
    usable = config.duration_minutes - config.warmup_minutes
    if usable <= 0:
        return config.round_minutes
    return max(1, usable // total_rounds)


def round_plan(segments: list[Segment],
               interleave: bool) -> list[tuple[Segment, int]]:
    """Urutan babak per ronde: (segmen, nomor ronde ke berapa di segmen itu).

    Tanpa selang-seling, tiap babak berjalan sebagai blok berurutan. Itu masalah
    kalau dua babak memakai orang yang berbeda: "Putri 4" lalu "Putra 4" berarti
    para putri main 4 ronde beruntun sementara para putra duduk 4 ronde beruntun,
    lalu bertukar. Melelahkan buat yang main, membosankan buat yang menunggu.

    Dengan selang-seling, ronde tiap babak disebar merata sepanjang acara. Tiap
    kemunculan ke-k dari babak bercount c ditaruh di posisi (k + 0.5) / c, lalu
    semuanya diurutkan. Hasilnya untuk 4 putri + 4 putra adalah P L P L P L P L,
    dan untuk 3 putra + 3 putri + 6 mixed adalah M Pa Pi M M Pa Pi M M Pa Pi M.

    Urutan babak tetap menjadi pemutus seri, jadi babak yang ditaruh lebih dulu
    tetap main lebih dulu.
    """
    if not interleave:
        return [(seg, i) for seg in segments for i in range(seg.rounds)]

    slots: list[tuple[float, int, Segment, int]] = []
    for order, seg in enumerate(segments):
        for k in range(seg.rounds):
            slots.append(((k + 0.5) / seg.rounds, order, seg, k))
    slots.sort(key=lambda s: (s[0], s[1]))
    return [(seg, k) for _pos, _order, seg, k in slots]


def _assign_tiers(players: list[Player], tier_count: int) -> dict[int, int]:
    """Bagi pemain jadi pool rating berukuran kelipatan 4 sebisa mungkin."""
    ordered = sorted(players, key=lambda p: (-p.rating, p.id))
    n = len(ordered)
    tier_count = max(1, min(tier_count, n // 4))
    if tier_count <= 1:
        return {p.id: 0 for p in ordered}

    # Ukuran dasar tiap pool, dibulatkan ke kelipatan 4 agar court terisi penuh.
    base = (n // tier_count) // 4 * 4
    sizes = [max(4, base)] * tier_count
    leftover = n - sum(sizes)
    i = 0
    while leftover > 0:
        sizes[i % tier_count] += 1
        leftover -= 1
        i += 1
    while leftover < 0:
        if sizes[i % tier_count] > 4:
            sizes[i % tier_count] -= 1
            leftover += 1
        i += 1

    tier_of: dict[int, int] = {}
    idx = 0
    for t, size in enumerate(sizes):
        for p in ordered[idx: idx + size]:
            tier_of[p.id] = t
        idx += size
    for p in ordered[idx:]:
        tier_of[p.id] = tier_count - 1
    return tier_of


# ---------------------------------------------------------------------------
# Kandidat pasangan per segmen
# ---------------------------------------------------------------------------

def _free_pair_rounds(
    members: list[int], locked: dict[int, int]
) -> list[list[tuple[int, int]]]:
    """Pasangan calon untuk sekelompok pemain, menghormati partner terkunci.

    Peserta yang minta partner tetap muncul sebagai tim baku di SETIAP ronde;
    sisanya dirotasi lewat 1-factorization seperti Americano biasa. Jadi satu
    meet bisa mencampur "pasangan tetap" dan "rotasi bebas" sekaligus.
    """
    member_set = set(members)
    fixed: set[tuple[int, int]] = set()
    free: list[int] = []
    for pid in members:
        mate = locked.get(pid)
        if mate is not None and mate in member_set:
            fixed.add((min(pid, mate), max(pid, mate)))
        else:
            free.append(pid)

    free_rounds = subset_pair_rounds(free) if len(free) >= 2 else []
    if not free_rounds:
        return [sorted(fixed)] if fixed else []

    return [sorted(fixed) + list(rnd) for rnd in free_rounds]


def _candidate_rounds(
    seg: Segment,
    players: list[Player],
    config: Config,
    tier_of: dict[int, int] | None,
    locked: dict[int, int],
) -> list[list[tuple[int, int]]]:
    """Daftar pasangan calon per ronde, sesuai aturan segmen + mode."""
    by_id = {p.id: p for p in players}
    eligible = _eligible_for(seg.rule, players)

    if seg.rule == "mixed":
        men = [p for p in eligible if by_id[p].gender == "M"]
        women = [p for p in eligible if by_id[p].gender == "F"]
        # Pasangan putra-putri yang dikunci tetap menempel sepanjang babak;
        # sisanya dirotasi Latin square seperti biasa. Tanpa ini konstruksi awal
        # sudah melanggar kunci, dan annealing tidak bisa memperbaikinya:
        # gerakannya per-ronde dan ronde yang lahir ilegal tidak punya jalan
        # keluar yang legal.
        fixed: list[tuple[int, int]] = []
        free_men, free_women = [], []
        women_set = set(women)
        for pid in men:
            mate = locked.get(pid)
            if mate is not None and mate in women_set:
                fixed.append((min(pid, mate), max(pid, mate)))
            else:
                free_men.append(pid)
        paired = {p for pr in fixed for p in pr}
        free_women = [p for p in women if p not in paired]
        if not fixed:
            return mixed_pair_rounds(men, women)
        rest = mixed_pair_rounds(free_men, free_women)
        if not rest:
            return [sorted(fixed)]
        return [sorted(fixed) + list(rnd) for rnd in rest]

    if seg.rule == "same_gender":
        men = [p for p in eligible if by_id[p].gender == "M"]
        women = [p for p in eligible if by_id[p].gender == "F"]
        rm = _free_pair_rounds(men, locked) if len(men) >= 2 else []
        rw = _free_pair_rounds(women, locked) if len(women) >= 2 else []
        n = max(len(rm), len(rw))
        merged: list[list[tuple[int, int]]] = []
        for i in range(n):
            row = list(rm[i % len(rm)]) if rm else []
            row += list(rw[i % len(rw)]) if rw else []
            merged.append(row)
        return merged

    if config.mode == "tiered" and tier_of:
        # Tiap pool punya 1-factorization sendiri; per ronde digabung.
        groups: dict[int, list[int]] = {}
        for pid in eligible:
            groups.setdefault(tier_of[pid], []).append(pid)
        per_tier = {t: _free_pair_rounds(g, locked) for t, g in groups.items()
                    if len(g) >= 2}
        per_tier = {t: v for t, v in per_tier.items() if v}
        if not per_tier:
            return _free_pair_rounds(eligible, locked)
        n = max(len(v) for v in per_tier.values())
        merged = []
        for i in range(n):
            row: list[tuple[int, int]] = []
            for t, rounds_t in per_tier.items():
                row.extend(rounds_t[i % len(rounds_t)])
            merged.append(row)
        return merged

    return _free_pair_rounds(eligible, locked)


# ---------------------------------------------------------------------------
# Seleksi & pengelompokan pasangan jadi match
# ---------------------------------------------------------------------------

_SEMUA_FORMAT = frozenset(MATCHUPS)

# Berapa berat pertemuan berulang dibanding satu ronde menunggu, saat memilih
# komposisi bentuk tim satu ronde. Disetel dari pengukuran pada setup nyata
# host (26 orang, 4 court, format dibatasi "sesama bentuk saja"): 0 berarti
# komposisinya itu-itu terus dan lawan berulang membengkak; terlalu besar dan
# giliran main jadi timpang karena orang yang sudah lama duduk terus dilewati.
OPPONENT_WEIGHT = 3.0


def _shape_supply(
    cands: list[list[tuple[int, int]]], gender: dict[int, str | None]
) -> list[dict[str, int]]:
    """Berapa pasangan tiap bentuk yang tersedia di tiap ronde kandidat."""
    out = []
    for row in cands:
        c = {s: 0 for s in TEAM_SHAPES}
        for a, b in row:
            s = team_shape(gender.get(a), gender.get(b))
            if s is not None:
                c[s] += 1
        out.append(c)
    return out


def _round_options(
    stok: dict[str, int], need: int, allowed: frozenset[str], memo: dict
) -> list[tuple[int, ...]]:
    """Komposisi bentuk tim yang benar-benar bisa dipakai satu ronde.

    Bukan sekadar "ada stoknya": komposisinya juga harus habis terpasangkan.
    Dengan format sesama-bentuk saja itu berarti tiap bentuk harus genap - satu
    tim LL yang tersisa tidak punya lawan sah. Batas inilah yang membuat ronde
    dengan 7 pasangan campur cuma bisa menurunkan 6, dan tanpa
    memperhitungkannya anggaran yang disusun akan tampak terjangkau padahal
    tidak.
    """
    out = []
    for n_ll in range(min(stok["LL"], need) + 1):
        for n_lp in range(min(stok["LP"], need - n_ll) + 1):
            n_pp = need - n_ll - n_lp
            if not 0 <= n_pp <= stok["PP"]:
                continue
            counts = (n_ll, n_lp, n_pp)
            if _shapes_pairable(counts, allowed, memo):
                out.append(counts)
    return out


def _supply_caps(
    options: list[list[tuple[int, ...]]], n_rounds: int
) -> dict[str, int]:
    """Paling banyak berapa tim tiap bentuk yang bisa diturunkan seluruh meet.

    Yang dijumlah hanya n_rounds ronde termurah hati untuk bentuk itu, karena
    memang cuma sebanyak itu ronde yang akan dipakai. Menjumlah seluruh ronde
    kandidat melahirkan batas yang jauh terlalu longgar - dan batas longgar
    itulah yang membuat anggaran menuntut lebih dari yang ada.
    """
    return {
        s: sum(sorted(
            (max((o[k] for o in opts), default=0) for opts in options),
            reverse=True,
        )[:n_rounds])
        for k, s in enumerate(TEAM_SHAPES)
    }


def _pick_candidate_rounds(
    options: list[list[tuple[int, ...]]], target: dict[str, int], n_rounds: int
) -> list[int]:
    """Ronde kandidat yang paling sanggup memenuhi anggaran bentuk tim.

    Rotasi partner memakai n_rounds dari sekian ronde 1-faktorisasi, dan SEMUA
    pilihan sama sahnya untuk keunikan partner - tiap ronde faktorisasi berisi
    pasangan yang belum pernah dipakai. Yang berbeda cuma bentuk timnya. Ronde
    yang kaya pasangan campur memang ada, tapi kalau yang diambil selalu
    n_rounds yang pertama, yang terpakai adalah apa adanya - dan anggaran yang
    menuntut banyak tim campur jadi tak terjangkau sejak awal.
    """
    ideal = [target.get(s, 0) / max(1, n_rounds) for s in TEAM_SHAPES]
    urut = sorted(
        range(len(options)),
        # Sebanyak apa jatah per-ronde bisa ditutup ronde ini, pada komposisi
        # terbaiknya. Indeks jadi pemutus seri supaya tetap deterministik.
        key=lambda r: (
            -max((sum(min(o[k], ideal[k]) for k in range(3)) for o in options[r]),
                 default=0.0),
            r,
        ),
    )
    return sorted(urut[:n_rounds])


def _reachable(options: list[list[tuple[int, ...]]], target: dict[str, int]) -> bool:
    """Bisakah anggaran ini benar-benar diambil dari ronde-ronde tersebut?

    Tiap ronde menurunkan satu komposisi utuh, jadi jatah tiap bentuk tidak
    bebas sendiri-sendiri. Syarat Hall-nya: untuk tiap gabungan bentuk, yang
    diminta tidak boleh melebihi yang sanggup disediakan. Tiga bentuk berarti
    cuma tujuh gabungan, jadi diperiksa semuanya, bukan diperkirakan.
    """
    for k in range(1, len(TEAM_SHAPES) + 1):
        for subset in combinations(range(len(TEAM_SHAPES)), k):
            minta = sum(target.get(TEAM_SHAPES[i], 0) for i in subset)
            bisa = sum(
                max((sum(o[i] for i in subset) for o in opts), default=0)
                for opts in options
            )
            if minta > bisa:
                return False
    return True


def _gender_slots(counts: tuple[int, ...]) -> tuple[int, int]:
    """Slot (laki-laki, perempuan) yang diturunkan satu ronde pada komposisi ini.

    counts mengikuti TEAM_SHAPES: berapa TIM berbentuk LL, LP, PP. Tim LL berisi
    2 laki-laki, tim LP satu-satu, tim PP 2 perempuan.
    """
    n_ll, n_lp, n_pp = counts
    return 2 * n_ll + n_lp, n_lp + 2 * n_pp


def _spread_dari_slot(male_slots: int, total_slots: int,
                      n_men: int, n_women: int) -> int | None:
    """Selisih main terbanyak-tersedikit kalau slotnya dibagi semerata mungkin.

    Inilah yang didenda bye_pen di _build_stats, jadi ini pula yang dipakai
    untuk membandingkan calon komposisi. None kalau komposisinya mustahil
    (mis. menuntut slot untuk gender yang pesertanya nol).
    """
    lo: list[int] = []
    hi: list[int] = []
    for slots, cnt in ((male_slots, n_men), (total_slots - male_slots, n_women)):
        if slots < 0:
            return None
        if cnt == 0:
            if slots:
                return None
            continue
        base, extra = divmod(slots, cnt)
        lo.append(base)
        hi.append(base + (1 if extra else 0))
    if not lo:
        return None
    return max(hi) - min(lo)


# Batas jumlah keadaan yang dilacak saat menelusuri total slot yang bisa
# dicapai. Ronde x komposisi tumbuh cepat kalau court banyak; batas ini menjaga
# waktunya tetap terikat. Yang dibuang selalu keadaan terjauh dari ideal, jadi
# kandidat terbaik tidak ikut hilang.
_MAX_KEADAAN_SLOT = 4000


def _slot_terjangkau(options: list[list[tuple[int, ...]]],
                     rows: list[int], ideal: float) -> set[int]:
    """Semua total slot laki-laki yang bisa dicapai urutan baris ini."""
    sums = {0}
    for i in rows:
        opts = options[i]
        if not opts:
            return set()
        nxt = {s + _gender_slots(o)[0] for s in sums for o in opts}
        if len(nxt) > _MAX_KEADAAN_SLOT:
            nxt = set(sorted(nxt, key=lambda s: abs(s - ideal))[:_MAX_KEADAAN_SLOT])
        sums = nxt
    return sums


def _spread_terbaik(options: list[list[tuple[int, ...]]], rows: list[int],
                    total_slots: int, n_men: int, n_women: int) -> int | None:
    """Spread jatah main terbaik yang masih mungkin dari urutan baris ini."""
    ideal = total_slots * (n_men / (n_men + n_women)) if n_men + n_women else 0.0
    kandidat = [
        s for s in (
            _spread_dari_slot(v, total_slots, n_men, n_women)
            for v in _slot_terjangkau(options, rows, ideal)
        ) if s is not None
    ]
    return min(kandidat) if kandidat else None


def _balanced_rows(options: list[list[tuple[int, ...]]], n_rounds: int,
                   n_men: int, n_women: int, need: int) -> list[int] | None:
    """Baris kandidat mana yang dipakai tiap ronde, supaya jatah main merata.

    Tanpa ini baris 1-faktorisasi dipakai apa adanya (0,1,2,...,0,1,...) dan
    komposisi format ikut apa adanya juga. Itu bukan detail kecil: annealing
    TIDAK BISA memperbaikinya. Mengubah satu ronde LL-LL jadi LP-LP menuntut
    dua pemain ditukar sekaligus, sedangkan gerakannya satu pemain dan keadaan
    antaranya ilegal - round_legal() menolaknya. Jadi komposisi format praktis
    ditentukan seluruhnya di sini, bukan di optimizer.

    Contoh nyata: 5 laki-laki + 3 perempuan, 12 ronde, 1 court, format sesama
    bentuk. Baris faktorisasi bergantian 6 baris yang cuma bisa LP-LP dan 6
    yang cuma bisa LL-LL, jadi perempuan main 4x dan laki-laki 7-8x. Susunan
    9 campuran + 3 putra membuat semuanya tepat 6x.

    None kalau tidak ada susunan yang lebih baik daripada urutan apa adanya.
    """
    if not options or n_rounds <= 0 or n_men + n_women == 0:
        return None
    total_slots = n_rounds * 2 * need
    ideal = total_slots * (n_men / (n_men + n_women))

    bawaan = [i % len(options) for i in range(n_rounds)]
    spread_bawaan = _spread_terbaik(options, bawaan, total_slots, n_men, n_women)

    # Nilai slot laki-laki yang bisa diturunkan satu ronde, dan baris mana saja
    # yang sanggup. Baris boleh dipakai berulang: dengan roster gender timpang,
    # komposisi merata sering menuntut lebih banyak ronde campuran daripada
    # baris campuran yang berbeda - pengulangannya tak terhindarkan, dan
    # menolaknya justru mengunci ketimpangan.
    baris_untuk: dict[int, list[int]] = {}
    for i, opts in enumerate(options):
        for o in opts:
            baris_untuk.setdefault(_gender_slots(o)[0], []).append(i)
    if not baris_untuk:
        return None
    for v in baris_untuk:
        baris_untuk[v] = sorted(set(baris_untuk[v]))

    nilai = sorted(baris_untuk)
    # jangkau[r] = {total slot laki-laki: nilai yang dipakai di ronde ke-r}
    jangkau: list[dict[int, int]] = [{} for _ in range(n_rounds + 1)]
    jangkau[0][0] = -1
    for r in range(n_rounds):
        sisa = n_rounds - r
        for s in jangkau[r]:
            for v in nilai:
                jangkau[r + 1].setdefault(s + v, v)
        if len(jangkau[r + 1]) > _MAX_KEADAAN_SLOT:
            # Sisa ronde masih bisa menambah antara min*sisa dan max*sisa slot,
            # jadi yang dinilai adalah jarak ke ideal pada akhir nanti.
            tengah = ideal - (nilai[0] + nilai[-1]) / 2 * (sisa - 1)
            terpilih = sorted(jangkau[r + 1], key=lambda s: abs(s - tengah))
            jangkau[r + 1] = {s: jangkau[r + 1][s] for s in terpilih[:_MAX_KEADAAN_SLOT]}

    def runut(s: int) -> dict[int, int]:
        """Berapa ronde memakai tiap nilai slot, untuk total akhir s."""
        jml: dict[int, int] = {}
        for r in range(n_rounds, 0, -1):
            v = jangkau[r][s]
            jml[v] = jml.get(v, 0) + 1
            s -= v
        return jml

    terbaik = None
    for s in jangkau[n_rounds]:
        sp = _spread_dari_slot(s, total_slots, n_men, n_women)
        if sp is None:
            continue
        key = (sp, abs(s - ideal))
        if terbaik is None or key < terbaik[0]:
            terbaik = (key, s, runut(s))
    if terbaik is None:
        return None

    spread_baru = terbaik[0][0]
    # Hanya menggeser kalau TERBUKTI lebih merata. Urutan apa adanya memakai
    # baris yang berbeda-beda, jadi rotasi partnernya lebih kaya; menggesernya
    # tanpa alasan menukar partner unik dengan pemerataan yang tidak bertambah.
    # Kalau spread bawaan tidak bisa dinilai (ada baris tanpa komposisi sah),
    # anggap tidak terbukti - jangan mengaku tahu.
    if spread_bawaan is None or spread_baru >= spread_bawaan:
        return None

    jumlah = terbaik[2]

    # Sebar merata sepanjang acara, bukan berblok. Runutan DP cenderung
    # mengelompokkan ronde sejenis berurutan, dan blok "6 ronde putra" berarti
    # para perempuan duduk enam kali beruntun - persis yang didenda b2b_pen.
    # Posisi pecahan (k+0.5)/jumlah menyebar tiap kelompok serata mungkin.
    urut_nilai = [
        v for _, v in sorted(
            ((k + 0.5) / jumlah[v], v)
            for v in jumlah for k in range(jumlah[v])
        )
    ]

    # Baris untuk tiap nilai, dipakai bergiliran supaya pengulangan tersebar
    # rata alih-alih menumpuk di satu baris.
    pakai: dict[int, int] = {}
    hasil: list[int] = []
    for v in urut_nilai:
        kandidat = baris_untuk[v]
        hasil.append(kandidat[pakai.get(v, 0) % len(kandidat)])
        pakai[v] = pakai.get(v, 0) + 1
    return hasil


@dataclass
class ShapeQuota:
    """Anggaran bentuk tim untuk SATU MEET, bukan satu ronde.

    Tanpa ini komposisi format lahir dari keputusan lokal tiap ronde, dan
    totalnya melenceng. Melencengnya bukan soal rapi-tidak rapi: kalau jatah
    PP-PP terpakai lebih banyak dari yang disanggupi kolam pasangan perempuan,
    lawan 100% unik jadi mustahil secara aritmetika - dan tidak ada iterasi
    annealing yang bisa memperbaikinya, karena tidak ada satu pun gerakan yang
    bisa memindahkan komposisi (menukar satu pemain selalu melahirkan bentuk
    tim ilegal, jadi selalu ditolak).

    Targetnya dihitung capacity.shape_budget() dan sudah dijamin muat. Selama
    tidak ada bentuk yang kelebihan jatah, totalnya pasti mendarat tepat di
    target: jumlah sisa selalu sama dengan jumlah tim yang belum disusun, jadi
    sisa yang tak pernah negatif hanya bisa habis merata.
    """

    target: dict[str, int]
    # Komposisi yang mungkin di tiap ronde terpilih, urut ronde. Dipakai untuk
    # melihat ke depan.
    options: list[list[tuple[int, ...]]] = field(default_factory=list)
    used: dict[str, int] = field(default_factory=lambda: {s: 0 for s in TEAM_SHAPES})
    idx: int = 0

    def sisa(self, shape: str) -> int:
        return self.target.get(shape, 0) - self.used.get(shape, 0)

    def kelebihan(self, counts: tuple[int, ...]) -> int:
        """Berapa tim yang melewati jatah kalau komposisi ini dipakai."""
        return sum(max(0, c - self.sisa(s)) for s, c in zip(TEAM_SHAPES, counts))

    def aman(self, counts: tuple[int, ...]) -> bool:
        """Masih bisakah sisa jatah diambil ronde-ronde berikutnya?

        Tidak melewati jatah saja tidak cukup. Ronde ini bisa mengambil bentuk
        yang masih ada jatahnya, tapi menyisakan kebutuhan yang tak satu pun
        ronde berikutnya sanggup sediakan - dan begitu itu terjadi, tidak ada
        cara menebusnya: komposisi beku setelah konstruksi.
        """
        if self.kelebihan(counts):
            return False
        sisa = {s: self.sisa(s) - c for s, c in zip(TEAM_SHAPES, counts)}
        return _reachable(self.options[self.idx + 1:], sisa)

    def pakai(self, counts: tuple[int, ...]) -> None:
        for s, c in zip(TEAM_SHAPES, counts):
            self.used[s] = self.used.get(s, 0) + c
        self.idx += 1


def _grouping_cost(picked: list[tuple[int, int]], st: ScheduleState) -> float:
    """Perkiraan berapa banyak pertemuan berulang yang lahir dari susunan ini.

    Penjodohannya greedy dan tanpa acak - ini cuma alat ukur untuk membandingkan
    komposisi, bukan penyusun jadwal. Memakai rng di sini akan menggeser seluruh
    urutan acak di bawahnya hanya karena ada komposisi tambahan yang dinilai.
    """
    sisa = list(picked)
    total = 0.0
    while len(sisa) >= 2:
        a = sisa.pop(0)
        best_i, best_c = None, None
        for i, b in enumerate(sisa):
            if not st.rules.matchup_ok([a[0], a[1], b[0], b[1]]):
                continue
            c = sum(st.oc[st._k(x, y)] for x in a for y in b)
            if best_c is None or c < best_c:
                best_i, best_c = i, c
        if best_i is None:  # tidak ada lawan sah - dinilai mahal, bukan gratis
            total += len(sisa) * 4
            break
        total += best_c
        sisa.pop(best_i)
    return total


def _same_gender_headroom(st: ScheduleState) -> dict[int, int]:
    """Berapa lawan segender lagi yang masih boleh dipakai tiap pemain.

    Ini batas per-orang yang tidak kelihatan di hitungan suplai se-meet.
    Tiap ronde, seorang pemain mendapat 0, 1, atau 2 lawan segender tergantung
    bentuk match: di tim campur melawan tim campur ia dapat 1, di tim segender
    melawan tim segender ia dapat 2. Sementara calon lawan segendernya cuma
    sebanyak orang lain yang gendernya sama.

    Contoh nyatanya, 11 perempuan yang masing-masing main 8 ronde: yang tidak
    pernah kebagian match putri-vs-putri butuh 8 dari 10 calon, yang kebagian
    sekali butuh 9, yang kebagian DUA KALI butuh 10 dari 10 - ia harus bertemu
    semua perempuan lain, tepat sekali, tanpa satu pun kesempatan meleset.
    Kolam pasangan se-meet masih longgar saat itu terjadi, jadi tidak ada
    peringatan apa pun; yang habis adalah jatah orang per orang.
    """
    g = st.rules.gender
    seang: dict[str, int] = {}
    for p in range(st.n):
        gp = g.get(p)
        if gp is not None:
            seang[gp] = seang.get(gp, 0) + 1

    sisa: dict[int, int] = {}
    for p in range(st.n):
        gp = g.get(p)
        if gp is None:
            continue
        pakai = 0
        for q in range(st.n):
            if q != p and g.get(q) == gp:
                pakai += st.oc[st._k(p, q)]
        sisa[p] = (seang[gp] - 1) - pakai
    return sisa


def _shapes_pairable(
    counts: tuple[int, ...], allowed: frozenset[str], memo: dict
) -> bool:
    """Bisakah multiset bentuk tim ini dihabiskan jadi match yang sah semua?

    counts sejajar dengan TEAM_SHAPES: (jumlah LL, jumlah LP, jumlah PP).
    Pencariannya kecil - cuma tiga jenis bentuk - jadi rekursi dengan memo
    sudah lebih dari cukup.
    """
    if not any(counts):
        return True
    if counts in memo:
        return memo[counts]
    # Bentuk pertama yang masih tersisa harus dapat lawan; kalau tidak ada satu
    # pun lawan yang sah untuknya, susunan ini memang buntu.
    i = next(k for k, c in enumerate(counts) if c)
    ok = False
    for j in range(len(TEAM_SHAPES)):
        if matchup_code(TEAM_SHAPES[i], TEAM_SHAPES[j]) not in allowed:
            continue
        rest = list(counts)
        rest[i] -= 1
        if rest[j] <= 0:
            continue
        rest[j] -= 1
        if _shapes_pairable(tuple(rest), allowed, memo):
            ok = True
            break
    memo[counts] = ok
    return ok


def _select_pairs_by_shape(
    scored: list[tuple[int, int]],
    st: ScheduleState,
    need: int,
    rng: random.Random,
    quota: ShapeQuota | None = None,
) -> list[tuple[int, int]] | None:
    """Pilih pasangan yang bentuk timnya masih bisa dipasangkan habis.

    Host yang membatasi format match ("putra vs putra saja, campur vs campur
    saja") membuat siapa yang turun dan format apa yang muncul jadi satu
    persoalan, bukan dua. Memilih pasangan hanya berdasarkan siapa yang paling
    lama duduk bisa menghasilkan mis. 3 tim putra dan 1 tim putri di ronde yang
    sama - satu tim putra dan satu tim putri pasti tidak kebagian lawan yang
    sah, dan pelanggarannya lahir di sini, bukan di annealing.

    Jadi bentuk timnya dipilih bersamaan: dari semua komposisi (LL, LP, PP)
    yang habis terpasangkan, diambil yang menurunkan orang-orang paling lama
    duduk. Prioritas istirahatnya tidak hilang, hanya dijalankan di dalam
    komposisi yang memang bisa dimainkan.

    None kalau aturan ini tidak bisa dinilai (ada gender yang belum diisi) atau
    tidak ada komposisi yang sah sama sekali - pemanggilnya kembali ke perilaku
    lama, dan pelanggaran yang tersisa tetap dilaporkan ke host.
    """
    g = st.rules.gender
    by_shape: dict[str, list[tuple[int, int]]] = {s: [] for s in TEAM_SHAPES}
    for pr in scored:  # sudah urut prioritas istirahat
        shape = team_shape(g.get(pr[0]), g.get(pr[1]))
        if shape is None:
            return None
        by_shape[shape].append(pr)

    # Tim segender (LL/PP) memakai DUA jatah lawan segender sekaligus, tim
    # campur cuma satu. Jadi di antara pasangan yang sama-sama layak turun
    # menurut lama duduk, yang jatahnya masih longgar didahulukan - kalau tidak,
    # orang yang sama bisa kebagian match segender dua kali dan jatahnya habis
    # persis, sehingga meleset sedikit saja langsung jadi lawan berulang yang
    # tak bisa ditebus.
    #
    # Hanya bentuk segender yang diurutkan ulang. Untuk tim campur, urutannya
    # dibiarkan apa adanya: mendahulukan yang jatahnya longgar di situ justru
    # terbalik - yang jatahnya menipis malah paling butuh tempat yang murah.
    #
    # sort() Python stabil dan kunci utamanya tetap lama duduk, jadi ini cuma
    # mengganti pemutus seri acak dengan pemutus seri yang punya alasan.
    #
    # Hanya dipakai kalau ada anggaran komposisi, yaitu justru saat nol
    # pengulangan memang bisa dicapai. Kalau jatahnya toh pasti jebol - 14 putra
    # 6 putri dengan format dibatasi, misalnya - semua orang minus dan
    # urutannya jadi derau belaka: ia menggeser siapa yang turun tanpa
    # menyelamatkan apa pun, dan malah membuat perataan jumlah main kehilangan
    # pertukaran sah yang tadinya ada.
    if quota is not None:
        sisa = _same_gender_headroom(st)
        for shape in ("LL", "PP"):
            by_shape[shape].sort(
                key=lambda pr: (
                    -(st.bye_count[pr[0]] + st.bye_count[pr[1]]),
                    -min(sisa.get(pr[0], 0), sisa.get(pr[1], 0)),
                )
            )

    allowed = frozenset(st.rules.allowed_matchups)
    avail = [len(by_shape[s]) for s in TEAM_SHAPES]
    memo: dict[tuple[int, ...], bool] = {}
    best: tuple[tuple[int, int], float, float, tuple[int, ...]] | None = None

    for n_ll in range(avail[0] + 1):
        for n_lp in range(avail[1] + 1):
            n_pp = need - n_ll - n_lp
            if not 0 <= n_pp <= avail[2]:
                continue
            counts = (n_ll, n_lp, n_pp)
            if not _shapes_pairable(counts, allowed, memo):
                continue
            picked = [pr for shape, k in zip(TEAM_SHAPES, counts)
                      for pr in by_shape[shape][:k]]
            # Dua hal ditimbang sekaligus, dan memang harus sekaligus: komposisi
            # yang menurunkan orang paling lama duduk belum tentu komposisi yang
            # menyisakan lawan segar. Menilai lama-duduk saja membuat ronde demi
            # ronde memakai komposisi yang itu-itu juga, dan orang yang sama
            # bertemu lagi - persis keluhan "lawan berulang".
            istirahat = float(sum(st.bye_count[a] + st.bye_count[b]
                                  for a, b in picked))
            score = istirahat - OPPONENT_WEIGHT * _grouping_cost(picked, st)
            # Jatah bentuk tim mendahului skor, dan bukan sebagai bobot
            # melainkan sebagai urutan: komposisi yang menjaga jatah selalu
            # menang atas yang merusaknya, berapa pun selisih skornya. Merusak
            # jatah tidak membuat jadwal sedikit lebih jelek - ia membuat lawan
            # unik mustahil untuk sisa meet.
            #
            # Dua tingkat, bukan satu. Tingkat pertama "aman": jatah utuh DAN
            # sisanya masih bisa diambil ronde berikutnya. Tingkat kedua cuma
            # dipakai kalau tidak ada yang aman - kelebihan sekecil mungkin,
            # supaya susunan peserta yang memang tidak menyediakan bentuk yang
            # dibutuhkan tetap dapat jadwal alih-alih ditolak mentah.
            aman = (1, 0) if quota is None else (
                int(quota.aman(counts)), -quota.kelebihan(counts))
            # Seri diputus acak. Tanpa ini komposisi dengan LL paling sedikit
            # selalu menang cuma karena urutan loop, dan gender yang sama terus
            # menerus jadi yang mengalah.
            key = (aman, score, rng.random(), counts)
            if best is None or key[:3] > best[:3]:
                best = key

    if best is None:
        return None
    counts = best[3]
    if quota:
        quota.pakai(counts)
    return [pr for shape, k in zip(TEAM_SHAPES, counts)
            for pr in by_shape[shape][:k]]


def _select_pairs(
    candidates: list[tuple[int, int]],
    st: ScheduleState,
    n_pairs_needed: int,
    rng: random.Random,
    quota: ShapeQuota | None = None,
) -> list[tuple[int, int]]:
    """Pilih pasangan yang turun, prioritas ke yang paling sering istirahat."""
    if n_pairs_needed >= len(candidates):
        # Tidak ada yang bisa dipilih, tapi jatahnya tetap terpakai - kalau
        # tidak dicatat, ronde-ronde berikutnya mengira masih punya sisa.
        if quota is not None:
            g = st.rules.gender
            counts = tuple(
                sum(1 for pr in candidates
                    if team_shape(g.get(pr[0]), g.get(pr[1])) == s)
                for s in TEAM_SHAPES
            )
            quota.pakai(counts)
        return list(candidates)
    scored = sorted(
        candidates,
        key=lambda pr: (-(st.bye_count[pr[0]] + st.bye_count[pr[1]]), rng.random()),
    )
    # Mengizinkan SEMUA format sama saja dengan tidak membatasi apa pun, dan
    # keduanya wajib menghasilkan jadwal yang identik. Kalau jalur sadar-bentuk
    # ikut jalan di situ, ia memakai rng untuk memutus seri dan jadwalnya
    # bergeser tanpa ada satu pun batasan yang ditegakkan.
    if st.rules.allowed_matchups and set(st.rules.allowed_matchups) != _SEMUA_FORMAT:
        picked = _select_pairs_by_shape(scored, st, n_pairs_needed, rng, quota)
        if picked is not None:
            return picked
    return scored[:n_pairs_needed]


def _allocate_courts(
    group_sizes: dict[int, int], courts: int
) -> dict[int, int]:
    """Bagi court antar pool rating, sebanding jumlah pasangan tiap pool.

    Batas atas tiap pool = jumlah_pasangan // 2 (satu court butuh 2 pasangan).
    Sisa court dibagikan dengan metode largest remainder.
    """
    caps = {t: size // 2 for t, size in group_sizes.items()}
    total_cap = sum(caps.values())
    if total_cap == 0:
        return {t: 0 for t in group_sizes}
    if courts >= total_cap:
        return dict(caps)

    alloc: dict[int, int] = {}
    remainders: list[tuple[float, int]] = []
    for t, cap in caps.items():
        exact = courts * cap / total_cap
        base = min(cap, int(exact))
        alloc[t] = base
        remainders.append((exact - base, t))

    left = courts - sum(alloc.values())
    for _, t in sorted(remainders, reverse=True):
        if left <= 0:
            break
        if alloc[t] < caps[t]:
            alloc[t] += 1
            left -= 1
    # Pool yang belum kebagian court sama sekali diprioritaskan, supaya tidak
    # ada pool yang menganggur sepanjang acara.
    for t in sorted(caps, key=lambda x: -caps[x]):
        if alloc[t] == 0 and caps[t] > 0:
            donor = max(alloc, key=lambda x: alloc[x])
            if alloc[donor] > 1:
                alloc[donor] -= 1
                alloc[t] = 1
    return alloc


def _still_pairable(
    a: tuple[int, int],
    remaining: list[tuple[int, int]],
    sah: list[int],
    st: ScheduleState,
) -> list[int]:
    """Dari lawan yang sah untuk `a`, mana yang menyisakan sisa yang sehat.

    Kosong kalau aturannya tidak bisa dinilai atau tidak ada yang memenuhi;
    pemanggilnya lalu memakai daftar `sah` apa adanya.
    """
    if not st.rules.allowed_matchups or len(sah) <= 1:
        return list(sah)
    g = st.rules.gender
    shapes = [team_shape(g.get(pr[0]), g.get(pr[1])) for pr in remaining]
    if any(s is None for s in shapes):
        return []
    idx = {s: k for k, s in enumerate(TEAM_SHAPES)}
    total = [0] * len(TEAM_SHAPES)
    for s in shapes:
        total[idx[s]] += 1

    allowed = frozenset(st.rules.allowed_matchups)
    memo: dict[tuple[int, ...], bool] = {}
    aman = []
    for i in sah:
        sisa = list(total)
        sisa[idx[shapes[i]]] -= 1
        if _shapes_pairable(tuple(sisa), allowed, memo):
            aman.append(i)
    return aman


def _group_into_matches(
    pairs: list[tuple[int, int]],
    st: ScheduleState,
    rng: random.Random,
    rating_weight: float = 0.0,
) -> list[list[int]]:
    """Pasangkan pair-vs-pair secara greedy.

    Kriteria: lawan yang paling jarang ditemui, dan (kalau mode peduli rating)
    total rating tim yang paling mirip.
    """
    remaining = list(pairs)
    if rating_weight > 0:
        # Urut berdasarkan kekuatan tim -> tim sepadan berdekatan.
        remaining.sort(key=lambda pr: st.ratings[pr[0]] + st.ratings[pr[1]])
    else:
        rng.shuffle(remaining)
    quads: list[list[int]] = []

    while len(remaining) >= 2:
        a = remaining.pop(0)
        sum_a = st.ratings[a[0]] + st.ratings[a[1]]
        best_i, best_cost = 0, None
        # Format match yang dilarang host disaring DI SINI, bukan diserahkan ke
        # annealing. Ronde yang lahir melanggar batas keras tidak punya jalan
        # keluar: tiap gerakan annealing hanya diterima kalau ronde hasilnya
        # legal, jadi ronde yang sejak awal ilegal justru membeku di sana.
        sah = [i for i, b in enumerate(remaining)
               if st.rules.matchup_ok([a[0], a[1], b[0], b[1]])]
        # Lawan yang sah belum tentu lawan yang benar: mengambil lawan sah yang
        # menyisakan bentuk tim tak terpasangkan hanya memindahkan pelanggaran
        # ke match berikutnya. Jadi dari yang sah, disaring lagi yang MENYISAKAN
        # sisa yang masih habis terpasangkan.
        aman = _still_pairable(a, remaining, sah, st)
        # Kalau tidak ada satu pun lawan yang sah, pasangan ini tetap harus
        # ditandingkan - lebih baik satu match melanggar daripada ada peserta
        # yang hilang dari ronde. Pelanggarannya dilaporkan ke host di catatan.
        kandidat = aman or sah or range(len(remaining))
        for i in kandidat:
            b = remaining[i]
            # Biaya = berapa kali keempat kombinasi lawan ini sudah terjadi.
            cost = sum(st.oc[st._k(x, y)] for x in a for y in b) * 100.0
            if rating_weight > 0:
                gap = abs(sum_a - (st.ratings[b[0]] + st.ratings[b[1]]))
                cost += rating_weight * gap
            if best_cost is None or cost < best_cost:
                best_i, best_cost = i, cost
        b = remaining.pop(best_i)
        quads.append([a[0], a[1], b[0], b[1]])

    return quads


def _build_round(
    row: list[tuple[int, int]],
    st: ScheduleState,
    courts: int,
    tier_of: dict[int, int] | None,
    rng: random.Random,
    rating_weight: float,
    group_of: dict[int, str] | None = None,
    quota: ShapeQuota | None = None,
) -> list[list[int]]:
    """Rakit satu ronde: pilih siapa turun, lalu tentukan siapa lawan siapa.

    `group_of` memaksa satu court diisi satu kelompok saja (dipakai babak
    "sesama gender": court putra dan court putri tidak boleh bercampur).
    """
    if group_of is not None:
        pools: dict[str, list[tuple[int, int]]] = {}
        for pr in row:
            pools.setdefault(group_of[pr[0]], []).append(pr)

        # Court diberikan ke kelompok yang paling banyak menganggur sejauh ini.
        # Tanpa ini satu gender bisa memonopoli court sepanjang acara.
        quads: list[list[int]] = []
        left = courts
        order = sorted(
            pools,
            key=lambda k: (
                -sum(st.bye_count[p] for pr in pools[k] for p in pr)
                / max(1, len(pools[k]) * 2),
                rng.random(),
            ),
        )
        for key in order:
            if left <= 0:
                break
            avail = pools[key]
            take = min(left, len(avail) // 2)
            if take <= 0:
                continue
            chosen = _select_pairs(avail, st, take * 2, rng)
            quads.extend(_group_into_matches(chosen, st, rng, rating_weight))
            left -= take
        return quads

    if tier_of is None:
        chosen = _select_pairs(row, st, min(courts, len(row) // 2) * 2, rng, quota)
        return _group_into_matches(chosen, st, rng, rating_weight)

    # Mode tiered: court dijatah per pool, dan pool tidak pernah bercampur.
    groups: dict[int, list[tuple[int, int]]] = {}
    for pr in row:
        groups.setdefault(tier_of[pr[0]], []).append(pr)

    alloc = _allocate_courts({t: len(v) for t, v in groups.items()}, courts)
    quads: list[list[int]] = []
    for t, pool_pairs in sorted(groups.items()):
        n_courts = alloc.get(t, 0)
        if n_courts <= 0:
            continue
        chosen = _select_pairs(pool_pairs, st, n_courts * 2, rng)
        quads.extend(_group_into_matches(chosen, st, rng, rating_weight))
    return quads


# ---------------------------------------------------------------------------
# Statistik
# ---------------------------------------------------------------------------

def _build_stats(st: ScheduleState, players: list[Player], n_rounds: int) -> ScheduleStats:
    n = st.n
    ids = [p.id for p in players]

    # Pasangan yang SENGAJA dikunci host. Mereka berulang tiap ronde - itu
    # justru formatnya, bukan kegagalan rotasi. Kalau ikut dihitung, meet
    # pasangan tetap selalu terlihat buruk: tiap pasangan menyumbang
    # (main - 1) pengulangan, dan skornya kehilangan hampir seluruh 45 poin
    # jatah partner. Yang diukur metrik ini adalah rotasi yang MELESET, jadi
    # pasangan terkunci dikeluarkan dari hitungan - bukan disembunyikan:
    # pasangannya tetap tercetak di jadwal dan di daftar peserta.
    locked_pairs: set[tuple[int, int]] = set()
    by_id = {p.id: p for p in players}
    for p in players:
        mate = p.partner_id
        if mate is not None and by_id.get(mate) is not None \
                and by_id[mate].partner_id == p.id:
            locked_pairs.add((min(p.id, mate), max(p.id, mate)))

    partner_repeat_pairs = partner_repeat_max = 0
    oppo_repeat_pairs = oppo_repeat_max = 0
    never_met = 0
    for i, j in combinations(ids, 2):
        k = st._k(i, j)
        pcv, ocv = st.pc[k], st.oc[k]
        if pcv > 1 and (i, j) not in locked_pairs:
            partner_repeat_pairs += 1
            partner_repeat_max = max(partner_repeat_max, pcv)
        if ocv > 1:
            oppo_repeat_pairs += 1
            oppo_repeat_max = max(oppo_repeat_max, ocv)
        if pcv == 0 and ocv == 0:
            never_met += 1

    plays = {pid: 0 for pid in ids}
    for r in range(n_rounds):
        for q in st.matches[r]:
            for p in q:
                plays[p] += 1
    byes = {pid: st.bye_count[pid] for pid in ids}

    b2b = 0
    for r in range(n_rounds - 1):
        b2b += len(st.byes[r] & st.byes[r + 1])

    gaps = []
    for r in range(n_rounds):
        for a, b, c, d in st.matches[r]:
            gaps.append(abs((st.ratings[a] + st.ratings[b]) - (st.ratings[c] + st.ratings[d])))

    # Batas bawah teoretis pengulangan, untuk menilai jadwal secara adil.
    # Kalau ada pasangan terkunci, orang yang masih rotasi bebas hanya bisa
    # berpasangan sesama mereka - kolam partnernya menyusut, jadi batasnya harus
    # dihitung dari kolam itu, bukan dari seluruh peserta. Tanpa koreksi ini
    # meet dengan 2 pasang terkunci dan 4 orang bebas dinilai seperti gagal
    # merotasi, padahal 4 orang memang cuma punya 3 partner yang mungkin.
    locked_members = {p for pair in locked_pairs for p in pair}
    free_ids = [pid for pid in ids if pid not in locked_members]
    partner_pool = len(free_ids) if locked_pairs else len(ids)
    min_partner_excess = sum(
        max(0, plays[p] - max(1, partner_pool - 1))
        for p in (free_ids if locked_pairs else ids)
    ) / 2
    min_oppo_excess = sum(max(0, 2 * plays[p] - (len(ids) - 1)) for p in ids) / 2
    actual_partner_excess = sum(
        max(0, st.pc[st._k(i, j)] - 1) for i, j in combinations(ids, 2)
        if (i, j) not in locked_pairs
    )
    actual_oppo_excess = sum(
        max(0, st.oc[st._k(i, j)] - 1) for i, j in combinations(ids, 2)
    )

    # Slot yang sudah dipesan pasangan terkunci ikut dikeluarkan dari penyebut;
    # kalau tidak, denda rotasi diencerkan oleh slot yang tidak pernah dirotasi.
    locked_slots = sum(st.pc[st._k(i, j)] for i, j in locked_pairs)
    total_partner_slots = max(1, sum(plays.values()) / 2 - locked_slots)
    total_oppo_slots = max(1, sum(plays.values()))
    p_pen = max(0.0, actual_partner_excess - min_partner_excess) / total_partner_slots
    o_pen = max(0.0, actual_oppo_excess - min_oppo_excess) / total_oppo_slots

    play_vals = list(plays.values())
    spread = (max(play_vals) - min(play_vals)) if play_vals else 0
    bye_pen = min(1.0, spread / 3.0)
    b2b_pen = min(1.0, b2b / max(1, len(ids)))

    score = 100.0 - 45 * min(1.0, p_pen) - 30 * min(1.0, o_pen) \
        - 15 * bye_pen - 10 * b2b_pen

    # Sudah menyentuh batas bawah? Kalau ya, tidak ada jadwal lain yang bisa
    # lebih sedikit pengulangannya - mengulang penjadwalan mustahil menolong.
    # Batasnya bisa pecahan (dibagi 2), jadi dibandingkan dengan toleransi.
    di_batas = (actual_partner_excess <= min_partner_excess + 1e-9
                and actual_oppo_excess <= min_oppo_excess + 1e-9)

    return ScheduleStats(
        rounds=n_rounds,
        players=len(ids),
        partner_repeat_pairs=partner_repeat_pairs,
        partner_repeat_max=partner_repeat_max,
        opponent_repeat_pairs=oppo_repeat_pairs,
        opponent_repeat_max=oppo_repeat_max,
        never_met_pairs=never_met,
        plays_per_player=plays,
        byes_per_player=byes,
        back_to_back_byes=b2b,
        avg_rating_gap=round(sum(gaps) / len(gaps), 2) if gaps else 0.0,
        max_rating_gap=round(max(gaps), 2) if gaps else 0.0,
        quality_score=round(max(0.0, min(100.0, score)), 1),
        at_theoretical_floor=di_batas,
    )


def pair_matrix(st: ScheduleState, players: list[Player]) -> list[PairStat]:
    return [
        PairStat(a=i, b=j,
                 as_partner=st.pc[st._k(i, j)],
                 as_opponent=st.oc[st._k(i, j)])
        for i, j in combinations([p.id for p in players], 2)
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _zero_repeats_possible(players: list[Player], config: Config) -> bool:
    """Mungkinkah jadwal ini sama sekali tanpa pertemuan berulang?

    Dua lapis. Yang umum: tiap ronde seorang pemain dapat 2 lawan, jadi lawan
    unik mentok di (N-1)/2 ronde main. Yang kedua khusus untuk format match
    yang dibatasi - kolam pasangan pecah per gender dan yang terkecil bisa
    habis jauh lebih dulu, sehingga setup yang lolos hitungan umum tetap
    mustahil.
    """
    segments = _resolve_segments(config)
    total_rounds = sum(s.rounds for s in segments)
    n = len(players)
    if total_rounds <= 0 or n < 4:
        return False
    courts_used = min(config.courts, n // 4)
    if math.ceil(total_rounds * 4 * courts_used / n) > (n - 1) // 2:
        return False
    men = sum(1 for p in players if p.gender == "M")
    women = sum(1 for p in players if p.gender == "F")
    if config.allowed_matchups and men + women == n:
        return shape_budget(
            men, women, total_rounds * courts_used,
            sorted(set(config.allowed_matchups)),
        ).feasible is not False
    return True


def _lebih_baik(a: Schedule, b: Schedule) -> bool:
    """Apakah jadwal a lebih layak dipakai daripada b?

    Urutannya sengaja bukan skor kualitas semata. Pengulangan partner lebih
    dulu, lalu pengulangan lawan, baru kualitas. Peserta mengingat "kok lawan
    dia lagi", bukan selisih 0.4 poin di angka kualitas - dan skor kualitas
    memberi lawan cuma 30 dari 100, jadi memilih dengannya bisa membuang jadwal
    yang lawannya bersih demi jadwal yang istirahatnya sedikit lebih rapi.
    """
    x, y = a.stats, b.stats
    return ((x.partner_repeat_pairs, x.opponent_repeat_pairs, -x.quality_score)
            < (y.partner_repeat_pairs, y.opponent_repeat_pairs, -y.quality_score))


def build_schedule(players: list[Player], config: Config,
                   progress=None) -> Schedule:
    """Bangun jadwal lengkap. Ini fungsi yang dipanggil UI.

    Penjadwalan diulang beberapa kali dengan seed turunan, lalu diambil yang
    terbaik. Annealing berhenti di optimum lokal yang berbeda-beda tergantung
    lintasan acaknya, dan selisihnya nyata: pada setup 26 orang dengan format
    dibatasi, satu percobaan mencapai nol lawan berulang di 13 dari 24 seed,
    tiga percobaan di 22 dari 24.

    Berhenti lebih awal begitu ada percobaan yang menyentuh batas bawah
    teoretis. Karena itu yang paling sering terjadi di percobaan pertama,
    ongkos rata-ratanya jauh di bawah `attempts` kali lipat - dan setup yang
    memang mudah tidak membayar apa pun.

    Seed yang dilaporkan tetap seed asli host, bukan seed turunan yang menang:
    seluruh rangkaian percobaan ditentukan oleh seed asli, jadi mengulang
    dengan angka yang sama tetap menghasilkan jadwal yang sama persis.

    `progress(frac, pesan)` opsional: dipanggil di tiap tahap supaya host tahu
    apa yang sedang dikerjakan. Angkanya nyata, bukan animasi.
    """
    percobaan = max(1, config.attempts)
    # Mengulang cuma masuk akal kalau ada yang dikejar. Saat pengulangan memang
    # wajib terjadi, tiap percobaan berhenti di sekitar batas bawah yang sama
    # dan bedanya tinggal derau - diukur: 60 orang / 15 court / 20 ronde tetap
    # 3 pasang berulang setelah 3 percobaan, dan waktunya naik 3.2 -> 9.8 detik.
    # Setup seperti itu dibiarkan satu percobaan saja.
    if percobaan > 1 and not _zero_repeats_possible(players, config):
        percobaan = 1
    terbaik: Schedule | None = None

    for k in range(percobaan):
        # Tiap percobaan dapat lintasan acak yang berbeda, tapi tetap turunan
        # deterministik dari seed host.
        cfg = config if k == 0 else replace(config, seed=config.seed + 1000 * k)

        def teruskan(frac, msg, k=k):
            if progress is not None:
                awal = k / percobaan
                label = msg if percobaan == 1 else f"[{k + 1}/{percobaan}] {msg}"
                progress(awal + frac / percobaan, label)

        sch = _build_once(players, cfg, teruskan if progress else None)
        if terbaik is None or _lebih_baik(sch, terbaik):
            terbaik = sch
        if sch.stats.at_theoretical_floor:
            break

    terbaik.config.seed = config.seed
    terbaik.config.attempts = config.attempts
    if progress is not None:
        progress(1.0, f"Selesai - kualitas {terbaik.stats.quality_score}/100")
    return terbaik


def _build_once(players: list[Player], config: Config,
                progress=None) -> Schedule:
    """Satu kali penjadwalan utuh, dari validasi sampai jadwal jadi."""
    def say(frac, msg):
        if progress is not None:
            progress(frac, msg)

    say(0.01, "Memeriksa setup")
    segments = _resolve_segments(config)
    _validate(players, config, segments)

    total_rounds = sum(s.rounds for s in segments)
    if total_rounds <= 0:
        raise ScheduleError(
            "Jumlah ronde nol. Perpanjang durasi sewa atau perpendek durasi per ronde."
        )

    round_minutes = _resolve_round_minutes(config, total_rounds) \
        if config.segments else config.round_minutes

    rng = random.Random(config.seed)
    # Ruang id dibuat rapat 0..n-1 agar count matrix tetap kecil.
    ids = sorted(p.id for p in players)
    remap = {pid: i for i, pid in enumerate(ids)}
    inv = {i: pid for pid, i in remap.items()}
    n = len(ids)

    local_players = [
        Player(id=remap[p.id], name=p.name, rating=p.rating, gender=p.gender,
               partner_id=remap[p.partner_id] if p.partner_id is not None else None,
               court_preference=p.court_preference)
        for p in sorted(players, key=lambda x: x.id)
    ]
    ratings = [0.0] * n
    for p in local_players:
        ratings[p.id] = p.rating

    tier_of = (_assign_tiers(local_players, config.tier_count)
               if config.mode == "tiered" else None)
    if tier_of:
        for p in local_players:
            p.tier = tier_of[p.id]

    # Partner terkunci berlaku di mode apa pun dan boleh sebagian: peserta yang
    # minta pasangan tetap dikunci, peserta lain tetap rotasi bebas.
    locked: dict[int, int] = {}
    for p in local_players:
        if p.partner_id is not None:
            mate = local_players[p.partner_id]
            if mate.partner_id != p.id:
                raise ScheduleError(
                    f"Permintaan partner tetap tidak nyambung: {p.name} memilih "
                    f"{mate.name}, tapi {mate.name} tidak memilih {p.name}."
                )
            locked[p.id] = p.partner_id

    weights = Weights.for_mode(config.mode)

    # Denda "pernah ketemu 2x" hanya masuk akal kalau nol pengulangan memang
    # bisa dicapai. Kalau tidak, ia justru merusak: begitu sebuah pasangan
    # terlanjur berulang, memperdalamnya jadi lebih murah daripada membuat
    # pasangan baru ikut berulang, sehingga pengulangan MENUMPUK di sedikit
    # orang alih-alih tersebar. Padahal saat pengulangan tak terhindarkan,
    # tersebar rata itulah satu-satunya yang bisa diperbaiki - dan itu tugas
    # bentuk konveks c*(c-1), yang bekerja paling baik tanpa denda ini.
    #
    # Contohnya 8 orang di 2 court: tiap orang main tiap ronde, lawan unik
    # mentok di 3 ronde. Dengan denda menyala, satu pasangan bisa berhadapan
    # 5 kali sementara pasangan lain belum pernah.
    n_men = sum(1 for p in local_players if p.gender == "M")
    n_women = sum(1 for p in local_players if p.gender == "F")
    if not _zero_repeats_possible(players, config):
        weights.opponent_cap = 0.0

    rules = Rules(
        gender={p.id: p.gender for p in local_players},
        locked_partner=locked,
        tier_of=dict(tier_of) if tier_of else {},
        court_pref={p.id: p.court_preference for p in local_players
                    if p.court_preference},
        allowed_matchups=set(config.allowed_matchups or ()),
    )

    # Peta ronde -> (segmen, ronde ke berapa di segmen itu). Kalau selang-seling
    # aktif, ronde tiap babak tersebar sepanjang acara alih-alih menjadi blok.
    plan = round_plan(segments, config.interleave_segments)
    round_segment: list[Segment] = [seg for seg, _ in plan]

    for seg in round_segment:
        rules.round_rule.append(seg.rule)
        rules.round_eligible.append(set(_eligible_for(seg.rule, local_players)))

    st = ScheduleState(n, ratings, weights, total_rounds, rules)

    notes: list[str] = []
    courts = config.courts

    # Anggaran bentuk tim hanya berlaku kalau seluruh meet memakai satu kolam
    # peserta dengan satu aturan. Babak putra/putri, pool rating, dan partner
    # terkunci masing-masing memecah kolamnya sendiri; modelnya tidak berlaku
    # di situ dan lebih baik tidak dipakai daripada dipakai dengan andaian yang
    # salah.
    izin = set(config.allowed_matchups or ())
    pakai_kuota = (
        bool(izin)
        and izin != _SEMUA_FORMAT
        and tier_of is None
        and not locked
        and len(segments) == 1
        and segments[0].rule == "open"
        and all(p.gender in ("M", "F") for p in local_players)
    )
    # Kalau rondenya melebihi stok 1-faktorisasi, rotasi partner mengulang dari
    # awal dan partner berulang sudah tak terhindarkan. Anggaran bentuk tim
    # tidak menolong di situ - jatahnya pasti terlampaui - jadi tidak dipasang.
    quota: ShapeQuota | None = None

    # --- Konstruksi awal, mengikuti urutan ronde ------------------------
    say(0.04, f"Menyusun pasangan untuk {n} peserta")
    cand_cache: dict[int, list] = {}
    for seg in segments:
        label = seg.label or "babak utama"
        say(0.05, f"Membentuk kombinasi partner - {label}")
        cands = _candidate_rounds(seg, local_players, config, tier_of, locked)
        if not cands:
            raise ScheduleError(
                f"Tidak bisa membentuk pasangan untuk segmen '{seg.label or 'Main'}'."
            )
        cand_cache[id(seg)] = cands

    # --- Anggaran bentuk tim se-meet ------------------------------------
    if pakai_kuota and total_rounds <= len(cand_cache[id(segments[0])]):
        seg = segments[0]
        cands = cand_cache[id(seg)]
        need = 2 * min(courts, n // 4)
        R = total_rounds

        memo: dict[tuple[int, ...], bool] = {}
        options = [_round_options(stok, need, frozenset(izin), memo)
                   for stok in _shape_supply(cands, rules.gender)]

        # Dua putaran: batas pertama dihitung dari semua ronde kandidat
        # (optimistis, tiap bentuk seolah boleh memilih rondenya sendiri),
        # lalu diperketat memakai ronde yang benar-benar terpilih. Kalau
        # setelah itu masih tak terjangkau, lebih baik tanpa anggaran daripada
        # dengan anggaran palsu yang dilesetkan tiap ronde.
        subset = list(range(len(options)))
        for _ in range(2):
            budget = shape_budget(
                n_men, n_women, R * (need // 2), sorted(izin),
                _supply_caps([options[i] for i in subset], R),
            )
            if not (budget.feasible and budget.target):
                break
            target = shape_totals(budget.target)
            subset = _pick_candidate_rounds(options, target, R)
            if _reachable([options[i] for i in subset], target):
                cand_cache[id(seg)] = [cands[i] for i in subset]
                quota = ShapeQuota(target, [options[i] for i in subset])
                break

    # --- Pemerataan jatah main kalau anggaran tidak terpasang ------------
    # Anggaran di atas menuntut lawan 100% unik; begitu itu mustahil - dan pada
    # meet yang rondenya melebihi stok 1-faktorisasi memang selalu mustahil - ia
    # menyerah total dan komposisi format jadi apa adanya. Yang hilang bukan
    # keunikan (itu memang tak terselamatkan) melainkan PEMERATAAN: roster
    # dengan gender timpang bisa berakhir sebagian orang main dua kali lipat
    # yang lain. Lapisan ini hanya mengurus pemerataan itu, tanpa menyentuh
    # syarat keunikan yang dipakai analyze() untuk melapor ke host.
    if quota is None and pakai_kuota:
        seg = segments[0]
        cands = cand_cache[id(seg)]
        need = 2 * min(courts, n // 4)
        memo_b: dict[tuple[int, ...], bool] = {}
        options_b = [_round_options(stok, need, frozenset(izin), memo_b)
                     for stok in _shape_supply(cands, rules.gender)]
        urutan = _balanced_rows(options_b, total_rounds, n_men, n_women, need)
        if urutan:
            cand_cache[id(seg)] = [cands[i] for i in urutan]

    for r_global, (seg, i) in enumerate(plan):
        cands = cand_cache[id(seg)]
        # `i` adalah nomor ronde di dalam segmennya, bukan nomor ronde acara -
        # jadi rotasi pasangannya tetap benar walau rondenya diselang-seling.
        row = list(cands[i % len(cands)])
        if len(row) < 2:
            raise ScheduleError(
                f"Segmen '{seg.label or 'Main'}' tidak punya cukup pemain "
                f"untuk mengisi satu court."
            )
        # Babak "sesama gender": tiap court dikunci ke satu gender, kalau tidak
        # optimizer bisa menyusun tim putri melawan tim putra.
        group_of = ({p.id: (p.gender or "?") for p in local_players}
                    if seg.rule == "same_gender" else None)
        quads = _build_round(row, st, courts, tier_of, rng, weights.rating,
                             group_of=group_of, quota=quota)
        if not quads:
            raise ScheduleError(
                f"Segmen '{seg.label or 'Main'}' tidak bisa mengisi court "
                f"mana pun. Cek jumlah pemain per pool rating."
            )

        playing = {p for q in quads for p in q}
        byes = sorted(set(range(n)) - playing)
        st.place_round(r_global, quads, byes)

    for seg in segments:
        eligible = set(_eligible_for(seg.rule, local_players))
        if seg.rule in ("men", "women"):
            sitting = n - len(eligible)
            if sitting:
                notes.append(
                    f"Segmen '{seg.label}': {sitting} pemain otomatis istirahat "
                    f"karena tidak masuk kriteria gender."
                )

    # --- Optimasi --------------------------------------------------------
    say(0.10, f"Mengoptimasi {total_rounds} ronde")
    anneal(
        st, max(1000, config.effort), rng,
        progress=(lambda f, m: say(0.10 + f * 0.72, m)) if progress else None,
    )
    # Kerataan jumlah main tidak boleh bergantung pada keberuntungan annealing:
    # ini menegakkannya secara deterministik setelahnya.
    say(0.84, "Meratakan jumlah main")
    swaps = rebalance_plays(st)
    plays_now = sorted(play_counts(st))
    say(0.86, f"Perataan: {swaps} pertukaran, jumlah main "
              f"{plays_now[0]}-{plays_now[-1]} ronde")

    # Perataan barusan menukar-nukar pemain tanpa ada yang menilai ulang
    # pertemuannya, jadi ia bisa melahirkan pengulangan baru. Sapuan ini
    # membersihkannya tanpa menyentuh jumlah main.
    say(0.88, "Merapikan sisa pertemuan berulang")
    polish_pairs(st)

    # --- Rakit hasil -----------------------------------------------------
    pref_labels = {
        "women_only": "court isi 4 perempuan",
        "men_only": "court isi 4 laki-laki",
        "same_gender": "court satu gender",
        "mixed_team": "partner lawan jenis",
    }
    name_of = {p.id: p.name for p in local_players}
    violations: list[PreferenceViolation] = []
    for r in range(total_rounds):
        for q in st.matches[r]:
            for pid, pref in rules.pref_violations(q):
                violations.append(
                    PreferenceViolation(
                        round_index=r + 1,
                        player_id=inv[pid],
                        player_name=name_of[pid],
                        preference=pref,
                        reason=(
                            f"Ronde {r + 1}: {name_of[pid]} minta "
                            f"{pref_labels.get(pref, pref)}, tapi komposisi "
                            f"court yang tersedia tidak memungkinkan."
                        ),
                    )
                )

    # Tugas wasit / ballboy diambil dari yang istirahat. Ini murni pasca-proses:
    # tidak mengubah susunan match, hanya memanfaatkan orang yang sedang duduk.
    say(0.90, "Membagi tugas wasit & ballboy")
    byes_seq = [sorted(st.byes[r]) for r in range(total_rounds)]
    courts_seq = [list(range(1, len(st.matches[r]) + 1)) for r in range(total_rounds)]
    role_assign, role_summary = assign_roles(
        byes_seq,
        courts_seq,
        referees_per_court=max(0, config.referees_per_court),
        ballboys_per_court=max(0, config.ballboys_per_court),
        rng=random.Random(config.seed + 991),
    )
    note = coverage_note(byes_seq, courts_seq,
                         max(0, config.referees_per_court),
                         max(0, config.ballboys_per_court))
    if note:
        notes.append(note)

    rounds: list[Round] = []
    start = config.warmup_minutes
    for r in range(total_rounds):
        seg = round_segment[r]
        matches = [
            Match(court=ci + 1, team_a=(inv[q[0]], inv[q[1]]), team_b=(inv[q[2]], inv[q[3]]))
            for ci, q in enumerate(st.matches[r])
        ]
        labels = {}
        if tier_of:
            for ci, q in enumerate(st.matches[r]):
                labels[ci + 1] = f"Pool {tier_of[q[0]] + 1}"
        rounds.append(
            Round(
                index=r + 1,
                matches=matches,
                byes=[inv[p] for p in sorted(st.byes[r])],
                start_min=start,
                end_min=start + round_minutes,
                segment=seg.label,
                court_labels=labels,
                roles=[
                    RoleAssignment(player_id=inv[pid], role=role, court=court)
                    for pid, role, court in role_assign[r]
                ],
            )
        )
        start += round_minutes

    say(0.95, "Menghitung statistik")
    stats = _build_stats(st, local_players, total_rounds)
    # Kembalikan statistik ke id asli.
    stats.plays_per_player = {inv[k]: v for k, v in stats.plays_per_player.items()}
    stats.byes_per_player = {inv[k]: v for k, v in stats.byes_per_player.items()}
    stats.roles_per_player = {inv[k]: v for k, v in role_summary.items()}

    # Sama seperti di run.py: model bentuk hanya berlaku kalau gender lengkap
    # dan seluruh ronde memakai kolam peserta yang sama.
    nilai_bentuk = (
        n_men + n_women == n and all(s.rule == "open" for s in segments)
    )

    cap = analyze(
        n_players=n,
        courts=config.courts,
        duration_minutes=config.duration_minutes,
        round_minutes=round_minutes,
        warmup_minutes=config.warmup_minutes,
        rounds_override=total_rounds,
        men=n_men if nilai_bentuk else None,
        women=n_women if nilai_bentuk else None,
        allowed_matchups=config.allowed_matchups,
    )
    for issue in cap.sorted_issues():
        if issue.severity in ("error", "warning"):
            notes.append(f"{issue.title}: {issue.detail}")

    final_players = sorted(players, key=lambda x: x.id)
    if tier_of:
        tmap = {inv[k]: v for k, v in tier_of.items()}
        for p in final_players:
            p.tier = tmap.get(p.id)

    resolved = Config(
        courts=config.courts,
        duration_minutes=config.duration_minutes,
        round_minutes=round_minutes,
        warmup_minutes=config.warmup_minutes,
        mode=config.mode,
        rounds_override=total_rounds,
        tier_count=config.tier_count,
        seed=config.seed,
        effort=config.effort,
        referees_per_court=config.referees_per_court,
        ballboys_per_court=config.ballboys_per_court,
        segments=segments,
        interleave_segments=config.interleave_segments,
        fit_rounds_to_duration=config.fit_rounds_to_duration,
        allowed_matchups=config.allowed_matchups,
        attempts=config.attempts,
    )

    # Format match yang dilarang tapi tetap muncul. Bisa terjadi kalau susunan
    # peserta memang tidak menyisakan lawan yang sah - mis. hanya 2 putri di
    # antara belasan putra: mereka harus melawan seseorang. Lebih baik satu
    # match melanggar daripada ada peserta yang hilang dari ronde, tapi host
    # harus tahu, bukan menemukannya sendiri dari jadwal.
    if config.allowed_matchups:
        izin = set(config.allowed_matchups)
        gmap = {p.id: p.gender for p in final_players}
        langgar: dict[str, list[int]] = {}
        for rnd in rounds:
            for m in rnd.matches:
                kode = matchup_code(
                    team_shape(gmap.get(m.team_a[0]), gmap.get(m.team_a[1])),
                    team_shape(gmap.get(m.team_b[0]), gmap.get(m.team_b[1])))
                if kode is not None and kode not in izin:
                    langgar.setdefault(kode, []).append(rnd.index)
        if langgar:
            rincian = "; ".join(
                f"{MATCHUP_LABELS.get(k, k).lower()} di ronde "
                f"{', '.join(str(x) for x in v[:5])}"
                for k, v in sorted(langgar.items()))
            notes.append(
                f"{sum(len(v) for v in langgar.values())} match memakai format "
                f"yang Anda larang ({rincian}). Susunan peserta tidak "
                f"menyisakan lawan yang sah untuk mereka - jadwal tetap dibuat "
                f"karena membiarkan mereka tanpa lawan berarti menghapus "
                f"peserta dari ronde itu."
            )

    # Match yang terulang UTUH (empat orang sama, tim sama) paling mudah dikira
    # bug oleh host. Sering kali itu batas matematis: kelompok p orang hanya
    # punya C(p,4) x 3 susunan match, dan untuk 4 orang itu cuma 3. Kalau memang
    # tak terhindarkan, katakan sekalian dengan angkanya.
    repeat_seen: dict[tuple, list[int]] = {}
    for rnd in rounds:
        for m in rnd.matches:
            key = tuple(sorted((tuple(sorted(m.team_a)), tuple(sorted(m.team_b)))))
            repeat_seen.setdefault(key, []).append(rnd.index)
    repeated = {k: v for k, v in repeat_seen.items() if len(v) > 1}
    if repeated:
        detail = "; ".join(
            f"ronde {' & '.join(str(x) for x in v)}"
            for v in list(repeated.values())[:4]
        )
        pool = min(
            (len(set(_eligible_for(seg.rule, local_players))) for seg in segments),
            default=n,
        )
        combos = math.comb(pool, 4) * 3 if pool >= 4 else 0
        # Jarak terdekat antar dua kemunculan yang sama. Optimizer sengaja
        # memaksimalkannya, jadi sebut angkanya: "terulang lagi 9 ronde
        # kemudian" terbaca sangat berbeda dari "terulang lagi ronde depan".
        gap = min(b - a for v in repeated.values() for a, b in zip(v, v[1:]))
        notes.append(
            f"{len(repeated)} match terulang persis sama ({detail}). "
            f"Kelompok terkecil di acara ini {pool} orang, yang hanya punya "
            f"{combos} susunan match berbeda - dengan {total_rounds} ronde, "
            f"pengulangan seperti ini tidak selalu bisa dihindari. "
            f"Jaraknya sudah disebar sejauh mungkin: pengulangan terdekat "
            f"berselang {gap} ronde."
        )

    # Kunci partner berlaku lintas babak, tapi aturan komposisi bisa membuatnya
    # mustahil: pasangan beda gender tidak punya tempat di babak "sesama
    # gender", pasangan sesama gender tidak punya tempat di babak "mixed". Di
    # babak seperti itu kuncinya dilonggarkan supaya orangnya tetap kebagian
    # main - tapi host yang memintanya harus tahu, bukan menemukannya sendiri
    # dari jadwal.
    if locked:
        relaxed: dict[str, set[str]] = {}
        for r, rnd in enumerate(rounds):
            eligible = (rules.round_eligible[r]
                        if r < len(rules.round_eligible) else None)
            for pid in locked:
                # Rekan yang memang tidak turun di babak ini tidak perlu
                # disebut - itu sudah jelas dari aturan babaknya sendiri.
                if eligible is not None and locked[pid] not in eligible:
                    continue
                if rules.active_mate(pid, r) is None:
                    label = rnd.segment or "babak ini"
                    relaxed.setdefault(label, set()).add(local_players[pid].name)
        for label, who in relaxed.items():
            notes.append(
                f"Babak '{label}': partner tetap {', '.join(sorted(who))} tidak "
                f"bisa diberlakukan karena aturan komposisi babak itu, jadi "
                f"mereka dirotasi biasa di sana. Partner tetapnya tetap berlaku "
                f"di babak lain."
            )

    if violations:
        affected = {v.player_name for v in violations}
        notes.append(
            f"{len(violations)} permintaan komposisi court tidak terpenuhi "
            f"({', '.join(sorted(affected))}). Detailnya ada di daftar preferensi."
        )

    # Catatan yang persis sama disatukan, urutannya dipertahankan. Beberapa
    # catatan lahir per segmen, sedangkan satu babak bisa terpecah jadi banyak
    # potongan - selang-seling memecah "Sesama gender 6 ronde" jadi enam
    # potongan 1 ronde, dan host melihat peringatan yang sama enam kali. Isinya
    # identik, jadi pengulangannya murni derau: menutupi catatan lain yang
    # justru perlu dibaca.
    notes = list(dict.fromkeys(notes))

    say(1.0, f"Selesai - kualitas {stats.quality_score}/100")
    return Schedule(players=final_players, config=resolved, rounds=rounds,
                    stats=stats, notes=notes, violations=violations)
