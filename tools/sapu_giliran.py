#!/usr/bin/env python3
"""Sapuan regresi keadilan giliran: apakah antrean rapi tanpa merusak keunikan.

Yang diukur per kasus:

  giliran_terlewat  berapa kali seseorang turun untuk kali ke-(k+1) padahal ada
                    orang lain yang sedang duduk dan belum kebagian kali ke-k
  tunggu            rentetan duduk terpanjang yang dialami seorang peserta
  batas_tunggu      rentetan terpanjang yang masih tak terhindarkan (ceil dari
                    duduk/(main+1)); `tunggu == batas` berarti sudah sempurna
  main_pertama      ronde tempat peserta TERAKHIR mendapat match pertamanya
  spread_main       selisih jumlah main terbanyak - tersedikit
  partner/lawan     pasangan yang berulang - ini yang tidak boleh dikorbankan

Dipakai dua kali, sebelum dan sesudah perubahan:

    git stash && python3 -u tools/sapu_giliran.py > /tmp/giliran_lama.txt
    git stash pop && python3 -u tools/sapu_giliran.py > /tmp/giliran_baru.txt
    python3 -u tools/sapu_giliran.py --bandingkan /tmp/giliran_lama.txt \
        /tmp/giliran_baru.txt

Tiap kasus dicatat lengkap dengan parameternya supaya kegagalan bisa diulang
dari log tanpa menebak. Baris HASIL berformat JSON supaya --bandingkan bisa
membacanya kembali.
"""
from __future__ import annotations

import itertools
import json
import math
import random
import sys
import time
import traceback

sys.path.insert(0, ".")

from padel_scheduler.models import Config, Player

# Roster: (jumlah putra, jumlah putri). Dipilih untuk mencakup yang seimbang,
# yang timpang, dan yang satu gender saja - ketiganya memakai jalur kode yang
# berbeda di pemilihan komposisi bentuk tim.
ROSTER = [(6, 4), (5, 3), (8, 8), (10, 2), (4, 4), (14, 6), (12, 0), (7, 5),
          (9, 9), (16, 10), (20, 6), (26, 0)]
COURT = [1, 2, 4]
FORMAT = [
    None,                                  # semua format boleh
    ["LL-LL", "LP-LP", "PP-PP"],           # sesama bentuk saja
    ["LP-LP"],                             # campur saja
]
SEED = [42, 7, 2024]
# Urutan gender dipasangkan ke INDEKS seed, bukan dijadikan sumbu sendiri.
# Sebagai sumbu ia mengalikan jumlah kasus tiga kali; dipasangkan begini tiap
# kombinasi roster x court x format tetap mencicipi ketiga keluarga instance
# dengan jumlah kasus yang persis sama. Kuncinya tetap stabil - urutan
# ditentukan oleh posisi seed - jadi perbandingan sebelum/sesudah tetap sah.
URUTAN = ["blok", "selang", "acak"]
DURASI = 120
RONDE_MENIT = 8

# Batas cakupan, disebut di log supaya tidak terbaca "semua sudah dicek":
# effort dipangkas dari default 30000 dan attempts dari 3 supaya sapuannya
# selesai dalam hitungan menit. Keduanya hanya mengubah seberapa jauh optimasi
# berjalan, bukan jalur kode mana yang dilewati.
EFFORT = 8000
ATTEMPTS = 1


def lapor(m: str = "") -> None:
    print(m, flush=True)


def _urutan_gender(n_pria: int, n_putri: int, urutan: str) -> list[str]:
    """Susunan gender roster. Ini BUKAN detail kosmetik.

    Urutan gender menentukan baris 1-faktorisasi yang terbentuk, jadi dua roster
    dengan jumlah yang persis sama tapi urutan berbeda adalah dua instance yang
    sama sekali lain. Sapuan ini dulu membangun semuanya blok - putra dulu, lalu
    putri - sehingga 324 kasusnya satu keluarga instance saja, dan kebetulan
    BUKAN keluarga roster host, yang menyelang-nyeling.

    Selisihnya besar, bukan derau: pada 16 putra + 10 putri di 4 court, serobotan
    berayun 85 (blok) vs 37 (selang) pada seed dan effort yang sama; pada 14
    putra + 6 putri di 3 court, 26 vs 4.
    """
    if urutan == "blok":
        return ["M"] * n_pria + ["F"] * n_putri
    if urutan == "selang":
        out = []
        i = j = 0
        while i < n_pria or j < n_putri:
            if i < n_pria:
                out.append("M")
                i += 1
            if j < n_putri:
                out.append("F")
                j += 1
        return out
    if urutan == "acak":
        out = ["M"] * n_pria + ["F"] * n_putri
        random.Random(9901 + n_pria * 100 + n_putri).shuffle(out)
        return out
    raise ValueError(f"urutan tidak dikenal: {urutan}")


