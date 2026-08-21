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

from . import cpsat
from .capacity import (
    analyze,
    bisa_liput_semua,
    rounds_from_duration,
    shape_budget,
    shape_totals,
)
from .factorization import mixed_pair_rounds, subset_pair_rounds
from .models import (
    CPSAT_BASE_MODES,
    CPSAT_MODES,
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
from .report import kolam_partner
from .optimizer import (
    Rules,
    ScheduleState,
    Weights,
    anneal,
    play_counts,
    anneal_giliran,
    polish_pairs,
    ratakan_giliran,
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

# Berapa putaran perataan giliran TAMBAHAN yang boleh dijalankan selama tunggu
# terpanjang masih di atas batas yang tak terhindarkan. Nol = perilaku lama.
# Angkanya dari pengukuran yang dicatat di _build_once; lihat komentar di sana.
_GILIRAN_EKSTRA = 3


def _kunci_giliran(st: ScheduleState, r: int):
    """Pengurut pasangan menurut antrean giliran: makin depan, makin layak turun.

    Sebelumnya prioritasnya adalah TOTAL istirahat sejauh ini
    (`-bye_count[a] - bye_count[b]`). Itu meratakan rekap akhir, bukan
    urutannya - dan keduanya bisa jauh berbeda. Peserta yang sudah main dua kali
    tetap bisa menang atas peserta yang belum main sekali pun, asalkan angka
    istirahat totalnya kebetulan sama besar; ketimpangannya lalu "dibalas" di
    ronde-ronde terakhir sehingga totalnya rata, padahal yang dirasakan orang
    adalah menunggu empat ronde pertama.

    Kuncinya berlapis, dan lapisan pertamanya sengaja `min`, bukan `sum`:

      1. jumlah main pemain yang PALING tertinggal di pasangan ini
      2. total jumlah main pasangan ini
      3. tunggu terpanjang di pasangan ini (makin lama makin didahulukan)
      4. total tunggu pasangan ini

    Lapisan 1 itulah yang menegakkan "tidak ada yang main dua kali sebelum
    semua orang kebagian sekali": pasangan yang memuat orang yang belum pernah
    turun selalu menang, berapa pun angka pasangannya yang lain. Kalau memakai
    `sum`, pasangan (belum pernah main, sudah 6x) dinilai sama dengan pasangan
    (3x, 3x) - dan yang belum pernah main kalah oleh undian.

    Rotasi partner memakai baris 1-faktorisasi, jadi pasangannya sudah tertentu:
    yang bisa dipilih di sini hanya pasangan yang mana yang turun, bukan siapa
    dengan siapa. Karena itu orang yang sudah kebagian banyak kadang tetap ikut
    turun - ia satu pasangan dengan orang yang paling tertinggal.
    """
    def kunci(pr: tuple[int, int]):
        a, b = pr
        main_a, main_b = st.play_count[a], st.play_count[b]
        tunggu_a, tunggu_b = st.wait_before(r, a), st.wait_before(r, b)
        return (
            min(main_a, main_b),
            main_a + main_b,
            -max(tunggu_a, tunggu_b),
            -(tunggu_a + tunggu_b),
        )

    return kunci


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


def _slot_terjangkau(opsi: dict[int, list[list[tuple[int, ...]]]],
                     needs: list[int], rows: list[int],
                     ideal: float) -> set[int]:
    """Semua total slot laki-laki yang bisa dicapai urutan baris ini."""
    sums = {0}
    for k, i in enumerate(rows):
        opts = opsi[needs[k]][i]
        if not opts:
            return set()
        nxt = {s + _gender_slots(o)[0] for s in sums for o in opts}
        if len(nxt) > _MAX_KEADAAN_SLOT:
            nxt = set(sorted(nxt, key=lambda s: abs(s - ideal))[:_MAX_KEADAAN_SLOT])
        sums = nxt
    return sums


def _spread_terbaik(opsi: dict[int, list[list[tuple[int, ...]]]],
                    needs: list[int], rows: list[int],
                    total_slots: int, n_men: int, n_women: int) -> int | None:
    """Spread jatah main terbaik yang masih mungkin dari urutan baris ini."""
    ideal = total_slots * (n_men / (n_men + n_women)) if n_men + n_women else 0.0
    kandidat = [
        s for s in (
            _spread_dari_slot(v, total_slots, n_men, n_women)
            for v in _slot_terjangkau(opsi, needs, rows, ideal)
        ) if s is not None
    ]
    return min(kandidat) if kandidat else None


def _balanced_rows(
    opsi: dict[int, list[list[tuple[int, ...]]]], needs: list[int],
    n_men: int, n_women: int
) -> tuple[list[int], list[int]] | None:
    """Baris kandidat mana yang dipakai tiap ronde, supaya jatah main merata.

    Tanpa ini baris 1-faktorisasi dipakai apa adanya (0,1,2,...,0,1,...) dan
    komposisi format ikut apa adanya juga. Itu bukan detail kecil: annealing
    TIDAK BISA memperbaikinya. Mengubah satu ronde LL-LL jadi LP-LP menuntut
    dua pemain ditukar sekaligus, sedangkan gerakannya satu pemain dan keadaan
    antaranya ilegal - round_legal() menolaknya. Jadi komposisi format praktis
    ditentukan seluruhnya di sini, bukan di optimizer.

    `needs` berisi berapa PASANGAN yang turun di tiap ronde, satu angka per
    ronde. Biasanya sama di semua ronde, tapi tidak wajib: acara yang court-nya
    berkurang di tengah jalan menurunkan lebih sedikit pasangan di ronde-ronde
    terakhir. `opsi[need][baris]` adalah komposisi bentuk tim yang sah untuk
    baris itu pada kebutuhan sebanyak itu - dipisah per `need` karena komposisi
    yang sah untuk 4 pasangan bukan komposisi yang sah untuk 2.

    Contoh nyata: 5 laki-laki + 3 perempuan, 12 ronde, 1 court, format sesama
    bentuk. Baris faktorisasi bergantian 6 baris yang cuma bisa LP-LP dan 6
    yang cuma bisa LL-LL, jadi perempuan main 4x dan laki-laki 7-8x. Susunan
    9 campuran + 3 putra membuat semuanya tepat 6x.

    Yang dikembalikan sepasang: baris kandidat per ronde, DAN berapa slot
    laki-laki yang direncanakan turun di tiap ronde itu. Angka kedua bukan
    hiasan - tanpanya rencana ini tidak berlaku sama sekali. Memilih barisnya
    saja cuma menyediakan komposisi yang tepat; yang memutuskan komposisi mana
    yang benar-benar dipakai adalah _select_pairs_by_shape, dan ia punya
    tujuannya sendiri (giliran, kesegaran lawan). Satu ronde yang seharusnya
    menurunkan tim campur bisa saja menurunkan tim putra karena kebetulan itu
    yang paling menolong antrean saat itu - dan begitu beberapa ronde melenceng,
    pemerataan yang dihitung di sini hilang tanpa jejak.

    None kalau tidak ada susunan yang lebih baik daripada urutan apa adanya.
    """
    n_rounds = len(needs)
    baris_semua = next(iter(opsi.values()), [])
    if not baris_semua or n_rounds <= 0 or n_men + n_women == 0:
        return None
    total_slots = 2 * sum(needs)
    ideal = total_slots * (n_men / (n_men + n_women))

    bawaan = [i % len(baris_semua) for i in range(n_rounds)]
    spread_bawaan = _spread_terbaik(opsi, needs, bawaan, total_slots,
                                    n_men, n_women)

    # Nilai slot laki-laki yang bisa diturunkan satu ronde, dan baris mana saja
    # yang sanggup. Baris boleh dipakai berulang: dengan roster gender timpang,
    # komposisi merata sering menuntut lebih banyak ronde campuran daripada
    # baris campuran yang berbeda - pengulangannya tak terhindarkan, dan
    # menolaknya justru mengunci ketimpangan.
    #
    # Didaftar per `need`, bukan sekali untuk seluruh acara: ronde 1 court dan
    # ronde 2 court tidak menawarkan nilai slot yang sama, dan menyatukannya
    # akan menjanjikan nilai yang ronde bersangkutan tidak sanggup menurunkan.
    baris_untuk: dict[int, dict[int, list[int]]] = {}
    for need, options in opsi.items():
        per: dict[int, list[int]] = {}
        for i, opts in enumerate(options):
            for o in opts:
                per.setdefault(_gender_slots(o)[0], []).append(i)
        baris_untuk[need] = {v: sorted(set(rows)) for v, rows in per.items()}
    if any(not baris_untuk.get(nd) for nd in needs):
        return None

    nilai = [sorted(baris_untuk[nd]) for nd in needs]
    # jangkau[r] = {total slot laki-laki: nilai yang dipakai di ronde ke-r}
    jangkau: list[dict[int, int]] = [{} for _ in range(n_rounds + 1)]
    jangkau[0][0] = -1
    for r in range(n_rounds):
        for s in jangkau[r]:
            for v in nilai[r]:
                jangkau[r + 1].setdefault(s + v, v)
        if len(jangkau[r + 1]) > _MAX_KEADAAN_SLOT:
            # Ronde yang belum dilewati masih bisa menambah antara min dan max
            # slot masing-masing, jadi yang dinilai adalah jarak ke ideal pada
            # akhir nanti, bukan jarak ke ideal sekarang.
            sisa_min = sum(p[0] for p in nilai[r + 1:])
            sisa_max = sum(p[-1] for p in nilai[r + 1:])
            tengah = ideal - (sisa_min + sisa_max) / 2
            terpilih = sorted(jangkau[r + 1], key=lambda s: abs(s - tengah))
            jangkau[r + 1] = {s: jangkau[r + 1][s] for s in terpilih[:_MAX_KEADAAN_SLOT]}
        if not jangkau[r + 1]:
            return None

    def runut(s: int) -> list[int]:
        """Nilai slot yang dipakai tiap ronde, untuk total akhir s."""
        urut = [0] * n_rounds
        for r in range(n_rounds, 0, -1):
            v = jangkau[r][s]
            urut[r - 1] = v
            s -= v
        return urut

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

    # Sebar merata sepanjang acara, bukan berblok. Runutan DP cenderung
    # mengelompokkan ronde sejenis berurutan, dan blok "6 ronde putra" berarti
    # para perempuan duduk enam kali beruntun - persis yang didenda b2b_pen.
    # Posisi pecahan (k+0.5)/jumlah menyebar tiap kelompok serata mungkin.
    #
    # Penyebarannya di dalam ronde yang `need`-nya sama, bukan lintas seluruh
    # acara. Ronde dengan need yang sama menawarkan pilihan nilai yang sama
    # persis, jadi menukar nilai antar mereka selalu sah; menukarnya dengan ronde
    # ber-need lain tidak - nilai 6 slot putra tidak bisa dipindah ke ronde yang
    # cuma menurunkan 2 pasangan.
    urut_nilai = [0] * n_rounds
    for need in dict.fromkeys(needs):
        posisi = [r for r, nd in enumerate(needs) if nd == need]
        jumlah: dict[int, int] = {}
        for r in posisi:
            jumlah[terbaik[2][r]] = jumlah.get(terbaik[2][r], 0) + 1
        tersebar = [
            v for _, v in sorted(
                ((k + 0.5) / jumlah[v], v)
                for v in jumlah for k in range(jumlah[v])
            )
        ]
        for r, v in zip(posisi, tersebar):
            urut_nilai[r] = v

    # Baris untuk tiap nilai, dipakai bergiliran supaya pengulangan tersebar
    # rata alih-alih menumpuk di satu baris.
    pakai: dict[tuple[int, int], int] = {}
    hasil: list[int] = []
    for r, v in enumerate(urut_nilai):
        kandidat = baris_untuk[needs[r]][v]
        kunci = (needs[r], v)
        hasil.append(kandidat[pakai.get(kunci, 0) % len(kandidat)])
        pakai[kunci] = pakai.get(kunci, 0) + 1
    return hasil, urut_nilai


def _slot_plan(opsi: dict[int, list[list[tuple[int, ...]]]], needs: list[int],
               rows: list[int], n_men: int, n_women: int) -> list[int] | None:
    """Berapa slot laki-laki yang harus turun di tiap ronde, urut ronde.

    Kenapa ini perlu terpisah dari _balanced_rows: yang menentukan jatah main
    merata BUKAN baris 1-faktorisasi yang dipakai, melainkan komposisi bentuk
    tim yang benar-benar diturunkan. Baris cuma menyediakan pilihan; yang
    memilih adalah _select_pairs_by_shape.

    Dengan 6 putra dan 4 putri di 1 court dan format sesama-bentuk, tiap ronde
    menurunkan 4 slot putra (LL-LL), 2-2 (LP-LP), atau 4 slot putri (PP-PP).
    Supaya 15 ronde memberi tepat 6 kali main untuk semua orang, total slot
    putra harus tepat 36 - tidak 34, tidak 38. Itu syarat se-MEET yang tidak
    kelihatan dari satu ronde mana pun: tiap ronde tampak sama sahnya, dan
    keputusan lokal yang mengejar antrean giliran akan melencengkannya sedikit
    di banyak ronde sampai jatah mainnya timpang permanen. Timpang permanen,
    karena rebalance_plays() tidak bisa menebusnya - menukar seorang putri
    dengan seorang putra mengubah bentuk tim dan langsung ditolak batas format.

    Dihitung lewat DP atas total slot putra, dan hanya memakai nilai yang
    memang tersedia di baris yang dipakai ronde itu - jadi rencananya bukan
    cita-cita, melainkan sesuatu yang bisa ditepati.

    None kalau tidak ada rencana yang bisa dinilai (mis. ada ronde tanpa
    komposisi sah, atau pesertanya satu gender saja sehingga tidak ada yang
    perlu diseimbangkan).
    """
    if not rows or n_men == 0 or n_women == 0:
        return None
    total_slots = 2 * sum(needs)
    ideal = total_slots * (n_men / (n_men + n_women))

    # nilai[k] = slot putra yang bisa diturunkan ronde ke-k, dari barisnya. Yang
    # dibaca opsi untuk `need` ronde itu: baris yang sama menawarkan komposisi
    # yang berbeda saat yang diminta 4 pasangan dan saat yang diminta 2.
    nilai: list[list[int]] = []
    for k, i in enumerate(rows):
        opts = opsi[needs[k]][i]
        if not opts:
            return None
        nilai.append(sorted({_gender_slots(o)[0] for o in opts}))

    # jangkau[k][total] = nilai yang dipakai di ronde ke-(k-1) untuk sampai ke
    # total itu. Cukup satu jalur per total: yang dicari cuma satu rencana yang
    # sah, bukan semuanya.
    jangkau: list[dict[int, int]] = [{} for _ in range(len(rows) + 1)]
    jangkau[0][0] = -1
    for k, pilihan in enumerate(nilai):
        for s in jangkau[k]:
            for v in pilihan:
                jangkau[k + 1].setdefault(s + v, v)
        if len(jangkau[k + 1]) > _MAX_KEADAAN_SLOT:
            # Ronde yang belum dilewati masih bisa menambah antara min dan max
            # slot masing-masing, jadi yang dinilai adalah jarak ke ideal di
            # akhir nanti - bukan jarak ke ideal sekarang.
            sisa_min = sum(p[0] for p in nilai[k + 1:])
            sisa_max = sum(p[-1] for p in nilai[k + 1:])
            tengah = ideal - (sisa_min + sisa_max) / 2
            urut = sorted(jangkau[k + 1], key=lambda s: abs(s - tengah))
            jangkau[k + 1] = {s: jangkau[k + 1][s]
                              for s in urut[:_MAX_KEADAAN_SLOT]}
        if not jangkau[k + 1]:
            return None

    terbaik = None
    for s in jangkau[len(rows)]:
        sp = _spread_dari_slot(s, total_slots, n_men, n_women)
        if sp is None:
            continue
        kunci = (sp, abs(s - ideal))
        if terbaik is None or kunci < terbaik[0]:
            terbaik = (kunci, s)
    if terbaik is None:
        return None

    hasil: list[int] = []
    s = terbaik[1]
    for k in range(len(rows), 0, -1):
        v = jangkau[k][s]
        hasil.append(v)
        s -= v
    hasil.reverse()
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
    r: int,
    quota: ShapeQuota | None = None,
    slot_target: int | None = None,
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
    # sort() Python stabil dan kunci utamanya tetap antrean giliran, jadi ini
    # cuma mengganti pemutus seri acak dengan pemutus seri yang punya alasan.
    #
    # Hanya dipakai kalau ada anggaran komposisi, yaitu justru saat nol
    # pengulangan memang bisa dicapai. Kalau jatahnya toh pasti jebol - 14 putra
    # 6 putri dengan format dibatasi, misalnya - semua orang minus dan
    # urutannya jadi derau belaka: ia menggeser siapa yang turun tanpa
    # menyelamatkan apa pun, dan malah membuat perataan jumlah main kehilangan
    # pertukaran sah yang tadinya ada.
    if quota is not None:
        sisa = _same_gender_headroom(st)
        giliran = _kunci_giliran(st, r)
        for shape in ("LL", "PP"):
            by_shape[shape].sort(
                key=lambda pr: (
                    giliran(pr),
                    -min(sisa.get(pr[0], 0), sisa.get(pr[1], 0)),
                )
            )

    allowed = frozenset(st.rules.allowed_matchups)
    avail = [len(by_shape[s]) for s in TEAM_SHAPES]
    memo: dict[tuple[int, ...], bool] = {}
    best: tuple[tuple[int, int], int, float, float, tuple[int, ...]] | None = None

    for n_ll in range(avail[0] + 1):
        for n_lp in range(avail[1] + 1):
            n_pp = need - n_ll - n_lp
            if not 0 <= n_pp <= avail[2]:
                continue
            counts = (n_ll, n_lp, n_pp)
            if not _shapes_pairable(counts, allowed, memo):
                continue
            # Sesuai rencana slot gender se-meet? Ini lapisan, bukan saringan:
            # kalau baris yang terpilih ternyata tidak punya komposisi yang
            # cocok, ronde ini tetap harus jadi - lebih baik satu ronde
            # melenceng daripada tidak ada match sama sekali.
            cocok = 1 if (slot_target is None
                          or _gender_slots(counts)[0] == slot_target) else 0
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
            # Urutan lapisannya: jatah bentuk tim, rencana slot gender, baru
            # kesegaran lawan. Dua yang pertama adalah syarat se-MEET -
            # melanggarnya membuat sesuatu mustahil untuk sisa acara (lawan unik
            # jadi mustahil secara aritmetika; jatah main jadi timpang permanen
            # karena tak ada pertukaran sah yang bisa menebusnya).
            #
            # Giliran TIDAK ikut jadi lapisan di sini, dan itu hasil pengukuran
            # bukan kelalaian: karena `cocok` sudah memaku komposisi mana yang
            # boleh dipakai di ronde ini, biasanya cuma tinggal satu komposisi
            # yang sah - jadi lapisan giliran tidak punya apa pun untuk dipilih.
            # Diuji dengan mematikannya pada enam roster: hasilnya identik sampai
            # angka terakhir. Yang mengurus giliran adalah urutan antrean di
            # _kunci_giliran (siapa yang jadi kandidat) dan ratakan_giliran()
            # (perbaikan setelahnya).
            key = (aman, cocok, score, rng.random(), counts)
            if best is None or key[:4] > best[:4]:
                best = key

    if best is None:
        return None
    counts = best[4]
    if quota:
        quota.pakai(counts)
    return [pr for shape, k in zip(TEAM_SHAPES, counts)
            for pr in by_shape[shape][:k]]


def _select_pairs(
    candidates: list[tuple[int, int]],
    st: ScheduleState,
    n_pairs_needed: int,
    rng: random.Random,
    r: int,
    quota: ShapeQuota | None = None,
    slot_target: int | None = None,
) -> list[tuple[int, int]]:
    """Pilih pasangan yang turun, prioritas ke yang paling depan di antrean."""
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
    giliran = _kunci_giliran(st, r)
    scored = sorted(candidates, key=lambda pr: (giliran(pr), rng.random()))
    # Mengizinkan SEMUA format sama saja dengan tidak membatasi apa pun, dan
    # keduanya wajib menghasilkan jadwal yang identik. Kalau jalur sadar-bentuk
    # ikut jalan di situ, ia memakai rng untuk memutus seri dan jadwalnya
    # bergeser tanpa ada satu pun batasan yang ditegakkan.
    if st.rules.allowed_matchups and set(st.rules.allowed_matchups) != _SEMUA_FORMAT:
        picked = _select_pairs_by_shape(scored, st, n_pairs_needed, rng, r, quota,
                                        slot_target)
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
    r: int,
    group_of: dict[int, str] | None = None,
    quota: ShapeQuota | None = None,
    slot_target: int | None = None,
) -> list[list[int]]:
    """Rakit satu ronde: pilih siapa turun, lalu tentukan siapa lawan siapa.

    `group_of` memaksa satu court diisi satu kelompok saja (dipakai babak
    "sesama gender": court putra dan court putri tidak boleh bercampur).
    """
    if group_of is not None:
        pools: dict[str, list[tuple[int, int]]] = {}
        for pr in row:
            pools.setdefault(group_of[pr[0]], []).append(pr)

        # Court diberikan ke kelompok yang antreannya paling tertinggal, diukur
        # dari jumlah main rata-rata anggotanya. Tanpa ini satu gender bisa
        # memonopoli court sepanjang acara.
        #
        # Dulu yang diukur adalah total istirahat rata-rata. Di meet satu babak
        # keduanya identik - istirahat = ronde berjalan dikurangi jumlah main -
        # tapi di meet bersegmen tidak: peserta putri mengumpulkan istirahat
        # sepanjang babak putra tanpa pernah punya kesempatan turun. Angka
        # istirahatnya lalu membengkak oleh ronde yang bukan haknya, dan
        # kelompoknya terlihat paling tertinggal padahal jatah mainnya di babak
        # sesama-gender ini justru sudah sama. Jumlah main tidak punya cacat itu.
        quads: list[list[int]] = []
        left = courts
        order = sorted(
            pools,
            key=lambda k: (
                sum(st.play_count[p] for pr in pools[k] for p in pr)
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
            chosen = _select_pairs(avail, st, take * 2, rng, r)
            quads.extend(_group_into_matches(chosen, st, rng, rating_weight))
            left -= take
        return quads

    if tier_of is None:
        chosen = _select_pairs(row, st, min(courts, len(row) // 2) * 2, rng, r,
                               quota, slot_target)
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
        chosen = _select_pairs(pool_pairs, st, n_courts * 2, rng, r)
        quads.extend(_group_into_matches(chosen, st, rng, rating_weight))
    return quads


# ---------------------------------------------------------------------------
# Statistik
# ---------------------------------------------------------------------------

@dataclass
class _Giliran:
    """Telaah giliran main: angka yang dilaporkan ke host, dan angka yang
    dipakai penjadwalan untuk tahu kapan berhenti merapikannya.

    Dipisahkan dari _build_stats karena tahap perataan giliran perlu tahu
    tunggu terpanjang DAN batas bawahnya di tengah pipeline, sebelum statistik
    lengkap dirakit. Yang penting bukan kerapiannya melainkan bahwa batasnya
    cuma ada SATU: kalau rumusnya disalin jadi versi kedua di tahap perataan,
    meet bersegmen dan acara yang court-nya berkurang akan mengejar target yang
    sebenarnya tak terjangkau - persis kekeliruan yang dijelaskan panjang di
    bagian (2) di bawah, cuma kali ini akibatnya bukan denda yang salah
    melainkan waktu tunggu host yang terbakar tanpa hasil.
    """

    turn_skips: int
    longest_wait: int
    last_first_play: int
    wait_floor: int
    # Berapa ronde tiap orang benar-benar BERHAK turun. Dipakai penyebut
    # duduk-beruntun di _build_stats, jadi ikut dikembalikan daripada dihitung
    # dua kali.
    ronde_berhak: dict[int, int]
    berhak_set: list[set[int]]
    duduk_berhak: list[set[int]]


def _telaah_giliran(st: ScheduleState, ids: list[int], n_rounds: int,
                    plays: dict[int, int]) -> _Giliran:
    """Hitung serobotan, tunggu terpanjang, dan batas bawah tunggu.

    `plays` = berapa kali tiap peserta benar-benar turun, dihitung dari jadwal
    akhir oleh pemanggil.
    """
    # Giliran: berapa kali seseorang turun untuk kali ke-(k+1) padahal masih ada
    # orang lain yang sedang duduk dan belum kebagian kali ke-k. Dihitung ulang
    # dari jadwal akhir, bukan diambil dari hitungan konstruksi - annealing,
    # perataan, dan sapuan terakhir semuanya menggeser isi ronde sesudahnya.
    #
    # Peserta yang memang tidak berhak turun di ronde itu - peserta putri di
    # babak putra - TIDAK dihitung sedang menunggu: ia bukan sedang dilewati,
    # ia sedang tidak berhak. Aturan ini sudah dipakai optimizer.turn_skips(),
    # tapi perhitungan di sini dulu tidak menyaring apa pun, jadi angka yang
    # dioptimasi dan angka yang dilaporkan ke host adalah dua angka yang
    # berbeda. Diukur pada 8 putra + 8 putri di 2 court dengan babak putra lalu
    # babak putri: yang dilaporkan 40 serobotan, tunggu 6 dari batas 1, dan
    # match pertama di ronde 7 - sementara yang benar-benar terjadi 0
    # serobotan. Host lalu diberi peringatan untuk keadaan yang tidak ada,
    # lengkap dengan saran "tambah court" yang tidak menyentuh sebabnya; obat
    # yang benar untuk babak berurutan adalah interleave_segments.
    #
    # Karena itu semuanya dihitung dalam RONDE MILIK PESERTA ITU SENDIRI, yaitu
    # ronde tempat ia berhak turun. Di meet tanpa babak, tiap orang berhak di
    # semua ronde dan angkanya persis sama seperti sebelumnya.
    sudah = {pid: 0 for pid in ids}
    turn_skips = 0
    tunggu_max = 0
    menunggu = {pid: 0 for pid in ids}
    ronde_berhak = {pid: 0 for pid in ids}
    main_pertama: dict[int, int] = {}
    duduk_berhak: list[set[int]] = []
    berhak_set: list[set[int]] = []
    for r in range(n_rounds):
        turun = {p for q in st.matches[r] for p in q}
        elig = (st.rules.round_eligible[r]
                if r < len(st.rules.round_eligible) else None)
        berhak = [p for p in ids if elig is None or p in elig or p in turun]
        berhak_set.append(set(berhak))
        duduk_berhak.append({p for p in berhak if p not in turun})
        if turun:
            tertinggal = min((sudah[p] for p in berhak if p not in turun),
                             default=None)
            if tertinggal is not None:
                turn_skips += sum(1 for p in turun if sudah[p] > tertinggal)
        for p in turun:
            sudah[p] += 1
        for p in berhak:
            ronde_berhak[p] += 1
            if p in turun:
                menunggu[p] = 0
                main_pertama.setdefault(p, ronde_berhak[p])
            else:
                menunggu[p] += 1
                tunggu_max = max(tunggu_max, menunggu[p])
    # Yang belum pernah turun sama sekali menunggu sepanjang acara.
    last_first_play = (max(main_pertama.values(), default=0)
                       if len(main_pertama) == len(ids) else n_rounds)
    # Tunggu terpanjang yang tak terhindarkan. Dua sebab yang sama sekali
    # berbeda, dan yang berlaku adalah yang terbesar.
    #
    # (1) Per orang: dengan `plays` kali main, ronde duduknya bisa dipecah ke
    #     paling banyak plays+1 rentetan, jadi rentetan terpanjangnya minimal
    #     sebanyak itu.
    wait_floor = max(
        (math.ceil((ronde_berhak[p] - plays[p]) / (plays[p] + 1))
         if ronde_berhak[p] - plays[p] > 0 else 0)
        for p in ids
    ) if ids else 0

    # (2) Kapasitas ronde, lewat pigeonhole. Kalau di dua ronde berurutan yang
    #     duduk 6 dan 6 orang sedangkan yang berhak turun cuma 10, minimal 2
    #     orang duduk di kedua-duanya - berapa pun pintarnya penjadwal. Sebabnya
    #     bukan rotasi: ronde berikutnya hanya punya 4 tempat untuk 6 orang yang
    #     baru saja duduk.
    #
    #     Rumus (1) tidak melihat ini. Ia mengandaikan ronde duduk seseorang
    #     boleh disebar ke mana saja sepanjang acara, padahal slot per ronde
    #     yang membatasi. Pada acara yang court-nya berkurang di tengah jalan
    #     bedanya nyata dan langsung terbaca host: 10 peserta, ronde 11-15 turun
    #     ke 1 court, rumus (1) memberi batas 1 ronde sehingga jadwal yang
    #     tunggu terpanjangnya 2 dituduh gagal - padahal 2 memang tak
    #     terhindarkan, dan skor kualitasnya ikut kena denda yang tidak ia
    #     sebabkan.
    #
    #     Untuk jendela L ronde berurutan: yang berhak di SEMUANYA sebanyak |U|,
    #     dan kalau jumlah yang duduk di tiap ronde (dihitung di dalam U) melebihi
    #     (L-1)*|U|, ada minimal satu orang yang duduk di seluruh L ronde itu.
    #     Batasnya tidak naik lagi begitu ia jadi <= 0 - tiap ronde tambahan
    #     menambah paling banyak |U| dan mengurangi tepat |U| - jadi
    #     perpanjangannya bisa dihentikan di situ, bukan diteruskan sampai habis.
    for awal in range(n_rounds):
        L = 2
        while awal + L <= n_rounds:
            U = set.intersection(*berhak_set[awal:awal + L])
            if not U:
                break
            bersama = (sum(len(duduk_berhak[k] & U) for k in range(awal, awal + L))
                       - (L - 1) * len(U))
            if bersama <= 0:
                break
            wait_floor = max(wait_floor, L)
            L += 1

    return _Giliran(
        turn_skips=turn_skips,
        longest_wait=tunggu_max,
        last_first_play=last_first_play,
        wait_floor=wait_floor,
        ronde_berhak=ronde_berhak,
        berhak_set=berhak_set,
        duduk_berhak=duduk_berhak,
    )


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

    # Duduk-beruntun dihitung setelah blok giliran di bawah, karena penyebutnya
    # butuh berapa ronde tiap orang benar-benar berhak turun.

    g = _telaah_giliran(st, ids, n_rounds, plays)
    turn_skips = g.turn_skips
    tunggu_max = g.longest_wait
    last_first_play = g.last_first_play
    wait_floor = g.wait_floor
    ronde_berhak = g.ronde_berhak
    berhak_set = g.berhak_set
    duduk_berhak = g.duduk_berhak

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

    # Kerataan jatah main diukur DI DALAM kelompok yang memperebutkan slot yang
    # sama, bukan lintas seluruh peserta.
    #
    # Dua orang yang berhak turun di ronde yang sama persis memang bersaing
    # memperebutkan slot yang sama, jadi selisih main di antara mereka adalah
    # kesalahan rotasi dan harus didenda. Selisih ANTAR kelompok bukan: ia
    # ditentukan berapa slot yang tersedia untuk tiap kelompok, dan itu hasil
    # komposisi roster ditambah aturan babak. Tidak ada jadwal yang bisa
    # mengubahnya.
    #
    # Diukur pada 7 meet bersegmen, selisih di dalam kelompok selalu 0 atau 1 -
    # rotasinya memang sudah rapi - sementara selisih global 2 sampai 7, dan
    # dendanya tersaturasi di 1,0 pada enam di antaranya. Dengan bobot 15, itu
    # potongan terbesar setelah keunikan, diberikan untuk sesuatu yang tidak
    # bisa diperbaiki siapa pun; dan begitu tersaturasi ia berhenti membedakan
    # jadwal rapi dari jadwal kacau. Contoh terjelasnya 20 putra + 4 putri
    # dengan babak putra/putri/mixed: para putra main 3 ronde dan para putri 10,
    # selisih di dalam kelompok NOL, dan skornya tetap kehilangan 15 poin penuh.
    #
    # Ketimpangan antar kelompok tidak disembunyikan - ia dikatakan sebagai
    # catatan dengan angkanya, tempat host bisa menindaklanjutinya. Di meet
    # tanpa babak semua orang satu kelompok, jadi angkanya persis sama seperti
    # sebelumnya.
    kelompok_main: dict[tuple, list[int]] = {}
    for pid in ids:
        sig = tuple(r for r in range(n_rounds)
                    if r >= len(st.rules.round_eligible)
                    or pid in st.rules.round_eligible[r])
        kelompok_main.setdefault(sig, []).append(pid)
    spread = max(
        (max(plays[p] for p in g) - min(plays[p] for p in g)
         for g in kelompok_main.values()),
        default=0,
    )
    bye_pen = min(1.0, spread / 3.0)

    # Duduk-beruntun diukur relatif terhadap rentangnya yang benar-benar bisa
    # dicapai, bukan dibagi jumlah peserta.
    #
    # Dulu: min(1.0, b2b / jumlah_peserta). Itu tersaturasi di 1.0 pada hampir
    # semua meet yang courtnya sedikit, dan begitu tersaturasi ia berhenti
    # menjadi ukuran: 10 orang di 1 court punya b2b antara 20 dan 80, jadi
    # jadwal terbaik dan terburuk sama-sama dinilai 1.0 dan kehilangan 10 poin
    # yang sama. Skornya lalu tidak bisa membedakan jadwal yang rotasinya rapi
    # dari yang kacau - termasuk saat memilih di antara beberapa percobaan.
    #
    # Batas bawahnya: pemain yang main m dari R ronde punya R-m ronde duduk yang
    # bisa dipecah ke paling banyak m+1 rentetan, jadi sisanya pasti bersebelahan.
    # Batas atasnya: seluruh ronde duduknya menyatu jadi satu rentetan.
    #
    # R di sini ronde yang PESERTANYA BERHAK turun, dan pasangan ronde yang
    # dihitung hanya yang ia berhak di keduanya. Tanpa itu cacat saturasi yang
    # sama kambuh lewat babak alih-alih lewat jumlah court: pada 8 putra + 8
    # putri di 2 court dengan babak putra lalu babak putri, para putri duduk
    # sepanjang babak putra dan b2b mentahnya 80 dari atap 80 - denda penuh 1,0
    # untuk jadwal yang sebenarnya tidak mendudukkan siapa pun di ronde yang ia
    # berhak mainkan. Dihitung dengan menghormati kelayakan, b2b-nya 0 dari atap
    # 0. Meet tanpa babak tidak berubah sama sekali: tiap orang berhak di semua
    # ronde, jadi kedua hitungan identik (diukur: 23 lawan 23, dan 15 lawan 15).
    b2b = sum(len(duduk_berhak[r] & duduk_berhak[r + 1])
              for r in range(n_rounds - 1))
    b2b_floor = sum(max(0, (ronde_berhak[p] - plays[p]) - (plays[p] + 1))
                    for p in ids)
    b2b_ceil = sum(max(0, (ronde_berhak[p] - plays[p]) - 1) for p in ids)
    # Duduk beruntun yang dipaksa kapasitas, dengan pigeonhole yang sama seperti
    # di wait_floor: di tiap pasangan ronde bersebelahan, yang duduk di
    # kedua-duanya minimal sebanyak kelebihan tempat duduk atas jumlah orang yang
    # berhak. Rumus per-orang di atas buta terhadap ini dan melaporkan batas 0
    # untuk acara 1 court yang b2b-nya justru sudah mentok - lalu jadwalnya kena
    # denda penuh untuk sesuatu yang tidak bisa dikurangi sama sekali.
    b2b_paksa = 0
    for r in range(n_rounds - 1):
        U = berhak_set[r] & berhak_set[r + 1]
        if U:
            b2b_paksa += max(0, len(duduk_berhak[r] & U)
                             + len(duduk_berhak[r + 1] & U) - len(U))
    # Tetap di bawah atap: penyebut denda memakai selisih atap-lantai, dan lantai
    # yang melewati atap membuatnya negatif.
    b2b_floor = min(max(b2b_floor, b2b_paksa), b2b_ceil)
    b2b_pen = min(1.0, max(0, b2b - b2b_floor) / max(1, b2b_ceil - b2b_floor))

    # Keadilan giliran, dua sisi yang berbeda dan dua-duanya perlu: berapa kali
    # antrean diserobot, dan seberapa jauh tunggu terpanjang melewati yang
    # memang tak terhindarkan.
    #
    # Dijumlah lalu dibatasi, BUKAN diambil yang terbesar. Dengan max(), yang
    # satu menutupi yang lain: pada 10 peserta di 1 court, tunggu 3 ronde dari
    # batas 2 sudah memberi 0.5, dan serobotan boleh membengkak dari 4 ke 15
    # tanpa mengubah skor sama sekali. Skor yang tidak bergerak berarti pemilih
    # antar-percobaan tidak bisa melihat bedanya - jadi ayunan serobotan sebesar
    # itu lewat tanpa terbaca di mana pun.
    lewat_pen = min(1.0, turn_skips / max(1, sum(play_vals)))
    tunggu_pen = min(1.0, max(0, tunggu_max - wait_floor) / 2.0)
    giliran_pen = min(1.0, lewat_pen + tunggu_pen)

    # 10 poin terakhir dibagi dua antara duduk-beruntun dan keadilan giliran.
    # Jatah partner (45) dan lawan (30) tidak disentuh: keunikan tetap yang
    # utama, giliran adalah pemutus di antara jadwal yang keunikannya setara.
    score = 100.0 - 45 * min(1.0, p_pen) - 30 * min(1.0, o_pen) \
        - 15 * bye_pen - 5 * b2b_pen - 5 * giliran_pen

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
        turn_skips=turn_skips,
        longest_wait=tunggu_max,
        last_first_play=last_first_play,
        wait_floor=wait_floor,
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

def _zero_repeats_possible(players: list[Player], config: Config,
                           courts_per_round: list[int] | None = None) -> bool:
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
    # Jumlah match seluruh acara, dihitung dari court yang benar-benar dipakai
    # tiap ronde. Acara yang court-nya berkurang di tengah jalan memberi lebih
    # sedikit ronde main per orang, dan itu bisa mengubah jawabannya: yang
    # menentukan mustahil-tidaknya adalah berapa kali orang turun, bukan berapa
    # court yang pernah tersedia.
    matches = (sum(min(c, n // 4) for c in courts_per_round)
               if courts_per_round else
               total_rounds * min(config.courts, n // 4))
    if math.ceil(matches * 4 / n) > (n - 1) // 2:
        return False
    men = sum(1 for p in players if p.gender == "M")
    women = sum(1 for p in players if p.gender == "F")
    if config.allowed_matchups and men + women == n:
        return shape_budget(
            men, women, matches,
            sorted(set(config.allowed_matchups)),
        ).feasible is not False
    return True


def _catatan_cpsat(lapor, rep_pc: int, rep_oc: int) -> str:
    """Satu kalimat tentang apa yang benar-benar dicapai solver.

    "Terbukti optimal" dan "yang terbaik dalam 30 detik" adalah dua klaim yang
    sangat berbeda, dan host berhak tahu yang mana yang sedang ia pegang - itu
    satu-satunya cara ia bisa tahu kapan menaikkan batas waktu ada gunanya, dan
    kapan justru setupnya yang harus diubah.

    Catatan yang menyertai kegagalan (OR-Tools tidak terpasang, solver tidak
    menemukan apa pun) sudah ditulis Hasil.catatan; yang di sini khusus soal
    MUTU jadwal yang akhirnya dipakai.
    """
    if lapor.status in ("tidak jalan", "OR-Tools tidak terpasang"):
        return ("Mode CP-SAT tidak bisa dijalankan, jadi jadwal ini murni hasil "
                "mesin biasa - sama persis dengan mode Americano.")

    # Angkanya disebut, bukan cuma kata "optimal": yang dibuktikan solver adalah
    # bahwa DUA ANGKA INI tidak bisa lebih kecil lagi, dan host perlu melihat
    # angka mana yang sedang dijamin.
    capaian = (f"{rep_pc} pasang partner berulang dan {rep_oc} pasang lawan "
               f"berulang")
    if lapor.terbukti_optimal:
        pokok = (
            f"Mode CP-SAT: {capaian} - dan itu TERBUKTI tidak bisa lebih kecil "
            f"lagi, dibuktikan dalam {lapor.detik:.1f} detik. Mengulang dengan "
            f"seed lain atau menambah waktu tidak akan menurunkannya; yang "
            f"tersisa cuma mengubah setupnya sendiri (jumlah court, ronde, "
            f"atau peserta)."
        )
        if not lapor.membaik:
            pokok += (" Mesin biasa ternyata sudah menyentuh batas itu; yang "
                      "dibeli waktu tadi adalah kepastiannya.")
        return pokok

    celah = ""
    if lapor.objective and lapor.batas_bawah is not None and lapor.objective > 0:
        sisa = 100.0 * (lapor.objective - lapor.batas_bawah) / lapor.objective
        celah = (f" Jarak ke batas bawah yang sudah terbukti masih "
                 f"{max(0.0, sisa):.0f}%.")
    if lapor.membaik:
        return (
            f"Mode CP-SAT: {capaian}. Solver berhasil memperbaiki jadwal mesin "
            f"biasa dalam {lapor.detik:.1f} detik, tapi belum sempat MEMBUKTIKAN "
            f"tidak ada yang lebih baik lagi.{celah} Naikkan batas waktunya "
            f"kalau mau dikejar lebih jauh."
        )
    return (
        f"Mode CP-SAT: {capaian}. Dalam {lapor.detik:.1f} detik solver tidak "
        f"menemukan yang lebih baik daripada mesin biasa, dan juga belum sempat "
        f"membuktikan bahwa memang tidak ada.{celah} Jadwal yang Anda pegang "
        f"sama dengan hasil mode Americano - naikkan batas waktunya kalau mau "
        f"kepastiannya."
    )


def _catatan_dasar(lapor, capaian_solver: tuple[int, int],
                   capaian_akhir: tuple[int, int]) -> str:
    """Satu kalimat tentang jadwal yang disusun solver sebagai mesin dasar.

    Bedanya dengan _catatan_cpsat bukan gaya bahasa. Di mode penyempurna,
    pembanding solver adalah jadwal mesin biasa yang sudah matang, jadi "solver
    tidak menemukan yang lebih baik" berarti kabar baik - jadwalnya sudah bagus.
    Di sini pembandingnya cuma konstruksi awal, jadi kalimat yang sama berarti
    kabar buruk: solver kehabisan waktu sebelum sampai ke mana-mana, dan yang
    dipegang host adalah jadwal yang belum dioptimasi siapa pun. Menyamakan
    keduanya berarti menyembunyikan satu-satunya keadaan yang benar-benar perlu
    ditindak host.

    Dua pasang angka, dan keduanya disebut kalau berbeda: apa yang dicapai
    solver sendiri, dan apa yang akhirnya dipegang host setelah perapian.
    Menyebut cuma yang pertama berarti angka di catatan tidak cocok dengan
    angka di statistik; menyebut cuma yang kedua berarti sumbangan solver
    dilaporkan lebih besar daripada yang sebenarnya.
    """
    if lapor.status in ("tidak jalan", "OR-Tools tidak terpasang"):
        return ("Solver eksak tidak bisa dijalankan, jadi jadwal ini disusun "
                "mesin biasa - sama persis dengan mode Americano.")

    def sebut(capaian: tuple[int, int]) -> str:
        return (f"{capaian[0]} pasang partner berulang dan {capaian[1]} pasang "
                f"lawan berulang")

    akhir = sebut(capaian_akhir)
    # Perapian setelah solver memang bisa menolong, dan di setup besar ia
    # menolong banyak - itu justru bukti bahwa solver dari nol belum sampai ke
    # dasar ruang pencarian. Disebut apa adanya.
    rapi = ""
    if capaian_akhir != capaian_solver:
        rapi = (f" Solver sendiri berhenti di {sebut(capaian_solver)}; sisanya "
                f"hasil perapian (pemerataan main & giliran) sesudahnya.")

    if not lapor.dipakai:
        # Dua sebab yang sangat berbeda, dan menyatukannya membuat host menaikkan
        # batas waktu untuk sesuatu yang tidak akan pernah tertolong waktu.
        if lapor.terbukti_optimal:
            return (
                f"Solver sebagai mesin dasar: jadwalnya TIDAK dipakai. Solver "
                f"membuktikan biaya modelnya optimal dalam {lapor.detik:.1f} "
                f"detik, tapi jadwal itu kalah menurut ukuran yang dipakai "
                f"aplikasi ini - yang menaruh partner berulang di atas lawan "
                f"berulang, sementara model solver menimbang keduanya sebagai "
                f"satu jumlah. Jadi yang dipertahankan jadwal konstruksi awal "
                f"({akhir}). Menambah waktu tidak akan mengubahnya; mode "
                f"'Americano' biasa yang paling menolong di setup ini."
            )
        return (
            f"Solver sebagai mesin dasar: dalam {lapor.detik:.1f} detik solver "
            f"TIDAK sampai melampaui konstruksi awal, jadi jadwal ini pada "
            f"dasarnya belum dioptimasi solver ({akhir}). Setup sebesar ini di "
            f"luar jangkauan solver tanpa titik awal - naikkan batas waktunya, "
            f"atau pakai mode 'Americano + solver eksak' yang memakai annealing "
            f"dulu lalu solver di ujungnya."
        )

    if lapor.terbukti_optimal:
        return (
            f"Solver sebagai mesin dasar: {akhir} - dan biaya modelnya TERBUKTI "
            f"tidak bisa lebih kecil lagi, dibuktikan dalam {lapor.detik:.1f} "
            f"detik dari nol, tanpa dibantu annealing sama sekali. Mengulang "
            f"dengan seed lain atau menambah waktu tidak akan menurunkannya; "
            f"yang tersisa cuma mengubah setupnya sendiri (jumlah court, ronde, "
            f"atau peserta).{rapi}"
        )

    celah = ""
    if lapor.objective and lapor.batas_bawah is not None and lapor.objective > 0:
        sisa = 100.0 * (lapor.objective - lapor.batas_bawah) / lapor.objective
        celah = (f" Jarak ke batas bawah yang sudah terbukti masih "
                 f"{max(0.0, sisa):.0f}%.")
    return (
        f"Solver sebagai mesin dasar: {akhir}, disusun dari nol oleh solver "
        f"dalam {lapor.detik:.1f} detik - tapi belum sempat MEMBUKTIKAN tidak "
        f"ada yang lebih baik.{celah}{rapi} Naikkan batas waktunya kalau mau "
        f"kepastiannya, dan bandingkan dengan mode Americano biasa: di setup "
        f"besar annealing masih sering lebih rapi daripada solver dari nol."
    )


def _lebih_baik(a: Schedule, b: Schedule) -> bool:
    """Apakah jadwal a lebih layak dipakai daripada b?

    Urutannya sengaja bukan skor kualitas semata. Pengulangan partner lebih
    dulu, lalu pengulangan lawan, baru kualitas. Peserta mengingat "kok lawan
    dia lagi", bukan selisih 0.4 poin di angka kualitas - dan skor kualitas
    memberi lawan cuma 30 dari 100, jadi memilih dengannya bisa membuang jadwal
    yang lawannya bersih demi jadwal yang istirahatnya sedikit lebih rapi.

    KONSEKUENSINYA UNTUK GILIRAN, diukur supaya tidak salah diharapkan. Karena
    urutan ini leksikografis, percobaan tambahan dibelanjakan untuk keunikan
    lawan lebih dulu, dan giliran cuma ikut apa adanya. Diuji pada 4 konfigurasi
    x 12 seed dengan attempts 1, 3, dan 6 pada effort tetap:

      lawan berulang turun MONOTON di keempatnya
        16L+10P/4court  5,6 -> 4,7 -> 3,6
        6L+4P/1court   15,5 -> 13,9 -> 13,2
        10L+6P/2court  25,1 -> 24,3 -> 22,8
        6L+6P/2court   40,5 -> 40,3 -> 39,3
      serobotan justru MEMBURUK di dua dari empat
        10L+6P/2court  18,5 -> 17,8 -> 20,3
        6L+6P/2court   12,1 -> 13,4 -> 13,7
      dan sebarannya tidak menyempit sama sekali: pada 16L+10P simpangan
        bakunya 20,3 -> 18,2 -> 20,6, rentangnya tetap 37 sampai sekitar 100

    Jadi attempts adalah kendali untuk KEUNIKAN, bukan untuk giliran. Menaikkan
    attempts demi merapikan giliran tidak akan menolong, dan itu bukan cacat
    melainkan akibat langsung dari urutan di fungsi ini.
    """
    x, y = a.stats, b.stats
    return ((x.partner_repeat_pairs, x.opponent_repeat_pairs, -x.quality_score)
            < (y.partner_repeat_pairs, y.opponent_repeat_pairs, -y.quality_score))


def build_schedule(players: list[Player], config: Config,
                   progress=None,
                   courts_per_round: list[int] | None = None) -> Schedule:
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

    `courts_per_round` opsional: berapa court yang tersedia di tiap ronde, satu
    angka per ronde, untuk acara yang court-nya tidak sama sepanjang jam sewa
    (mis. court kedua dilepas setelah dua jam). Kosong = config.courts untuk
    semua ronde, dan hasilnya identik dengan sebelum parameter ini ada.
    """
    # Solver eksak sebagai mesin dasar tidak ikut multi-start, dan itu bukan
    # penghematan malas. Multi-start ada untuk annealing, yang berhenti di
    # optimum lokal berbeda-beda tergantung lintasan acaknya; CP-SAT tidak punya
    # lintasan acak yang bisa diadu - dengan model dan batas waktu yang sama ia
    # menempuh pencarian yang sama. Yang berbeda antar percobaan cuma konstruksi
    # awalnya, dan di mode ini konstruksi awal bahkan tidak dipakai sebagai hint.
    # Jadi tiga percobaan berarti membayar tiga kali batas waktu solver untuk
    # jadwal yang sama, dan anggaran yang sama jauh lebih berguna diberikan
    # SELURUHNYA ke satu pencarian - lihat cpsat_seconds.
    dasar = config.mode in CPSAT_BASE_MODES
    percobaan = 1 if dasar else max(1, config.attempts)
    # Dulu percobaan dipangkas jadi satu begitu pengulangan lawan wajib
    # terjadi, karena tiap percobaan berhenti di sekitar batas bawah yang sama
    # dan bedanya tinggal derau - diukur: 60 orang / 15 court / 20 ronde tetap
    # 3 pasang berulang setelah 3 percobaan, sementara waktunya naik dari 3,2
    # ke 9,8 detik. Waktu yang dibayar tidak membeli apa pun.
    #
    # Pemangkasan itu tetap salah, tapi ALASANNYA berbeda dari yang pernah
    # ditulis di sini, dan dua-duanya sudah diukur ulang.
    #
    # Alasan lama pemangkasan - "kalau pengulangan memang wajib, percobaan
    # tambahan cuma menemukan derau" - keliru. Diuji pada 4 konfigurasi x 12
    # seed yang SEMUANYA punya pengulangan lawan tak terhindarkan, attempts 1
    # -> 3 -> 6 menurunkan pasang lawan berulang secara monoton di keempatnya:
    # 5,6 -> 3,6 pada 16 putra + 10 putri di 4 court, dan 25,1 -> 22,8 pada 10
    # putra + 6 putri di 2 court. Jadi percobaan tambahan memang membeli
    # sesuatu, persis di keadaan yang dulu dianggap sia-sia.
    #
    # Alasan yang sempat menggantikannya - "giliran BUKAN derau antar
    # percobaan" - juga keliru, dan ke arah sebaliknya. Serobotan berayun lebar
    # antar lintasan acak dan attempts tidak menyempitkannya: pada 16 putra +
    # 10 putri simpangan bakunya 20,3 di attempts=1 dan 20,6 di attempts=6,
    # rentangnya tetap 37 sampai sekitar 100, dan pada dua dari empat
    # konfigurasi rata-ratanya justru naik. Lihat _lebih_baik: karena urutannya
    # leksikografis, percobaan tambahan dibelanjakan untuk keunikan lebih dulu.
    #
    # Ringkasnya: percobaan tetap dijalankan, tapi yang dibelinya keunikan.
    # Giliran ikut apa adanya, dan _lebih_baik memutus di antara yang
    # keunikannya setara.
    terbaik: Schedule | None = None
    # Config percobaan yang sedang memimpin. Hanya dipakai mode CP-SAT, yang
    # menjalankan solvernya SEKALI di atas pemenang - bukan di tiap percobaan.
    # Membagi anggaran waktu solver ke beberapa percobaan selalu merugi: satu
    # pencarian 30 detik dari titik awal terbaik mengalahkan tiga pencarian 10
    # detik dari titik awal yang sebagian memang lebih buruk.
    cfg_terbaik: Config | None = None
    # Strategi komposisi milik percobaan yang memimpin. Ikut disimpan supaya
    # putaran CP-SAT di bawah mengulang percobaan yang SAMA - tanpa ini ia
    # mengulang dengan strategi bawaan dan mendarat di jadwal lain sebelum
    # solvernya mulai, sehingga titik awalnya bukan lagi yang menang.
    kuota_terbaik = False
    # Berapa bagian dari batang kemajuan yang dipegang rangkaian percobaan.
    # Mode CP-SAT menambahkan satu putaran lagi setelah semuanya selesai, jadi
    # kalau percobaan tetap memakai seluruh batang, batangnya penuh lalu mundur
    # ke nol - dan host membaca itu sebagai jadwalnya diulang dari awal.
    #
    # Mode solver-sebagai-dasar tidak butuh putaran itu: percobaannya cuma satu,
    # jadi pemenangnya sudah pasti sejak awal dan solver bisa langsung jalan di
    # dalamnya. Mengulang di sini berarti menjalankan solver dua kali.
    perlu_putaran_akhir = (not dasar
                           and (config.mode in CPSAT_MODES
                                or config.lns_seconds > 0))
    bagian = 0.5 if perlu_putaran_akhir else 1.0
    # Berapa ronde yang dibutuhkan supaya semua peserta kebagian match pertama,
    # kalau tiap slot dipakai untuk orang yang berbeda. Dipakai sebagai syarat
    # berhenti lebih awal di bawah.
    # Dihitung ronde demi ronde, bukan dibagi rata: kalau court-nya berkurang di
    # tengah acara, slot per ronde tidak satu angka.
    urut_court = list(courts_per_round) if courts_per_round else [config.courts]
    putaran, terisi = 0, 0
    while terisi < len(players):
        c = urut_court[min(putaran, len(urut_court) - 1)]
        terisi += 4 * max(1, min(c, len(players) // 4))
        putaran += 1

    for k in range(percobaan):
        # Tiap percobaan dapat lintasan acak yang berbeda, tapi tetap turunan
        # deterministik dari seed host.
        cfg = config if k == 0 else replace(config, seed=config.seed + 1000 * k)

        def teruskan(frac, msg, k=k):
            if progress is not None:
                awal = (k / percobaan) * bagian
                label = msg if percobaan == 1 else f"[{k + 1}/{percobaan}] {msg}"
                progress(awal + frac / percobaan * bagian, label)

        # Mengarahkan komposisi format saat lawan unik MUSTAHIL adalah taruhan,
        # bukan perbaikan pasti, jadi ia diadu di sini alih-alih dipaksakan.
        #
        # Ia menang besar ketika komposisi yang dipilih sendiri oleh penjadwal
        # memang boros terhadap kolam pasangan yang langka - pada 26 peserta
        # (18 putra, 8 putri) di 4 court: 11-12 pasang lawan berulang turun ke
        # 4, batas aritmetikanya, di tiga seed yang dicoba. Tapi ia kalah
        # ketika komposisi itu sudah bagus tanpa diarahkan, karena memasang
        # kuota ikut mempersempit ronde kandidat yang boleh dipakai annealer.
        # Disapu pada 12 kombinasi roster: 7 menang, 4 kalah, 1 seri.
        #
        # Karena itu keputusannya diserahkan ke multi-start, yang memang ada
        # untuk ini. Percobaan ke-0 memakai perilaku lama persis, jadi
        # attempts=1 tidak berubah sama sekali; sisanya berselang-seling, dan
        # _lebih_baik() memungut yang menang. Hasilnya tidak pernah lebih buruk
        # daripada salah satu strategi sendirian.
        kuota_mustahil = k % 2 == 1
        sch = _build_once(players, cfg, teruskan if progress else None,
                          courts_per_round, kuota_mustahil=kuota_mustahil,
                          cpsat_dasar=dasar,
                          pakai_lns=dasar and config.lns_seconds > 0)
        if terbaik is None or _lebih_baik(sch, terbaik):
            terbaik, cfg_terbaik = sch, cfg
            kuota_terbaik = kuota_mustahil
        # Berhenti lebih awal hanya kalau tidak ada lagi yang bisa dikejar, dan
        # sejak giliran ikut dinilai itu berarti keunikan DAN putaran pertama
        # sama-sama sudah di batasnya. Tanpa syarat kedua, percobaan pertama
        # yang kebetulan menyentuh batas keunikan menghentikan pencarian sambil
        # membawa giliran yang buruk - persis bagaimana satu roster bisa
        # berakhir dengan peserta yang baru turun di ronde 4.
        #
        # Yang dipakai putaran pertama, bukan jumlah serobotan. Serobotan tidak
        # punya batas bawah yang bisa dihitung, dan di meet berokupansi tinggi
        # ia selalu besar tanpa ada yang benar-benar dirugikan - 40 orang di 8
        # court berarti hampir semua orang turun tiap ronde, jadi batas-batas
        # putaran terlewati terus-menerus sambil tidak ada yang menunggu lebih
        # dari 2 ronde. Menuntutnya nol berarti keluar-awal tidak pernah
        # menyala sama sekali: diukur, itu membuat 26 orang di 4 court naik dari
        # 2,3 ke 10,3 detik tanpa satu pun perbaikan yang bisa ditunjukkan.
        #
        # Putaran pertama tidak punya cacat itu. Dengan `slot` slot per ronde
        # untuk n peserta, semua orang bisa turun dalam ceil(n / slot) ronde,
        # dan itu batas yang selalu bisa dicapai dan selalu terasa.
        if sch.stats.at_theoretical_floor and sch.stats.last_first_play <= putaran:
            break

    # Mode CP-SAT: percobaan yang menang diulang sekali lagi, kali ini dengan
    # solver eksak dipasang di ujungnya. Mengulang memang berarti membayar satu
    # kali annealing lagi, tapi jauh lebih murah daripada menjalankan solver di
    # SETIAP percobaan - dan lintasan acaknya deterministik dari seed, jadi
    # pengulangan itu mendarat di jadwal yang sama persis sebelum solver mulai.
    #
    # Penyempurnaan jendela ikut jalur yang sama, dan alasannya juga sama:
    # menjalankannya di tiap percobaan berarti membelanjakan anggaran host untuk
    # jadwal yang pada akhirnya dibuang.
    if perlu_putaran_akhir and cfg_terbaik is not None:
        def teruskan_akhir(frac, msg):
            if progress is not None:
                progress(bagian + frac * (1.0 - bagian), msg)

        terbaik = _build_once(players, cfg_terbaik,
                              teruskan_akhir if progress else None,
                              courts_per_round,
                              pakai_cpsat=config.mode in CPSAT_MODES,
                              kuota_mustahil=kuota_terbaik,
                              pakai_lns=config.lns_seconds > 0)

    terbaik.config.seed = config.seed
    terbaik.config.attempts = config.attempts
    if progress is not None:
        progress(1.0, f"Selesai - kualitas {terbaik.stats.quality_score}/100")
    return terbaik


def _build_once(players: list[Player], config: Config,
                progress=None,
                courts_per_round: list[int] | None = None,
                pakai_cpsat: bool = False,
                kuota_mustahil: bool = False,
                pakai_lns: bool = False,
                cpsat_dasar: bool = False) -> Schedule:
    """Satu kali penjadwalan utuh, dari validasi sampai jadwal jadi.

    `pakai_cpsat` memasang solver eksak di ujung rangkaian. Dipisah dari
    config.mode karena build_schedule menjalankan beberapa percobaan lalu
    menyalakan solver hanya untuk yang menang - jadi mode-nya sama sepanjang
    percobaan, sakelarnya yang berbeda.

    `cpsat_dasar` menukar MESINNYA: annealing tidak dijalankan sama sekali dan
    jadwalnya disusun solver eksak dari konstruksi awal, tanpa hint. Keduanya
    tidak pernah menyala bersama - yang satu memakai solver untuk memungut sisa
    perbaikan annealing, yang lain memakai solver sebagai gantinya.
    """
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

    # Court yang tersedia di tiap ronde. Satu angka untuk semua ronde adalah
    # keadaan biasa; daftar yang berbeda-beda dipakai acara yang melepas court di
    # tengah jam sewa. Diselesaikan di sini, sebelum ada yang membacanya, supaya
    # tidak ada satu pun hitungan yang memakai jumlah court yang salah.
    # Aturan di Config dulu; daftar eksplisit dari pemanggil menimpanya. Yang
    # dipakai UI adalah aturannya, dan daftar itu bentuk umum untuk skrip yang
    # butuh pola di luar "berkurang sekali".
    courts_r = config.court_plan(total_rounds)
    if courts_per_round is not None:
        if len(courts_per_round) != total_rounds:
            raise ScheduleError(
                f"Rencana court berisi {len(courts_per_round)} angka, sedangkan "
                f"acaranya {total_rounds} ronde. Keduanya harus sama panjang."
            )
        if any(c < 1 for c in courts_per_round):
            raise ScheduleError("Tiap ronde minimal punya 1 court.")
        courts_r = list(courts_per_round)
    courts = max(courts_r)
    court_seragam = len(set(courts_r)) == 1

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
    if not _zero_repeats_possible(players, config, courts_r):
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
    # Segmen -> berapa slot laki-laki yang direncanakan turun tiap ronde, kalau
    # _balanced_rows sempat menyusun rencana. Kosong = tidak ada rencana, jadi
    # komposisinya bebas ditentukan per ronde seperti sebelumnya.
    rencana_slot: dict[int, list[int]] = {}
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
    # Hanya untuk acara yang jumlah court-nya sama sepanjang jalan: seluruh model
    # anggaran ini dibangun di atas satu angka "berapa tim per ronde", dan
    # memakainya saat angkanya berbeda-beda berarti menyusun jatah dari
    # kebutuhan yang tidak pernah ada. Pemerataan di blok berikutnya tetap jalan.
    if (pakai_kuota and court_seragam
            and total_rounds <= len(cand_cache[id(segments[0])])):
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
            # Saat lawan 100% unik masih mungkin, kuota selalu dipasang - itu
            # perilaku yang sudah terbukti. Saat MUSTAHIL, mengarahkan komposisi
            # ke yang paling sedikit rugi (24/24/4 -> 20/32/0 pada roster 18
            # putra + 8 putri, 12 pasang lawan berulang -> 4) menang di sebagian
            # roster dan kalah di sebagian lain, jadi ia dijalankan sebagai
            # strategi yang diadu antar percobaan - lihat build_schedule().
            #
            # _reachable() di bawah tetap penjaganya: target yang tak terjangkau
            # rotasi partner tetap ditolak, muat penuh atau tidak.
            if not budget.target or not (budget.feasible or kuota_mustahil):
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
        # Berapa PASANGAN yang turun di tiap ronde. Beda-beda kalau court-nya
        # berkurang di tengah acara, dan komposisi yang sah ikut berbeda - jadi
        # opsinya didaftar per nilai `need`, bukan sekali untuk seluruh acara.
        needs = [2 * min(c, n // 4) for c in courts_r]
        memo_b: dict[tuple[int, ...], bool] = {}
        stok_baris = _shape_supply(cands, rules.gender)
        options_b = {
            nd: [_round_options(stok, nd, frozenset(izin), memo_b)
                 for stok in stok_baris]
            for nd in sorted(set(needs))
        }
        rencana = _balanced_rows(options_b, needs, n_men, n_women)
        # Urutan baris hanya digeser kalau TERBUKTI lebih merata (lihat
        # _balanced_rows); rencana slotnya dipasang apa pun urutannya. Keduanya
        # dipisah karena ongkosnya beda: menggeser baris menukar kekayaan
        # rotasi partner, sedangkan menepati rencana slot tidak mengorbankan
        # apa pun - komposisi itu toh harus dipilih, tinggal dipilih yang mana.
        urutan = rencana[0] if rencana else [
            i % len(stok_baris) for i in range(total_rounds)
        ]
        if rencana:
            cand_cache[id(seg)] = [cands[i] for i in urutan]
        slot_pria = _slot_plan(options_b, needs, urutan, n_men, n_women)
        if slot_pria:
            rencana_slot[id(seg)] = slot_pria

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
        slot_seg = rencana_slot.get(id(seg))
        quads = _build_round(row, st, courts_r[r_global], tier_of, rng,
                             weights.rating,
                             r_global, group_of=group_of, quota=quota,
                             slot_target=(slot_seg[i % len(slot_seg)]
                                          if slot_seg else None))
        if not quads:
            raise ScheduleError(
                f"Segmen '{seg.label or 'Main'}' tidak bisa mengisi court "
                f"mana pun. Cek jumlah pemain per pool rating."
            )

        playing = {p for q in quads for p in q}
        byes = sorted(set(range(n)) - playing)
        st.place_round(r_global, quads, byes)

    # Court yang berkurang di tengah acara adalah fakta paling menentukan tentang
    # jadwal seperti ini, dan tidak terbaca dari angka mana pun di ringkasan -
    # yang tercatat cuma jumlah court terbanyak. Tanpa kalimat ini host melihat
    # ronde-ronde belakang cuma punya satu match dan mengira penjadwalnya gagal
    # mengisi court kedua.
    if not court_seragam:
        blok: list[tuple[int, int, int]] = []
        for r, c in enumerate(courts_r):
            if blok and blok[-1][2] == c:
                blok[-1] = (blok[-1][0], r + 1, c)
            else:
                blok.append((r + 1, r + 1, c))
        rincian = ", ".join(
            (f"ronde {a} pakai {c} court" if a == b
             else f"ronde {a}-{b} pakai {c} court")
            for a, b, c in blok
        )
        duduk = {c: n - 4 * min(c, n // 4) for c in sorted(set(courts_r))}
        notes.append(
            f"Jumlah court tidak sama sepanjang acara: {rincian}. Yang duduk tiap "
            "ronde ikut berubah - "
            + ", ".join(f"{v} orang saat {k} court" for k, v in duduk.items())
            + ". Jadi tunggu terpanjang di bagian yang court-nya lebih sedikit "
            "memang lebih besar, dan itu batas kapasitas, bukan hasil "
            "penjadwalan. Jatah main tetap diratakan untuk seluruh acara."
        )

    for seg in segments:
        eligible = set(_eligible_for(seg.rule, local_players))
        if seg.rule in ("men", "women"):
            sitting = n - len(eligible)
            if sitting:
                notes.append(
                    f"Segmen '{seg.label}': {sitting} pemain otomatis istirahat "
                    f"karena tidak masuk kriteria gender."
                )

    # Babak berurutan membuat peserta babak belakangan menunggu lama sebelum
    # match pertamanya, dan itu tidak terbaca di angka mana pun.
    #
    # Dulu terhukum secara tidak sengaja: denda duduk-beruntun menghitung ronde
    # babak lain sebagai duduk, jadi jadwal berurutan selalu kena denda penuh.
    # Itu sinyal yang salah dengan dua cara - tersaturasi, jadi jadwal rapi dan
    # jadwal kacau kehilangan 5 poin yang sama, dan tersembunyi, jadi host tidak
    # pernah tahu apa yang membuat skornya turun maupun apa obatnya. Sejak denda
    # itu menghormati kelayakan, hukuman diam-diamnya hilang; ini penggantinya,
    # dan bentuknya kalimat dengan angka, bukan potongan skor.
    if len(segments) > 1 and not config.interleave_segments:
        menit_ronde = round_minutes
        # Ronde pertama tempat tiap peserta BERHAK turun; yang dicari peserta
        # yang paling lama menunggunya. Bukan "ronde pertama yang babaknya
        # terbatas" - di babak putra lalu putri, itu ronde 1 dan angkanya nol,
        # padahal yang menunggu justru para putri sampai ronde 7.
        pertama_berhak: dict[int, int] = {}
        for r, (seg, _) in enumerate(round_plan(segments,
                                                config.interleave_segments)):
            for p in _eligible_for(seg.rule, local_players):
                pertama_berhak.setdefault(p, r)
        tunggu = max(pertama_berhak.values(), default=0)
        if tunggu > 0:
            notes.append(
                f"Babak dijalankan berurutan sebagai blok, jadi peserta babak "
                f"belakangan menunggu sampai ronde {tunggu + 1} - sekitar "
                f"{tunggu * menit_ronde} menit - sebelum match pertamanya, lalu "
                f"peserta babak awal duduk selama sisa acara. Jumlah main tiap "
                f"orang tetap sama. Nyalakan 'Selang-seling babak' kalau mau "
                f"ronde tiap babak tersebar sepanjang acara."
            )

    # Penilai yang dipakai solver untuk memutuskan apakah hasilnya layak dipakai.
    # Sengaja kunci yang sama persis dengan _lebih_baik(), yang memilih di antara
    # percobaan: kalau dua tempat itu memakai ukuran yang berbeda, solver bisa
    # menyerahkan jadwal yang menurut ukurannya sendiri menang tapi menurut host
    # kalah. Dipakai bersama oleh penyempurnaan jendela, solver seutuh-jadwal,
    # dan penjaga perapian di mode solver-sebagai-dasar.
    def nilai(state):
        s = _build_stats(state, local_players, total_rounds)
        return (s.partner_repeat_pairs, s.opponent_repeat_pairs,
                -s.quality_score)

    # --- Optimasi --------------------------------------------------------
    # Dua mesin, dan hanya satu yang jalan.
    #
    # Solver eksak sebagai mesin dasar hanya bisa dipakai kalau OR-Tools memang
    # terpasang. Kalau tidak, yang tersisa dari jalur ini cuma konstruksi awal -
    # jadwal yang belum dioptimasi siapa pun - dan menyerahkan itu ke host jauh
    # lebih buruk daripada diam-diam memakai annealing. Jadi mundurnya ke
    # annealing, dan alasannya dicatat.
    solver_dasar = cpsat_dasar and cpsat.tersedia()
    if cpsat_dasar and not solver_dasar:
        notes.append(
            "Mode solver eksak sebagai mesin dasar butuh OR-Tools, dan paket itu "
            "tidak ada di Python yang menjalankan aplikasi ini. Jadwal ini "
            "disusun dengan mesin biasa (annealing) - sama seperti mode "
            "Americano."
        )

    if solver_dasar:
        # --- Solver eksak sebagai mesin dasar ----------------------------
        # Ini kebalikan dari mode "americano_cpsat": di sana annealing yang
        # menyusun jadwal dan solver memungut sisanya; di sini solver yang
        # menyusun, dan annealing tidak dijalankan sama sekali.
        #
        # PERINGATAN YANG SUDAH DIUKUR, supaya tidak salah diharapkan. Pada 26
        # orang / 4 court, annealing sampai di NOL lawan berulang dalam 7 detik
        # sementara solver dari nol masih di 13 pasang setelah 20 detik.
        # Sebabnya bukan modelnya salah, melainkan bentuk masalahnya:
        # penjadwalan ini sangat simetris dan ruang solusinya raksasa, dan di
        # medan seperti itu pencarian lokal mengungguli cabang-dan-batas dengan
        # selisih yang jauh. Model utuh juga mulai kehabisan tenaga di 12 ronde
        # ke atas.
        #
        # Yang dibeli mode ini bukan mutu, melainkan ASAL-USUL jadwalnya: yang
        # dipegang host benar-benar keluar dari solver, dan di setup kecil
        # (sampai sekitar 12-16 peserta / 8-10 ronde) solver bisa MEMBUKTIKAN
        # jadwalnya optimal tanpa dibantu titik awal siapa pun - klaim yang tidak
        # bisa diberikan mesin lain, dan tidak tercampur pertanyaan "seberapa
        # banyak sebenarnya sumbangan annealing".
        #
        # Konstruksi awal tetap dipakai untuk dua hal, dan cuma dua: menentukan
        # berapa court yang realistis terisi tiap ronde (model membacanya dari
        # st.matches), dan menjadi jaring kalau solver gagal. Sebagai titik awal
        # pencarian ia sengaja TIDAK dipakai - itulah arti dasar=True.
        say(0.10, f"Menyusun {total_rounds} ronde dengan solver eksak (CP-SAT)")
        lapor_dasar = cpsat.optimize(
            st, courts_r,
            time_limit=config.cpsat_seconds,
            workers=config.cpsat_workers,
            nilai=nilai,
            progress=(lambda f, m: say(0.10 + f * 0.72, m)) if progress else None,
            dasar=True,
            deterministic=config.cpsat_deterministic,
            seed=config.seed,
        )
        notes.extend(lapor_dasar.catatan)
        # Angka capaian solver dicatat DI SINI, sebelum perapian menyentuhnya.
        # Catatannya sendiri ditulis setelah perapian selesai, supaya angka yang
        # dibaca host adalah angka jadwal yang benar-benar ia pegang - lihat
        # _catatan_dasar, yang menyebut keduanya kalau berbeda.
        capaian_solver = (st.rep_pc, st.rep_oc)
    else:
        lapor_dasar = None
        capaian_solver = (0, 0)
        say(0.10, f"Mengoptimasi {total_rounds} ronde")
        anneal(
            st, max(1000, config.effort), rng,
            progress=(lambda f, m: say(0.10 + f * 0.72, m)) if progress else None,
        )

    # Perapian di bawah ini dirancang untuk membereskan sisa-sisa annealing, dan
    # jadwal solver bukan itu: pemerataan main dan penyapu pertemuan berulang
    # bisa MENGURAI jadwal yang sudah optimal menurut modelnya sendiri. Karena
    # itu di jalur solver seluruh rangkaian perapian diberi jaring - dijalankan
    # apa adanya, lalu hasilnya dibandingkan dengan ukuran yang sama yang dipakai
    # memilih jadwal di tempat lain, dan kalau ternyata lebih buruk jadwal solver
    # yang dikembalikan.
    #
    # Dijalankan, bukan dilewati, karena yang dibereskannya nyata: giliran main
    # (siapa menunggu berapa lama) tidak ada di dalam model solver sama sekali.
    titik_solver = st.snapshot() if solver_dasar else None
    nilai_solver = nilai(st) if solver_dasar else None
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

    # --- Giliran main ----------------------------------------------------
    # Dijalankan setelah semuanya selesai, dan itu bukan urutan yang kebetulan:
    # jumlah pasang yang berulang pada titik ini adalah hasil terbaik yang
    # sanggup dicapai seluruh tahap di atas, dan justru angka itulah yang jadi
    # BATAS KERAS untuk dua tahap berikut. Keunikan berhenti jadi harga yang
    # bisa ditawar dan menjadi sesuatu yang tidak boleh berkurang sama sekali.
    #
    # Keduanya juga hanya memakai gerakan yang tidak mengubah jumlah main siapa
    # pun, jadi kerataan yang baru dijamin rebalance_plays() aman tanpa perlu
    # diratakan ulang.
    say(0.90, "Meratakan giliran main")
    anggaran_giliran = max(1000, config.effort // 2)
    anneal_giliran(
        st, anggaran_giliran, rng,
        progress=(lambda f, m: say(0.90 + f * 0.04, m)) if progress else None,
    )
    # Sapuan deterministik penutup: memungut perbaikan giliran yang masih
    # persis gratis dan kebetulan terlewat oleh lintasan acak annealing.
    say(0.94, "Merapikan sisa giliran")
    ratakan_giliran(st)

    # Putaran giliran TAMBAHAN, dan hanya selama tunggu terpanjang masih di ATAS
    # batas yang tak terhindarkan. Berhenti begitu batasnya tercapai, jadi setup
    # yang memang sudah di batasnya tidak menjalankan ini sama sekali.
    #
    # Sebabnya: anggaran `effort // 2` di atas kurang, bukan mentok. Diukur
    # dengan mengalikan anggaran itu saja (x1 / x4 / x10), attempts=1, 12 seed,
    # yang dihitung berapa seed mencapai batas tunggu:
    #
    #   26 org / 4 court bebas      6/12 -> 11/12 -> 12/12   mutu 93,03 -> 94,34
    #   26 org / 4 court 13 ronde   0/12 ->  1/12 ->  8/12   mutu 96,64 -> 98,66
    #   20 org / 3 court            0/12 ->  0/12 ->  1/11   mutu 91,27 -> 91,77
    #   16L+10P sesama-bentuk       0/12 ->  0/12 ->  0/12   mutu 90,92 -> 91,50
    #   26 org sesama-bentuk        0/12 ->  0/12 ->  0/12   mutu 91,03 -> 91,20
    #
    # Mutunya naik monoton di kelimanya, dan yang paling banyak tertinggal
    # justru setup 13 ronde - satu-satunya jumlah ronde yang membagi jatah main
    # rata untuk 26 peserta, jadi setup yang paling sering disarankan panel
    # kelayakan sekaligus yang paling banyak dirugikan anggaran lama.
    #
    # Kenapa berpatokan pada tunggu terpanjang dan bukan sekadar menaikkan
    # anggaran untuk semua: dua baris terakhir di atas TIDAK PERNAH mencapai
    # batasnya walau dikali sepuluh - format yang dibatasi sesama-bentuk memang
    # mengunci gerakan yang dibutuhkan. Menaikkan anggaran rata untuk semua
    # membuat setup seperti itu membayar empat kali lipat waktu tunggu host
    # untuk perbaikan 0,2 poin. Dengan syarat ini, yang sudah di batasnya
    # berongkos nol dan yang di atasnya membayar sampai batas putaran.
    #
    # Batas bawahnya diambil dari _telaah_giliran - fungsi yang sama yang
    # dipakai statistik yang dilaporkan ke host. Menyalin rumusnya jadi versi
    # kedua di sini akan membuat meet bersegmen dan acara yang court-nya
    # berkurang mengejar target yang tak terjangkau.
    #
    # Mode CP-SAT: putaran ini berjalan SEBELUM solver, jadi solver dapat titik
    # awal yang lebih baik - dan mode itu menjalankan _build_once dua kali
    # (tiap percobaan, lalu sekali lagi untuk pemenang), jadi ongkos waktunya
    # dibayar dua kali di sana. Disapu pada 3 setup x 3 seed, batas solver 15
    # detik: mutu tidak turun di satu pun kasus (naik di 4, sama di 5), lawan
    # berulang tidak memburuk di satu pun, dan pada 26 orang bebas seed 3
    # solver justru jadi BISA membuktikan optimal - 91,4 tidak terbukti dalam
    # 18,3 detik menjadi 94,3 terbukti dalam 11,5 detik, karena titik awal yang
    # lebih rapi memperkecil ruang yang harus ditutup. Ongkosnya ditanggung
    # setup yang batasnya tak terjangkau: 26 orang sesama-bentuk 18-20 detik
    # menjadi 22-30 detik untuk +0,2..+0,6 poin.
    ids_lokal = [lp.id for lp in local_players]
    for putaran in range(_GILIRAN_EKSTRA):
        plays_kini = {pid: 0 for pid in ids_lokal}
        for r in range(total_rounds):
            for q in st.matches[r]:
                for pid in q:
                    plays_kini[pid] += 1
        g = _telaah_giliran(st, ids_lokal, total_rounds, plays_kini)
        if g.longest_wait <= g.wait_floor:
            break
        say(0.94 + (putaran + 1) * 0.002,
            f"Tunggu terpanjang {g.longest_wait} ronde, batas {g.wait_floor} - "
            f"putaran giliran tambahan {putaran + 1}/{_GILIRAN_EKSTRA}")
        anneal_giliran(st, anggaran_giliran, rng)
        ratakan_giliran(st)

    # Jaring untuk jalur solver-sebagai-dasar: kalau perapian di atas ternyata
    # merugikan, jadwal solver yang dikembalikan. Tanpa ini janji "yang Anda
    # pegang adalah jadwal solver" bisa dilanggar oleh tahap yang justru
    # dimaksudkan menolong.
    if titik_solver is not None and nilai(st) > nilai_solver:
        st.restore(titik_solver)
        notes.append(
            "Perapian setelah solver (pemerataan main & giliran) ternyata "
            "menurunkan mutu jadwal, jadi hasil solver yang dipakai apa adanya."
        )
    if lapor_dasar is not None:
        notes.append(_catatan_dasar(lapor_dasar, capaian_solver,
                                    (st.rep_pc, st.rep_oc)))

    # --- Solver eksak (mode CP-SAT saja) ---------------------------------
    # Dijalankan PALING AKHIR, dengan jadwal hasil seluruh tahap di atas sebagai
    # titik awal. Urutan ini bukan selera - ia diukur.
    #
    # Versi pertama mode ini menyuruh CP-SAT menggantikan annealing dan mulai
    # dari konstruksi greedy. Hasilnya kalah telak: pada 26 orang / 4 court,
    # annealing sampai di NOL lawan berulang dalam 7 detik sementara CP-SAT
    # masih di 13 pasang setelah 20 detik. Sebabnya bukan modelnya salah,
    # melainkan bentuk masalahnya: penjadwalan ini sangat simetris dan ruang
    # solusinya raksasa, dan di medan seperti itu pencarian lokal memang
    # mengungguli cabang-dan-batas dengan selisih yang jauh.
    #
    # Yang benar-benar bisa disumbangkan solver eksak ada dua, dan dua-duanya
    # butuh titik awal yang sudah bagus: memungut perbaikan terakhir yang tidak
    # terjangkau gerakan acak, dan - ini yang tidak bisa dilakukan mesin mana
    # pun selain dia - MEMBUKTIKAN bahwa tidak ada lagi yang tersisa.
    # --- Penyempurnaan jendela (tombol "Sempurnakan jadwal ini") ----------
    # Dijalankan SEBELUM solver seutuh-jadwal, kalau dua-duanya diminta: yang
    # ini bekerja cepat di submasalah kecil, dan hasilnya jadi titik awal yang
    # lebih baik untuk yang berikutnya.
    if pakai_lns and config.lns_seconds > 0:
        say(0.95, "Menyempurnakan jadwal per kelompok ronde")
        hasil_lns = cpsat.sempurnakan(
            st, courts_r,
            anggaran=config.lns_seconds,
            workers=config.cpsat_workers,
            nilai=nilai,
            deterministic=config.cpsat_deterministic,
            seed=config.seed,
            progress=(lambda f, m: say(0.95 + f * 0.03, m)) if progress else None,
        )
        notes.extend(hasil_lns.catatan)
        notes.append(cpsat.catatan_sempurna(hasil_lns))

    if pakai_cpsat:
        say(0.95, "Mencari sisa perbaikan dengan solver eksak (CP-SAT)")
        lapor = cpsat.optimize(
            st, courts_r,
            time_limit=config.cpsat_seconds,
            workers=config.cpsat_workers,
            nilai=nilai,
            progress=(lambda f, m: say(0.95 + f * 0.03, m)) if progress else None,
            deterministic=config.cpsat_deterministic,
            seed=config.seed,
        )
        notes.extend(lapor.catatan)
        notes.append(_catatan_cpsat(lapor, st.rep_pc, st.rep_oc))

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
        segments=[(s.rule, s.rounds) for s in segments],
        roster_men=n_men,
        roster_women=n_women,
        # Dihitung dari jadwal yang sudah jadi, bukan dari ronde x court: itu satu-
        # satunya angka yang benar kalau court-nya berkurang di tengah acara.
        matches_per_round=[len(st.matches[r]) for r in range(total_rounds)],
    )
    for issue in cap.sorted_issues():
        if issue.severity in ("error", "warning"):
            notes.append(f"{issue.title}: {issue.detail}")

    # Peserta yang tidak turun sama sekali - fakta terpenting tentang jadwal
    # ini, dan sebelumnya tidak disebut di mana pun. Yang bersangkutan cuma
    # hilang dari tiap ronde sementara catatan yang muncul bicara soal rotasi
    # partner dan menyarankan menambah court.
    #
    # Sengaja diperiksa dari jadwal jadi, bukan diserahkan seluruhnya ke
    # analyze(): model bentuk di sana hanya berlaku kalau gender lengkap dan
    # semua babak memakai kolam yang sama, jadi meet bersegmen, pool rating,
    # dan partner terkunci lolos darinya. Sebabnya biar dijelaskan analyze()
    # yang tahu formatnya; yang wajib ada di sini NAMANYA.
    nama_peserta = {p.id: p.name for p in players}
    tidak_main = [nama_peserta.get(pid, str(pid))
                  for pid, v in sorted(stats.plays_per_player.items())
                  if v == 0]
    if tidak_main:
        notes.append(
            f"{len(tidak_main)} peserta tidak kebagian main sama sekali: "
            + ", ".join(tidak_main)
            + ". Jadwal tetap dibuat supaya sisanya bisa jalan, tapi ini "
            "hampir selalu berarti setupnya yang perlu diubah - lihat catatan "
            "lain di daftar ini untuk sebabnya."
        )

    # Jatah main yang timpang ANTAR kelompok babak. Skor kualitas sengaja tidak
    # lagi mendendanya - tidak ada jadwal yang bisa mengubahnya, dan dendanya
    # dulu tersaturasi sehingga berhenti membedakan apa pun - jadi ini
    # satu-satunya tempat host mendengarnya, dan karena itu angkanya harus
    # angka sebenarnya.
    #
    # Yang selama ini muncul justru menyesatkan: analyze() buta babak dan
    # melaporkan "rata-rata tiap peserta main 5.0 dari 15 ronde" untuk meet 20
    # putra + 4 putri, padahal para putra main 3 dan para putri 10. Rata-rata
    # itu tidak berlaku bagi satu peserta pun.
    if len(segments) > 1 and stats.plays_per_player:
        plan_note = round_plan(segments, config.interleave_segments)
        elig_note = [set(_eligible_for(s.rule, players)) for s, _ in plan_note]
        kelompok: dict[tuple, list[Player]] = {}
        for p in players:
            sig = tuple(i for i in range(len(elig_note)) if p.id in elig_note[i])
            kelompok.setdefault(sig, []).append(p)
        if len(kelompok) > 1:
            rincian = []
            for anggota in sorted(kelompok.values(), key=len, reverse=True):
                m = [stats.plays_per_player.get(p.id, 0) for p in anggota]
                gender = {p.gender for p in anggota}
                nama = ("putra" if gender == {"M"} else
                        "putri" if gender == {"F"} else "peserta")
                rentang = (f"{min(m)}" if min(m) == max(m)
                           else f"{min(m)}-{max(m)}")
                rincian.append(f"{len(anggota)} {nama} main {rentang} ronde")
            rerata = [sum(stats.plays_per_player.get(p.id, 0) for p in g) / len(g)
                      for g in kelompok.values()]
            if max(rerata) - min(rerata) >= 2:
                notes.append(
                    "Jatah main tidak sama antar babak: " + "; ".join(rincian)
                    + ". Selisihnya datang dari berapa slot yang tersedia untuk "
                    "tiap kelompok - jumlah peserta tiap gender dibanding court "
                    "dan ronde babaknya - bukan dari rotasi, jadi tidak ada "
                    "jadwal yang bisa meratakannya. Yang menggeser angkanya: "
                    "ubah jumlah ronde tiap babak, atau ubah komposisi peserta."
                )

    # Aturan court yang BENAR-BENAR dipakai, diturunkan dari plan-nya - jadi ia
    # betul baik saat datang dari Config maupun dari daftar eksplisit sebuah
    # skrip. Plan yang bukan "berkurang sekali lalu tetap" tidak bisa diwakili
    # satu aturan, dan di situ field-nya dibiarkan kosong: laporan tetap membaca
    # court per ronde dari jadwalnya, jadi yang hilang cuma ringkasan biayanya,
    # bukan jadwalnya.
    titik = [r for r in range(1, total_rounds) if courts_r[r] != courts_r[r - 1]]
    aturan_court: tuple[int | None, int | None] = (None, None)
    if (len(titik) == 1 and courts_r[0] == config.courts
            and courts_r[titik[0]] < courts_r[0]
            and len(set(courts_r[titik[0]:])) == 1):
        aturan_court = (courts_r[titik[0]], titik[0] + 1)

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
        courts_after=aturan_court[0],
        courts_from_round=aturan_court[1],
        cpsat_seconds=config.cpsat_seconds,
        cpsat_workers=config.cpsat_workers,
        # Ikut dibawa karena footer laporan cetak memakainya untuk memutuskan
        # apakah jadwal ini bisa dibuat ulang dari seed - dan itu satu-satunya
        # petunjuk yang dipegang pembaca laporan berbulan-bulan kemudian.
        cpsat_deterministic=config.cpsat_deterministic,
        # Ikut dibawa supaya jadwal ini tahu ia sudah lewat penyempurnaan.
        # Laporan cetak mencantumkannya di cetakan kecil - penyempurnaan
        # dibatasi WAKTU, jadi ia satu-satunya bagian yang bisa berhenti di
        # titik berbeda saat jadwal yang sama dibuat ulang, dan pembaca yang
        # mengulangnya harus tahu itu.
        lns_seconds=config.lns_seconds,
        court_names=config.court_names,
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

    # Partner berulang yang dipaksa FORMAT. analyze() punya catatannya sendiri
    # ("Partner pasti ada yang berulang") tapi hitungannya buta gender: ia
    # membandingkan ronde main dengan jumlah peserta dikurangi satu, jadi ia
    # diam persis saat formatnya yang memotong kolam. Pada 5 putra + 3 putri
    # dengan "putra vs putra" dan "campur vs campur" saja, tiap putri cuma
    # punya lima calon partner sementara mainnya enam ronde - tiga pasang
    # berulang di jadwal itu batas bawahnya, bukan kelalaian.
    #
    # Ditulis di sini, bukan cuma di laporan cetak dan teks share: `notes` yang
    # muncul di layar jadwal dan di info debug, dan tiga permukaan yang
    # menceritakan hal berbeda tentang angka yang sama lebih membingungkan
    # daripada satu pun tidak menjelaskan.
    if stats.partner_repeat_pairs and not any(
            n.startswith("Partner pasti ada yang berulang") for n in notes):
        muat = kolam_partner(final_players, config.allowed_matchups)
        gmap_p = {p.id: p.gender for p in final_players}
        terikat = [(stats.plays_per_player.get(p, 0) - k, p, k)
                   for p, k in muat.items()
                   if stats.plays_per_player.get(p, 0) > k]
        if terikat:
            _, pid, batas = max(terikat)
            label = ("putri" if gmap_p.get(pid) == "F"
                     else "putra" if gmap_p.get(pid) == "M" else "peserta")
            jumlah = sum(1 for p in muat
                         if gmap_p.get(p) == gmap_p.get(pid)
                         and stats.plays_per_player.get(p, 0) > muat[p])
            notes.append(
                f"Partner pasti ada yang berulang: format yang Anda pilih "
                f"membuat {jumlah} peserta {label} hanya punya {batas} calon "
                f"partner yang sah, sedangkan jadwal ini memberi "
                f"{stats.plays_per_player.get(pid, 0)} ronde main. "
                f"{stats.partner_repeat_pairs} pasang berulang di jadwal ini "
                f"adalah batas bawahnya, bukan kelemahan algoritma. Yang "
                f"menggesernya cuma dua: izinkan lebih banyak format match, "
                f"atau ubah komposisi peserta."
            )

    # Giliran yang belum berurutan. Host menyadarinya lewat satu peserta yang
    # mengeluh, bukan lewat angka - jadi angkanya disebut lebih dulu, beserta
    # batas yang memang tak terhindarkan. "Menunggu 2 ronde" pada 10 orang di 1
    # court bukan kelemahan algoritma: cuma ada 4 slot per ronde.
    if stats.turn_skips or stats.longest_wait > stats.wait_floor:
        bagian = []
        if stats.turn_skips:
            # "menunggu giliran PERTAMANYA" salah menggambarkan angkanya, dan
            # salahnya besar. turn_skips menghitung orang yang turun untuk kali
            # ke-(k+1) padahal ada yang duduk dan baru main kurang dari k kali -
            # bukan khusus yang belum pernah main. Diperiksa pada jadwal host:
            # dari 4 serobotan, korbannya sudah main 0, 1, 2, dan 4 kali, jadi
            # kalimat lama benar untuk SATU kejadian dan keliru untuk tiga.
            # Host yang membacanya akan mencari peserta yang belum turun sama
            # sekali, dan tidak menemukannya.
            bagian.append(
                f"{stats.turn_skips} kali seseorang turun lagi padahal ada "
                f"peserta lain yang sedang duduk dan baru main lebih sedikit")

            # Siapa, bukan cuma berapa. "4 kali" tanpa nama adalah tuduhan
            # tanpa alamat: yang pertama dicari host justru siapa orangnya, dan
            # tanpa itu angkanya tidak bisa dicek sendiri di jadwal. Dihitung
            # dengan aturan yang sama seperti stats.turn_skips, termasuk
            # menghormati siapa yang berhak turun di ronde itu.
            elig_giliran = [set(_eligible_for(s.rule, final_players))
                            for s, _ in round_plan(segments,
                                                   config.interleave_segments)]
            sudah_g = {pid: 0 for pid in stats.plays_per_player}
            lewat_n: dict[int, int] = {}
            dilewati_n: dict[int, int] = {}
            for idx, rnd in enumerate(rounds):
                turun = {p for m in rnd.matches for p in m.players()}
                elig = (elig_giliran[idx] if idx < len(elig_giliran) else None)
                duduk = [p for p in sudah_g
                         if p not in turun and (elig is None or p in elig)]
                if turun and duduk:
                    lo = min(sudah_g[p] for p in duduk)
                    lewat = [p for p in turun if sudah_g[p] > lo]
                    for p in lewat:
                        lewat_n[p] = lewat_n.get(p, 0) + 1
                    if lewat:
                        for p in duduk:
                            if sudah_g[p] == lo:
                                dilewati_n[p] = dilewati_n.get(p, 0) + 1
                for p in turun:
                    sudah_g[p] = sudah_g.get(p, 0) + 1

            def _sebut(hitung: dict[int, int], batas: int = 3) -> str:
                urut = sorted(hitung.items(), key=lambda kv: (-kv[1], kv[0]))
                sebagian = ", ".join(
                    f"{nama_peserta.get(p, p)} {n}x" for p, n in urut[:batas])
                sisa = len(urut) - batas
                return sebagian + (f", dan {sisa} lainnya" if sisa > 0 else "")

            if dilewati_n:
                bagian.append("yang dilewati " + _sebut(dilewati_n))
            if lewat_n:
                bagian.append("yang turun lagi " + _sebut(lewat_n))
        if stats.longest_wait > stats.wait_floor:
            bagian.append(
                f"tunggu terpanjang {stats.longest_wait} ronde, sedangkan yang "
                f"tak terhindarkan {stats.wait_floor} ronde")
        # Sebabnya diperiksa, bukan diasumsikan. Selama ini catatan ini selalu
        # menuduh rotasi partner dan menyarankan memperpendek durasi ronde -
        # dan pada setup yang formatnya dibatasi, keduanya salah. Contoh nyata
        # dari host: 5 putra + 3 putri di 1 court dengan "putra vs putra" dan
        # "campur vs campur" saja. Dua ronde memberi delapan slot untuk delapan
        # orang, tapi dua match cuma bisa menghabiskan (8,0), (6,2), atau (4,4)
        # putra-putri - tidak ada yang (5,3). Satu peserta PASTI baru turun di
        # ronde ketiga, dan memperpendek ronde tidak mengubahnya sedikit pun.
        # Dihitung dari court ronde-ronde AWAL, bukan dari court terbanyak: yang
        # ditanya adalah berapa ronde sampai semua orang kebagian match pertama,
        # dan itu diputuskan di awal acara. Pada acara yang court-nya berkurang
        # belakangan keduanya kebetulan sama; pada yang bertambah tidak.
        putaran_min, terisi_min = 0, 0
        while terisi_min < n:
            c = courts_r[min(putaran_min, total_rounds - 1)]
            terisi_min += 4 * max(1, min(c, n // 4))
            putaran_min += 1
        format_mengikat = (
            nilai_bentuk
            and stats.last_first_play > putaran_min
            and not bisa_liput_semua(n_men, n_women, courts_r[0],
                                     putaran_min, config.allowed_matchups)
        )
        if format_mengikat:
            # Yang dibuktikan bisa_liput_semua() cuma MATCH PERTAMA yang telat.
            # Serobotan di tengah acara dan rentetan duduk tidak dijelaskannya,
            # dan menyapu semuanya ke "penyebabnya format" adalah klaim yang
            # lebih besar daripada buktinya. Diukur pada jadwal host: dari 4
            # serobotan, hanya 1 terjadi sebelum semua orang kebagian main -
            # tiga sisanya di ronde 4, 6, dan 10, jauh setelah itu. Jadi
            # porsinya dihitung dan disebut apa adanya.
            sudah_main: dict[int, int] = {pid: 0 for pid in stats.plays_per_player}
            belum = set(sudah_main)
            serobot_awal = 0
            for rnd in rounds:
                turun = {p for m in rnd.matches for p in m.players()}
                if belum:
                    duduk = [sudah_main[p] for p in sudah_main if p not in turun]
                    if duduk:
                        serobot_awal += sum(1 for p in turun
                                            if sudah_main[p] > min(duduk))
                for p in turun:
                    sudah_main[p] = sudah_main.get(p, 0) + 1
                belum -= turun

            porsi = (f"{serobot_awal} dari {stats.turn_skips} serobotan itu"
                     if stats.turn_skips else "Bagian awalnya")
            notes.append(
                "Giliran belum sepenuhnya berurutan: " + "; ".join(bagian)
                + f". {porsi} dipaksa format, bukan rotasi: dengan {n_men} "
                f"putra dan {n_women} putri tidak ada susunan {putaran_min} "
                f"ronde pertama yang sah sekaligus memakai semua peserta, jadi "
                f"satu peserta pasti baru turun di ronde "
                f"{stats.last_first_play} dan ronde awal terpaksa mengulang "
                f"orang yang sudah main. Memperpendek durasi per ronde tidak "
                f"mengubah bagian itu; yang menggesernya cuma mengizinkan lebih "
                f"banyak format match, mengubah komposisi peserta, atau menambah "
                f"court. Sisanya rotasi partner - pasangan tiap ronde diambil "
                f"dari satu baris kombinasi yang sudah tertentu supaya tidak "
                f"ada yang berpasangan dua kali."
            )
        else:
            notes.append(
                "Giliran belum sepenuhnya berurutan: " + "; ".join(bagian) + ". "
                "Penyebabnya rotasi partner: pasangan tiap ronde diambil dari "
                "satu baris kombinasi yang sudah tertentu supaya tidak ada yang "
                "berpasangan dua kali, jadi peserta yang paling lama menunggu "
                "kadang hanya bisa turun bersama orang yang baru saja main. "
                "Menambah court atau memperpendek durasi per ronde adalah yang "
                "paling banyak menolong."
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
