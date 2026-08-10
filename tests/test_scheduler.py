"""Uji korektnes generator jadwal.

Yang diuji bukan "apakah jadwalnya bagus" (itu subjektif), tapi properti keras
yang tidak boleh dilanggar apa pun setup-nya:
  - tidak ada pemain di dua court sekaligus
  - jumlah pemain per match tepat 4
  - aturan gender per segmen ditegakkan 100%
  - partner terkunci tetap terkunci di mode team
  - istirahat terbagi merata
"""

from __future__ import annotations

import math
import unittest
from collections import Counter
from itertools import combinations

from padel_scheduler import Config, Player, Segment, build_schedule
from padel_scheduler.capacity import analyze, shape_budget, shape_totals
from padel_scheduler.factorization import (
    mixed_pair_rounds,
    verify_one_factorization,
)
from padel_scheduler.models import MATCHUPS, matchup_code, team_shape
from padel_scheduler.optimizer import (
    Rules,
    ScheduleState,
    Weights,
    play_counts,
    polish_pairs,
)
from padel_scheduler.scheduler import ScheduleError


def make_players(n, genders=None, ratings=None):
    out = []
    for i in range(n):
        out.append(
            Player(
                id=i,
                name=f"P{i + 1}",
                rating=ratings[i] if ratings else 3.0,
                gender=genders[i] if genders else None,
            )
        )
    return out


def assert_structurally_valid(tc, schedule):
    ids = {p.id for p in schedule.players}
    for rnd in schedule.rounds:
        seen = []
        for m in rnd.matches:
            quad = m.players()
            tc.assertEqual(len(quad), 4, "match harus 4 pemain")
            tc.assertEqual(len(set(quad)), 4, "pemain dobel dalam satu match")
            seen.extend(quad)
        tc.assertEqual(
            len(seen), len(set(seen)),
            f"ronde {rnd.index}: pemain main di dua court sekaligus",
        )
        tc.assertEqual(
            set(seen) | set(rnd.byes), ids,
            f"ronde {rnd.index}: ada pemain yang hilang dari jadwal",
        )
        tc.assertFalse(
            set(seen) & set(rnd.byes),
            f"ronde {rnd.index}: pemain tercatat main sekaligus istirahat",
        )


class TestFactorization(unittest.TestCase):
    def test_all_sizes_4_to_26(self):
        for n in range(4, 27):
            ok, msg = verify_one_factorization(n)
            self.assertTrue(ok, f"n={n}: {msg}")

    def test_mixed_pairs_all_unique(self):
        men, women = [0, 1, 2, 3], [4, 5, 6, 7]
        rounds = mixed_pair_rounds(men, women)
        flat = [p for rnd in rounds for p in rnd]
        self.assertEqual(len(flat), len(set(flat)), "pasangan mixed berulang")
        self.assertEqual(len(flat), 16, "harus mencakup 4x4 kombinasi")
        for rnd in rounds:
            used = [x for pr in rnd for x in pr]
            self.assertEqual(len(used), len(set(used)), "pemain dobel dalam ronde")

    def test_mixed_pairs_uneven_groups(self):
        rounds = mixed_pair_rounds([0, 1], [2, 3, 4, 5])
        flat = [p for rnd in rounds for p in rnd]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(len(flat), 8)


