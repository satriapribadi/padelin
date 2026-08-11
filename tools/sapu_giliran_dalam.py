#!/usr/bin/env python3
"""Sapuan giliran MENDALAM: roster lain, pada effort yang benar-benar dipakai host.

Kenapa sapuan ini ada di samping tools/sapu_giliran.py, bukan menggantikannya.
Sapuan itu menjalankan 324 kasus pada effort=8000/attempts=1 supaya selesai dalam
hitungan menit - pilihan yang benar untuk regresi lebar. Tapi cacat giliran yang
dilaporkan host muncul pada effort 160.000/attempts=3, dan analisisnya sendiri
menyimpulkan effort yang LEBIH TINGGI bisa memperburuk giliran. Jadi rezim tempat
bug itu hidup justru yang tidak pernah tersapu, dan satu-satunya roster yang
pernah diperiksa lintas effort adalah roster host.

Dua sumbu yang ditambahkan di sini:

  1. EFFORT produksi (30k/60k/160k) dengan attempts=3, bukan 8000/1.
  2. URUTAN GENDER dalam roster. Ini bukan detail kosmetik - commit cdf26ac
     sendiri menemukan uji kasus host menguji instance yang salah karena
     memakai "6 putra dulu, lalu 4 putri" padahal roster host menyelang-nyeling.
     Urutan menentukan baris 1-faktorisasi yang terbentuk. sapu_giliran.py
     membangun rosternya blok (semua putra dulu), jadi seluruh 324 kasus itu
     satu keluarga instance saja.

Yang diukur, dan kenapa bukan cuma serobotan mentah:

  serobot       berapa kali seseorang turun untuk kali ke-(k+1) padahal ada
                yang duduk dan baru main < k kali. Sama persis dengan definisi
                ScheduleStats.turn_skips.
  defisit_max   selisih TERBESAR antara jumlah main terbanyak dan tersedikit,
                diukur di akhir tiap ronde. Ini yang benar-benar dirasakan:
                keluhan host "satu peserta main di ronde 1 dan 3 sementara yang
                lain baru turun di ronde 4" adalah defisit 2. Serobotan mentah
                tidak bisa memisahkan itu dari meet berokupansi tinggi, tempat
                hampir semua orang turun tiap ronde sehingga angkanya selalu
                besar tanpa ada yang dirugikan. defisit_max bisa.
  serobot_elig  serobotan yang menghormati round_eligible. Untuk meet biasa ia
                sama dengan serobot; untuk meet bersegmen tidak, dan selisihnya
                adalah selisih antara yang dioptimasi optimizer dan yang
                dilaporkan ke host.

Jalankan:

    python3 -u tools/sapu_giliran_dalam.py            # semua blok
    python3 -u tools/sapu_giliran_dalam.py --blok A   # satu blok saja
    python3 -u tools/sapu_giliran_dalam.py --analisa /tmp/hasil.txt

Baris HASIL berformat JSON supaya --analisa bisa membacanya kembali.
"""
from __future__ import annotations

import itertools
import json
import math
import multiprocessing as mp
import os
import random
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMA = ["LL-LL", "LP-LP", "PP-PP"]
CAMPUR = ["LP-LP"]

DURASI = 120
RONDE_MENIT = 8
URUTAN = ["blok", "selang", "acak"]

# --- Blok A: tetangga kasus host. 10 orang, 1 court - persis bentuk yang
# dilaporkan, tapi komposisi gender dan urutannya digeser. Kalau perbaikannya
# memang menangkap sifat jadwalnya dan bukan satu titik yang beruntung, seluruh
# keluarga ini harus ikut rapi.
A_ROSTER = [(6, 4), (5, 5), (4, 6), (7, 3), (8, 2), (10, 0)]
A_COURT = [1]
A_EFFORT = [30_000, 60_000, 160_000]
A_SEED = [42, 7, 2024]
A_FORMAT = [SAMA]

# --- Blok B: roster lain yang lebih besar, dua effort ekstrem saja. Yang dicari
# di sini bukan nilai mutlaknya melainkan ARAHNYA: apakah menaikkan effort masih
# bisa memperburuk giliran pada roster selain roster host.
B_KASUS = [
    ((10, 6), 2), ((14, 6), 3), ((16, 10), 4), ((12, 8), 2),
    ((9, 3), 1), ((20, 6), 4), ((5, 3), 1), ((11, 1), 2),
    ((13, 7), 2), ((7, 7), 2),
]
B_EFFORT = [30_000, 160_000]
B_SEED = [42, 7]
B_FORMAT = [SAMA]

