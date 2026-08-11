#!/usr/bin/env python3
"""Analisa kelayakan: bisakah 8 pemain (5L/3P) main TEPAT 6x masing-masing
dalam 12 ronde 1 court, dengan format dibatasi [LL-LL, LP-LP, PP-PP]?

Skenario diambil dari INFO DEBUG PADELIN yang dilaporkan host:
  court=1 durasi=120m ronde=10m -> 12 ronde, mode=americano
  peserta=8: P1 r3 M, P2 r2 F, P3 r3 M, P4 r3 F, P5 r4 M,
             P6 r2 F, P7 r2 M, P8 r2 M
  hasil aktual: kualitas 70.3, main_per_orang 4-8

Skor dihitung memakai _build_stats ASLI dari padel_scheduler, bukan salinan
rumus - supaya kesimpulannya tidak bergantung pada tafsir saya atas kode.

Jalankan: python3 -u tools/analisa_merata.py
"""
from __future__ import annotations

import random
import sys
import time
from itertools import combinations

sys.path.insert(0, ".")

from padel_scheduler.models import Player
from padel_scheduler.optimizer import ScheduleState, Weights
from padel_scheduler.scheduler import _build_stats

# ---------------------------------------------------------------- skenario --
RONDE = 12
NAMA = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"]
RATING = [3, 2, 3, 3, 4, 2, 2, 2]
GENDER = ["M", "F", "M", "F", "M", "F", "M", "M"]

PLAYERS = [Player(id=i, name=NAMA[i], rating=float(RATING[i]), gender=GENDER[i])
           for i in range(8)]
PRIA = [i for i in range(8) if GENDER[i] == "M"]      # 0,2,4,6,7
WANITA = [i for i in range(8) if GENDER[i] == "F"]    # 1,3,5

# Jadwal yang benar-benar dihasilkan aplikasi (indeks 0-based dari P1..P8).
JADWAL_AKTUAL = [
    (6, 4, 2, 7), (3, 0, 1, 6), (2, 0, 4, 7), (3, 6, 5, 2),
    (4, 0, 7, 6), (7, 5, 1, 2), (0, 5, 3, 4), (6, 0, 2, 7),
    (4, 1, 3, 2), (2, 6, 7, 0), (5, 4, 7, 1), (6, 0, 4, 2),
]


def lapor(msg: str) -> None:
    print(msg, flush=True)


def sah(quad) -> bool:
    """Format diizinkan hanya LL-LL, LP-LP, PP-PP: komposisi gender kedua tim
    harus IDENTIK. LL-PP (tim putra lawan tim putri) TIDAK sah."""
    a, b, c, d = quad
    return sorted((GENDER[a], GENDER[b])) == sorted((GENDER[c], GENDER[d]))


def sah_semua(jadwal) -> bool:
    return all(sah(q) for q in jadwal)


# ------------------------------------------------------------------ skor ----
def nilai(jadwal: list[tuple[int, int, int, int]]):
    """Bangun ScheduleState dan panggil penilai asli."""
    st = ScheduleState(8, [float(r) for r in RATING], Weights(), len(jadwal))
    for r, (a, b, c, d) in enumerate(jadwal):
        st.matches[r] = [[a, b, c, d]]
        main = {a, b, c, d}
        st.byes[r] = set(range(8)) - main
        for p in st.byes[r]:
            st.bye_count[p] += 1
        st.pc[st._k(a, b)] += 1
        st.pc[st._k(c, d)] += 1
        for x in (a, b):
            for y in (c, d):
                st.oc[st._k(x, y)] += 1
    return _build_stats(st, PLAYERS, len(jadwal))


def rincian(jadwal):
    """Pecah skor jadi 4 komponen, memakai rumus di scheduler.py:993."""
    stx = nilai(jadwal)
    plays = stx.plays_per_player
    st = ScheduleState(8, [float(r) for r in RATING], Weights(), len(jadwal))
    for r, (a, b, c, d) in enumerate(jadwal):
        st.pc[st._k(a, b)] += 1
        st.pc[st._k(c, d)] += 1
        for x in (a, b):
            for y in (c, d):
                st.oc[st._k(x, y)] += 1
    ap = sum(max(0, st.pc[st._k(i, j)] - 1) for i, j in combinations(range(8), 2))
    ao = sum(max(0, st.oc[st._k(i, j)] - 1) for i, j in combinations(range(8), 2))
    mp = sum(max(0, plays[p] - 7) for p in range(8)) / 2
    mo = sum(max(0, 2 * plays[p] - 7) for p in range(8)) / 2
    tp = max(1, sum(plays.values()) / 2)
    to = max(1, sum(plays.values()))
    p_pen = max(0.0, ap - mp) / tp
    o_pen = max(0.0, ao - mo) / to
    spread = max(plays.values()) - min(plays.values())
    bye_pen = min(1.0, spread / 3.0)
    b2b_pen = min(1.0, stx.back_to_back_byes / 8)
    return {
        "skor": stx.quality_score, "spread": spread,
        "d_partner": 45 * min(1.0, p_pen), "d_lawan": 30 * min(1.0, o_pen),
        "d_jatah": 15 * bye_pen, "d_beruntun": 10 * b2b_pen,
        "partner_ulang": stx.partner_repeat_pairs,
        "lawan_ulang": stx.opponent_repeat_pairs,
        "b2b": stx.back_to_back_byes,
        "ap": ap, "ao": ao, "mp": mp, "mo": mo,
    }


