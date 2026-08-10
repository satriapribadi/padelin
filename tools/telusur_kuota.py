#!/usr/bin/env python3
"""Telusuri kenapa penjadwal berhenti di m=6 ronde campuran, bukan m=9.

Mereproduksi konfigurasi dari INFO DEBUG PADELIN host, lalu mengintip
keputusan internal di jalur anggaran bentuk tim (shape budget).

Jalankan: python3 -u tools/telusur_kuota.py
"""
from __future__ import annotations

import sys
from itertools import combinations

sys.path.insert(0, ".")

from padel_scheduler.capacity import shape_budget, shape_totals
from padel_scheduler.models import Config, Player
from padel_scheduler.scheduler import (
    _SEMUA_FORMAT, _candidate_rounds, _free_pair_rounds, _resolve_segments,
    build_schedule,
)


def lapor(m=""):
    print(m, flush=True)


NAMA = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"]
RATING = [3, 2, 3, 3, 4, 2, 2, 2]
GENDER = ["M", "F", "M", "F", "M", "F", "M", "M"]
PLAYERS = [Player(id=i, name=NAMA[i], rating=float(RATING[i]), gender=GENDER[i])
           for i in range(8)]

CFG = Config(
    courts=1, duration_minutes=120, round_minutes=10, warmup_minutes=0,
    mode="americano", tier_count=2, seed=77, effort=160_000, attempts=3,
    referees_per_court=1, ballboys_per_court=1, interleave_segments=False,
    allowed_matchups=["LL-LL", "LP-LP", "PP-PP"],
)

IZIN = set(CFG.allowed_matchups)
N_PRIA = sum(1 for g in GENDER if g == "M")
N_WANITA = sum(1 for g in GENDER if g == "F")

lapor("=" * 72)
lapor("TAHAP 1 - reproduksi jadwal aplikasi")
lapor("=" * 72)
sch = build_schedule(PLAYERS, CFG)
plays = sch.stats.plays_per_player
campur = putra = lain = 0
for rnd in sch.rounds:
    for m in rnd.matches:
        g = [GENDER[p] for p in (m.team_a[0], m.team_a[1], m.team_b[0], m.team_b[1])]
        if g.count("F") == 0:
            putra += 1
        elif g.count("F") == 2:
            campur += 1
        else:
            lain += 1
lapor(f"  kualitas          = {sch.stats.quality_score}   (host melaporkan 70.3)")
lapor(f"  main per orang    = {min(plays.values())}-{max(plays.values())}"
      f"   (host: 4-8)")
lapor(f"  ronde campuran m  = {campur}")
lapor(f"  ronde putra       = {putra}")
lapor(f"  ronde bentuk lain = {lain}")
lapor(f"  total ronde       = {len(sch.rounds)}")

lapor()
lapor("=" * 72)
lapor("TAHAP 2 - apakah syarat pemakaian anggaran terpenuhi?")
lapor("=" * 72)
segments = _resolve_segments(CFG)
syarat = {
    "izin tidak kosong": bool(IZIN),
    "izin != semua format": IZIN != _SEMUA_FORMAT,
    "tier_of is None (mode != tiered)": CFG.mode != "tiered",
    "tidak ada partner terkunci": not any(p.partner_id for p in PLAYERS),
    "cuma 1 segmen": len(segments) == 1,
    "segmen open": segments[0].rule == "open",
    "semua gender terisi": all(p.gender in ("M", "F") for p in PLAYERS),
}
for k, v in syarat.items():
    lapor(f"  {'OK  ' if v else 'GAGAL'} {k}")
pakai_kuota = all(syarat.values())
lapor(f"  -> pakai_kuota = {pakai_kuota}")

lapor()
lapor("=" * 72)
lapor("TAHAP 3 - GERBANG KEDUA: total_rounds <= jumlah ronde kandidat?")
lapor("=" * 72)
cands = _candidate_rounds(segments[0], PLAYERS, CFG, None, {})
total_rounds = len(sch.rounds)
lapor(f"  total_rounds            = {total_rounds}")
lapor(f"  len(ronde kandidat)     = {len(cands)}   <- 1-faktorisasi 8 pemain")
lapor(f"  syarat di scheduler.py:1261: {total_rounds} <= {len(cands)} "
      f"-> {total_rounds <= len(cands)}")
if total_rounds > len(cands):
    lapor("  >> GERBANG TERTUTUP. Blok anggaran bentuk tim TIDAK PERNAH jalan.")
    lapor("  >> quota tetap None; komposisi format diserahkan ke annealing.")