# --- Blok C: format campur saja (LP-LP). Jalur pemilihan komposisi tim yang
# berbeda, dan roster yang gendernya timpang jadi terpaksa mendudukkan kelebihan
# gender itu - persis tempat giliran paling gampang jadi tidak adil.
C_KASUS = [
    ((5, 5), 1), ((6, 4), 1), ((8, 4), 2), ((6, 6), 2), ((7, 5), 1),
    ((10, 6), 2),
]
C_EFFORT = [30_000, 160_000]
C_SEED = [42, 7]
C_FORMAT = [CAMPUR]


def lapor(*a) -> None:
    print(*a, flush=True)


def buat_roster(n_l: int, n_p: int, urutan: str):
    """Roster dengan jumlah gender tetap tapi URUTAN berbeda.

    Rating melekat pada POSISI, bukan pada orangnya, supaya satu-satunya yang
    berubah antar urutan adalah letak gendernya. Kalau rating ikut berpindah,
    dua efek bercampur dan tidak ada yang bisa disimpulkan dari selisihnya.
    """
    from padel_scheduler.models import Player

    if urutan == "blok":
        gender = ["M"] * n_l + ["F"] * n_p
    elif urutan == "selang":
        gender = []
        i = j = 0
        while i < n_l or j < n_p:
            if i < n_l:
                gender.append("M")
                i += 1
            if j < n_p:
                gender.append("F")
                j += 1
    elif urutan == "acak":
        gender = ["M"] * n_l + ["F"] * n_p
        random.Random(9901 + n_l * 100 + n_p).shuffle(gender)
    else:
        raise ValueError(f"urutan tidak dikenal: {urutan}")
    return [
        Player(id=i, name=f"P{i+1}", rating=float(2 + i % 4), gender=g)
        for i, g in enumerate(gender)
    ]


def ukur(sch) -> dict:
    """Metrik giliran, dihitung ulang dari isi ronde - bukan dibaca dari stats.

    Membaca stats hanya memeriksa bahwa penjadwal setuju dengan dirinya sendiri.
    Yang harus dijaga jadwalnya. Nilai stats tetap ikut dicatat terpisah supaya
    ketidakcocokan antara keduanya kelihatan, bukan tersembunyi.
    """
    ids = [p.id for p in sch.players]
    n_ronde = len(sch.rounds)
    sudah = {p: 0 for p in ids}
    sejak = {p: 0 for p in ids}
    main = {p: 0 for p in ids}
    pertama: dict[int, int] = {}
    serobot = 0
    tunggu = 0
    defisit_max = 0
    for rnd in sch.rounds:
        turun = {p for m in rnd.matches for p in m.players()}
        if turun:
            duduk = [sudah[p] for p in ids if p not in turun]
            if duduk:
                serobot += sum(1 for p in turun if sudah[p] > min(duduk))
        for p in turun:
            sudah[p] += 1
            main[p] += 1
            pertama.setdefault(p, rnd.index)
        for p in ids:
            if p in turun:
                sejak[p] = rnd.index
            else:
                tunggu = max(tunggu, rnd.index - sejak[p])
        defisit_max = max(defisit_max,
                          max(sudah.values()) - min(sudah.values()))
    batas = max(
        (math.ceil((n_ronde - main[p]) / (main[p] + 1)) if n_ronde > main[p] else 0)
        for p in ids
    )
    nilai = list(main.values())
    return {
        "serobot": serobot,
        "defisit_max": defisit_max,
        "tunggu": tunggu,
        "batas_tunggu": batas,
        "main1": (max(pertama.values()) if len(pertama) == len(ids) else n_ronde),
        "spread_main": max(nilai) - min(nilai),
        "belum_main": sum(1 for p in ids if main[p] == 0),
    }