# --------------------------------------------------- validasi harness -------
lapor("=" * 70)
lapor("TAHAP 1 - validasi harness terhadap jadwal aktual aplikasi")
lapor("=" * 70)
akt = rincian(JADWAL_AKTUAL)
lapor(f"  skor harness      = {akt['skor']}   (aplikasi melaporkan 70.3)")
lapor(f"  partner_ulang     = {akt['partner_ulang']}   (aplikasi: 2)")
lapor(f"  lawan_ulang       = {akt['lawan_ulang']}   (aplikasi: 13)")
lapor(f"  duduk_beruntun    = {akt['b2b']}   (aplikasi: 11)")
lapor(f"  main_per_orang    = {min(nilai(JADWAL_AKTUAL).plays_per_player.values())}"
      f"-{max(nilai(JADWAL_AKTUAL).plays_per_player.values())}   (aplikasi: 4-8)")
cocok = (akt["skor"] == 70.3 and akt["partner_ulang"] == 2
         and akt["lawan_ulang"] == 13 and akt["b2b"] == 11)
lapor(f"  format jadwal app = {'SAH' if sah_semua(JADWAL_AKTUAL) else 'MELANGGAR'}"
      f"  (campur={sum(1 for q in JADWAL_AKTUAL if 'F' in [GENDER[p] for p in q])}"
      f" putra={sum(1 for q in JADWAL_AKTUAL if all(GENDER[p]=='M' for p in q))})")
lapor(f"  -> harness {'COCOK' if cocok else 'TIDAK COCOK - hasil di bawah tidak sahih'}")
if not cocok:
    lapor("  BERHENTI: harness tidak mereproduksi output aplikasi.")
    sys.exit(1)
lapor(f"  rincian denda: partner -{akt['d_partner']:.2f}  lawan -{akt['d_lawan']:.2f}"
      f"  jatah -{akt['d_jatah']:.2f}  beruntun -{akt['d_beruntun']:.2f}")

