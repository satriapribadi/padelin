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
        self.assertLessEqual(
            max(plays) - min(plays), 2,
            f"jumlah main tidak merata: {min(plays)}..{max(plays)}",
        )


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