def _roster(n_pria: int, n_putri: int, urutan: str = "blok") -> list[Player]:
    """Roster dengan rating melekat pada POSISI, bukan pada orangnya.

    Kalau rating ikut berpindah saat urutan gender berubah, dua efek bercampur
    dan selisih antar urutan tidak bisa disimpulkan dari apa pun.
    """
    return [
        Player(id=i + 1, name=(f"L{i+1}" if g == "M" else f"P{i+1}"),
               rating=2 + i % 4, gender=g)
        for i, g in enumerate(_urutan_gender(n_pria, n_putri, urutan))
    ]


def _giliran(sch) -> dict:
    """Hitung ulang metrik giliran dari jadwal jadi, bukan dari stats.

    Sengaja dihitung ulang di sini: kalau angkanya diambil dari
    ScheduleStats, sapuan ini cuma memeriksa bahwa dua versi kode melaporkan
    apa yang mereka masing-masing yakini - bukan bahwa jadwalnya memang
    membaik. Ia juga harus bisa jalan pada kode LAMA, yang belum punya
    field-field itu sama sekali.
    """
    ids = [p.id for p in sch.players]
    n_ronde = len(sch.rounds)
    sudah = {p: 0 for p in ids}
    sejak = {p: 0 for p in ids}
    main = {p: 0 for p in ids}
    pertama: dict[int, int] = {}
    terlewat = 0
    tunggu = 0
    for rnd in sch.rounds:
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
    nilai = list(main.values())
    return {
        "giliran_terlewat": terlewat,
        "tunggu": tunggu,
        "batas_tunggu": batas,
        "main_pertama": (max(pertama.values()) if len(pertama) == len(ids)
                         else n_ronde),
        "spread_main": max(nilai) - min(nilai),
        "belum_main": sum(1 for p in ids if main[p] == 0),
    }


