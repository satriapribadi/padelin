"""Uji modul pendukung: pembagian tugas, ekonomi, ekspor, dan database."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from padel_scheduler import (
    Config,
    Economics,
    Player,
    Segment,
    build_schedule,
    storage,
)
from padel_scheduler.economics import compare, fee_for_target_margin, upgrade_analysis
from padel_scheduler.html_report import build_html
from padel_scheduler.report import (
    batas_keunikan,
    kolam_partner,
    to_csv,
    to_dict,
    to_personal_text,
    to_text,
)
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

    def test_duty_split_is_even_per_role(self):
        """Tugas harus rata PER PERAN, bukan cuma totalnya.

        Dilaporkan host: jumlah main dan duduk sudah sama, tapi ada yang jadi
        wasit 1 kali dan ada yang 3 kali. Pembagian greedy per ronde menyeimbang-
        kan total tapi membiarkan komposisi peran timpang.
        """
        from collections import Counter

        cases = [(8, 1, 12), (12, 2, 12), (10, 2, 10), (26, 4, 11), (14, 3, 12)]
        for n, courts, rounds in cases:
            players = [Player(id=i, name=f"P{i}") for i in range(n)]
            cfg = Config(courts=courts, duration_minutes=120, round_minutes=10,
                         warmup_minutes=0, effort=8000, rounds_override=rounds,
                         referees_per_court=1, ballboys_per_court=1)
            sch = build_schedule(players, cfg)

            w, b, tot = Counter(), Counter(), Counter()
            for rnd in sch.rounds:
                for r in rnd.roles:
                    (w if r.role == "wasit" else b)[r.player_id] += 1
                    tot[r.player_id] += 1
            ids = [p.id for p in sch.players]
            for label, c in (("wasit", w), ("ballboy", b), ("total", tot)):
                vals = [c[i] for i in ids]
                spread = max(vals) - min(vals)
                self.assertLessEqual(
                    spread, 1,
                    f"{n}p/{courts}c/{rounds}r {label} timpang: "
                    f"{min(vals)}..{max(vals)}")
                # Kalau habis dibagi rata, selisih 1 pun tidak boleh ada.
                if sum(vals) % n == 0:
                    self.assertEqual(
                        spread, 0,
                        f"{n}p/{courts}c/{rounds}r {label} habis dibagi tapi "
                        f"{sorted(vals)}")

    def test_every_court_gets_its_full_duty_set(self):
        """Tiap court harus dapat satu wasit DAN satu ballboy sendiri.

        Pass perataan sempat menukar peran sambil mempertahankan court, sehingga
        satu court bisa berakhir punya dua ballboy dan tanpa wasit sama sekali.
        Yang benar: yang ditukar adalah slot tugas utuh (peran + court).
        """
        from collections import Counter

        for n, courts, rounds in ((26, 4, 11), (12, 2, 12), (8, 1, 12),
                                  (10, 2, 10), (24, 5, 11)):
            players = [Player(id=i, name=f"P{i}") for i in range(n)]
            cfg = Config(courts=courts, duration_minutes=120, round_minutes=10,
                         warmup_minutes=0, effort=6000, rounds_override=rounds,
                         referees_per_court=1, ballboys_per_court=1)
            sch = build_schedule(players, cfg)

            for rnd in sch.rounds:
                per_court = Counter()
                for r in rnd.roles:
                    per_court[(r.court, r.role)] += 1
                    self.assertLessEqual(
                        per_court[(r.court, r.role)], 1,
                        f"{n}p/{courts}c ronde {rnd.index}: court {r.court} "
                        f"punya lebih dari satu {r.role}")

                # Kalau yang istirahat cukup, tiap court wajib lengkap.
                if len(rnd.byes) >= len(rnd.matches) * 2:
                    for m in rnd.matches:
                        for role in ("wasit", "ballboy"):
                            self.assertEqual(
                                per_court[(m.court, role)], 1,
                                f"{n}p/{courts}c ronde {rnd.index}: court "
                                f"{m.court} tanpa {role}")

    def test_referees_filled_before_ballboys_when_short(self):
        """Kalau yang istirahat kurang, wasit didahulukan - court tanpa wasit
        jauh lebih mengganggu daripada court tanpa ballboy."""
        # 20 pemain / 4 court -> 4 istirahat untuk 8 tugas.
        players = [Player(id=i, name=f"P{i}") for i in range(20)]
        cfg = Config(courts=4, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, effort=6000, rounds_override=10,
                     referees_per_court=1, ballboys_per_court=1)
        sch = build_schedule(players, cfg)
        for rnd in sch.rounds:
            refs = sum(1 for r in rnd.roles if r.role == "wasit")
            self.assertEqual(refs, len(rnd.matches),
                             f"ronde {rnd.index}: ada court tanpa wasit padahal "
                             f"orang yang istirahat cukup untuk semua wasit")

    def test_duty_never_double_booked(self):
        sch = make_schedule(n=26, courts=4, refs=1, balls=1)
        for rnd in sch.rounds:
            ids = [r.player_id for r in rnd.roles]
            self.assertEqual(len(ids), len(set(ids)),
                             f"ronde {rnd.index}: satu orang dua tugas")
            for r in rnd.roles:
                self.assertIn(r.player_id, rnd.byes,
                              f"ronde {rnd.index}: petugas sedang main")

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

    def test_break_even_fee_never_leaves_host_short(self):
        """Titik impas adalah AMBANG, jadi pembulatannya harus ke atas.

        Dulu dibulatkan ke terdekat, dan host yang menagih persis angka itu bisa
        nombok: biaya 306.593 dibagi 8 orang = 38.324,125, dibulatkan jadi
        38.324, pemasukan kurang Rp 1 - lalu panel biayanya sendiri menandai
        acara itu "bermasalah". Kelebihannya dibatasi: pembulatan ke rupiah
        terdekat ke atas tidak boleh menambah lebih dari Rp 1 per peserta.
        """
        for n, price, other in ((26, 250000, 100000), (8, 150000, 6593),
                                (7, 175000, 33333), (13, 120000, 1)):
            econ = Economics(court_price_per_hour=price, fee_per_player=0,
                             other_costs=other)
            fee = compare(n, econ, court_options=[4], hour_options=[2.0])[0].break_even_fee
            o2 = compare(n, Economics(price, fee, other),
                         court_options=[4], hour_options=[2.0])[0]
            self.assertGreaterEqual(
                o2.profit, 0,
                f"{n} peserta: menuruti titik impas justru rugi {o2.profit}")
            self.assertLess(
                o2.profit, n,
                f"{n} peserta: titik impas kelebihan {o2.profit}, "
                f"lebih dari Rp 1 per peserta")

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

    def test_html_carries_branding_and_limits(self):
        """Branding pernah hilang diam-diam dari footer saat rebrand.

        Juga dijaga: kartu pengulangan harus menyebut batas matematisnya, kalau
        tidak angkanya terbaca seperti cacat jadwal oleh peserta.
        """
        # 8 pemain, 9 ronde -> lawan unik mustahil di atas 3 ronde.
        players = [Player(id=i, name=f"P{i}", gender="F") for i in range(8)]
        sch = build_schedule(players, Config(courts=1, duration_minutes=120))
        h = build_html(sch, title="Uji")

        self.assertIn("Padelin", h, "branding hilang dari laporan")
        self.assertNotIn("generator jadwal padel", h, "teks branding lama tersisa")
        self.assertIn("batas matematis 3 ronde", h,
                      "kartu lawan berulang tidak menyebut batasnya")

    def test_html_omits_limit_note_when_not_forced(self):
        # 26 pemain, 4 court -> tiap orang main sedikit, keunikan tercapai.
        players = [Player(id=i, name=f"P{i}") for i in range(26)]
        sch = build_schedule(players, Config(courts=4, duration_minutes=120,
                                             effort=4000))
        h = build_html(sch)
        self.assertNotIn("batas matematis", h,
                         "batas disebut padahal tidak ada pengulangan paksa")

    def test_html_batas_dihitung_dari_kolam_babaknya(self):
        """Batas keunikan harus dari kolam yang benar-benar dihadapi.

        8 putra + 8 putri dengan babak putra lalu putri: tiap putra cuma pernah
        berhadapan dengan 7 putra lain, jadi 6 ronde main sudah jauh melewati
        batas 3 ronde dan pengulangannya wajib terjadi. Hitungan lama memakai 16
        peserta, menyimpulkan batasnya 7 ronde, lalu DIAM - persis di kasus yang
        paling butuh penjelasan, dan angka "15 pasang" tanpa konteks terbaca
        sebagai cacat jadwal oleh peserta yang membaca laporannya.
        """
        players = [Player(id=i, name=f"P{i}",
                          gender=("M" if i < 8 else "F")) for i in range(16)]
        cfg = Config(courts=2, duration_minutes=120, round_minutes=8,
                     warmup_minutes=0, effort=4000, attempts=1,
                     segments=[Segment("Putra", 6, "men"),
                               Segment("Putri", 6, "women")])
        sch = build_schedule(players, cfg)
        h = build_html(sch, title="Uji")

        self.assertGreater(sch.stats.opponent_repeat_pairs, 0,
                           "prasyarat: pengulangan lawan memang terjadi")
        self.assertIn("batas matematis 3 ronde", h,
                      "batas dihitung dari seluruh peserta, bukan dari kolam "
                      "babaknya")
        self.assertIn("di babak", h, "babak yang mengikat tidak disebut")

    def test_html_tidak_menyebut_babak_di_meet_biasa(self):
        """Meet tanpa babak: tidak ada kolam terpisah, jadi jangan sebut babak."""
        players = [Player(id=i, name=f"P{i}", gender="F") for i in range(8)]
        sch = build_schedule(players, Config(courts=1, duration_minutes=120))
        h = build_html(sch, title="Uji")
        self.assertIn("batas matematis 3 ronde", h)
        self.assertNotIn("di babak", h,
                         "menyebut babak padahal meetnya tidak berbabak")

    def test_html_fee_per_menit_jadi_rentang_kalau_main_beda(self):
        """Satu angka menyesatkan kalau jatah mainnya terbelah.

        20 putra + 4 putri: para putra main 3 ronde dan para putri 10, tapi
        fee-nya sama. min(plays) saja memberi harga per menit versi putra -
        3,3 kali lipat dari yang sebenarnya dibayar para putri.
        """
        players = [Player(id=i, name=f"P{i}",
                          gender=("M" if i < 20 else "F")) for i in range(24)]
        cfg = Config(courts=2, duration_minutes=120, round_minutes=8,
                     warmup_minutes=0, effort=4000, attempts=1,
                     segments=[Segment("Putra", 5, "men"),
                               Segment("Putri", 5, "women"),
                               Segment("Mixed", 5, "mixed")])
        sch = build_schedule(players, cfg)
        main = sch.stats.plays_per_player
        self.assertGreater(max(main.values()) - min(main.values()), 1,
                           "prasyarat: jatah mainnya memang terbelah")
        h = build_html(sch, title="Uji", fee=75_000)
        self.assertRegex(
            h, r"Rp [\d.]+-[\d.]+ / menit main",
            "fee per menit masih satu angka padahal jatah mainnya terbelah")

    def test_teks_share_menyebut_batas_yang_sama_dengan_laporan(self):
        """Teks WhatsApp dan laporan cetak harus menjelaskan angka yang sama.

        Teks inilah yang ditempel ke grup dan dibaca semua peserta, jadi justru
        di sini angka pengulangan telanjang paling mudah terbaca sebagai
        kegagalan jadwal. Dulu ia mencetak "Lawan berulang: 15 pasang" tanpa
        sepatah kata pun, sementara laporan cetak untuk jadwal yang sama sudah
        menjelaskan batasnya.
        """
        players = [Player(id=i, name=f"P{i}",
                          gender=("M" if i < 8 else "F")) for i in range(16)]
        cfg = Config(courts=2, duration_minutes=120, round_minutes=8,
                     warmup_minutes=0, effort=4000, attempts=1,
                     segments=[Segment("Putra", 6, "men"),
                               Segment("Putri", 6, "women")])
        sch = build_schedule(players, cfg)
        teks = to_text(sch)

        self.assertGreater(sch.stats.opponent_repeat_pairs, 0,
                           "prasyarat: pengulangan lawan memang terjadi")
        self.assertIn("tak terhindarkan", teks,
                      "angka pengulangan dicetak telanjang di teks share")
        self.assertIn("3 ronde main di babak Putra", teks,
                      "batas atau babak pengikatnya tidak disebut")

    def test_teks_share_diam_kalau_keunikan_masih_mungkin(self):
        """Jangan menempelkan "tak terhindarkan" ke jadwal yang memang bersih."""
        players = [Player(id=i, name=f"P{i}") for i in range(26)]
        sch = build_schedule(players, Config(courts=4, duration_minutes=120,
                                             effort=4000))
        teks = to_text(sch)
        self.assertNotIn("tak terhindarkan", teks)

    def test_kolam_partner_dipotong_oleh_format(self):
        """Format yang dibatasi memotong kolam partner, bukan cuma kolam lawan.

        Kasus nyata host: 5 putra + 3 putri dengan "putra vs putra" dan "campur
        vs campur" saja. Tim dua putri tidak pernah sah - PP-PP tidak diizinkan
        dan di LL-LL tidak ada putri - jadi tiap putri hanya bisa berpasangan
        dengan putra, dan calonnya cuma lima. Dengan main 6 ronde, tiga pasang
        berulang di jadwal itu persis batas bawahnya.

        Dihitung dari jumlah peserta, batasnya terbaca 7 dan angka 3 itu lewat
        tanpa penjelasan - terbaca sebagai kelalaian penjadwal, padahal optimal.
        """
        roster = [(3.0, "F"), (2.0, "M"), (2.0, "F"), (4.0, "M"),
                  (3.0, "M"), (2.0, "F"), (3.0, "M"), (2.0, "M")]
        players = [Player(id=i + 1, name=f"P{i+1}", rating=r, gender=g)
                   for i, (r, g) in enumerate(roster)]
        cfg = Config(courts=1, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, effort=4000, attempts=1,
                     allowed_matchups=["LL-LL", "LP-LP"])
        sch = build_schedule(players, cfg)

        kolam = kolam_partner(sch.players, sch.config.allowed_matchups)
        putri = [p.id for p in players if p.gender == "F"]
        putra = [p.id for p in players if p.gender == "M"]
        self.assertTrue(all(kolam[p] == len(putra) for p in putri),
                        f"kolam partner putri bukan sejumlah putra: {kolam}")
        self.assertTrue(all(kolam[p] == len(players) - 1 for p in putra),
                        f"kolam partner putra ikut terpotong: {kolam}")

        b = batas_keunikan(sch)["partner"]
        self.assertIsNotNone(b, "batas partner tidak terdeteksi")
        self.assertEqual(b["batas"], len(putra))
        self.assertEqual(b["kelompok"], "putri")
        self.assertIn("bagi peserta putri", to_text(sch))

    def test_batas_keunikan_diam_alih_alih_menebak(self):
        """Diamnya bukan klaim "bisa dihindari" - lihat docstring-nya.

        Babak "sesama gender" memuat kedua gender di kolam yang sama padahal
        seorang putra tidak pernah berhadapan dengan putri di situ, jadi kolam
        sebenarnya lebih kecil daripada yang terbaca dari jadwal. Yang dijaga di
        sini: fungsi itu tidak boleh MENGAKU tahu batasnya untuk kasus yang
        tidak bisa ia buktikan.
        """
        players = [Player(id=i, name=f"P{i}",
                          gender=("M" if i < 8 else "F")) for i in range(16)]
        cfg = Config(courts=2, duration_minutes=120, round_minutes=8,
                     warmup_minutes=0, effort=4000, attempts=1,
                     segments=[Segment("Sesama gender", 6, "same_gender"),
                               Segment("Mixed", 6, "mixed")],
                     interleave_segments=True)
        sch = build_schedule(players, cfg)
        b = batas_keunikan(sch)
        # Kolam terbacanya 16 orang dan tiap orang main 3 ronde per babak, jadi
        # tidak ada yang bisa dibuktikan tak terhindarkan dari angka itu saja.
        self.assertIsNone(b["lawan"])
        self.assertIsNone(b["partner"])

    def test_kartu_cetak_tidak_terpecah_beda_dari_layar(self):
        """Cetakan dan layar harus menyusun kartu yang sama dengan cara sama.

        minmax(94px) disetel untuk TUJUH kartu. Begitu wasit/ballboy aktif
        kartunya jadi delapan, dan 8x94 + 7x5 = 787px tidak muat di isi A4 yang
        cuma 703px - kartu terakhir jatuh sendirian ke baris kedua. Diukur pada
        laporan yang sama: layar 900px memberi satu baris berisi delapan, cetak
        memberi 7+1. Host melihat yang satu, peserta memegang yang lain.

        Delapan kartu dipecah jadi dua baris berimbang, bukan dipaksa sebaris:
        sebaris memberi 83px per kartu sehingga labelnya membungkus tiga baris
        dan "Rp 24.750" yang butuh 75px tidak muat di 66px isi. Empat kolom
        memberi 172px - semua label dan nilai muat satu baris, dan tidak ada
        kartu yatim.

        Yang dijaga di sini jumlah KOLOM yang dikirim ke CSS, karena itu yang
        menentukan susunannya - tata letak sebenarnya diperiksa dengan
        merender, bukan oleh uji ini.
        """
        players = [Player(id=i, name=f"P{i}") for i in range(8)]
        cfg = Config(courts=1, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, effort=4000, attempts=1,
                     referees_per_court=1, ballboys_per_court=1)
        sch = build_schedule(players, cfg)

        # Dihitung per pembuka div, bukan dengan prefiks "class='tile": itu ikut
        # mencocoki kontainer <div class='tiles'> dan hasilnya kelebihan satu.
        def n_kartu(html):
            return html.count("<div class='tile'>")

        h = build_html(sch, title="Uji", fee=24_750)
        self.assertEqual(n_kartu(h), 8,
                         "jumlah kartu berubah - ambang di uji ini ikut basi")
        self.assertIn("--n:4", h, "delapan kartu tidak dipecah jadi 4+4")

        # Tujuh kartu masih muat sebaris dengan 96px per kartu - itu lebar
        # rancangan aslinya, jadi jangan dipecah tanpa sebab.
        tanpa = build_html(sch, title="Uji")
        self.assertEqual(n_kartu(tanpa), 7)
        self.assertIn("--n:7", tanpa)

    def test_html_shows_fee_per_player(self):
        """Fee itu hal pertama yang dicari peserta saat laporan dibagikan."""
        players = [Player(id=i, name=f"P{i}") for i in range(8)]
        cfg = Config(courts=1, duration_minutes=120, round_minutes=10,
                     warmup_minutes=0, effort=4000, rounds_override=12)
        sch = build_schedule(players, cfg)

        h = build_html(sch, fee=85000)
        self.assertIn("Fee per peserta", h)
        self.assertIn("Rp 85.000", h, "pemisah ribuan harus titik")
        # Keterangannya harga per menit main - peserta menilai harga dari waktu
        # di lapangan, bukan dari lama acara.
        self.assertIn("/ menit main", h)

        # Tanpa fee, kartunya tidak boleh muncul sama sekali.
        self.assertNotIn("Fee per peserta", build_html(sch, fee=0))

    def test_html_escapes_player_names(self):
        players = [Player(id=i, name=f"<script>{i}</script>") for i in range(8)]
        sch = build_schedule(players, Config(courts=2, duration_minutes=60))
        h = build_html(sch)
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)

    def test_html_never_falls_back_to_one_column(self):
        """Banyak court tidak boleh membuat card ronde selebar halaman.

        Card selebar A4 membuat kolom tim (1fr) melar sampai kedua nama
        terlempar ke tepi kiri dan kanan dengan "vs" terdampar di tengah, dan
        tiap match memakan satu baris penuh - persis kebalikan dari padat.
        Jumlah kolom mengikuti lebar yang DIBUTUHKAN isi card, bukan banyaknya
        match: card 4 match tidak perlu lebih lebar, hanya lebih tinggi.
        """
        for courts, n in ((1, 8), (2, 12), (3, 18), (4, 24), (6, 26)):
            with self.subTest(courts=courts):
                players = [Player(id=i, name=f"P{i}") for i in range(n)]
                sch = build_schedule(players, Config(courts=courts,
                                                     duration_minutes=120,
                                                     effort=2000))
                h = build_html(sch)
                self.assertNotIn("rounds cols-1", h,
                                 f"{courts} court jatuh ke satu kolom")
                self.assertIn("rounds cols-3" if courts == 1 else "rounds cols-2",
                              h)

    def test_html_colours_names_by_gender(self):
        """Warna nama harus punya padanan huruf L/P, bukan warna saja.

        Laporan sering dicetak hitam-putih. Kalau gender hanya disampaikan
        lewat warna, salinan cetaknya kehilangan informasi tanpa memberi tahu.
        """
        players = [Player(id=i, name=f"P{i}", gender="M" if i < 4 else "F")
                   for i in range(8)]
        sch = build_schedule(players, Config(courts=2, duration_minutes=60))
        h = build_html(sch)

        self.assertIn("class='g-m'", h, "nama laki-laki tidak diwarnai")
        self.assertIn("class='g-f'", h, "nama perempuan tidak diwarnai")
        self.assertIn("class='gp m'>L<", h, "huruf L hilang dari rekap")
        self.assertIn("class='gp f'>P<", h, "huruf P hilang dari rekap")
        # Dicari markupnya, bukan sekadar kata "gkey" - aturan CSS-nya selalu
        # ikut terbit, jadi mencari namanya saja lolos tanpa legenda apa pun.
        self.assertIn("<div class='gkey'>", h, "legenda warna tidak muncul")

        # Roster tanpa gender: tidak ada warna, dan legendanya ikut hilang -
        # menjelaskan warna yang tidak ada di mana pun cuma bikin bingung.
        polos = [Player(id=i, name=f"P{i}") for i in range(8)]
        h2 = build_html(build_schedule(polos, Config(courts=2,
                                                     duration_minutes=60)))
        self.assertNotIn("<div class='gkey'>", h2)
        self.assertNotIn("class='g-m'", h2)

    def test_html_round_timeline_matches_recap_numbers(self):
        """Susunan per ronde harus menceritakan hal yang sama dengan rekap.

        Tabel ini gampang menyimpang diam-diam: kalau peran tugas ditulis
        sebelum peran main, orang yang main jadi tertimpa "W" dan barisnya
        tetap terlihat masuk akal - hanya jumlahnya yang tidak lagi cocok
        dengan angka di rekap.
        """
        sch = make_schedule(n=26, courts=4)
        h = build_html(sch)
        st = sch.stats

        # Dicari captionnya, bukan kalimatnya: namanya juga muncul di komentar
        # CSS, jadi mencari teks bebas lolos tanpa satu baris tabel pun.
        marker = "<caption>Susunan per ronde"
        self.assertIn(marker, h, "tabel per ronde tidak terbit")

        # Ambil baris tabelnya saja, lalu hitung hurufnya per pemain.
        body = h.split(marker, 1)[1].split("<tbody>", 1)[1].split("</table>", 1)[0]
        rows = re.findall(r"<th class='nm'>(.*?)</th>(.*?)</tr>", body, re.S)
        self.assertEqual(len(rows), len(sch.players),
                         "ada peserta yang tidak dapat baris")

        by_name = {p.name: p.id for p in sch.players}
        for raw_name, cells in rows:
            name = re.sub(r"<[^>]+>", "", raw_name)
            pid = by_name[name]
            letters = re.findall(r"<b class='(\w)'>", cells)
            self.assertEqual(len(letters), len(sch.rounds),
                             f"{name}: sel tidak satu per ronde")

            roles = st.roles_per_player.get(pid, {})
            idle = st.byes_per_player.get(pid, 0) - int(roles.get("total", 0) or 0)
            self.assertEqual(letters.count("m"), st.plays_per_player.get(pid, 0),
                             f"{name}: jumlah M tidak sama dengan kolom Main")
            self.assertEqual(letters.count("w"), roles.get("wasit", 0),
                             f"{name}: jumlah W tidak sama dengan kolom Wasit")
            self.assertEqual(letters.count("b"), roles.get("ballboy", 0),
                             f"{name}: jumlah B tidak sama dengan kolom Ballboy")
            self.assertEqual(letters.count("r"), max(0, idle),
                             f"{name}: jumlah R tidak sama dengan kolom Istirahat")


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

    def test_readd_after_delete_revives_row(self):
        """Hapus lalu tambah lagi dengan nama sama harus menghidupkan barisnya.

        Penghapusan bersifat soft agar riwayat acara tetap utuh, tapi baris itu
        masih memegang UNIQUE(nama). Tanpa upsert, menambahkan kembali anggota
        yang pernah keluar gagal dengan error internal - bukan skenario langka:
        peserta keluar lalu bergabung lagi itu hal biasa di klub.
        """
        with storage.session(self.db) as conn:
            cid = storage.ensure_default_club(conn)

            for label, save, delete, lister, key in (
                ("venue", storage.save_venue, storage.delete_venue,
                 storage.list_venues, "court_count"),
                ("player", storage.save_player, storage.delete_player,
                 storage.list_players, "rating"),
            ):
                first = save(conn, {"club_id": cid, "name": f"Sama {label}",
                                    "court_count": 2, "rating": 2.0})
                delete(conn, first)
                self.assertEqual(len(lister(conn, cid)), 0, label)

                again = save(conn, {"club_id": cid, "name": f"Sama {label}",
                                    "court_count": 5, "rating": 4.5})
                rows = lister(conn, cid)
                self.assertEqual(len(rows), 1, f"{label} terduplikasi")
                self.assertEqual(again, first, f"{label} tidak memakai baris lama")
                self.assertEqual(
                    rows[0][key], 5 if key == "court_count" else 4.5,
                    f"{label}: nilai baru tidak tersimpan")

    def test_readd_club_after_delete(self):
        with storage.session(self.db) as conn:
            cid = storage.save_club(conn, {"name": "Klub Balik", "city": "A"})
            storage.delete_club(conn, cid)
            self.assertEqual(len(storage.list_clubs(conn)), 0)
            again = storage.save_club(conn, {"name": "Klub Balik", "city": "B"})
            rows = storage.list_clubs(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(again, cid)
            self.assertEqual(rows[0]["city"], "B")

    def test_duplicate_names_merge_regardless_of_case(self):
        """"Nisa" dan "NISA" itu orang yang sama.

        UNIQUE di SQLite membandingkan teks persis, jadi beda kapital lolos
        sebagai dua anggota berbeda - sangat mungkin terjadi kalau host mengetik
        nama yang sama di waktu berbeda.
        """
        with storage.session(self.db) as conn:
            cid = storage.ensure_default_club(conn)
            for names in (["Aci", "Aci"], ["Ebbie", " Ebbie "],
                          ["Nisa", "NISA"], ["Orin", "orin"]):
                before = len(storage.list_players(conn, cid))
                storage.bulk_save_players(
                    conn, cid, [{"name": n, "rating": 3} for n in names])
                added = len(storage.list_players(conn, cid)) - before
                self.assertEqual(added, 1, f"{names} jadi {added} baris")

    def test_case_variant_updates_without_renaming(self):
        """Menyimpan variasi kapital memperbarui datanya, bukan mengganti nama."""
        with storage.session(self.db) as conn:
            cid = storage.ensure_default_club(conn)
            pid = storage.save_player(conn, {"club_id": cid, "name": "Aci",
                                             "rating": 3})
            again = storage.save_player(conn, {"club_id": cid, "name": "aCi",
                                               "rating": 5})
            rows = storage.list_players(conn, cid)
            self.assertEqual(again, pid, "harusnya memakai baris yang sama")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "Aci", "ejaan lama tidak boleh berubah")
            self.assertEqual(rows[0]["rating"], 5.0)

            # Ganti nama lewat id eksplisit tetap boleh - itu memang disengaja.
            storage.save_player(conn, {"id": pid, "club_id": cid,
                                       "name": "Aci Baru", "rating": 4})
            self.assertEqual(storage.list_players(conn, cid)[0]["name"], "Aci Baru")

    def test_venue_and_club_duplicates_also_merge(self):
        with storage.session(self.db) as conn:
            cid = storage.ensure_default_club(conn)
            a = storage.save_venue(conn, {"club_id": cid, "name": "Arena Utama",
                                          "court_count": 4})
            b = storage.save_venue(conn, {"club_id": cid, "name": "ARENA UTAMA",
                                          "court_count": 6})
            self.assertEqual(a, b)
            self.assertEqual(len(storage.list_venues(conn, cid)), 1)
            self.assertEqual(storage.list_venues(conn, cid)[0]["court_count"], 6)

            c1 = storage.save_club(conn, {"name": "Vinotek"})
            c2 = storage.save_club(conn, {"name": "vinotek", "city": "Jakarta"})
            self.assertEqual(c1, c2)

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