class TestStructure(unittest.TestCase):
    def test_every_size_4_to_26(self):
        for n in range(4, 27):
            courts = max(1, n // 4)
            cfg = Config(courts=courts, duration_minutes=120, round_minutes=12,
                         mode="americano", effort=4000)
            sch = build_schedule(make_players(n), cfg)
            assert_structurally_valid(self, sch)
            self.assertGreater(len(sch.rounds), 0, f"n={n} tidak menghasilkan ronde")

    def test_rejects_too_few_players(self):
        cfg = Config(courts=1, duration_minutes=60)
        with self.assertRaises(ScheduleError):
            build_schedule(make_players(3), cfg)


class TestUniqueness(unittest.TestCase):
    def test_partners_unique_when_mathematically_possible(self):
        # 16 pemain, 4 court, 9 ronde -> tiap orang main 9x, punya 15 calon partner.
        cfg = Config(courts=4, duration_minutes=120, round_minutes=12,
                     mode="americano", effort=20000)
        sch = build_schedule(make_players(16), cfg)
        assert_structurally_valid(self, sch)
        self.assertEqual(
            sch.stats.partner_repeat_pairs, 0,
            "partner harusnya 100% unik pada setup ini",
        )

    def test_opponent_repeats_stay_near_theoretical_floor(self):
        # 8 pemain, 2 court: tiap orang main tiap ronde. Lawan unik maksimal 3 ronde,
        # jadi pengulangan wajib terjadi -- yang diuji: tersebar rata, tidak menumpuk.
        cfg = Config(courts=2, duration_minutes=120, round_minutes=12,
                     mode="americano", effort=30000)
        sch = build_schedule(make_players(8), cfg)
        assert_structurally_valid(self, sch)
        self.assertLessEqual(
            sch.stats.opponent_repeat_max, 4,
            "pengulangan lawan menumpuk di satu pasangan",
        )


class TestByeFairness(unittest.TestCase):
    def test_play_counts_are_balanced(self):
        # 26 pemain, 4 court -> 10 orang duduk tiap ronde. Harus rata.
        cfg = Config(courts=4, duration_minutes=120, round_minutes=12,
                     mode="americano", effort=30000)
        sch = build_schedule(make_players(26), cfg)
        assert_structurally_valid(self, sch)
        plays = list(sch.stats.plays_per_player.values())
        # 9 ronde x 16 slot = 144, dibagi 26 -> 5.54, jadi minimum yang mungkin
        # adalah selisih 1. Dulu ambangnya 2 dan menyembunyikan ketimpangan.
        self.assertLessEqual(
            max(plays) - min(plays), 1,
            f"jumlah main tidak merata: {min(plays)}..{max(plays)}",
        )


class TestSameGenderRule(unittest.TestCase):
    """"Sesama gender" berarti KEEMPAT pemain satu gender.

    Dulu syaratnya hanya "tiap tim satu gender", yang membolehkan tim putri
    melawan tim putra - bukan itu yang dimaksud, dan terbukti muncul di jadwal
    sungguhan.
    """

    def _build(self, seed, rounds=4, courts=1):
        players = make_players(8, genders=["M"] * 4 + ["F"] * 4)
        cfg = Config(courts=courts, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, effort=15000, seed=seed,
                     interleave_segments=True,
                     segments=[Segment("Sesama gender", rounds, "same_gender"),
                               Segment("Mixed", 4, "mixed")])
        return build_schedule(players, cfg)

    def test_all_four_players_share_gender(self):
        for seed in range(6):
            sch = self._build(seed)
            assert_structurally_valid(self, sch)
            gmap = {p.id: p.gender for p in sch.players}
            for rnd in sch.rounds:
                if rnd.segment != "Sesama gender":
                    continue
                for m in rnd.matches:
                    genders = {gmap[p] for p in m.players()}
                    self.assertEqual(
                        len(genders), 1,
                        f"seed {seed} ronde {rnd.index}: court bercampur "
                        f"{sorted(genders)}")

    def test_courts_alternate_between_genders(self):
        """Satu gender tidak boleh memonopoli court sepanjang babak."""
        sch = self._build(3, rounds=6)
        gmap = {p.id: p.gender for p in sch.players}
        men = women = 0
        for rnd in sch.rounds:
            if rnd.segment != "Sesama gender":
                continue
            for m in rnd.matches:
                if gmap[m.team_a[0]] == "M":
                    men += 1
                else:
                    women += 1
        self.assertGreater(men, 0, "putra tidak pernah kebagian court")
        self.assertGreater(women, 0, "putri tidak pernah kebagian court")
        self.assertLessEqual(abs(men - women), 1,
                             f"court timpang antar gender: {men} vs {women}")

    def test_forced_repeat_is_explained(self):
        """4 orang hanya punya 3 susunan match; ronde ke-4 pasti mengulang.

        Yang penting bukan pengulangannya - itu tak terhindarkan - tapi bahwa
        host diberi tahu, karena tanpa keterangan itu terbaca seperti bug.
        """
        sch = self._build(0, rounds=8)
        seen = {}
        for rnd in sch.rounds:
            for m in rnd.matches:
                key = tuple(sorted((tuple(sorted(m.team_a)),
                                    tuple(sorted(m.team_b)))))
                seen.setdefault(key, []).append(rnd.index)
        repeats = [v for v in seen.values() if len(v) > 1]
        self.assertTrue(repeats, "setup ini memang harus memaksa pengulangan")
        self.assertTrue(
            any("terulang persis sama" in nt for nt in sch.notes),
            f"pengulangan tidak dijelaskan di catatan: {sch.notes}")

    def test_forced_repeat_is_pushed_far_apart(self):
        """Pengulangan yang terpaksa harus jatuh sejauh mungkin.

        Kasus nyata dari host: babak putra dapat ronde 1, 4, 7, 10 sementara
        4 orang cuma punya 3 susunan match, jadi satu pengulangan wajib ada.
        Optimizer menaruhnya di ronde 1 & 4 - dua ronde putra yang berurutan,
        yang terbaca sebagai bug. Dulu memang tidak ada bedanya bagi fungsi
        biaya: A-B-C-A dan A-A-B-C punya hitungan partner & lawan yang sama.
        Sekarang jaraknya ikut dihitung, jadi pilihannya ronde 1 & 10.
        """
        players = make_players(8, genders=["M"] * 4 + ["F"] * 4)
        for seed in range(4):
            cfg = Config(courts=1, duration_minutes=120, round_minutes=10,
                         warmup_minutes=0, effort=15000, seed=seed,
                         interleave_segments=True,
                         segments=[Segment("Sesama gender", 4, "men"),
                                   Segment("Sesama gender", 4, "women"),
                                   Segment("Mixed", 4, "mixed")])
            sch = build_schedule(players, cfg)
            seen = {}
            for rnd in sch.rounds:
                for m in rnd.matches:
                    key = tuple(sorted((tuple(sorted(m.team_a)),
                                        tuple(sorted(m.team_b)))))
                    seen.setdefault(key, []).append(rnd.index)
            gaps = [(b - a, v) for v in seen.values() if len(v) > 1
                    for a, b in zip(v, v[1:])]
            self.assertTrue(gaps, f"seed {seed}: setup ini harus memaksa ulang")
            # Ronde gender pertama & terakhir berjarak 9 (1..10 / 2..11); itu
            # jarak maksimum yang mungkin, dan optimizer harus mencapainya.
            worst, where = min(gaps)
            self.assertGreaterEqual(
                worst, 9,
                f"seed {seed}: pengulangan terlalu berdekatan di ronde {where}")


class TestInterleaveSegments(unittest.TestCase):
    """Babak yang memakai orang berbeda tidak boleh berjalan sebagai blok.

    "Putri 4" lalu "Putra 4" berarti para putri main 4 ronde beruntun sementara
    para putra duduk 4 ronde beruntun, lalu bertukar. Melelahkan buat yang main,
    membosankan buat yang menunggu.
    """

    def _streaks(self, sch, player_ids):
        """(main beruntun terpanjang, duduk beruntun terpanjang)."""
        longest_play = longest_rest = 0
        for pid in player_ids:
            play = rest = 0
            for rnd in sch.rounds:
                on = any(pid in m.players() for m in rnd.matches)
                play = play + 1 if on else 0
                rest = 0 if on else rest + 1
                longest_play = max(longest_play, play)
                longest_rest = max(longest_rest, rest)
        return longest_play, longest_rest

    def _build(self, interleave):
        players = make_players(8, genders=["F"] * 4 + ["M"] * 4)
        cfg = Config(courts=1, duration_minutes=120, round_minutes=15,
                     warmup_minutes=0, effort=8000,
                     interleave_segments=interleave,
                     segments=[Segment("Putri", 4, "women"),
                               Segment("Putra", 4, "men")])
        return build_schedule(players, cfg)

    def test_blocks_cause_long_streaks(self):
        sch = self._build(False)
        assert_structurally_valid(self, sch)
        play, rest = self._streaks(sch, [p.id for p in sch.players])
        self.assertEqual((play, rest), (4, 4),
                         "tanpa selang-seling seharusnya memang berblok")

    def test_interleave_removes_streaks(self):
        sch = self._build(True)
        assert_structurally_valid(self, sch)
        play, rest = self._streaks(sch, [p.id for p in sch.players])
        self.assertEqual(play, 1, f"masih main beruntun {play} ronde")
        self.assertEqual(rest, 1, f"masih duduk beruntun {rest} ronde")

    def test_interleave_keeps_round_counts_per_segment(self):
        """Menyelang-nyeling hanya mengubah URUTAN, bukan komposisinya."""
        from collections import Counter
        for flag in (False, True):
            sch = self._build(flag)
            counts = Counter(r.segment for r in sch.rounds)
            self.assertEqual(counts, {"Putri": 4, "Putra": 4}, f"interleave={flag}")

    def test_interleave_preserves_gender_rules(self):
        genders = ["F"] * 4 + ["M"] * 4
        players = make_players(8, genders=genders)
        gmap = {p.id: p.gender for p in players}
        cfg = Config(courts=1, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, effort=8000, interleave_segments=True,
                     segments=[Segment("Putri", 3, "women"),
                               Segment("Putra", 3, "men"),
                               Segment("Mixed", 6, "mixed")])
        sch = build_schedule(players, cfg)
        assert_structurally_valid(self, sch)
        for rnd in sch.rounds:
            for m in rnd.matches:
                gs = [gmap[p] for p in m.players()]
                if rnd.segment == "Putri":
                    self.assertTrue(all(g == "F" for g in gs), f"ronde {rnd.index}")
                elif rnd.segment == "Putra":
                    self.assertTrue(all(g == "M" for g in gs), f"ronde {rnd.index}")
                elif rnd.segment == "Mixed":
                    self.assertNotEqual(gmap[m.team_a[0]], gmap[m.team_a[1]])
                    self.assertNotEqual(gmap[m.team_b[0]], gmap[m.team_b[1]])

    def test_partner_rotation_still_unique_within_segment(self):
        """Rotasi pasangan memakai nomor ronde DI DALAM segmennya.

        Kalau yang dipakai nomor ronde acara, ronde yang diselang-seling akan
        melewati kombinasi dan mengulang pasangan lebih cepat.

        Diuji dengan 3 ronde per gender, bukan 4: empat orang hanya punya tiga
        kombinasi partner, jadi ronde keempat pasti mengulang - batas matematis,
        bukan cacat rotasinya.
        """
        players = make_players(8, genders=["F"] * 4 + ["M"] * 4)
        cfg = Config(courts=1, duration_minutes=120, round_minutes=15,
                     warmup_minutes=0, effort=8000, interleave_segments=True,
                     segments=[Segment("Putri", 3, "women"),
                               Segment("Putra", 3, "men")])
        sch = build_schedule(players, cfg)

        for label in ("Putri", "Putra"):
            seen = set()
            for rnd in sch.rounds:
                if rnd.segment != label:
                    continue
                for m in rnd.matches:
                    for team in (m.team_a, m.team_b):
                        key = tuple(sorted(team))
                        self.assertNotIn(key, seen,
                                         f"{label}: pasangan {key} berulang")
                        seen.add(key)
            self.assertEqual(len(seen), 6,
                             f"{label}: 3 ronde x 2 tim = 6 pasangan berbeda")

    def test_order_matches_expected_pattern(self):
        from padel_scheduler.scheduler import round_plan
        segs = [Segment("Putra", 3, "men"), Segment("Putri", 3, "women"),
                Segment("Mixed", 6, "mixed")]
        order = [s.label for s, _ in round_plan(segs, True)]
        self.assertEqual(order, ["Mixed", "Putra", "Putri", "Mixed", "Mixed",
                                 "Putra", "Putri", "Mixed", "Mixed", "Putra",
                                 "Putri", "Mixed"])
        blocks = [s.label for s, _ in round_plan(segs, False)]
        self.assertEqual(blocks[:3], ["Putra"] * 3)


class TestPlayFairness(unittest.TestCase):
    """Jumlah main harus serata yang dimungkinkan aritmetika.

    Ini jaminan keadilan yang paling terasa buat peserta: mereka membayar fee
    yang sama. Sebelum ada pass perataan, optimizer menukar kerataan demi
    variasi lawan - dan makin lama optimasinya makin timpang hasilnya.
    """

    def _plays(self, n, courts, rounds, seed=0, effort=20000, genders=None):
        cfg = Config(courts=courts, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, mode="americano", seed=seed,
                     effort=effort, rounds_override=rounds)
        sch = build_schedule(make_players(n, genders=genders), cfg)
        assert_structurally_valid(self, sch)
        return sorted(sch.stats.plays_per_player.values())

    def test_exactly_even_when_slots_divide(self):
        # 12 ronde x 4 slot = 48, dibagi 8 pemain = 6 tepat.
        for seed in range(6):
            plays = self._plays(8, 1, 12, seed)
            self.assertEqual(plays, [6] * 8, f"seed {seed}: {plays}")

    def test_more_effort_never_makes_it_worse(self):
        # Justru inilah gejala bug lamanya: optimasi lebih lama = lebih timpang.
        for effort in (5000, 30000, 120000):
            plays = self._plays(8, 1, 12, seed=2, effort=effort)
            self.assertEqual(plays, [6] * 8, f"effort {effort}: {plays}")

    def test_spread_at_most_one_across_configs(self):
        cases = [
            (8, 1, 11), (8, 2, 9), (10, 2, 10), (12, 2, 12),
            (16, 3, 10), (20, 4, 12), (26, 4, 9), (14, 2, 11),
        ]
        for n, courts, rounds in cases:
            plays = self._plays(n, courts, rounds, seed=1, effort=12000)
            slots = rounds * 4 * min(courts, n // 4)
            spread = plays[-1] - plays[0]
            self.assertLessEqual(spread, 1, f"{n}/{courts}/{rounds}: {plays}")
            self.assertEqual(sum(plays), slots, f"{n}/{courts}/{rounds}")
            if slots % n == 0:
                self.assertEqual(spread, 0,
                                 f"{n}/{courts}/{rounds} habis dibagi tapi {plays}")

    def test_rebalance_respects_gender_segments(self):
        """Perataan tidak boleh menurunkan pemain yang tidak berhak main."""
        genders = ["M"] * 4 + ["F"] * 4
        players = make_players(8, genders=genders)
        gmap = {p.id: p.gender for p in players}
        cfg = Config(
            courts=1, duration_minutes=120, warmup_minutes=10, effort=20000,
            segments=[Segment("Putra", 3, "men"), Segment("Putri", 3, "women"),
                      Segment("Mixed", 6, "mixed")],
        )
        sch = build_schedule(players, cfg)
        assert_structurally_valid(self, sch)
        for rnd in sch.rounds:
            for m in rnd.matches:
                gs = [gmap[p] for p in m.players()]
                if rnd.segment == "Putra":
                    self.assertTrue(all(g == "M" for g in gs), f"ronde {rnd.index}")
                elif rnd.segment == "Putri":
                    self.assertTrue(all(g == "F" for g in gs), f"ronde {rnd.index}")
                elif rnd.segment == "Mixed":
                    self.assertNotEqual(gmap[m.team_a[0]], gmap[m.team_a[1]])
                    self.assertNotEqual(gmap[m.team_b[0]], gmap[m.team_b[1]])

    def test_rebalance_respects_locked_partners(self):
        players = make_players(12)
        for i in range(0, 12, 2):
            players[i].partner_id = i + 1
            players[i + 1].partner_id = i
        cfg = Config(courts=2, duration_minutes=120, mode="team", effort=15000,
                     rounds_override=11)
        sch = build_schedule(players, cfg)
        expected = {p.id: p.partner_id for p in players}
        for rnd in sch.rounds:
            for m in rnd.matches:
                for team in (m.team_a, m.team_b):
                    self.assertEqual(expected[team[0]], team[1])

    def test_cost_bookkeeping_stays_exact(self):
        """Perataan memasang & melepas match berulang kali; biaya inkremental
        harus tetap sama dengan hitungan dari nol."""
        from padel_scheduler.optimizer import ScheduleState, Weights, rebalance_plays

        cfg = Config(courts=1, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, effort=8000, rounds_override=12)
        sch = build_schedule(make_players(8), cfg)

        st = ScheduleState(8, [3.0] * 8, Weights(), len(sch.rounds))
        for r, rnd in enumerate(sch.rounds):
            quads = [[*m.team_a, *m.team_b] for m in rnd.matches]
            st.place_round(r, quads, list(rnd.byes))
        incremental = st.cost()
        rebalance_plays(st)

        fresh = ScheduleState(8, [3.0] * 8, Weights(), st.n_rounds)
        for r in range(st.n_rounds):
            playing = {p for q in st.matches[r] for p in q}
            fresh.place_round(r, [q[:] for q in st.matches[r]],
                              sorted(set(range(8)) - playing))
        self.assertAlmostEqual(st.cost(), fresh.cost(), places=6,
                               msg="pembukuan biaya inkremental melenceng")
        self.assertGreater(incremental, -1)


class TestGenderSegments(unittest.TestCase):
    """Format nyata host: 8 orang, 1 court, 2 jam, 3 putra / 3 putri / 6 mixed."""

    def setUp(self):
        self.genders = ["M"] * 4 + ["F"] * 4
        self.players = make_players(8, genders=self.genders)
        self.gmap = {p.id: p.gender for p in self.players}

    def _build(self):
        cfg = Config(
            courts=1,
            duration_minutes=120,
            warmup_minutes=10,
            mode="americano",
            effort=30000,
            segments=[
                Segment("Putra", 3, "men"),
                Segment("Putri", 3, "women"),
                Segment("Mixed", 6, "mixed"),
            ],
        )
        return build_schedule(self.players, cfg)

    def test_segment_rules_never_violated(self):
        sch = self._build()
        assert_structurally_valid(self, sch)
        self.assertEqual(len(sch.rounds), 12)

        for rnd in sch.rounds:
            for m in rnd.matches:
                gs = [self.gmap[p] for p in m.players()]
                if rnd.segment == "Putra":
                    self.assertTrue(all(g == "M" for g in gs),
                                    f"ronde {rnd.index} putra kemasukan putri")
                elif rnd.segment == "Putri":
                    self.assertTrue(all(g == "F" for g in gs),
                                    f"ronde {rnd.index} putri kemasukan putra")
                elif rnd.segment == "Mixed":
                    ta = [self.gmap[p] for p in m.team_a]
                    tb = [self.gmap[p] for p in m.team_b]
                    self.assertNotEqual(ta[0], ta[1],
                                        f"ronde {rnd.index}: tim A bukan mixed")
                    self.assertNotEqual(tb[0], tb[1],
                                        f"ronde {rnd.index}: tim B bukan mixed")

    def test_gender_rounds_use_full_uniqueness(self):
        # 4 putra punya tepat 3 kombinasi partner -> 3 ronde harus 100% unik.
        sch = self._build()
        seen = set()
        for rnd in sch.rounds:
            if rnd.segment != "Putra":
                continue
            for m in rnd.matches:
                for team in (m.team_a, m.team_b):
                    key = tuple(sorted(team))
                    self.assertNotIn(key, seen, "partner putra berulang")
                    seen.add(key)
        self.assertEqual(len(seen), 6, "3 ronde x 2 tim = 6 pasangan berbeda")

    def test_mixed_partners_unique(self):
        sch = self._build()
        seen = set()
        for rnd in sch.rounds:
            if rnd.segment != "Mixed":
                continue
            for m in rnd.matches:
                for team in (m.team_a, m.team_b):
                    key = tuple(sorted(team))
                    self.assertNotIn(key, seen, f"pasangan mixed {key} berulang")
                    seen.add(key)

    def test_rejects_mixed_without_enough_women(self):
        players = make_players(8, genders=["M"] * 7 + ["F"])
        cfg = Config(courts=1, duration_minutes=120,
                     segments=[Segment("Mixed", 6, "mixed")])
        with self.assertRaises(ScheduleError):
            build_schedule(players, cfg)


class TestAllWomenMeet(unittest.TestCase):
    """Meet perempuan semua: 4, 8, 12, ... harus jalan seperti Americano biasa."""

    def test_sizes(self):
        for n in (4, 8, 12, 16, 20):
            players = make_players(n, genders=["F"] * n)
            cfg = Config(courts=max(1, n // 4), duration_minutes=120,
                         mode="americano", effort=8000)
            sch = build_schedule(players, cfg)
            assert_structurally_valid(self, sch)

    def test_women_segment_works_when_all_female(self):
        players = make_players(12, genders=["F"] * 12)
        cfg = Config(courts=3, duration_minutes=120,
                     segments=[Segment("Putri", 8, "women")])
        sch = build_schedule(players, cfg)
        assert_structurally_valid(self, sch)
        self.assertEqual(len(sch.rounds), 8)


class TestTeamMode(unittest.TestCase):
    def test_partners_stay_locked(self):
        players = make_players(12)
        for i in range(0, 12, 2):
            players[i].partner_id = i + 1
            players[i + 1].partner_id = i
        cfg = Config(courts=3, duration_minutes=120, mode="team", effort=15000)
        sch = build_schedule(players, cfg)
        assert_structurally_valid(self, sch)

        expected = {p.id: p.partner_id for p in players}
        for rnd in sch.rounds:
            for m in rnd.matches:
                for team in (m.team_a, m.team_b):
                    self.assertEqual(expected[team[0]], team[1],
                                     f"pasangan tetap terpecah: {team}")


class TestMixedWithLockedPartner(unittest.TestCase):
    """Format yang ditanyakan host: babak mixed, tapi partner tidak berganti.

    Dulu ini diam-diam gagal. Konstruksi awal babak mixed memakai rotasi Latin
    square yang mengabaikan kunci partner, dan annealing tidak bisa
    memperbaikinya: gerakannya per-ronde, sedangkan ronde yang lahir melanggar
    kunci tidak punya satu pun susunan pengganti yang legal. Hasilnya jadwal
    yang melanggar permintaan host tanpa satu pun peringatan.
    """

    def _mixed_pairs(self):
        players = make_players(8, genders=["M"] * 4 + ["F"] * 4)
        for i in range(4):                      # tiap putra dikunci ke satu putri
            players[i].partner_id = 4 + i
            players[4 + i].partner_id = i
        return players, {p.id: p.partner_id for p in players}

    def test_partner_never_changes_during_mixed(self):
        players, expected = self._mixed_pairs()
        for seed in range(4):
            cfg = Config(courts=1, duration_minutes=120, round_minutes=10,
                         warmup_minutes=0, seed=seed, effort=15000,
                         segments=[Segment("Mixed", 6, "mixed")])
            sch = build_schedule(players, cfg)
            assert_structurally_valid(self, sch)
            for rnd in sch.rounds:
                for m in rnd.matches:
                    for team in (m.team_a, m.team_b):
                        self.assertEqual(
                            expected[team[0]], team[1],
                            f"seed {seed} ronde {rnd.index}: pasangan tetap "
                            f"terpecah di babak mixed: {team}")

    def test_locked_mixed_pair_still_plays_in_same_gender_segment(self):
        """Kunci beda gender mustahil di babak "sesama gender".

        Yang tidak boleh terjadi: kunci ditegakkan buta sampai tidak ada susunan
        yang lolos, lalu orangnya hilang sama sekali dari babak itu. Kuncinya
        dilonggarkan di sana, dan host diberi tahu.
        """
        players, expected = self._mixed_pairs()
        cfg = Config(courts=1, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, seed=2, effort=15000,
                     segments=[Segment("Sesama gender", 3, "same_gender"),
                               Segment("Mixed", 3, "mixed")])
        sch = build_schedule(players, cfg)
        assert_structurally_valid(self, sch)

        plays = sch.stats.plays_per_player
        self.assertTrue(all(v > 0 for v in plays.values()),
                        f"ada peserta yang hilang dari jadwal: {plays}")
        # Kunci tetap ditegakkan di babak yang sanggup menampungnya.
        for rnd in sch.rounds:
            if rnd.segment != "Mixed":
                continue
            for m in rnd.matches:
                for team in (m.team_a, m.team_b):
                    self.assertEqual(expected[team[0]], team[1],
                                     f"ronde {rnd.index}: kunci terpecah di mixed")
        self.assertTrue(
            any("partner tetap" in nt and "Sesama gender" in nt
                for nt in sch.notes),
            f"pelonggaran kunci tidak dilaporkan ke host: {sch.notes}")


class TestRoleFairness(unittest.TestCase):
    """Tugas wasit & ballboy harus serata yang dimungkinkan aritmetika.

    Dilaporkan host pada 26 peserta di 4 court: 13 ronde x 4 court x 2 peran =
    104 tugas untuk 26 orang, yaitu tepat 4 masing-masing - tapi hasilnya 3, 4,
    dan 5. Satu orang duduk dua ronde tanpa melakukan apa-apa sementara yang
    lain tidak pernah menganggur sama sekali.

    Penyebabnya bukan algoritma pembagiannya, melainkan urutan di dalam pass
    perataan: rantai pemecah kebuntuan untuk PERAN dicoba lebih dulu dan hampir
    selalu menemukan sesuatu, sehingga rantai untuk TOTAL tidak pernah kebagian
    giliran sampai jatah langkah habis.
    """

    def _tugas(self, sch):
        return {p: (sch.stats.roles_per_player.get(p, {}) or {}).get("total", 0)
                for p in sch.stats.plays_per_player}

    def test_host_case_is_perfectly_even(self):
        """Kasus yang dilaporkan: 104 tugas / 26 orang = tepat 4."""
        ps = make_players(26)
        sch = build_schedule(ps, Config(
            courts=4, duration_minutes=120, round_minutes=9, warmup_minutes=0,
            seed=77, effort=20000, referees_per_court=1, ballboys_per_court=1))
        tugas = self._tugas(sch)
        slot = sum(len(r.roles) for r in sch.rounds)
        self.assertEqual(slot % len(ps), 0, "setup ini memang harus bisa rata sempurna")
        self.assertEqual(
            max(tugas.values()) - min(tugas.values()), 0,
            f"tugas tidak rata: {sorted(set(tugas.values()))}")

    def test_spread_is_always_minimal(self):
        """Selisih 0 kalau slot habis dibagi peserta, 1 kalau tidak. Tidak boleh lebih."""
        for n, courts, wasit, ballboy in ((26, 4, 1, 1), (20, 4, 1, 1),
                                          (14, 3, 1, 1), (30, 5, 1, 2),
                                          (9, 2, 1, 1)):
            with self.subTest(peserta=n, court=courts):
                sch = build_schedule(make_players(n), Config(
                    courts=courts, duration_minutes=120, round_minutes=10,
                    warmup_minutes=0, seed=5, effort=15000,
                    referees_per_court=wasit, ballboys_per_court=ballboy))
                tugas = self._tugas(sch)
                slot = sum(len(r.roles) for r in sch.rounds)
                batas = 0 if slot % n == 0 else 1
                self.assertLessEqual(
                    max(tugas.values()) - min(tugas.values()), batas,
                    f"{n} peserta {courts} court: selisih tugas "
                    f"{max(tugas.values()) - min(tugas.values())}, batasnya {batas}")

    def test_idle_rest_is_shared(self):
        """Yang diukur peserta: berapa ronde ia duduk tanpa melakukan apa pun."""
        sch = build_schedule(make_players(26), Config(
            courts=4, duration_minutes=120, round_minutes=9, warmup_minutes=0,
            seed=77, effort=20000, referees_per_court=1, ballboys_per_court=1))
        tugas = self._tugas(sch)
        nganggur = [sch.stats.byes_per_player[p] - tugas[p] for p in tugas]
        self.assertLessEqual(
            max(nganggur) - min(nganggur), 1,
            f"ronde menganggur timpang: {sorted(set(nganggur))}")


class TestAllowedMatchups(unittest.TestCase):
    """Host boleh melarang format match yang timpang.

    Beda dari Segment.rule: itu mengatur SIAPA yang turun dan bagaimana satu
    tim disusun. Ini mengatur tim seperti apa boleh berhadapan dengan tim
    seperti apa - mis. melarang dua putra melawan dua putri.
    """

    def _players(self, putra=15, putri=11):
        return make_players(putra + putri, genders=["M"] * putra + ["F"] * putri)

    def _cfg(self, izin):
        return Config(courts=4, duration_minutes=120, round_minutes=9,
                      warmup_minutes=0, seed=77, effort=20000,
                      allowed_matchups=izin)

    def _formats(self, sch):
        g = {p.id: p.gender for p in sch.players}
        keluar = []
        for rnd in sch.rounds:
            for m in rnd.matches:
                keluar.append(matchup_code(
                    team_shape(g[m.team_a[0]], g[m.team_a[1]]),
                    team_shape(g[m.team_b[0]], g[m.team_b[1]])))
        return keluar

    def test_forbidden_formats_disappear(self):
        dilarang = {"LL-PP", "LP-PP"}
        izin = [m for m in MATCHUPS if m not in dilarang]
        sch = build_schedule(self._players(), self._cfg(izin))
        assert_structurally_valid(self, sch)
        muncul = set(self._formats(sch))
        self.assertFalse(
            muncul & dilarang,
            f"format terlarang tetap muncul: {sorted(muncul & dilarang)}")

    def test_only_same_shape(self):
        """Kasus paling ketat: tim hanya melawan tim sesusunan."""
        izin = ["LL-LL", "LP-LP", "PP-PP"]
        sch = build_schedule(self._players(), self._cfg(izin))
        assert_structurally_valid(self, sch)
        self.assertTrue(
            set(self._formats(sch)) <= set(izin),
            f"ada format di luar izin: {sorted(set(self._formats(sch)) - set(izin))}")

    def test_only_same_shape_holds_across_seeds(self):
        """Kasus paling ketat, diperiksa di banyak seed.

        Satu seed tidak cukup. Pelanggaran format lahir dari komposisi gender
        yang kebetulan tersisa di satu ronde, jadi ia datang dan pergi
        mengikuti seed: versi lama lolos di seed 77 - satu-satunya yang diuji -
        sambil menerbitkan 6 match terlarang di seed 46, yang justru dipakai
        host sungguhan.

        Akarnya, siapa yang turun dipilih hanya dari lama duduk, buta terhadap
        bentuk timnya. Ronde yang terlanjur berisi 3 tim putra dan 1 tim putri
        tidak punya jalan keluar: satu tim putra dan satu tim putri pasti tidak
        kebagian lawan yang sah, dan annealing tidak bisa memperbaikinya karena
        tiap gerakannya hanya diterima kalau rondenya legal.
        """
        izin = ["LL-LL", "LP-LP", "PP-PP"]
        for seed in (42, 44, 46, 48, 50):
            with self.subTest(seed=seed):
                cfg = self._cfg(izin)
                cfg.seed = seed
                sch = build_schedule(self._players(), cfg)
                keluar = set(self._formats(sch))
                self.assertTrue(
                    keluar <= set(izin),
                    f"seed {seed}: format di luar izin "
                    f"{sorted(keluar - set(izin))}")

    def test_restriction_does_not_wreck_opponent_variety(self):
        """Menyaring bentuk tim tidak boleh membuat lawan berulang membengkak.

        Menyaring bentuk tim mempersempit pilihan lawan, jadi memilih komposisi
        ronde HANYA dari lama duduk membuat komposisi yang sama terpakai
        berulang-ulang - dan orang yang sama bertemu lagi. Karena itu komposisi
        dinilai dari dua hal sekaligus, lama duduk DAN kesegaran lawan.

        Ambangnya hasil pengukuran berturut-turut di konfigurasi tes ini
        (effort 20000): tanpa penimbang keunikan lawan 17 pasang; dengan
        penimbang 11; setelah anggaran komposisi format dan denda "pernah
        ketemu 2x" turun ke 5; setelah jatah lawan segender disebar dan
        tukar-berpasangan ditambahkan, puncaknya 2 dari 10 seed. Ambang 4
        memberi ruang seed yang kurang beruntung tanpa membiarkan pembengkakan
        lama lolos lagi.
        """
        izin = ["LL-LL", "LP-LP", "PP-PP"]
        for seed in (42, 46, 50):
            with self.subTest(seed=seed):
                cfg = self._cfg(izin)
                cfg.seed = seed
                sch = build_schedule(self._players(), cfg)
                self.assertLessEqual(
                    sch.stats.opponent_repeat_pairs, 4,
                    f"seed {seed}: lawan berulang membengkak "
                    f"({sch.stats.opponent_repeat_pairs} pasang)")

    def test_same_gender_budget_is_not_blown(self):
        """Jatah lawan segender per orang tidak boleh dilampaui.

        Batas yang tidak kelihatan di hitungan suplai se-meet. Tiap ronde
        seorang pemain mendapat 1 lawan segender kalau tim campur melawan tim
        campur, tapi 2 kalau tim segender melawan tim segender - sementara
        calon lawan segendernya cuma sebanyak orang segender lainnya.

        11 putri yang main 8 ronde: yang dua kali kebagian putri-vs-putri butuh
        10 dari 10 calon, jadi harus bertemu semua tepat sekali tanpa satu pun
        kesempatan meleset. Melewati batas ini membuat lawan berulang WAJIB
        terjadi, dan kolam pasangan se-meet masih terlihat longgar saat itu -
        tidak ada peringatan apa pun yang menangkapnya.
        """
        izin = ["LL-LL", "LP-LP", "PP-PP"]
        for seed in (42, 46, 123):
            with self.subTest(seed=seed):
                cfg = self._cfg(izin)
                cfg.seed = seed
                sch = build_schedule(self._players(), cfg)
                g = {p.id: p.gender for p in sch.players}
                tersedia = Counter(g.values())
                slot = Counter()
                for rnd in sch.rounds:
                    for m in rnd.matches:
                        for x in m.team_a:
                            for y in m.team_b:
                                if g[x] == g[y]:
                                    slot[x] += 1
                                    slot[y] += 1
                for p, n in slot.items():
                    self.assertLessEqual(
                        n, tersedia[g[p]] - 1,
                        f"seed {seed}: {p} butuh {n} lawan segender padahal "
                        f"cuma ada {tersedia[g[p]] - 1} orang segender lain")

    def test_unavoidable_repeats_stay_spread(self):
        """Kalau nol mustahil, pengulangan harus TERSEBAR, bukan menumpuk.

        14 putra / 6 putri dengan format sesama-bentuk: cuma ada 15 pasangan
        putri-putri, jauh dari cukup, jadi pengulangan pasti terjadi. Di
        keadaan seperti ini denda "pernah ketemu 2x" justru merugikan -
        memperdalam pasangan yang sudah berulang jadi lebih murah daripada
        membuat pasangan baru ikut berulang - sehingga ia harus padam sendiri.

        Yang diuji bukan jumlahnya (memang banyak), tapi tidak adanya satu
        pasangan yang dipaksa bertemu jauh lebih sering daripada yang lain.
        """
        cfg = self._cfg(["LL-LL", "LP-LP", "PP-PP"])
        cfg.round_minutes = 12
        sch = build_schedule(self._players(14, 6), cfg)
        assert_structurally_valid(self, sch)
        self.assertLessEqual(
            sch.stats.opponent_repeat_max, 3,
            f"pengulangan menumpuk di satu pasangan "
            f"({sch.stats.opponent_repeat_max}x)")

    def _pair_demand(self, sch):
        """Berapa pasangan tiap jenis yang dihabiskan jadwal ini sebagai LAWAN.

        Inilah yang menentukan mungkin-tidaknya lawan 100% unik: dua orang
        hanya bisa berhadapan sekali kalau pasangannya tidak diminta lebih
        banyak dari yang ada.
        """
        g = {p.id: p.gender for p in sch.players}
        pakai = {"LL": 0, "LP": 0, "PP": 0}
        for rnd in sch.rounds:
            for m in rnd.matches:
                for x in m.team_a:
                    for y in m.team_b:
                        pakai[team_shape(g[x], g[y])] += 1
        return pakai

    def test_composition_stays_within_pair_supply(self):
        """Komposisi format tidak boleh menuntut lebih dari kolam pasangan.

        Ini invarian yang sebenarnya, bukan sekadar "lawan berulangnya sedikit".
        Dengan 11 putri hanya ada C(11,2) = 55 pasangan putri-putri; tiap match
        putri vs putri menghabiskan 4 sekaligus. Komposisi yang menuntut 56
        membuat lawan unik mustahil secara aritmetika sebelum optimizer mulai
        bekerja, dan tidak ada effort yang bisa menebusnya.

        Dulu komposisi dipilih per ronde tanpa anggaran se-meet dan mendarat di
        14/32/6 - kebutuhan pasangan putri-putri 56 dari 55 yang ada.
        """
        putra, putri = 15, 11
        stok = {
            "LL": putra * (putra - 1) // 2,
            "LP": putra * putri,
            "PP": putri * (putri - 1) // 2,
        }
        for seed in (42, 46, 50, 77):
            with self.subTest(seed=seed):
                cfg = self._cfg(["LL-LL", "LP-LP", "PP-PP"])
                cfg.seed = seed
                sch = build_schedule(self._players(putra, putri), cfg)
                pakai = self._pair_demand(sch)
                for jenis, n in pakai.items():
                    self.assertLessEqual(
                        n, stok[jenis],
                        f"seed {seed}: butuh {n} pasangan {jenis} padahal cuma "
                        f"ada {stok[jenis]} - lawan unik jadi mustahil")

    def test_restriction_keeps_plays_even(self):
        """Membatasi format tidak boleh membuat jumlah main timpang.

        Komposisi format menentukan berapa slot putra dan berapa slot putri
        yang dipakai seluruh meet. Kalau totalnya tidak cocok dengan roster,
        sebagian orang pasti main lebih sering - dan rebalance_plays tidak bisa
        menambalnya, karena menukar putra dengan putri melahirkan bentuk tim
        yang ilegal dan langsung ditolak.
        """
        for seed in (42, 46, 50, 77):
            with self.subTest(seed=seed):
                cfg = self._cfg(["LL-LL", "LP-LP", "PP-PP"])
                cfg.seed = seed
                sch = build_schedule(self._players(), cfg)
                main = sch.stats.plays_per_player.values()
                self.assertLessEqual(
                    max(main) - min(main), 1,
                    f"seed {seed}: jumlah main timpang {sorted(set(main))}")

    def test_default_unchanged(self):
        """Tanpa batasan, perilakunya harus persis seperti sebelum fitur ini."""
        a = build_schedule(self._players(), self._cfg(None))
        b = build_schedule(self._players(), self._cfg(list(MATCHUPS)))
        self.assertEqual([[m.players() for m in r.matches] for r in a.rounds],
                         [[m.players() for m in r.matches] for r in b.rounds],
                         "mengizinkan semua format harus sama dengan tanpa batasan")

    def test_empty_list_rejected(self):
        """Nol format berarti tidak ada susunan yang sah sama sekali."""
        with self.assertRaises(ValueError):
            self._cfg([])

    def test_unknown_code_rejected(self):
        with self.assertRaises(ValueError):
            self._cfg(["LL-LL", "XX-YY"])

    def test_missing_gender_does_not_block(self):
        """Meet tanpa data gender harus tetap bisa jalan."""
        ps = make_players(16)          # gender None semua
        sch = build_schedule(ps, self._cfg(["PP-PP"]))
        assert_structurally_valid(self, sch)
        self.assertTrue(any(r.matches for r in sch.rounds),
                        "jadwal kosong padahal gender tidak diisi")


class TestLockedPairsScoring(unittest.TestCase):
    """Pasangan yang sengaja dikunci bukan kegagalan rotasi.

    Skor memotong 45 poin untuk pengulangan partner. Di format pasangan tetap
    tiap pasangan mengulang partnernya tiap ronde - itu justru yang diminta
    host - sehingga meet seperti itu selalu dinilai buruk (52,5/100) dan kartu
    "Partner ulang" selalu menyala. Yang diukur metrik ini adalah rotasi yang
    MELESET, jadi pasangan terkunci tidak ikut dihitung.
    """

    def _cfg(self):
        return Config(courts=1, duration_minutes=120, round_minutes=10,
                      warmup_minutes=0, mode="americano", seed=42, effort=20000,
                      segments=[Segment("Mixed", 12, "mixed")])

    def _players(self, n_locked):
        ps = make_players(8, genders=["M"] * 4 + ["F"] * 4)
        for i in range(n_locked):
            ps[i].partner_id = 4 + i
            ps[4 + i].partner_id = i
        return ps

    def test_all_locked_is_not_penalised(self):
        sch = build_schedule(self._players(4), self._cfg())
        self.assertEqual(
            sch.stats.partner_repeat_pairs, 0,
            "pasangan terkunci dihitung sebagai pengulangan yang meleset")
        self.assertGreater(
            sch.stats.quality_score, 80,
            f"format pasangan tetap dinilai buruk: {sch.stats.quality_score}")

    def test_free_pairs_still_counted(self):
        """Yang dikecualikan hanya yang dikunci, bukan semua pengulangan."""
        loose = build_schedule(self._players(0), self._cfg())
        self.assertGreater(
            loose.stats.partner_repeat_pairs, 0,
            "tanpa kunci, pengulangan partner harus tetap dilaporkan")

    def test_score_rises_as_more_pairs_are_locked(self):
        """Makin banyak yang dikunci, makin sedikit rotasi yang bisa meleset."""
        scores = [build_schedule(self._players(k), self._cfg()).stats.quality_score
                  for k in (0, 2, 4)]
        self.assertLess(scores[0], scores[2], f"skor tidak naik: {scores}")
        self.assertLessEqual(
            scores[0], scores[1] + 1e-9,
            f"mengunci sebagian justru menurunkan skor: {scores}")


class TestTieredMode(unittest.TestCase):
    def test_beginners_never_face_advanced(self):
        # 8 pemain kuat (rating 5) + 8 pemula (rating 1).
        ratings = [5.0] * 8 + [1.0] * 8
        players = make_players(16, ratings=ratings)
        cfg = Config(courts=4, duration_minutes=120, mode="tiered",
                     tier_count=2, effort=20000)
        sch = build_schedule(players, cfg)
        assert_structurally_valid(self, sch)

        rmap = {p.id: p.rating for p in players}
        for rnd in sch.rounds:
            for m in rnd.matches:
                rs = [rmap[p] for p in m.players()]
                self.assertLessEqual(
                    max(rs) - min(rs), 0.01,
                    f"ronde {rnd.index}: pemula dicampur dengan pemain kuat",
                )


class TestMexicanoMode(unittest.TestCase):
    def test_teams_are_rating_balanced(self):
        ratings = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5,
                   5.0, 5.5, 6.0, 1.2, 2.2, 3.2, 4.2, 5.2]
        players = make_players(16, ratings=ratings)
        cfg = Config(courts=4, duration_minutes=120, mode="mexicano", effort=30000)
        sch = build_schedule(players, cfg)
        assert_structurally_valid(self, sch)
        self.assertLess(
            sch.stats.avg_rating_gap, 1.5,
            f"selisih rating antar tim terlalu besar: {sch.stats.avg_rating_gap}",
        )


class TestMultiStart(unittest.TestCase):
    """Penjadwalan diulang beberapa kali, lalu diambil yang terbaik.

    Annealing berhenti di optimum lokal yang berbeda-beda tergantung lintasan
    acaknya. Pada setup 26 orang dengan format dibatasi (24 seed, effort
    160000), satu percobaan mencapai nol lawan berulang di 13 seed; tiga
    percobaan di 22 seed, dengan ongkos 1.6x waktu - bukan 3x, karena mayoritas
    berhenti di percobaan pertama.
    """

    def _players(self):
        return make_players(26, genders=["M"] * 15 + ["F"] * 11)

    def _cfg(self, attempts, seed=1):
        return Config(courts=4, duration_minutes=120, round_minutes=9,
                      warmup_minutes=0, seed=seed, effort=8000,
                      attempts=attempts,
                      allowed_matchups=["LL-LL", "LP-LP", "PP-PP"])

    def test_rejects_zero_attempts(self):
        with self.assertRaises(ValueError):
            Config(courts=2, duration_minutes=60, attempts=0)

    def test_deterministic(self):
        """Seed yang sama harus tetap memberi jadwal yang sama persis.

        Multi-start memakai seed turunan, jadi gampang tanpa sengaja menarik
        keacakan dari luar - dan begitu itu terjadi, laporan host tidak bisa
        direproduksi lagi.
        """
        a = build_schedule(self._players(), self._cfg(3))
        b = build_schedule(self._players(), self._cfg(3))
        self.assertEqual(
            [[m.players() for m in r.matches] for r in a.rounds],
            [[m.players() for m in r.matches] for r in b.rounds])

    def test_reports_original_seed(self):
        """Yang dilaporkan seed host, bukan seed turunan yang kebetulan menang.

        Seluruh rangkaian percobaan ditentukan seed asli, jadi angka itulah
        yang mengulang hasilnya - seed turunan tidak.
        """
        sch = build_schedule(self._players(), self._cfg(3, seed=7))
        self.assertEqual(sch.config.seed, 7)
        self.assertEqual(sch.config.attempts, 3)

    def test_never_worse_than_single(self):
        """Percobaan pertama identik dengan attempts=1, dan yang terbaik dipilih.

        Jadi hasilnya mustahil lebih buruk - kalau pernah lebih buruk, berarti
        pemilihannya atau seed turunannya bocor.
        """
        for seed in (1, 7, 21):
            with self.subTest(seed=seed):
                satu = build_schedule(self._players(), self._cfg(1, seed))
                tiga = build_schedule(self._players(), self._cfg(3, seed))
                self.assertLessEqual(
                    (tiga.stats.partner_repeat_pairs,
                     tiga.stats.opponent_repeat_pairs),
                    (satu.stats.partner_repeat_pairs,
                     satu.stats.opponent_repeat_pairs),
                    f"seed {seed}: 3 percobaan lebih buruk daripada 1")

    def test_floor_flag_is_honest(self):
        """Tanda "sudah di batas bawah" harus cocok dengan kenyataan.

        Tanda inilah yang menghentikan percobaan lebih awal, jadi kalau ia
        menyala terlalu cepat, multi-start berhenti sebelum waktunya.
        """
        # 16 pemain, 4 court, 9 ronde: tiap orang main 9x dari 15 calon partner
        # dan 7 batas lawan unik - pengulangan lawan wajib, jadi nol mustahil.
        cfg = Config(courts=4, duration_minutes=120, round_minutes=12,
                     mode="americano", effort=8000, attempts=1)
        sch = build_schedule(make_players(16), cfg)
        if sch.stats.at_theoretical_floor:
            self.assertGreater(
                sch.stats.opponent_repeat_pairs, 0,
                "batas bawah di setup ini bukan nol, jadi tandanya menyesatkan")

    def test_floor_reached_means_perfect_when_possible(self):
        """Kalau nol memang mungkin, tanda batas bawah cuma boleh menyala di nol."""
        sch = build_schedule(self._players(), self._cfg(3))
        if sch.stats.at_theoretical_floor:
            self.assertEqual(sch.stats.opponent_repeat_pairs, 0)
            self.assertEqual(sch.stats.partner_repeat_pairs, 0)


class TestPolishPairs(unittest.TestCase):
    """Sapuan deterministik yang jalan setelah perataan jumlah main.

    Perataan menukar pemain demi menyamakan jumlah main tanpa ada yang menilai
    ulang pertemuannya, jadi ia bisa melahirkan pengulangan baru di keadaan
    akhir - keadaan yang sudah tidak dilihat annealing lagi.
    """

    def test_removes_avoidable_repeats(self):
        """Partner yang terulang padahal cuma perlu ditukar susunannya."""
        st = ScheduleState(8, [3.0] * 8, Weights(), 2, Rules())
        st.place_round(0, [[0, 1, 2, 3], [4, 5, 6, 7]], [])
        st.place_round(1, [[0, 1, 4, 5], [2, 3, 6, 7]], [])
        sebelum = st.cost()
        self.assertGreater(polish_pairs(st), 0, "ada perbaikan yang terlewat")
        self.assertLess(st.cost(), sebelum)

    def test_does_not_disturb_play_counts(self):
        """Jumlah main tiap orang harus utuh, per orang, bukan cuma totalnya.

        Ini yang membuatnya aman dijalankan SETELAH rebalance_plays: kerataan
        yang baru saja dijamin tidak mungkin tergerus di sini.

        Yang boleh bergeser adalah LETAK istirahatnya - fase tukar-berpasangan
        memang memindahkan orang antar ronde. Yang tidak boleh berubah adalah
        berapa kali tiap orang main dan berapa kali tiap orang duduk.
        """
        st = ScheduleState(10, [3.0] * 10, Weights(), 4, Rules())
        st.place_round(0, [[0, 1, 2, 3], [4, 5, 6, 7]], [8, 9])
        st.place_round(1, [[0, 1, 4, 5], [2, 3, 8, 9]], [6, 7])
        st.place_round(2, [[0, 1, 6, 7], [2, 3, 4, 5]], [8, 9])
        st.place_round(3, [[8, 9, 2, 4], [1, 6, 3, 5]], [0, 7])
        main = list(play_counts(st))
        duduk = list(st.bye_count)
        polish_pairs(st)
        self.assertEqual(list(play_counts(st)), main, "jumlah main bergeser")
        self.assertEqual(list(st.bye_count), duduk, "jumlah duduk bergeser")
        for r in range(st.n_rounds):
            hadir = [p for q in st.matches[r] for p in q]
            self.assertEqual(len(hadir), len(set(hadir)),
                             f"ronde {r}: ada yang main dobel")

    def test_stops_when_nothing_left(self):
        st = ScheduleState(8, [3.0] * 8, Weights(), 2, Rules())
        st.place_round(0, [[0, 1, 2, 3], [4, 5, 6, 7]], [])
        st.place_round(1, [[0, 4, 2, 6], [1, 5, 3, 7]], [])
        polish_pairs(st)
        self.assertEqual(polish_pairs(st), 0, "sapuan kedua harus tidak berbuah")


class TestOpponentCap(unittest.TestCase):
    """Denda sekali-bayar saat sepasang orang berhadapan untuk kedua kalinya.

    Bentuk c*(c-1) sendirian konveks, jadi pengulangan PERTAMA justru yang
    paling murah - bagus untuk menyebar pengulangan yang tak terhindarkan,
    buruk untuk mengejar nol.
    """

    def _weights(self):
        return Weights(partner=0.0, opponent=0.0, opponent_cap=400.0,
                       bye=0.0, b2b_bye=0.0, repeat_gap=0.0, preference=0.0)

    def test_charged_once_on_second_meeting(self):
        st = ScheduleState(8, [3.0] * 8, self._weights(), 2, Rules())
        st.place_round(0, [[0, 1, 2, 3], [4, 5, 6, 7]], [])
        self.assertEqual(st.cost(), 0.0, "pertemuan pertama tidak didenda")
        st.place_round(1, [[0, 1, 2, 3], [4, 5, 6, 7]], [])
        # 4 pasang lawan per match x 2 match, semuanya jadi 2x.
        self.assertAlmostEqual(st.cost(), 8 * 400.0)

    def test_not_charged_again_on_third_meeting(self):
        """Dendanya di perbatasan 1->2, bukan tiap tambahan.

        Kalau ia menagih tiap kali, jadwal yang memang mustahil nol akan
        terhukum berulang-ulang dan penyebaran pengulangan jadi kacau.
        """
        st = ScheduleState(8, [3.0] * 8, self._weights(), 3, Rules())
        st.place_round(0, [[0, 1, 2, 3], [4, 5, 6, 7]], [])
        st.place_round(1, [[0, 1, 2, 3], [4, 5, 6, 7]], [])
        dua_kali = st.cost()
        st.place_round(2, [[0, 1, 2, 3], [4, 5, 6, 7]], [])
        self.assertAlmostEqual(st.cost(), dua_kali)

    def test_incremental_bookkeeping_matches_recount(self):
        """Biaya inkremental O(1) harus sama dengan hitungan dari nol.

        Pembukuan delta gampang meleset di perbatasan naik/turun, dan
        melesetnya tidak kelihatan - annealing cuma jadi mengoptimasi angka
        yang salah.
        """
        st = ScheduleState(10, [3.0] * 10, Weights(opponent_cap=400.0),
                           3, Rules())
        st.place_round(0, [[0, 1, 2, 3], [4, 5, 6, 7]], [8, 9])
        st.place_round(1, [[0, 1, 4, 5], [2, 3, 8, 9]], [6, 7])
        st.place_round(2, [[0, 1, 6, 7], [2, 3, 4, 5]], [8, 9])
        polish_pairs(st)
        w = st.w
        ulang = 0.0
        for i in range(st.n):
            for j in range(i + 1, st.n):
                k = i * st.n + j
                pc, oc = st.pc[k], st.oc[k]
                ulang += w.partner * pc * (pc - 1) + w.opponent * oc * (oc - 1)
                if oc >= 2:
                    ulang += w.opponent_cap
        self.assertAlmostEqual(st.cost_pair, ulang)


class TestShapeBudget(unittest.TestCase):
    """Kelayakan lawan unik yang sadar gender dan sadar format.

    Batas umum (N-1)//2 mengandaikan satu kolam pasangan: siapa pun boleh
    melawan siapa pun. Begitu format dibatasi, kolamnya pecah tiga dan yang
    terkecil yang menentukan - kadang mustahil justru di setup yang menurut
    batas umum aman.
    """

    SAMA = ["LL-LL", "LP-LP", "PP-PP"]

    def test_feasible_window_for_real_roster(self):
        """15 putra / 11 putri, 52 match: layak, tapi jendelanya sempit."""
        b = shape_budget(15, 11, 52, self.SAMA)
        self.assertTrue(b.feasible)
        self.assertIsNotNone(b.target)
        self.assertEqual(sum(b.target.values()), 52)
        # Slot gender harus habis persis, kalau tidak jumlah main jadi timpang.
        tim = shape_totals(b.target)
        self.assertEqual(2 * tim["LL"] + tim["LP"], 15 * 8)
        self.assertEqual(2 * tim["PP"] + tim["LP"], 11 * 8)

    def test_impossible_when_women_too_few(self):
        """Setup yang batas umumnya bilang aman, tapi sebenarnya mustahil."""
        rep = analyze(20, courts=4, duration_minutes=120, round_minutes=12,
                      warmup_minutes=0, men=14, women=6,
                      allowed_matchups=self.SAMA)
        # Batas gender-blind meloloskannya: 8 ronde main <= (20-1)//2 = 9.
        self.assertEqual(rep.max_unique_opponent_rounds, 9)
        self.assertFalse(rep.shape_feasible)
        self.assertFalse(rep.opponent_unique_feasible)
        self.assertEqual(rep.shape_binding, "PP")
        self.assertGreater(rep.shape_shortfall, 0)
        self.assertTrue(
            any("mustahil" in i.title.lower() for i in rep.issues),
            "host harus diberi tahu, bukan menemukannya sendiri dari jadwal")

    def test_loosening_formats_rescues_it(self):
        """Roster yang sama jadi layak begitu formatnya tidak dibatasi."""
        self.assertFalse(shape_budget(14, 6, 40, self.SAMA).feasible)
        self.assertTrue(shape_budget(14, 6, 40, None).feasible)

    def test_not_assessed_without_gender(self):
        """Tanpa data gender jangan mengaku tahu - dan jangan mengubah apa pun."""
        rep = analyze(26, courts=4, duration_minutes=120, round_minutes=12)
        self.assertIsNone(rep.shape_feasible)
        self.assertTrue(rep.opponent_unique_feasible)

    def test_cap_limits_the_target(self):
        """Muat di atas kertas tidak sama dengan terjangkau.

        Target yang menuntut lebih banyak tim campur daripada yang sanggup
        disediakan rotasi partner lebih buruk daripada tidak punya target:
        penjadwalnya melesetinya tiap ronde lalu tidak bisa menebusnya.
        """
        bebas = shape_budget(15, 11, 52, self.SAMA)
        sempit = shape_budget(15, 11, 52, self.SAMA,
                              cap={"LL": 60, "LP": 80, "PP": 20})
        self.assertTrue(sempit.feasible)
        self.assertLessEqual(shape_totals(sempit.target)["LP"], 80)
        self.assertGreater(shape_totals(bebas.target)["LP"], 0)

    def test_shape_totals_counts_both_teams(self):
        self.assertEqual(shape_totals({"LL-LL": 3}), {"LL": 6, "LP": 0, "PP": 0})
        self.assertEqual(shape_totals({"LL-LP": 2}), {"LL": 2, "LP": 2, "PP": 0})


class TestCapacity(unittest.TestCase):
    def test_flags_overcrowding(self):
        # 26 pemain di 4 court -> 10 duduk tiap ronde (38%).
        rep = analyze(26, courts=4, duration_minutes=120, round_minutes=12)
        self.assertEqual(rep.byes_per_round, 10)
        self.assertGreater(rep.rest_ratio, 0.33)
        self.assertEqual(rep.courts_for_comfort, 5)
        self.assertTrue(any("duduk" in i.title.lower() for i in rep.issues))

    def test_flags_impossible_uniqueness(self):
        # 8 pemain, 2 court, 9 ronde -> lawan unik mustahil (batas 3).
        rep = analyze(8, courts=2, duration_minutes=120, round_minutes=12)
        self.assertEqual(rep.max_unique_opponent_rounds, 3)
        self.assertFalse(rep.opponent_unique_feasible)

    def test_large_group_uniqueness_is_easy(self):
        # 26 pemain, 4 court: karena banyak yang duduk, tiap orang main sedikit,
        # jadi keunikan justru gampang tercapai. Ini yang sering disalahpahami.
        rep = analyze(26, courts=4, duration_minutes=120, round_minutes=12)
        self.assertTrue(rep.opponent_unique_feasible)
        self.assertTrue(rep.partner_unique_feasible)

    def test_flags_idle_courts(self):
        rep = analyze(6, courts=3, duration_minutes=90, round_minutes=12)
        self.assertEqual(rep.courts_used, 1)
        self.assertEqual(rep.courts_idle, 2)

    def test_zero_rounds_is_error(self):
        rep = analyze(8, courts=2, duration_minutes=10, round_minutes=12)
        self.assertEqual(rep.rounds, 0)
        self.assertEqual(rep.verdict, "error")


class TestPemerataanGenderTimpang(unittest.TestCase):
    """Jatah main tetap merata walau roster gendernya timpang.

    Format "sesama bentuk" membuat tiap ronde memakai 0 atau 2 perempuan (kalau
    perempuannya ganjil, PP-PP mustahil). Berapa banyak ronde campuran yang
    dipilih menentukan jatah main tiap gender - dan pilihan itu lahir di
    pemilihan baris 1-faktorisasi, bukan di annealing: mengubah ronde LL-LL
    jadi LP-LP menuntut dua pemain ditukar sekaligus, sedangkan gerakan
    annealing satu pemain dan keadaan antaranya ilegal.
    """

    SAMA = ["LL-LL", "LP-LP", "PP-PP"]

    def _roster(self, pria, wanita):
        return [
            Player(id=i, name=f"P{i+1}", rating=float(2 + (i % 4)),
                   gender="M" if i < pria else "F")
            for i in range(pria + wanita)
        ]

    def test_lima_pria_tiga_wanita_main_sama_rata(self):
        """5L/3P, 12 ronde, 1 court: semua main 6x, bukan 4-8.

        Aritmetikanya: 9 ronde campuran memberi perempuan 18 slot (6 each) dan
        laki-laki 30 slot (6 each). Sebelum diperbaiki penjadwal memakai 6
        ronde campuran dan berakhir 4-8.
        """
        cfg = Config(courts=1, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, mode="americano", seed=77,
                     effort=20_000, attempts=1, allowed_matchups=self.SAMA)
        sch = build_schedule(self._roster(5, 3), cfg)
        main = sch.stats.plays_per_player
        self.assertEqual(sch.stats.rounds, 12)
        self.assertEqual(
            (min(main.values()), max(main.values())), (6, 6),
            f"jatah main tidak merata: {main}",
        )

    def test_pemerataan_tidak_mengorbankan_keunikan_partner(self):
        """9L/3P, 2 court: merata TANPA menambah partner berulang.

        Penjaga di _balanced_rows hanya menggeser kalau spread benar-benar
        turun, jadi kasus yang sudah rapi tidak ikut diacak.
        """
        cfg = Config(courts=2, duration_minutes=90, round_minutes=10,
                     warmup_minutes=0, mode="americano", seed=77,
                     effort=20_000, attempts=1, allowed_matchups=self.SAMA)
        sch = build_schedule(self._roster(9, 3), cfg)
        main = sch.stats.plays_per_player
        self.assertEqual(max(main.values()) - min(main.values()), 0,
                         f"jatah main tidak merata: {main}")
        self.assertEqual(sch.stats.partner_repeat_pairs, 0)

    def test_roster_seimbang_tidak_berubah(self):
        """Roster gender seimbang sudah merata - jangan diutak-atik.

        Tanpa penjaga, pemerataan yang tidak menambah apa pun tetap menggeser
        baris dan menukar partner unik dengan nol perbaikan.
        """
        cfg = Config(courts=3, duration_minutes=90, round_minutes=10,
                     warmup_minutes=0, mode="americano", seed=77,
                     effort=20_000, attempts=1, allowed_matchups=self.SAMA)
        sch = build_schedule(self._roster(6, 6), cfg)
        main = sch.stats.plays_per_player
        self.assertEqual(max(main.values()) - min(main.values()), 0,
                         f"jatah main tidak merata: {main}")


def hitung_giliran(schedule):
    """Metrik giliran, dihitung ulang dari jadwal - bukan dibaca dari stats.

    Uji yang membaca stats hanya memeriksa bahwa penjadwal setuju dengan
    dirinya sendiri. Yang harus dijaga adalah jadwalnya, jadi angkanya
    dihitung ulang dari isi ronde.
    """
    ids = [p.id for p in schedule.players]
    n_ronde = len(schedule.rounds)
    sudah = {p: 0 for p in ids}
    sejak = {p: 0 for p in ids}
    main = {p: 0 for p in ids}
    pertama: dict[int, int] = {}
    terlewat = 0
    tunggu = 0
    for rnd in schedule.rounds:
        turun = {p for m in rnd.matches for p in m.players()}
        if turun:
            duduk = [sudah[p] for p in ids if p not in turun]
            if duduk:
                terlewat += sum(1 for p in turun if sudah[p] > min(duduk))
        for p in turun:
            sudah[p] += 1
            main[p] += 1
            pertama.setdefault(p, rnd.index)
        for p in ids:
            if p in turun:
                sejak[p] = rnd.index
            else:
                tunggu = max(tunggu, rnd.index - sejak[p])
    batas = max(
        (math.ceil((n_ronde - main[p]) / (main[p] + 1)) if n_ronde > main[p] else 0)
        for p in ids
    )
    return {
        "terlewat": terlewat,
        "tunggu": tunggu,
        "batas_tunggu": batas,
        "main_pertama_terakhir": (max(pertama.values())
                                  if len(pertama) == len(ids) else n_ronde),
    }


class TestGiliranBerurutan(unittest.TestCase):
    """Peserta yang belum main mendapat giliran lebih dulu.

    Ini properti yang berbeda dari "jumlah main merata", dan yang kedua tidak
    menyiratkan yang pertama. Jadwal bisa berakhir 6-6 untuk semua orang
    sementara satu peserta baru turun di ronde 4 dan peserta lain sudah dua kali
    di ronde 3; totalnya dibalas di ronde-ronde terakhir. Yang dirasakan peserta
    adalah urutannya, bukan rekapnya.

    Batas bawahnya nyata dan bukan kelemahan algoritma: dengan 4 slot per court
    per ronde, peserta yang main m dari R ronde punya R-m ronde duduk untuk
    dibagi ke paling banyak m+1 sela, jadi rentetan terpanjang tidak bisa lebih
    pendek dari ceil((R-m)/(m+1)). Uji-uji di bawah membandingkan dengan batas
    itu, bukan dengan nol.
    """

    SAMA = ["LL-LL", "LP-LP", "PP-PP"]

    def _roster(self, pria, wanita):
        return [
            Player(id=i, name=f"P{i+1}", rating=float(2 + (i % 4)),
                   gender="M" if i < pria else "F")
            for i in range(pria + wanita)
        ]

    def test_kasus_host_10_orang_1_court(self):
        """6L/4P, 1 court, 15 ronde: kasus yang dilaporkan host.

        Sebelum diperbaiki: peserta terakhir baru turun di ronde 5, tunggu
        terpanjang 4 ronde, dan 13 kali antrean diserobot - sementara jumlah
        mainnya 6-6 sehingga tidak ada satu pun angka lama yang menunjukkannya.
        """
        cfg = Config(courts=1, duration_minutes=120, round_minutes=8,
                     warmup_minutes=0, mode="americano", seed=42,
                     effort=30_000, attempts=3, allowed_matchups=self.SAMA)
        sch = build_schedule(self._roster(6, 4), cfg)
        g = hitung_giliran(sch)
        main = sch.stats.plays_per_player

        # Jumlah main tetap rata - pemerataan tidak boleh jadi korban.
        self.assertEqual(max(main.values()) - min(main.values()), 0,
                         f"jatah main tidak merata lagi: {main}")
        self.assertEqual(sch.stats.partner_repeat_pairs, 0,
                         "partner unik tidak boleh jadi korban")
        # Tunggu terpanjang mendekati batasnya. 9 ronde duduk di 7 sela berarti
        # rentetan 2 memang tak terhindarkan; jadwal lama sampai 5, sekarang 3.
        # Sisa satu rentetan berlebih itu tidak bisa dibuang tanpa melahirkan
        # lawan berulang baru, dan di situ keunikan yang menang.
        self.assertLessEqual(
            g["tunggu"], g["batas_tunggu"] + 1,
            f"tunggu terpanjang jauh di atas batas yang tak terhindarkan: {g}")
        # Antrean masih bisa terserobot, tapi tidak sesering dulu (16 kali).
        # Sisanya lahir dari rotasi partner: baris kombinasi ronde itu kadang
        # hanya memasangkan yang paling lama menunggu dengan yang baru main.
        self.assertLessEqual(g["terlewat"], 10,
                             f"antrean terlalu sering diserobot: {g}")

    def test_stats_setuju_dengan_jadwal(self):
        """Angka yang dilaporkan ke host harus angka jadwalnya, bukan tebakan."""
        cfg = Config(courts=1, duration_minutes=120, round_minutes=8,
                     warmup_minutes=0, mode="americano", seed=42,
                     effort=20_000, attempts=1, allowed_matchups=self.SAMA)
        sch = build_schedule(self._roster(6, 4), cfg)
        g = hitung_giliran(sch)
        self.assertEqual(sch.stats.turn_skips, g["terlewat"])
        self.assertEqual(sch.stats.longest_wait, g["tunggu"])
        self.assertEqual(sch.stats.wait_floor, g["batas_tunggu"])
        self.assertEqual(sch.stats.last_first_play, g["main_pertama_terakhir"])

    def test_semua_turun_di_putaran_pertama(self):
        """14 orang, 2 court (8 slot): tidak ada yang menunggu sampai ronde 3.

        Dua ronde sudah menyediakan 16 slot untuk 14 orang, jadi menahan
        seseorang sampai ronde 3 berarti ada yang main dua kali lebih dulu.
        """
        cfg = Config(courts=2, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, mode="americano", seed=5,
                     effort=20_000, attempts=1)
        sch = build_schedule(make_players(14), cfg)
        g = hitung_giliran(sch)
        self.assertLessEqual(
            g["main_pertama_terakhir"], 2,
            f"ada peserta yang belum turun setelah dua ronde: {g}")

    def test_giliran_rapi_lintas_roster(self):
        """Tunggu terpanjang tidak boleh jauh di atas batasnya, apa pun rosternya.

        Yang diperiksa selisih terhadap batas, bukan angka mutlak: 20 orang di 1
        court memang harus menunggu lebih lama daripada 8 orang di 2 court, dan
        menuntut angka yang sama untuk keduanya cuma akan menghasilkan uji yang
        menguji jumlah court.
        """
        kasus = [
            (8, 0, 1), (10, 0, 1), (12, 0, 2), (16, 0, 2),
            (20, 0, 1), (9, 0, 2), (11, 0, 1),
        ]
        for pria, wanita, court in kasus:
            with self.subTest(pemain=pria + wanita, court=court):
                cfg = Config(courts=court, duration_minutes=120,
                             round_minutes=10, warmup_minutes=0,
                             mode="americano", seed=13, effort=20_000,
                             attempts=1)
                sch = build_schedule(make_players(pria + wanita), cfg)
                g = hitung_giliran(sch)
                # +2 dari batas, bukan +1: pada beberapa roster satu rentetan
                # berlebih memang tidak bisa dibuang tanpa melahirkan lawan
                # berulang, dan keunikan yang menang. Yang dijaga di sini adalah
                # ekornya - jadwal lama sampai menunggu 9 ronde dari batas 4.
                self.assertLessEqual(
                    g["tunggu"], g["batas_tunggu"] + 2,
                    f"{pria + wanita} orang / {court} court: tunggu "
                    f"{g['tunggu']} vs batas {g['batas_tunggu']} - {g}")

    def test_biaya_tunggu_konveks_memilih_yang_terpecah(self):
        """Satu rentetan panjang harus dinilai lebih mahal dari dua yang pendek.

        Ini yang membedakan biaya baru dari b2b_bye: b2b_bye linear, jadi
        3+3 dan 5+1 dinilai sama dan optimizer tidak punya alasan memilih.
        """
        w = Weights()
        rules = Rules()

        def biaya(duduk_di):
            st = ScheduleState(4, [3.0] * 4, w, 7, rules)
            for r in range(7):
                st._set_bye(r, 0, r in duduk_di)
            return st.cost_wait

        satu_panjang = biaya({0, 1, 2, 3, 4})          # satu rentetan 5
        dua_pendek = biaya({0, 1, 2, 4, 5, 6})         # rentetan 3 lalu 3
        self.assertGreater(
            satu_panjang, dua_pendek,
            "rentetan 5 harus lebih mahal daripada 3+3, kalau tidak optimizer "
            "tidak punya dorongan memecahnya")


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_schedule(self):
        def gen():
            cfg = Config(courts=3, duration_minutes=120, mode="americano",
                         seed=7, effort=5000)
            return build_schedule(make_players(12), cfg)

        a, b = gen(), gen()
        self.assertEqual(
            [[m.players() for m in r.matches] for r in a.rounds],
            [[m.players() for m in r.matches] for r in b.rounds],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
