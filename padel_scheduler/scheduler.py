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
from itertools import combinations

from .capacity import analyze, rounds_from_duration
from .factorization import mixed_pair_rounds, subset_pair_rounds
from .models import (
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
)
from .optimizer import (
    Rules,
    ScheduleState,
    Weights,
    anneal,
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
        return mixed_pair_rounds(men, women)

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

def _select_pairs(
    candidates: list[tuple[int, int]],
    st: ScheduleState,
    n_pairs_needed: int,
    rng: random.Random,
) -> list[tuple[int, int]]:
    """Pilih pasangan yang turun, prioritas ke yang paling sering istirahat."""
    if n_pairs_needed >= len(candidates):
        return list(candidates)
    scored = sorted(
        candidates,
        key=lambda pr: (-(st.bye_count[pr[0]] + st.bye_count[pr[1]]), rng.random()),
    )
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
        for i, b in enumerate(remaining):
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
) -> list[list[int]]:
    """Rakit satu ronde: pilih siapa turun, lalu tentukan siapa lawan siapa."""
    if tier_of is None:
        chosen = _select_pairs(row, st, min(courts, len(row) // 2) * 2, rng)
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

    partner_repeat_pairs = partner_repeat_max = 0
    oppo_repeat_pairs = oppo_repeat_max = 0
    never_met = 0
    for i, j in combinations(ids, 2):
        k = st._k(i, j)
        pcv, ocv = st.pc[k], st.oc[k]
        if pcv > 1:
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
    min_partner_excess = sum(max(0, plays[p] - (len(ids) - 1)) for p in ids) / 2
    min_oppo_excess = sum(max(0, 2 * plays[p] - (len(ids) - 1)) for p in ids) / 2
    actual_partner_excess = sum(
        max(0, st.pc[st._k(i, j)] - 1) for i, j in combinations(ids, 2)
    )
    actual_oppo_excess = sum(
        max(0, st.oc[st._k(i, j)] - 1) for i, j in combinations(ids, 2)
    )

    total_partner_slots = max(1, sum(plays.values()) / 2)
    total_oppo_slots = max(1, sum(plays.values()))
    p_pen = max(0.0, actual_partner_excess - min_partner_excess) / total_partner_slots
    o_pen = max(0.0, actual_oppo_excess - min_oppo_excess) / total_oppo_slots

    play_vals = list(plays.values())
    spread = (max(play_vals) - min(play_vals)) if play_vals else 0
    bye_pen = min(1.0, spread / 3.0)
    b2b_pen = min(1.0, b2b / max(1, len(ids)))

    score = 100.0 - 45 * min(1.0, p_pen) - 30 * min(1.0, o_pen) \
        - 15 * bye_pen - 10 * b2b_pen

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

def build_schedule(players: list[Player], config: Config) -> Schedule:
    """Bangun jadwal lengkap. Ini fungsi yang dipanggil UI."""
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
    rules = Rules(
        gender={p.id: p.gender for p in local_players},
        locked_partner=locked,
        tier_of=dict(tier_of) if tier_of else {},
        court_pref={p.id: p.court_preference for p in local_players
                    if p.court_preference},
    )

    # Peta ronde -> segmen.
    round_segment: list[Segment] = []
    for seg in segments:
        round_segment.extend([seg] * seg.rounds)

    for seg in round_segment:
        rules.round_rule.append(seg.rule)
        rules.round_eligible.append(set(_eligible_for(seg.rule, local_players)))

    st = ScheduleState(n, ratings, weights, total_rounds, rules)

    notes: list[str] = []
    courts = config.courts

    # --- Konstruksi awal, segmen demi segmen ----------------------------
    r_global = 0
    for seg in segments:
        cands = _candidate_rounds(seg, local_players, config, tier_of, locked)
        if not cands:
            raise ScheduleError(
                f"Tidak bisa membentuk pasangan untuk segmen '{seg.label or 'Main'}'."
            )
        eligible = set(_eligible_for(seg.rule, local_players))

        for i in range(seg.rounds):
            row = list(cands[i % len(cands)])
            if len(row) < 2:
                raise ScheduleError(
                    f"Segmen '{seg.label or 'Main'}' tidak punya cukup pemain "
                    f"untuk mengisi satu court."
                )
            quads = _build_round(row, st, courts, tier_of, rng, weights.rating)
            if not quads:
                raise ScheduleError(
                    f"Segmen '{seg.label or 'Main'}' tidak bisa mengisi court "
                    f"mana pun. Cek jumlah pemain per pool rating."
                )

            playing = {p for q in quads for p in q}
            byes = sorted(set(range(n)) - playing)
            st.place_round(r_global, quads, byes)
            r_global += 1

        if seg.rule in ("men", "women"):
            sitting = n - len(eligible)
            if sitting:
                notes.append(
                    f"Segmen '{seg.label}': {sitting} pemain otomatis istirahat "
                    f"karena tidak masuk kriteria gender."
                )

    # --- Optimasi --------------------------------------------------------
    anneal(st, max(1000, config.effort), rng)
    # Kerataan jumlah main tidak boleh bergantung pada keberuntungan annealing:
    # ini menegakkannya secara deterministik setelahnya.
    rebalance_plays(st)

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

    stats = _build_stats(st, local_players, total_rounds)
    # Kembalikan statistik ke id asli.
    stats.plays_per_player = {inv[k]: v for k, v in stats.plays_per_player.items()}
    stats.byes_per_player = {inv[k]: v for k, v in stats.byes_per_player.items()}
    stats.roles_per_player = {inv[k]: v for k, v in role_summary.items()}

    cap = analyze(
        n_players=n,
        courts=config.courts,
        duration_minutes=config.duration_minutes,
        round_minutes=round_minutes,
        warmup_minutes=config.warmup_minutes,
        rounds_override=total_rounds,
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
        fit_rounds_to_duration=config.fit_rounds_to_duration,
    )

    if violations:
        affected = {v.player_name for v in violations}
        notes.append(
            f"{len(violations)} permintaan komposisi court tidak terpenuhi "
            f"({', '.join(sorted(affected))}). Detailnya ada di daftar preferensi."
        )

    return Schedule(players=final_players, config=resolved, rounds=rounds,
                    stats=stats, notes=notes, violations=violations)