# ------------------------------------------- batas teoretis tiap komposisi --
lapor("")
lapor("=" * 70)
lapor("TAHAP 2 - berapa ronde campuran yang membuat jatah main merata?")
lapor("=" * 70)
lapor("  m = jumlah ronde LP-LP (campuran). PP-PP mustahil: butuh 4 wanita, ada 3.")
lapor("  slot wanita = 2m dibagi 3 orang; slot pria = (48-2m) dibagi 5 orang.")
for m in range(0, 13):
    sw, sp = 2 * m, 48 - 2 * m
    if sp < 0 or sp > 4 * (12 - m) + 2 * m:
        lapor(f"  m={m:2d} -> tidak sah, dilewati (slot pria {sp} tak muat)")
        continue
    w = [sw // 3 + (1 if i < sw % 3 else 0) for i in range(3)]
    p = [sp // 5 + (1 if i < sp % 5 else 0) for i in range(5)]
    spread = max(w + p) - min(w + p)
    bye_pen = min(1.0, spread / 3.0)
    tanda = "  <== spread minimum" if spread == 0 else ""
    lapor(f"  m={m:2d} -> wanita {w}  pria {p}  spread={spread}  "
          f"denda_jatah=-{15*bye_pen:.1f}{tanda}")

lapor("")
lapor("  Jadwal aktual memakai m=6 (12 slot wanita) -> spread 4 -> denda -15 MENTOK.")

# ----------------------------------------- batas bawah pengulangan di m=9 ---
lapor("")
lapor("=" * 70)
lapor("TAHAP 3 - batas bawah pengulangan KALAU dipaksa merata (m=9)")
lapor("=" * 70)
lapor("  partner: 3 ronde putra x2 = 6 pasangan L-L (tersedia C(5,2)=10 -> muat)")
lapor("           9 ronde campur x2 = 18 pasangan L-P (tersedia 5x3=15 -> KELEBIHAN 3)")
lapor("  -> actual_partner_excess >= 3, padahal min_partner_excess kode = 0")
lapor(f"  -> denda partner minimum = 45 * 3/24 = -{45*3/24:.3f}")
lapor("  lawan  : L-L 3x4 + 9x1 = 21 pairing atas 10 pasang -> kelebihan >= 11")
lapor("           P-P 9x1 = 9 pairing atas 3 pasang        -> kelebihan >= 6")
lapor("           L-P 9x2 = 18 pairing atas 15 pasang      -> kelebihan >= 3")
lapor("  -> total >= 20, dan min_oppo_excess kode = 20 -> denda lawan bisa 0")

# ------------------------------------------------------------- pencarian ----
lapor("")
lapor("=" * 70)
lapor("TAHAP 4 - cari jadwal nyata dengan semua orang main tepat 6x")
lapor("=" * 70)


def acak_seimbang(rng: random.Random):
    """Bangun kerangka: 3 ronde putra + 9 ronde campur, semua main 6x."""
    for _ in range(4000):
        duduk = [rng.choice(PRIA) for _ in range(3)]          # 1 pria duduk / ronde putra
        ronde_putra = [[m for m in PRIA if m != s] for s in duduk]
        # x_i = berapa kali pria i tampil di ronde campuran
        x = {m: 6 - sum(1 for rp in ronde_putra if m in rp) for m in PRIA}
        if any(v < 0 for v in x.values()) or sum(x.values()) != 18:
            continue
        # wanita: tiap pasangan wanita dipakai 3x -> tiap wanita main 6x
        pasangan_w = list(combinations(WANITA, 2)) * 3
        rng.shuffle(pasangan_w)
        # pria di ronde campuran: 2 per ronde sesuai kuota x
        kolam = [m for m in PRIA for _ in range(x[m])]
        rng.shuffle(kolam)
        campur = []
        ok = True
        sisa = kolam[:]
        for k in range(9):
            pilih = []
            for _ in range(2):
                kand = [m for m in sisa if m not in pilih]
                if not kand:
                    ok = False
                    break
                m = rng.choice(kand)
                sisa.remove(m)
                pilih.append(m)
            if not ok:
                break
            campur.append((pilih, list(pasangan_w[k])))
        if not ok or sisa:
            continue
        jadwal = []
        for rp in ronde_putra:
            a, b, c, d = rng.sample(rp, 4)
            jadwal.append((a, b, c, d))
        for pria2, wan2 in campur:
            m1, m2 = pria2
            w1, w2 = wan2
            if rng.random() < 0.5:
                w1, w2 = w2, w1
            jadwal.append((m1, w1, m2, w2))
        rng.shuffle(jadwal)
        return jadwal
    return None


def tetangga(jadwal, rng):
    """Gerakan yang MEMPERTAHANKAN jumlah main tiap orang."""
    j = [tuple(q) for q in jadwal]
    if rng.random() < 0.35:
        r = rng.randrange(12)
        a, b, c, d = j[r]
        # Hanya susunan ulang yang tetap SAH formatnya. Tanpa saringan ini,
        # ronde campuran (M,F,M,F) bisa berubah jadi LL-PP - format terlarang
        # yang memberi skor palsu karena lolos dari batas pigeonhole L-P.
        opsi = [q for q in ((a, c, b, d), (a, d, b, c), (a, b, c, d)) if sah(q)]
        j[r] = rng.choice(opsi)
        return j
    for _ in range(60):
        r1, r2 = rng.sample(range(12), 2)
        s1, s2 = set(j[r1]), set(j[r2])
        kand1 = [p for p in s1 - s2]
        kand2 = [p for p in s2 - s1]
        if not kand1 or not kand2:
            continue
        i = rng.choice(kand1)
        cocok = [p for p in kand2 if GENDER[p] == GENDER[i]]
        if not cocok:
            continue
        p = rng.choice(cocok)
        b1 = tuple(p if x == i else x for x in j[r1])
        b2 = tuple(i if x == p else x for x in j[r2])
        if not (sah(b1) and sah(b2)):
            continue
        j[r1], j[r2] = b1, b2
        return j
    return j


def kunci(jadwal):
    """Fitness = skor, dengan b2b sebagai tie-break.

    Perlu tie-break karena b2b_pen JENUH di b2b=8: di atas itu skor tidak
    berubah lagi, jadi pencarian yang hanya mengejar skor akan membiarkan
    duduk-beruntun membengkak ke tingkat acak (~22). Bobot 0.001 cukup kecil
    untuk tidak pernah menukar 1 poin skor demi b2b.
    """
    d = rincian(jadwal)
    return d["skor"] - 0.001 * d["b2b"], d


t0 = time.time()
rng = random.Random(20260810)
terbaik = None
terbaik_d = None
gagal_kerangka = 0
ditolak_format = 0
ditolak_spread = 0
RESTART = 60
ITER = 4000

for restart in range(RESTART):
    awal = acak_seimbang(rng)
    if awal is None:
        gagal_kerangka += 1
        lapor(f"  restart {restart}: kerangka seimbang GAGAL dibangun (dilewati)")
        continue
    if not sah_semua(awal):
        lapor(f"  restart {restart}: BUG kerangka, ada ronde format terlarang (dilewati)")
        continue
    cur = awal
    cur_s, cur_d = kunci(cur)
    if cur_d["spread"] != 0:
        lapor(f"  restart {restart}: BUG kerangka, spread={cur_d['spread']} (dilewati)")
        continue
    T0, T1 = 6.0, 0.05
    for it in range(ITER):
        T = T0 * (T1 / T0) ** (it / ITER)
        kand = tetangga(cur, rng)
        if not sah_semua(kand):
            ditolak_format += 1
            continue          # tolak: format di luar [LL-LL, LP-LP, PP-PP]
        s, d = kunci(kand)
        if d["spread"] != 0:
            ditolak_spread += 1
            continue          # tolak: melanggar syarat merata
        if s >= cur_s or rng.random() < pow(2.718, (s - cur_s) / max(T, 1e-9)):
            cur, cur_s, cur_d = kand, s, d
        fit_terbaik = (-1e9 if terbaik_d is None
                       else terbaik_d["skor"] - 0.001 * terbaik_d["b2b"])
        if s > fit_terbaik:
            terbaik, terbaik_d = kand, d
            lapor(f"  [{time.time()-t0:5.1f}s] restart {restart} iter {it}: "
                  f"skor BARU {d['skor']}  (partner_ulang={d['partner_ulang']} "
                  f"lawan_ulang={d['lawan_ulang']} b2b={d['b2b']} spread=0)")
    if (restart + 1) % 10 == 0:
        lapor(f"  ...restart {restart+1}/{RESTART}  terbaik={terbaik_d['skor']}  "
              f"({time.time()-t0:.1f}s)")

lapor("")
lapor(f"  kerangka gagal dibangun: {gagal_kerangka}/{RESTART} restart")
lapor(f"  kandidat ditolak       : {ditolak_format} format terlarang, "
      f"{ditolak_spread} spread != 0")
if not sah_semua(terbaik):
    lapor("  FATAL: jadwal terbaik melanggar format - hasil TIDAK sahih.")
    sys.exit(1)
lapor("  cek format jadwal akhir: SAH (semua ronde LL-LL / LP-LP / PP-PP)")
lapor(f"  total waktu pencarian  : {time.time()-t0:.1f}s")
lapor(f"  TIDAK diuji            : m=8 dan m=10 (spread 2) - hanya m=9 dicari;")
lapor(f"                           pencarian heuristik, bukan bukti optimalitas.")

# --------------------------------------------------------------- hasil ------
lapor("")
lapor("=" * 70)
lapor("TAHAP 5 - perbandingan")
lapor("=" * 70)
d = terbaik_d
lapor(f"{'':22s} {'AKTUAL (app)':>14s} {'MERATA (dicari)':>16s}")
lapor(f"{'skor kualitas':22s} {akt['skor']:>14} {d['skor']:>16}")
lapor(f"{'main per orang':22s} {'4-8':>14s} {'6-6':>16s}")
lapor(f"{'partner berulang':22s} {akt['partner_ulang']:>14} {d['partner_ulang']:>16}")
lapor(f"{'lawan berulang':22s} {akt['lawan_ulang']:>14} {d['lawan_ulang']:>16}")
lapor(f"{'duduk beruntun':22s} {akt['b2b']:>14} {d['b2b']:>16}")
lapor("  -- denda --")
lapor(f"{'  partner':22s} {-akt['d_partner']:>14.2f} {-d['d_partner']:>16.2f}")
lapor(f"{'  lawan':22s} {-akt['d_lawan']:>14.2f} {-d['d_lawan']:>16.2f}")
lapor(f"{'  jatah main':22s} {-akt['d_jatah']:>14.2f} {-d['d_jatah']:>16.2f}")
lapor(f"{'  duduk beruntun':22s} {-akt['d_beruntun']:>14.2f} {-d['d_beruntun']:>16.2f}")

lapor("")
lapor("jadwal merata terbaik:")
for r, (a, b, c, d_) in enumerate(terbaik, 1):
    tipe = "putra " if all(GENDER[p] == "M" for p in (a, b, c, d_)) else "campur"
    lapor(f"  R{r:<2d} [{tipe}] {NAMA[a]}+{NAMA[b]} vs {NAMA[c]}+{NAMA[d_]}")
st_akhir = nilai(terbaik)
lapor(f"  cek main per orang: "
      f"{ {NAMA[i]: st_akhir.plays_per_player[i] for i in range(8)} }")