def bandingkan(f_lama: str, f_baru: str) -> None:
    def muat(path):
        out = {}
        with open(path) as fh:
            for baris in fh:
                if baris.startswith("HASIL "):
                    d = json.loads(baris[6:])
                    out[d["kunci"]] = d
        return out

    lama, baru = muat(f_lama), muat(f_baru)
    sama = sorted(set(lama) & set(baru))
    lapor(f"kasus dibandingkan: {len(sama)} (lama {len(lama)}, baru {len(baru)})")
    for k in sorted((set(lama) | set(baru)) - set(sama)):
        lapor(f"  HANYA DI SALAH SATU: {k}")
    lapor("")

    metrik = ["giliran_terlewat", "tunggu", "main_pertama", "spread_main",
              "belum_main", "partner_ulang", "lawan_ulang", "b2b", "kualitas"]
    # Untuk semua metrik ini, lebih kecil = lebih baik, kecuali kualitas.
    lapor("perubahan per kasus (hanya yang berubah):")
    ringkas = {m: [0, 0, 0, 0] for m in metrik}   # membaik, memburuk, total_d, n
    for k in sama:
        a, b = lama[k], baru[k]
        if a.get("error") or b.get("error"):
            lapor(f"  ERROR {k}: lama={a.get('error')} baru={b.get('error')}")
            continue
        potong = []
        for m in metrik:
            if m not in a or m not in b:
                continue
            d = b[m] - a[m]
            baik = d > 0 if m == "kualitas" else d < 0
            ringkas[m][3] += 1
            ringkas[m][2] += d
            if abs(d) > (0.05 if m == "kualitas" else 0):
                ringkas[m][0 if baik else 1] += 1
                potong.append(f"{m} {a[m]}->{b[m]}")
        if potong:
            lapor(f"  {k}: " + ", ".join(potong))

    lapor("")
    lapor(f"{'metrik':20s} {'membaik':>8s} {'memburuk':>9s} {'rata2 delta':>12s}")
    for m in metrik:
        naik, turun, tot, n = ringkas[m]
        if not n:
            continue
        lapor(f"{m:20s} {naik:8d} {turun:9d} {tot / n:12.3f}")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--bandingkan":
        bandingkan(sys.argv[2], sys.argv[3])
        return

    kombinasi = list(itertools.product(ROSTER, COURT, FORMAT,
                                       list(enumerate(SEED))))
    lapor(f"sapuan keadilan giliran: {len(kombinasi)} kasus")
    lapor(f"roster={ROSTER}")
    lapor(f"court={COURT} seed={SEED} durasi={DURASI}m ronde={RONDE_MENIT}m")
    lapor(f"format={[('semua' if f is None else '+'.join(f)) for f in FORMAT]}")
    lapor(f"urutan gender={URUTAN}, dipasangkan ke indeks seed "
          f"({', '.join(f'{s}->{URUTAN[i % len(URUTAN)]}' for i, s in enumerate(SEED))})")
    lapor(f"CATATAN cakupan: effort={EFFORT} (default produksi 30000) dan "
          f"attempts={ATTEMPTS} (default 3) supaya sapuan selesai dalam "
          f"hitungan menit. Cacat giliran yang dilaporkan host justru muncul di "
          f"effort 160.000/attempts=3 - rezim itu disapu terpisah oleh "
          f"tools/sapu_giliran_dalam.py, bukan di sini. Mode selain americano, "
          f"meet bersegmen, partner terkunci, dan preferensi court TIDAK diuji "
          f"di sini - uji unit tests/ yang menjaganya. Tiap kombinasi mencicipi "
          f"satu urutan gender per seed, bukan ketiganya sekaligus.")
    lapor("")

    # Import di sini supaya kegagalan import ikut terlaporkan per kasus, bukan
    # menjatuhkan seluruh sapuan sebelum satu baris pun tercetak.
    from padel_scheduler.scheduler import build_schedule

    mulai = time.time()
    n_error = 0
    for i, ((n_pria, n_putri), court, fmt, (si, seed)) in enumerate(kombinasi, 1):
        urutan = URUTAN[si % len(URUTAN)]
        label = (f"{n_pria}L+{n_putri}P {court}court "
                 f"fmt={'semua' if fmt is None else '+'.join(fmt)} "
                 f"urut={urutan} seed={seed}")
        kunci = f"{n_pria}L{n_putri}P/c{court}/" \
                f"{'semua' if fmt is None else '+'.join(fmt)}/{urutan}/s{seed}"
        players = _roster(n_pria, n_putri, urutan)
        rec: dict = {"kunci": kunci, "label": label}
        try:
            cfg = Config(
                courts=court, duration_minutes=DURASI,
                round_minutes=RONDE_MENIT, warmup_minutes=0,
                mode="americano", seed=seed, effort=EFFORT, attempts=ATTEMPTS,
                allowed_matchups=fmt,
            )
            t0 = time.time()
            sch = build_schedule(players, cfg)
            rec.update(_giliran(sch))
            rec.update(
                ronde=len(sch.rounds),
                partner_ulang=sch.stats.partner_repeat_pairs,
                lawan_ulang=sch.stats.opponent_repeat_pairs,
                b2b=sch.stats.back_to_back_byes,
                kualitas=sch.stats.quality_score,
                detik=round(time.time() - t0, 2),
            )
        except Exception as exc:
            # Pesan UTUH, bukan baris terakhir: dua kegagalan yang berbeda
            # sering berakhir dengan kalimat penutup yang sama, dan kalau cuma
            # baris terakhir yang disimpan keduanya jadi tak terbedakan.
            n_error += 1
            rec["error"] = f"{type(exc).__name__}: {exc}"
            lapor(f"  [ERROR] {label}")
            for baris in traceback.format_exc().rstrip().splitlines():
                lapor(f"      {baris}")

        lapor("HASIL " + json.dumps(rec, sort_keys=True))
        if "error" not in rec:
            tanda = "" if rec["tunggu"] <= rec["batas_tunggu"] else "  <-- tunggu di atas batas"
            lapor(f"  {label}: ronde={rec['ronde']} "
                  f"giliran_terlewat={rec['giliran_terlewat']} "
                  f"tunggu={rec['tunggu']}/batas{rec['batas_tunggu']} "
                  f"main_pertama={rec['main_pertama']} "
                  f"spread_main={rec['spread_main']} "
                  f"partner_ulang={rec['partner_ulang']} "
                  f"lawan_ulang={rec['lawan_ulang']} "
                  f"kualitas={rec['kualitas']} {rec['detik']}s{tanda}")
        if i % 20 == 0:
            lapor(f"  ...{i}/{len(kombinasi)} kasus, {n_error} error, "
                  f"{time.time() - mulai:.0f}s berjalan")

    lapor("")
    lapor(f"selesai: {len(kombinasi)} kasus, {n_error} error, "
          f"{time.time() - mulai:.0f}s")


if __name__ == "__main__":
    main()
