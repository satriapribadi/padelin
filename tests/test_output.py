"""Uji modul pendukung: pembagian tugas, ekonomi, ekspor, dan database."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from padel_scheduler import Config, Economics, Player, build_schedule, storage
from padel_scheduler.economics import compare, fee_for_target_margin, upgrade_analysis
from padel_scheduler.html_report import build_html
from padel_scheduler.report import to_csv, to_dict, to_personal_text, to_text
from padel_scheduler.roles import assign_roles


def make_schedule(n=26, courts=4, refs=1, balls=1):
    players = [
        Player(id=i, name=f"Pemain {i + 1}", rating=2 + (i % 6) * 0.5,
               gender="M" if i < n // 2 else "F")
        for i in range(n)
    ]
    cfg = Config(courts=courts, duration_minutes=120, mode="americano",
                 effort=8000, referees_per_court=refs, ballboys_per_court=balls)
    return build_schedule(players, cfg)


class TestRoles(unittest.TestCase):
    def test_duties_go_only_to_resting_players(self):
        sch = make_schedule()
        for rnd in sch.rounds:
            playing = {p for m in rnd.matches for p in m.players()}
            for r in rnd.roles:
                self.assertIn(r.player_id, rnd.byes,
                              f"ronde {rnd.index}: petugas tidak sedang istirahat")
                self.assertNotIn(r.player_id, playing,
                                 f"ronde {rnd.index}: petugas sedang main")

    def test_one_duty_per_person_per_round(self):
        sch = make_schedule()
        for rnd in sch.rounds:
            ids = [r.player_id for r in rnd.roles]
            self.assertEqual(len(ids), len(set(ids)),
                             f"ronde {rnd.index}: satu orang dapat dua tugas")

    def test_every_court_gets_a_referee_when_enough_resting(self):
        sch = make_schedule(n=26, courts=4, refs=1, balls=0)
        for rnd in sch.rounds:
            courts = {m.court for m in rnd.matches}
            reffed = {r.court for r in rnd.roles if r.role == "wasit"}
            self.assertEqual(courts, reffed,
                             f"ronde {rnd.index}: ada court tanpa wasit")

    def test_duties_are_shared_fairly(self):
        sch = make_schedule()
        totals = [v.get("total", 0) for v in sch.stats.roles_per_player.values()]
        self.assertLessEqual(max(totals) - min(totals), 2,
                             f"pembagian tugas timpang: {min(totals)}..{max(totals)}")

    def test_duties_absorb_most_of_the_waiting(self):
        # Inti fiturnya: 26 pemain di 4 court -> 10 duduk, tapi 8 harus punya tugas.
        sch = make_schedule(n=26, courts=4, refs=1, balls=1)
        rnd = sch.rounds[0]
        self.assertEqual(len(rnd.byes), 10)
        self.assertEqual(len(rnd.roles), 8)
        self.assertEqual(len(rnd.resting_only()), 2)

    def test_disabled_by_default(self):
        players = [Player(id=i, name=f"P{i}") for i in range(8)]
        sch = build_schedule(players, Config(courts=2, duration_minutes=60))
        self.assertTrue(all(not r.roles for r in sch.rounds))

    def test_handles_more_courts_than_resting_players(self):
        # 8 pemain / 2 court -> tidak ada yang istirahat, jadi tidak ada petugas.
        assignments, summary = assign_roles([[], []], [[1, 2], [1, 2]], 1, 1)
        self.assertEqual(assignments, [[], []])
        self.assertEqual(summary, {})


class TestEconomics(unittest.TestCase):
    def setUp(self):
        self.econ = Economics(court_price_per_hour=250000, fee_per_player=85000,
                              other_costs=100000)

    def test_profit_math(self):
        opts = compare(26, self.econ, court_options=[4], hour_options=[2.0])
        o = opts[0]
        self.assertEqual(o.total_cost, 4 * 2 * 250000 + 100000)
        self.assertEqual(o.revenue, 26 * 85000)
        self.assertEqual(o.profit, o.revenue - o.total_cost)

    def test_break_even_fee_yields_zero_profit(self):
        econ = Economics(court_price_per_hour=250000, fee_per_player=0,
                         other_costs=100000)
        opts = compare(26, econ, court_options=[4], hour_options=[2.0])
        fee = opts[0].break_even_fee
        econ2 = Economics(250000, fee, 100000)
        o2 = compare(26, econ2, court_options=[4], hour_options=[2.0])[0]
        self.assertAlmostEqual(o2.profit, 0, delta=1.0)

    def test_target_margin_fee_hits_target(self):
        fee = fee_for_target_margin(26, 4, 2.0, self.econ, 30.0, round_to=0)
        econ2 = Economics(250000, fee, 100000)
        o = compare(26, econ2, court_options=[4], hour_options=[2.0])[0]
        self.assertAlmostEqual(o.margin_pct, 30.0, delta=0.5)

    def test_extra_court_costs_money_and_buys_play_time(self):
        up = upgrade_analysis(26, 4, 2.0, self.econ)
        self.assertGreater(up["extra_cost"], 0)
        self.assertGreater(up["extra_play_minutes_per_player"], 0)
        # Kenaikan fee minimal = tambahan biaya dibagi jumlah peserta.
        self.assertAlmostEqual(up["fee_bump_to_break_even"],
                               up["extra_cost"] / 26, delta=1.0)

    def test_extra_court_is_useless_when_players_cannot_fill_it(self):
        # 8 pemain sudah muat di 2 court; court ketiga tidak menambah apa pun.
        up = upgrade_analysis(8, 2, 2.0, self.econ)
        self.assertEqual(up["extra_play_minutes_per_player"], 0)
        self.assertFalse(up["worth_it"])


class TestExports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sch = make_schedule()

    def test_dict_includes_roles_and_idle(self):
        d = to_dict(self.sch)
        r = d["rounds"][0]
        self.assertTrue(r["roles"], "roles hilang dari payload API")
        self.assertIn("name", r["roles"][0])
        self.assertEqual(
            len(r["resting_only"]), len(r["byes"]) - len(r["roles"])
        )
        json.dumps(d)  # harus JSON-able

    def test_text_mentions_duties(self):
        t = to_text(self.sch, start_clock="19:00")
        self.assertIn("wasit", t)
        self.assertIn("19:", t, "jam dinding tidak dihitung")
        self.assertIn("Ronde 1", t)

    def test_personal_text_covers_every_player(self):
        t = to_personal_text(self.sch)
        for p in self.sch.players:
            self.assertIn(p.name, t)

    def test_csv_row_count(self):
        rows = to_csv(self.sch).strip().split("\n")
        total_matches = sum(len(r.matches) for r in self.sch.rounds)
        self.assertEqual(len(rows), total_matches + 1)
        self.assertIn("wasit", rows[0])

    def test_html_has_print_rules(self):
        h = build_html(self.sch, title="Tes")
        for needle in ("@page", "break-inside:avoid", "print-color-adjust",
                       "Rekap per pemain"):
            self.assertIn(needle, h)

    def test_html_escapes_player_names(self):
        players = [Player(id=i, name=f"<script>{i}</script>") for i in range(8)]
        sch = build_schedule(players, Config(courts=2, duration_minutes=60))
        h = build_html(sch)
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_master_data_roundtrip(self):
        with storage.session(self.db) as conn:
            cid = storage.save_club(conn, {"name": "Klub A", "city": "Jakarta"})
            vid = storage.save_venue(conn, {"club_id": cid, "name": "Arena",
                                            "court_count": 4,
                                            "price_per_hour": 250000})
            pid = storage.save_player(conn, {"club_id": cid, "name": "Andi",
                                             "gender": "M", "rating": 4.0})
            self.assertEqual(len(storage.list_clubs(conn)), 1)
            self.assertEqual(storage.list_venues(conn, cid)[0]["price_per_hour"],
                             250000)
            self.assertEqual(storage.list_players(conn, cid)[0]["name"], "Andi")

            storage.delete_player(conn, pid)
            self.assertEqual(len(storage.list_players(conn, cid)), 0)
            self.assertEqual(len(storage.list_players(conn, cid, True)), 1)
            self.assertTrue(vid)

    def test_bulk_players_upsert_by_name(self):
        with storage.session(self.db) as conn:
            cid = storage.ensure_default_club(conn)
            storage.bulk_save_players(conn, cid, [{"name": "Budi", "rating": 3}])
            storage.bulk_save_players(conn, cid, [{"name": "Budi", "rating": 4.5}])
            rows = storage.list_players(conn, cid)
            self.assertEqual(len(rows), 1, "nama sama tidak boleh dobel")
            self.assertEqual(rows[0]["rating"], 4.5)

    def test_event_roundtrip_and_participants(self):
        sch = make_schedule(n=12, courts=2)
        data = to_dict(sch)
        request = {"title": "Meet A", "event_date": "2026-08-15",
                   "economics": {"court_price_per_hour": 250000,
                                 "fee_per_player": 85000, "other_costs": 0}}
        with storage.session(self.db) as conn:
            request["club_id"] = storage.ensure_default_club(conn)
            eid = storage.save_event(conn, request, data)
            got = storage.get_event(conn, eid)
            self.assertEqual(got["title"], "Meet A")
            self.assertEqual(len(got["schedule"]["rounds"]), len(sch.rounds))
            self.assertEqual(got["revenue"], 12 * 85000)

            stats = storage.player_stats(conn, request["club_id"])
            self.assertEqual(len(stats), 12)
            for s in stats:
                self.assertEqual(s["rounds_played"] + s["rounds_rested"],
                                 len(sch.rounds))

            summary = storage.club_summary(conn, request["club_id"])
            self.assertEqual(summary["events"], 1)
            self.assertEqual(summary["attendances"], 12)

    def test_updating_event_does_not_duplicate(self):
        sch = make_schedule(n=8, courts=2)
        data = to_dict(sch)
        with storage.session(self.db) as conn:
            cid = storage.ensure_default_club(conn)
            req = {"title": "A", "club_id": cid}
            eid = storage.save_event(conn, req, data)
            storage.save_event(conn, {**req, "title": "B"}, data, event_id=eid)
            events = storage.list_events(conn)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["title"], "B")
            n = conn.execute(
                "SELECT COUNT(*) c FROM event_participants WHERE event_id=?",
                (eid,)).fetchone()["c"]
            self.assertEqual(n, 8, "peserta terduplikasi saat update")


if __name__ == "__main__":
    unittest.main(verbosity=2)