def satu_kasus(spek: tuple) -> dict:
    """Satu build + pengukurannya. Dijalankan di proses worker.

    Seluruh kegagalan ditangkap dan dikembalikan sebagai data, bukan dilempar:
    satu kasus yang meledak tidak boleh menjatuhkan sapuannya, dan kasus yang
    hilang tanpa jejak lebih buruk daripada kasus yang gagal dengan catatan.
    """
    blok, n_l, n_p, court, urutan, fmt_nama, effort, seed = spek
    kunci = (f"{blok}/{n_l}L{n_p}P/c{court}/{urutan}/{fmt_nama}"
             f"/e{effort}/s{seed}")
    rec = {"kunci": kunci, "blok": blok, "n_l": n_l, "n_p": n_p,
           "court": court, "urutan": urutan, "fmt": fmt_nama,
           "effort": effort, "seed": seed}
    try:
        from padel_scheduler.models import Config
        from padel_scheduler.scheduler import build_schedule

        fmt = {"sama": SAMA, "campur": CAMPUR, "semua": None}[fmt_nama]
        players = buat_roster(n_l, n_p, urutan)
        cfg = Config(courts=court, duration_minutes=DURASI,
                     round_minutes=RONDE_MENIT, warmup_minutes=0,
                     mode="americano", seed=seed, effort=effort, attempts=3,
                     allowed_matchups=fmt)
        t0 = time.time()
        sch = build_schedule(players, cfg)
        detik = time.time() - t0
        rec.update(ukur(sch))
        n = len(players)
        slot = 4 * max(1, min(court, n // 4))
        rec.update(
            ronde=len(sch.rounds),
            putaran=math.ceil(n / slot),
            partner_ulang=sch.stats.partner_repeat_pairs,
            lawan_ulang=sch.stats.opponent_repeat_pairs,
            kualitas=sch.stats.quality_score,
            di_batas=sch.stats.at_theoretical_floor,
            stats_serobot=sch.stats.turn_skips,
            stats_tunggu=sch.stats.longest_wait,
            stats_main1=sch.stats.last_first_play,
            stats_batas=sch.stats.wait_floor,
            detik=round(detik, 2),
        )
    except Exception as exc:
        # Traceback UTUH. Dua kegagalan yang berbeda sering berakhir dengan
        # kalimat penutup yang sama; kalau cuma baris terakhir yang disimpan,
        # keduanya jadi tak terbedakan dan perubahan yang benar-benar
        # berpengaruh bisa ditandai "tidak berpengaruh".
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["traceback"] = traceback.format_exc()
    return rec


def rugi(rec: dict) -> list[str]:
    """Siapa yang benar-benar dirugikan di jadwal ini.

    Serobotan mentah sengaja TIDAK dipakai sebagai tanda kerugian: di meet
    berokupansi tinggi ia selalu besar tanpa ada yang menunggu lebih lama dari
    seharusnya. Yang dipakai hal-hal yang punya batas bawah yang bisa dihitung,
    jadi kelebihannya benar-benar berarti ada yang dirugikan.

    Ambang defisit ada di 3, bukan 2, dan angkanya hasil pengukuran bukan
    tebakan. Roster host apa adanya - kasus yang commit cdf26ac nyatakan sudah
    beres - memberi defisit_max=2 di keempat effort (30k/60k/160k/300k), dengan
    puncaknya di ronde 5 dan 10, jauh setelah semua orang kebagian main. Jadi
    defisit 2 adalah tinggi lantai meet 10-orang-1-court, bukan cacat; memakai
    2 sebagai ambang cuma akan menandai 56 dari 60 kasus pertama dan tidak
    memisahkan apa pun. Yang menangkap keluhan host - "main di ronde 1 dan 3
    sementara yang lain baru turun di ronde 4" - adalah main1 > putaran.
    """
    out = []
    if rec.get("defisit_max", 0) >= 3:
        out.append(f"defisit_max={rec['defisit_max']} (ada yang main 3x lebih "
                   f"banyak dari yang lain di tengah acara)")
    if rec.get("main1", 0) > rec.get("putaran", 0):
        out.append(f"main1={rec['main1']} > putaran={rec['putaran']} "
                   f"(match pertama kesorean)")
    if rec.get("tunggu", 0) > rec.get("batas_tunggu", 0) + 1:
        out.append(f"tunggu={rec['tunggu']} > batas+1={rec['batas_tunggu']+1}")
    if rec.get("spread_main", 0) > 1:
        out.append(f"spread_main={rec['spread_main']}")
    if rec.get("stats_serobot") is not None and \
            rec["stats_serobot"] != rec["serobot"]:
        out.append(f"stats.turn_skips={rec['stats_serobot']} != hitung ulang "
                   f"{rec['serobot']}")
    return out


def daftar_kasus(pilih: str | None) -> list[tuple]:
    kasus = []
    if pilih in (None, "A"):
        for (nl, np_), c, u, f, e, s in itertools.product(
                A_ROSTER, A_COURT, URUTAN, ["sama"], A_EFFORT, A_SEED):
            kasus.append(("A", nl, np_, c, u, f, e, s))
    if pilih in (None, "B"):
        for ((nl, np_), c), u, f, e, s in itertools.product(
                B_KASUS, URUTAN[:2], ["sama"], B_EFFORT, B_SEED):
            kasus.append(("B", nl, np_, c, u, f, e, s))
    if pilih in (None, "C"):
        for ((nl, np_), c), u, f, e, s in itertools.product(
                C_KASUS, URUTAN[:2], ["campur"], C_EFFORT, C_SEED):
            kasus.append(("C", nl, np_, c, u, f, e, s))
    return kasus


def analisa(path: str) -> None:
    """Baca kembali baris HASIL dan cari polanya.

    Dipisah dari sapuannya supaya bisa dijalankan ulang tanpa membangun ulang
    ratusan jadwal - dan supaya sudut pandang analisis bisa ditambah belakangan
    tanpa kehilangan datanya.
    """
    rec = []
    with open(path) as fh:
        for baris in fh:
            if baris.startswith("HASIL "):
                rec.append(json.loads(baris[6:]))
    ok = [r for r in rec if "error" not in r]
    lapor(f"kasus terbaca: {len(rec)} ({len(rec) - len(ok)} error)")
    lapor("")

    lapor("=== 1. Kasus yang ada peserta dirugikan ===")
    n_rugi = 0
    for r in sorted(ok, key=lambda r: (-r.get("defisit_max", 0), r["kunci"])):
        alasan = rugi(r)
        if alasan:
            n_rugi += 1
            lapor(f"  {r['kunci']}: " + "; ".join(alasan)
                  + f"  [serobot={r['serobot']} kualitas={r['kualitas']}]")
    lapor(f"  total {n_rugi} dari {len(ok)} kasus")
    lapor("")

    lapor("=== 2. Effort naik, giliran memburuk (per roster+urutan+seed) ===")
    grup: dict[tuple, list] = {}
    for r in ok:
        k = (r["blok"], r["n_l"], r["n_p"], r["court"], r["urutan"],
             r["fmt"], r["seed"])
        grup.setdefault(k, []).append(r)
    n_buruk = 0
    for k, g in sorted(grup.items()):
        g.sort(key=lambda r: r["effort"])
        if len(g) < 2:
            lapor(f"  [DILEWATI] {k}: cuma {len(g)} titik effort, tidak bisa "
                  f"dibandingkan")
            continue
        jejak = " -> ".join(f"{r['effort']//1000}k:{r['serobot']}" for r in g)
        d_jejak = " -> ".join(f"{r['defisit_max']}" for r in g)
        buruk = (g[-1]["serobot"] > g[0]["serobot"]
                 or g[-1]["defisit_max"] > g[0]["defisit_max"])
        if buruk:
            n_buruk += 1
            lapor(f"  [MEMBURUK] {k[1]}L{k[2]}P/c{k[3]}/{k[4]}/{k[5]}/s{k[6]}: "
                  f"serobot {jejak}  defisit {d_jejak}")
    lapor(f"  total {n_buruk} dari {len(grup)} grup memburuk saat effort naik")
    lapor("")

    lapor("=== 3. Pengaruh urutan gender (roster+court+effort+seed sama) ===")
    pas: dict[tuple, dict] = {}
    for r in ok:
        k = (r["blok"], r["n_l"], r["n_p"], r["court"], r["fmt"], r["effort"],
             r["seed"])
        pas.setdefault(k, {})[r["urutan"]] = r
    beda = []
    for k, d in sorted(pas.items()):
        if len(d) < 2:
            continue
        s = {u: v["serobot"] for u, v in d.items()}
        dm = {u: v["defisit_max"] for u, v in d.items()}
        if max(s.values()) - min(s.values()) >= 4 or \
                max(dm.values()) - min(dm.values()) >= 1:
            beda.append((max(s.values()) - min(s.values()), k, s, dm))
    beda.sort(reverse=True)
    for d, k, s, dm in beda[:25]:
        lapor(f"  {k[1]}L{k[2]}P/c{k[3]}/{k[4]}/e{k[5]//1000}k/s{k[6]}: "
              f"serobot={s} defisit={dm}")
    lapor(f"  total {len(beda)} dari {len(pas)} pasangan berbeda nyata "
          f"(ditampilkan 25 teratas)")
    lapor("")

    lapor("=== 4. Ringkasan per blok ===")
    lapor(f"{'blok':6s} {'n':>4s} {'serobot rata2':>14s} {'defisit maks':>13s} "
          f"{'rugi':>5s} {'detik rata2':>12s} {'detik maks':>11s}")
    for b in sorted({r["blok"] for r in ok}):
        g = [r for r in ok if r["blok"] == b]
        lapor(f"{b:6s} {len(g):4d} "
              f"{sum(r['serobot'] for r in g) / len(g):14.1f} "
              f"{max(r['defisit_max'] for r in g):13d} "
              f"{sum(1 for r in g if rugi(r)):5d} "
              f"{sum(r['detik'] for r in g) / len(g):12.1f} "
              f"{max(r['detik'] for r in g):11.1f}")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--analisa":
        analisa(sys.argv[2])
        return
    pilih = None
    if len(sys.argv) > 2 and sys.argv[1] == "--blok":
        pilih = sys.argv[2]

    kasus = daftar_kasus(pilih)
    n_proc = max(1, min(8, (os.cpu_count() or 4) - 2))
    lapor(f"sapuan giliran mendalam: {len(kasus)} kasus, {n_proc} proses")
    lapor(f"blok A (tetangga kasus host, 10 orang 1 court): "
          f"roster={A_ROSTER} effort={A_EFFORT} seed={A_SEED}")
    lapor(f"blok B (roster lebih besar): {B_KASUS} effort={B_EFFORT} "
          f"seed={B_SEED}")
    lapor(f"blok C (format campur LP-LP): {C_KASUS} effort={C_EFFORT} "
          f"seed={C_SEED}")
    lapor(f"urutan gender: {URUTAN} (blok B dan C cuma blok+selang)")
    lapor(f"attempts=3 (default produksi), durasi={DURASI}m ronde={RONDE_MENIT}m")
    lapor("")
    lapor("CATATAN cakupan - yang TIDAK diuji di sini: mode selain americano, "
          "meet bersegmen, partner terkunci, preferensi court, wasit/ballboy, "
          "rounds_override, dan format 'semua boleh' (pada format itu gender "
          "tidak dilihat sama sekali, jadi urutan gender tidak punya arti - "
          "diverifikasi di kalibrasi: 20L+6P dan 26L+0P memberi jadwal yang "
          "identik). Seed hanya 2-3 per kasus, jadi angka mutlaknya bukan "
          "sebaran melainkan sampel.")
    lapor("")

    mulai = time.time()
    n_error = n_rugi = 0
    with mp.Pool(n_proc) as pool:
        for i, rec in enumerate(pool.imap_unordered(satu_kasus, kasus), 1):
            if "error" in rec:
                n_error += 1
                lapor(f"  [ERROR] {rec['kunci']}")
                for b in rec["traceback"].rstrip().splitlines():
                    lapor(f"      {b}")
                # Traceback dibuang dari baris HASIL supaya JSON-nya tetap satu
                # baris; versi lengkapnya sudah tercetak persis di atas.
                rec = {k: v for k, v in rec.items() if k != "traceback"}
            else:
                alasan = rugi(rec)
                if alasan:
                    n_rugi += 1
                    lapor(f"  [RUGI] {rec['kunci']}: " + "; ".join(alasan))
                    lapor(f"         serobot={rec['serobot']} "
                          f"tunggu={rec['tunggu']}/{rec['batas_tunggu']} "
                          f"main1={rec['main1']}/{rec['putaran']} "
                          f"partner_ulang={rec['partner_ulang']} "
                          f"lawan_ulang={rec['lawan_ulang']} "
                          f"kualitas={rec['kualitas']} {rec['detik']}s")
            lapor("HASIL " + json.dumps(rec, sort_keys=True))
            if i % 20 == 0:
                lapor(f"  ...{i}/{len(kasus)} kasus, {n_rugi} rugi, "
                      f"{n_error} error, {time.time() - mulai:.0f}s berjalan")

    lapor("")
    lapor(f"selesai: {len(kasus)} kasus, {n_rugi} rugi, {n_error} error, "
          f"{time.time() - mulai:.0f}s")


if __name__ == "__main__":
    main()
