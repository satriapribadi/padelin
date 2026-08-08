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

import unittest
from itertools import combinations

from padel_scheduler import Config, Player, Segment, build_schedule
from padel_scheduler.capacity import analyze
from padel_scheduler.factorization import (
    mixed_pair_rounds,
    verify_one_factorization,
)
from padel_scheduler.models import MATCHUPS, matchup_code, team_shape
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