lapor()
lapor("=" * 72)
lapor("TAHAP 4 - apa yang AKAN dihitung anggaran seandainya gerbang terbuka?")
lapor("=" * 72)
need = 2 * min(CFG.courts, len(PLAYERS) // 4)
matches = total_rounds * (need // 2)
lapor(f"  need (tim per ronde)    = {need}")
lapor(f"  matches (se-meet)       = {matches}")
b = shape_budget(N_PRIA, N_WANITA, matches, sorted(IZIN), None)
lapor(f"  feasible                = {b.feasible}")
lapor(f"  target komposisi        = {b.target}")
if b.target:
    tot = shape_totals(b.target)
    lapor(f"  total tim per bentuk    = {tot}")
    m_campur = b.target.get("LP-LP", 0)
    lapor(f"  -> ronde campuran yang ditargetkan m = {m_campur}")
    slot_w = 2 * m_campur + 4 * b.target.get("PP-PP", 0)
    slot_p = 4 * b.target.get("LL-LL", 0) + 2 * m_campur
    lapor(f"  -> slot wanita {slot_w} (/{N_WANITA} = {slot_w/N_WANITA:.1f} main),"
          f" slot pria {slot_p} (/{N_PRIA} = {slot_p/N_PRIA:.1f} main)")
    if slot_w / N_WANITA == slot_p / N_PRIA == 6:
        lapor("  -> ANGGARAN INI MERATA (semua main 6x). Tapi tidak pernah dipakai.")

lapor()
lapor("=" * 72)
lapor("TAHAP 5 - kenapa kandidatnya cuma 7 ronde?")
lapor("=" * 72)
fak = _free_pair_rounds(list(range(8)), {})
lapor(f"  _free_pair_rounds(8 pemain) -> {len(fak)} ronde, "
      f"{len(fak[0])} pasangan per ronde")
lapor("  1-faktorisasi K8: 7 ronde x 4 pasangan = 28 = C(8,2) pasangan unik.")
lapor(f"  Meet ini butuh {total_rounds} ronde, jadi baris faktorisasi diulang:")
for r in range(total_rounds):
    tandai = " <- ULANG" if r >= len(fak) else ""
    lapor(f"    ronde {r+1:2d} pakai baris faktorisasi {r % len(fak)}{tandai}")
lapor(f"  Dengan 1 court cuma {need} dari {len(fak[0])} pasangan yang dipakai")
lapor("  tiap ronde - jadi 28 pasangan unik itu TIDAK habis terpakai:")
lapor(f"    dipakai = {total_rounds} ronde x {need//2*2} pasangan "
      f"= {total_rounds * need} pasangan-slot")
lapor(f"    tersedia = 28 pasangan unik")
lapor("  >> Premis gerbang ('partner berulang tak terhindarkan') TIDAK berlaku")
lapor("     saat court < pemain/4: hanya sebagian pasangan tiap baris dipakai.")

lapor()
lapor("=" * 72)
lapor("TAHAP 6 - bukti: apakah 12 ronde bisa tanpa partner berulang?")
lapor("=" * 72)
lapor(f"  butuh {total_rounds * (need//2) * 2} pasangan; tersedia 28 unik "
      f"-> {'MUAT' if total_rounds*need <= 28 else 'TIDAK MUAT'}")
lapor(f"  jadwal aktual punya {sch.stats.partner_repeat_pairs} partner berulang")
lapor("  (hasil pencarian sebelumnya menemukan jadwal 12 ronde dengan 0-3)")

lapor()
lapor("  TIDAK diuji: roster/court lain, dan apakah membuka gerbang benar-benar")
lapor("               memperbaiki hasil - itu perlu perubahan kode + uji ulang.")


# ---------------------------------------------------------------------------
lapor()
lapor("=" * 72)
lapor("TAHAP 7 - kenapa shape_budget bilang infeasible?")
lapor("=" * 72)
supply = {"LL": N_PRIA * (N_PRIA - 1) // 2, "LP": N_PRIA * N_WANITA,
          "PP": N_WANITA * (N_WANITA - 1) // 2}
lapor(f"  kolam pasangan unik: {supply}")
base, extra = divmod(4 * matches, len(PLAYERS))
lapor(f"  divmod(4*{matches}, 8) = base {base}, extra {extra}"
      f"  -> target {base} main/orang (MERATA)")
lapor(f"  slot pria yang dituntut = {N_PRIA} x {base} = {N_PRIA*base}")
lapor()
lapor("  Komposisi 12 match dengan 30 slot pria (a=LL-LL, b=LP-LP, c=PP-PP).")
lapor("  Syarat shape_budget: max(partner, lawan) <= kolam, untuk TIAP bentuk.")
lapor("    partner: LL=2a  LP=2b  PP=2c")
lapor("    lawan  : LL=4a+b  LP=2b  PP=b+4c")
lapor()
lapor(f"  {'a':>2s} {'b':>2s} {'c':>2s} | {'par(LL/LP/PP)':>14s} | {'lawan(LL/LP/PP)':>16s} | vonis")
for a in range(13):
    for b in range(13 - a):
        c = 12 - a - b
        if 4 * a + 2 * b != 30:
            continue
        par = {"LL": 2 * a, "LP": 2 * b, "PP": 2 * c}
        opp = {"LL": 4 * a + b, "LP": 2 * b, "PP": b + 4 * c}
        jebol = [f"{k} butuh {max(par[k], opp[k])} > kolam {supply[k]}"
                 for k in ("LL", "LP", "PP") if max(par[k], opp[k]) > supply[k]]
        vonis = "OK" if not jebol else "DITOLAK: " + "; ".join(jebol)
        lapor(f"  {a:2d} {b:2d} {c:2d} |"
              f" {par['LL']:4d}/{par['LP']:4d}/{par['PP']:4d} |"
              f" {opp['LL']:5d}/{opp['LP']:5d}/{opp['PP']:5d} | {vonis}")
lapor()
lapor("  SEMUA komposisi merata ditolak, dan yang mengikat adalah LAWAN L-L:")
lapor("  5 pria cuma punya C(5,2)=10 pasangan, sedangkan 12 match menghasilkan")
lapor("  21-29 pertemuan L-L. shape_budget memakai syarat KERAS")
lapor("  'max(par,opp) <= supply', yaitu NOL pengulangan partner DAN lawan.")
lapor("  Pada 12 ronde / 8 pemain, nol pengulangan memang mustahil - aplikasi")
lapor("  sendiri mencetak catatan itu. Jadi anggaran menyerah total, bukan")
lapor("  mundur ke komposisi merata dengan sedikit pengulangan.")

lapor()
lapor("=" * 72)
lapor("TAHAP 8 - kenapa annealing mendarat tepat di m=6?")
lapor("=" * 72)
lapor("  Tiap baris 1-faktorisasi berisi 4 pasangan yang menutup semua 8 pemain.")
lapor("  3 wanita hanya bisa terbagi 2 cara: (1 pasangan PP + 1 LP) atau (3 LP).")
lapor()
lapor(f"  {'baris':>5s} | {'LL':>2s} {'LP':>2s} {'PP':>2s} | bisa LL-LL? bisa LP-LP?")
bisa_ll = bisa_lp = 0
tipe = []
for i, row in enumerate(fak):
    c = {"LL": 0, "LP": 0, "PP": 0}
    for x, y in row:
        gx, gy = GENDER[x], GENDER[y]
        c["LL" if gx == gy == "M" else ("PP" if gx == gy == "F" else "LP")] += 1
    ll = c["LL"] >= 2
    lp = c["LP"] >= 2
    tipe.append((ll, lp))
    lapor(f"  {i:5d} | {c['LL']:2d} {c['LP']:2d} {c['PP']:2d} |"
          f" {'ya ' if ll else 'TIDAK':>9s}  {'ya' if lp else 'TIDAK':>9s}")
lapor()
pakai = [i % len(fak) for i in range(total_rounds)]
n_ll_only = sum(1 for i in pakai if tipe[i][0] and not tipe[i][1])
n_lp_only = sum(1 for i in pakai if tipe[i][1] and not tipe[i][0])
n_dua = sum(1 for i in pakai if tipe[i][0] and tipe[i][1])
lapor(f"  Baris yang dipakai 12 ronde: {pakai}")
lapor(f"    hanya bisa LL-LL : {n_ll_only} ronde")
lapor(f"    hanya bisa LP-LP : {n_lp_only} ronde")
lapor(f"    bisa dua-duanya  : {n_dua} ronde")
lapor(f"  -> m TERKUNCI di {n_lp_only}..{n_lp_only + n_dua} oleh pilihan baris,")
lapor(f"     bukan oleh annealing. Hasil aktual m={campur} ada di rentang itu.")
lapor(f"  -> m=9 {'TERCAPAI' if n_lp_only <= 9 <= n_lp_only+n_dua else 'DI LUAR JANGKAUAN'}"
      f" dengan set baris ini.")
lapor()
lapor("  _pick_candidate_rounds() yang tugasnya memilih baris kaya-campuran")
lapor("  hanya dipanggil DI DALAM blok anggaran - yang tidak pernah jalan.")
lapor("  Tanpa itu, baris dipakai apa adanya: 0,1,2,3,4,5,6,0,1,2,3,4.")
