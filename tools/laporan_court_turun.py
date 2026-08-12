#!/usr/bin/env python3
"""Susun ulang satu acara tersimpan dengan jumlah court yang berkurang di tengah.

Court berkurang sudah ada di UI ("Setup lapangan" -> Court berkurang di tengah
acara), dan untuk pola "berkurang sekali" pakailah itu. Skrip ini tetap berguna
untuk dua hal yang tidak dilayani UI: pola court yang lebih rumit daripada satu
kali pengurangan (mis. 2x8, 1x4, 2x3), dan memakai kembali setup acara yang
sudah tersimpan tanpa mengubah acaranya.

    python3 -u tools/laporan_court_turun.py --acara 6 \
        --durasi 180 --ronde 12 --rencana-court 2x10,1x5

Yang dikerjakan:
  1. baca setup acara dari padel.db (peserta, seed, effort, format, ekonomi)
  2. generate jadwal dengan courts_per_round, lalu PERIKSA hasilnya
  3. tulis laporan HTML yang siap dicetak jadi PDF

Yang TIDAK dikerjakan: database tidak disentuh sama sekali. Acara tersimpan
tetap seperti aslinya, dan kolom biaya di tabel events dihitung dengan rumus
court x jam yang tidak mengenal court berkurang - menyimpan ke situ akan
mencatat ongkos yang salah. Ongkos yang benar dihitung dan dicetak di sini.

Nama peserta TIDAK pernah masuk ke stdout: yang dicetak alias P1..Pn seperti
info debug di UI, supaya log ini aman ditempel ke mana pun. Nama asli hanya
ada di berkas HTML yang dihasilkan.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from padel_scheduler import Config, Player, Segment, build_schedule  # noqa: E402
from padel_scheduler.html_report import build_html  # noqa: E402
from padel_scheduler.models import team_shape  # noqa: E402
from padel_scheduler.scheduler import ScheduleError  # noqa: E402


def catat(pesan: str = "") -> None:
    """Satu baris log, langsung disiram.

    Tanpa flush, stdout yang dialihkan ke berkas menahan menit-menit pertama di
    buffer dan proses yang sedang bekerja tidak bisa dibedakan dari yang macet.
    """
    print(pesan, flush=True)


def baca_rencana(spec: str, total: int) -> list[int]:
    """"2x10,1x5" -> [2]*10 + [1]*5. Angka tunggal berarti seluruh acara.

    Panjangnya diperiksa di sini, bukan diserahkan ke penjadwal: pesan
    "kurang 1 ronde" jauh lebih murah dibaca di awal daripada di tengah log.
    """
    plan: list[int] = []
    for bagian in spec.split(","):
        bagian = bagian.strip()
        if not bagian:
            continue
        if "x" in bagian:
            court, kali = bagian.split("x", 1)
            plan.extend([int(court)] * int(kali))
        else:
            plan.extend([int(bagian)] * total)
    return plan


def blok_court(plan: list[int]) -> str:
    """"ronde 1-10: 2 court, ronde 11-15: 1 court" untuk dicetak."""
    blok: list[list[int]] = []
    for r, c in enumerate(plan, start=1):
        if blok and blok[-1][2] == c:
            blok[-1][1] = r
        else:
            blok.append([r, r, c])
    return ", ".join(
        (f"ronde {a}: {c} court" if a == b else f"ronde {a}-{b}: {c} court")
        for a, b, c in blok
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="padel.db")
    ap.add_argument("--acara", type=int, required=True,
                    help="id acara di tabel events")
    ap.add_argument("--durasi", type=int, default=None,
                    help="total menit acara; kosong = seperti tersimpan")
    ap.add_argument("--ronde", type=int, default=None,
                    help="menit per ronde; kosong = seperti tersimpan")
    ap.add_argument("--rencana-court", required=True,
                    help="mis. 2x10,1x5 - court per ronde, urut ronde")
    ap.add_argument("--out", default=None, help="berkas HTML tujuan")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        catat(f"GAGAL: database tidak ada di {db.resolve()}")
        return 1

    conn = sqlite3.connect(db)
    baris = conn.execute(
        "SELECT request_json FROM events WHERE id=?", (args.acara,)
    ).fetchone()
    if baris is None:
        ada = [r[0] for r in conn.execute("SELECT id FROM events ORDER BY id")]
        catat(f"GAGAL: acara {args.acara} tidak ada. Yang ada: {ada}")
        return 1
    req = json.loads(baris[0])

    # --- setup, dicetak lengkap supaya log ini bisa diulang tanpa menebak ---
    peserta = req["players"]
    alias = {p["id"]: f"P{i + 1}" for i, p in enumerate(peserta)}
    durasi = args.durasi if args.durasi is not None else req["duration_minutes"]
    menit_ronde = args.ronde if args.ronde is not None else req["round_minutes"]
    warmup = req.get("warmup_minutes", 0)

    total_ronde = max(0, durasi - warmup) // menit_ronde
    plan = baca_rencana(args.rencana_court, total_ronde)

    catat("--- SUSUN ULANG DENGAN COURT BERKURANG ---")
    catat(f"acara={args.acara} db={db.resolve()}")
    catat(f"durasi={durasi}m pemanasan={warmup}m ronde={menit_ronde}m "
          f"-> {total_ronde} ronde")
    catat(f"rencana court: {blok_court(plan)}")
    catat(f"mode={req['mode']} seed={req['seed']} effort={req['effort']} "
          f"percobaan={req.get('attempts', 3)}")
    catat(f"wasit={req['referees_per_court']} "
          f"ballboy={req['ballboys_per_court']}")
    catat(f"format_diizinkan={req.get('allowed_matchups') or 'semua'}")
    n_l = sum(1 for p in peserta if p["gender"] == "M")
    n_p = sum(1 for p in peserta if p["gender"] == "F")
    catat(f"peserta={len(peserta)} (L{n_l} P{n_p})")
    for p in peserta:
        catat(f"  {alias[p['id']]} rating={p['rating']} g={p['gender'] or '-'}")

    if len(plan) != total_ronde:
        catat(f"GAGAL: rencana court berisi {len(plan)} angka, acaranya "
              f"{total_ronde} ronde. Perbaiki --rencana-court dan ulangi.")
        return 1

    # Slot yang dijanjikan rencana ini, dihitung sebelum generate supaya
    # hasilnya bisa dibandingkan dengan yang dijanjikan, bukan dipercaya saja.
    slot = sum(4 * min(c, len(peserta) // 4) for c in plan)
    catat(f"slot main total={slot} -> rata-rata {slot / len(peserta):.1f} "
          f"ronde main per orang")
    catat()

    players = [
        Player(id=p["id"], name=p["name"], rating=p["rating"],
               gender=p["gender"], partner_id=p.get("partner_id"),
               court_preference=p.get("court_preference"))
        for p in peserta
    ]
    cfg = Config(
        courts=max(plan),
        duration_minutes=durasi,
        round_minutes=menit_ronde,
        warmup_minutes=warmup,
        mode=req["mode"],
        tier_count=req.get("tier_count", 2),
        seed=req["seed"],
        effort=req["effort"],
        attempts=req.get("attempts", 3),
        referees_per_court=req["referees_per_court"],
        ballboys_per_court=req["ballboys_per_court"],
        segments=[Segment(**s) for s in req.get("segments") or []],
        interleave_segments=req.get("interleave_segments", False),
        allowed_matchups=req.get("allowed_matchups"),
    )

    catat("menyusun jadwal (denyut progres dari penjadwal):")
    terakhir = [-1.0]

    def progres(frac: float, msg: str) -> None:
        # Tiap 5% saja: effort 80.000 memanggil ini ratusan kali, dan log yang
        # panjangnya ribuan baris justru menyembunyikan yang penting.
        if frac - terakhir[0] >= 0.05 or frac >= 1.0:
            terakhir[0] = frac
            catat(f"  ...{frac * 100:5.1f}%  {msg}")

    try:
        sch = build_schedule(players, cfg, progres, courts_per_round=plan)
    except ScheduleError as exc:
        catat(f"GAGAL: penjadwal menolak setup ini - {exc}")
        return 1
    catat()

    # --- periksa hasilnya, bukan percaya saja -----------------------------
    temuan: list[str] = []

    catat("court per ronde (rencana vs jadi):")
    for rnd, minta in zip(sch.rounds, plan):
        jadi = len(rnd.matches)
        tanda = "ok" if jadi == minta else "MELENCENG"
        if jadi != minta:
            temuan.append(f"ronde {rnd.index}: diminta {minta} court, "
                          f"jadi {jadi} match")
        catat(f"  R{rnd.index:<3} minta {minta} -> {jadi} match  {tanda}")

    st = sch.stats
    main = st.plays_per_player
    g = {p.id: p.gender for p in sch.players}
    main_l = sorted(v for k, v in main.items() if g[k] == "M")
    main_p = sorted(v for k, v in main.items() if g[k] == "F")
    catat()
    catat("hasil:")
    catat(f"  ronde={len(sch.rounds)} kualitas={st.quality_score}")
    catat(f"  partner_ulang={st.partner_repeat_pairs} "
          f"(maks {st.partner_repeat_max}x) "
          f"lawan_ulang={st.opponent_repeat_pairs} "
          f"(maks {st.opponent_repeat_max}x)")
    catat(f"  main_per_orang={min(main.values())}-{max(main.values())} "
          f"(L: {main_l}, P: {main_p})")
    catat(f"  duduk_beruntun={st.back_to_back_byes} "
          f"giliran_terlewat={st.turn_skips}")
    catat(f"  tunggu_terpanjang={st.longest_wait} (batas {st.wait_floor}) "
          f"main_pertama_terakhir=R{st.last_first_play}")

    if max(main.values()) - min(main.values()) > 1:
        temuan.append(
            f"jatah main timpang: {min(main.values())} sampai "
            f"{max(main.values())} ronde. Dengan format dibatasi, ini tidak bisa "
            f"ditebus optimizer - lihat _slot_plan di scheduler.py")

    # Komposisi format per ronde: yang paling mudah salah begitu court berubah,
    # karena komposisi yang sah untuk 2 match bukan yang sah untuk 1.
    catat()
    catat("komposisi bentuk tim per ronde (LL/LP/PP, slot L-P):")
    for rnd in sch.rounds:
        bentuk = []
        for m in rnd.matches:
            for tim in (m.team_a, m.team_b):
                bentuk.append(team_shape(g[tim[0]], g[tim[1]]))
        sl = sum(2 if b == "LL" else 1 if b == "LP" else 0 for b in bentuk)
        tl = sum(2 if b == "PP" else 1 if b == "LP" else 0 for b in bentuk)
        catat(f"  R{rnd.index:<3} {'+'.join(bentuk):<16} slot L{sl} P{tl}")

    catat()
    catat("catatan dari penjadwal:")
    if not sch.notes:
        catat("  (tidak ada)")
    # Nama peserta bisa muncul di catatan; disamarkan dari yang TERPANJANG dulu
    # supaya nama yang jadi bagian nama lain tidak memotongnya duluan.
    nama_urut = sorted((p for p in sch.players if p.name),
                       key=lambda p: -len(p.name))
    for nt in sch.notes:
        for p in nama_urut:
            nt = nt.replace(p.name, alias.get(p.id, "P?"))
        catat(f"  - {nt}")

    if sch.violations:
        catat()
        catat(f"permintaan peserta yang tidak terpenuhi: {len(sch.violations)}")
        for v in sch.violations:
            catat(f"  - R{v.round_index} {alias.get(v.player_id, 'P?')} "
                  f"minta {v.preference}")

    # --- ongkos, dihitung dari court yang benar-benar disewa --------------
    econ = req.get("economics") or {}
    harga = float(econ.get("court_price_per_hour") or 0)
    fee = float(econ.get("fee_per_player") or 0)
    lain = float(econ.get("other_costs") or 0)
    jam_court = sum(plan) * menit_ronde / 60.0
    biaya = jam_court * harga + lain
    pemasukan = len(peserta) * fee
    catat()
    catat("ongkos (court-jam dihitung per ronde, bukan court terbanyak x jam):")
    catat(f"  court-jam terpakai={jam_court:.2f} "
          f"(harga {harga:,.0f}/jam) biaya={biaya:,.0f}"
          .replace(",", "."))
    catat(f"  pemasukan={pemasukan:,.0f} ({len(peserta)} x {fee:,.0f}) "
          f"selisih={pemasukan - biaya:,.0f}".replace(",", "."))
    if pemasukan < biaya:
        temuan.append(
            f"acara ini rugi {biaya - pemasukan:,.0f} pada fee sekarang; "
            f"fee impas = {biaya / len(peserta):,.0f} per peserta"
            .replace(",", "."))

    # --- laporan ---------------------------------------------------------
    out = Path(args.out) if args.out else Path(
        f"laporan-acara{args.acara}-court-turun.html")
    html = build_html(
        sch,
        title=req.get("title") or "Jadwal Meet Padel",
        event_date=req.get("event_date", ""),
        venue=req.get("venue", ""),
        start_clock=req.get("start_clock") or None,
        fee=fee,
    )
    out.write_text(html, encoding="utf-8")
    catat()
    catat(f"laporan ditulis: {out.resolve()} ({len(html):,} byte)"
          .replace(",", "."))
    catat("buka di browser lalu 'Simpan sebagai PDF' untuk versi cetaknya.")

    catat()
    if temuan:
        catat(f"TEMUAN ({len(temuan)}) - baca sebelum dipakai:")
        for t in temuan:
            catat(f"  ! {t}")
    else:
        catat("TEMUAN: tidak ada. Court per ronde sesuai rencana, jatah main "
              "selisih maksimal 1 ronde.")
    catat()
    catat("yang TIDAK diperiksa skrip ini: database tidak disentuh (acara "
          f"{args.acara} masih versi lama), dan ringkasan kapasitas di dalam "
          "laporan dihitung dengan andaian court tetap sebanyak "
          f"{max(plan)} sepanjang acara.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
