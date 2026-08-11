"""Analisa kelayakan jadwal: court, durasi, dan batas matematis keunikan.

Modul ini yang menjawab pertanyaan "apakah rencana saya masuk akal?" SEBELUM
jadwal dibuat. Dipanggil UI setiap kali host mengubah angka, supaya host
langsung lihat konsekuensinya.

Dua kegagalan yang saling berlawanan, dan keduanya diperiksa di sini:

  1. Grup KECIL + durasi PANJANG  -> pengulangan partner/lawan tak terhindarkan.
  2. Grup BESAR + court SEDIKIT   -> terlalu banyak pemain duduk menunggu.

Host biasanya cuma sadar masalah (1) dan kaget kena masalah (2), atau sebaliknya.

Masalah (1) punya versi yang jauh lebih tajam begitu host membatasi format match
lewat allowed_matchups. Lihat shape_budget() di bawah.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import MATCHUP_LABELS, MATCHUPS

# Ambang "nyaman": pemain duduk maksimal ~25% dari total ronde.
COMFORT_REST_RATIO = 0.25
# Ambang "masih oke": duduk maksimal ~33%.
TOLERABLE_REST_RATIO = 1.0 / 3.0

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Issue:
    severity: str  # "error" | "warning" | "info"
    title: str
    detail: str
    fix: str = ""


@dataclass
class CapacityReport:
    n_players: int
    courts: int
    courts_used: int
    courts_idle: int
    duration_minutes: int
    round_minutes: int
    warmup_minutes: int

    rounds: int
    slots_per_round: int
    byes_per_round: int

    total_slots: int
    avg_plays_per_player: float
    rest_ratio: float
    playing_minutes_per_player: float

    # Batas teoretis keunikan.
    max_unique_partner_rounds: int
    max_unique_opponent_rounds: int
    partner_unique_feasible: bool
    opponent_unique_feasible: bool

    # Rekomendasi.
    ideal_players_for_courts: int
    comfortable_max_players: int
    courts_for_zero_rest: int
    courts_for_comfort: int

    # Kelayakan sadar gender & format. None kalau tidak dinilai - jangan dibaca
    # sebagai "aman". opponent_unique_feasible di atas sudah memperhitungkannya.
    shape_feasible: bool | None = None
    shape_target: dict[str, int] | None = None
    shape_supply: dict[str, int] | None = None
    shape_binding: str | None = None
    shape_shortfall: int = 0

    # Jatah main per kelompok gender di meet bersegmen. None kalau meetnya tanpa
    # babak atau pembagiannya tidak ditentukan aturan babak - lihat
    # kapasitas_per_kelompok(). Kalau terisi dan angkanya berbeda,
    # avg_plays_per_player di atas TIDAK menggambarkan siapa pun.
    groups: list[dict] | None = None

    # Jumlah duduk per ronde berayun antar babak. None kalau angkanya sama di
    # semua babak - byes_per_round di atas sudah cukup. Kalau terisi,
    # byes_per_round adalah ujung TERKECILNYA, bukan yang khas.
    byes_per_round_max: int | None = None

    issues: list[Issue] = field(default_factory=list)
    verdict: str = "ok"  # "ok" | "warning" | "error"

    def sorted_issues(self) -> list[Issue]:
        return sorted(self.issues, key=lambda i: SEVERITY_ORDER[i.severity])


def rounds_from_duration(
    duration_minutes: int, round_minutes: int, warmup_minutes: int
) -> int:
    """Berapa ronde yang muat dalam durasi sewa, setelah dipotong pemanasan."""
    usable = duration_minutes - warmup_minutes
    if usable <= 0:
        return 0
    return max(0, usable // round_minutes)


# ---------------------------------------------------------------------------
# Model suplai pasangan: sadar gender, sadar format
# ---------------------------------------------------------------------------
#
# Batas (N-1)//2 di atas mengandaikan satu kolam pasangan: siapa pun boleh
# berhadapan dengan siapa pun. Begitu host membatasi format match, andaian itu
# runtuh. "Sesama bentuk saja" (LL-LL, LP-LP, PP-PP) memecah kolamnya jadi tiga
# yang tidak bisa saling menutupi: pasangan laki-laki lawan laki-laki tidak
# pernah bisa dipakai untuk memenuhi kebutuhan lawan perempuan.
#
# Kolam terkecil yang menentukan. Dengan 11 perempuan hanya ada C(11,2) = 55
# pasangan P-P di dunia; satu match PP-PP menghabiskan 4 sekaligus. Jadi
# komposisi formatnya sendiri yang menentukan mungkin-tidaknya lawan 100% unik,
# jauh sebelum penjadwalnya mulai bekerja.

# Slot laki-laki yang dipakai satu match, per format. Sisanya slot perempuan -
# tiap match selalu 4 orang - jadi persamaan slot perempuan otomatis terpenuhi
# begitu jumlah match dan total slot laki-laki cocok. Tidak perlu dicek dua kali.
_MALE_SLOTS: dict[str, int] = {
    "LL-LL": 4, "LL-LP": 3, "LL-PP": 2, "LP-LP": 2, "LP-PP": 1, "PP-PP": 0,
}

_KINDS = ("LL", "LP", "PP")

# Berapa pasangan yang dihabiskan satu match, dipecah per jenis pasangan
# (LL = dua laki-laki, LP = campur, PP = dua perempuan).
#
# Partner dan lawan dihitung TERPISAH karena memang dibatasi terpisah: dua orang
# boleh sekali setim dan sekali berhadapan tanpa salah satunya disebut
# pengulangan. Menjumlahkan keduanya jadi satu anggaran akan menolak jadwal yang
# sebenarnya sah.
_PAIR_DEMAND: dict[str, tuple[tuple[int, ...], tuple[int, ...]]] = {
    # format:  partner (LL, LP, PP)   lawan (LL, LP, PP)
    "LL-LL": ((2, 0, 0), (4, 0, 0)),
    "LL-LP": ((1, 1, 0), (2, 2, 0)),
    "LL-PP": ((1, 0, 1), (0, 4, 0)),
    "LP-LP": ((0, 2, 0), (1, 2, 1)),
    "LP-PP": ((0, 1, 1), (0, 2, 2)),
    "PP-PP": ((0, 0, 2), (0, 0, 4)),
}

KIND_LABELS = {
    "LL": "laki-laki dengan laki-laki",
    "LP": "laki-laki dengan perempuan",
    "PP": "perempuan dengan perempuan",
}

# Batas simpul pencarian. Kasus yang benar-benar mengikat (format dibatasi ke
# 3 format) cuma punya satu derajat kebebasan dan selesai dalam puluhan simpul;
# batas ini hanya jaring pengaman supaya analyze() tidak pernah menggantung UI.
_NODE_BUDGET = 200_000


@dataclass
class ShapeBudget:
    """Masihkah lawan 100% unik mungkin, setelah format match dibatasi?

    feasible None berarti tidak dinilai (gender belum lengkap, format tidak
    dibatasi, atau pencarian kena batas simpul) - pemanggilnya harus kembali ke
    batas gender-blind, bukan menganggapnya aman.
    """

    feasible: bool | None
    # Komposisi format paling lega, mis. {"LL-LL": 10, "LP-LP": 40, "PP-PP": 2}.
    # Tanpa `cap` ini optimum di atas kertas - penjadwal memanggil ulang dengan
    # cap dari rotasi partner yang benar-benar tersedia, dan hasilnya bisa
    # berbeda. Yang di sini untuk dilaporkan ke host, bukan untuk dieksekusi.
    target: dict[str, int] | None
    supply: dict[str, int]
    demand_partner: dict[str, int] | None
    demand_opponent: dict[str, int] | None
    # Jenis pasangan yang paling mepet (kalau layak) atau yang jebol (kalau
    # tidak), plus kekurangannya dalam satuan pasangan.
    binding: str | None = None
    shortfall: int = 0


def _walk(codes, matches, male_slots, supply, prune_supply, visit):
    """Telusuri semua komposisi format yang menghabiskan match & slot.

    Mengembalikan False kalau kena batas simpul (hasilnya jadi tidak tuntas).

    Pemangkasan suplai sah dan tidak menghilangkan solusi: permintaan pasangan
    hanya bertambah kalau match ditambah, jadi cabang yang sudah kelebihan tidak
    akan pernah tertolong oleh sisa match.
    """
    n = len(codes)
    ml = [_MALE_SLOTS[c] for c in codes]
    # Sisa slot laki-laki harus masih mungkin dicapai oleh format yang tersisa.
    suf_lo = [0] * (n + 1)
    suf_hi = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suf_lo[i] = min(ml[i], suf_lo[i + 1]) if i < n - 1 else ml[i]
        suf_hi[i] = max(ml[i], suf_hi[i + 1])

    nodes = 0
    counts = [0] * n

    def rec(i, rm, rs, par, opp):
        nonlocal nodes
        nodes += 1
        if nodes > _NODE_BUDGET:
            return False
        if i == n:
            if rm == 0 and rs == 0:
                visit(tuple(counts), par, opp)
            return True
        if rs < suf_lo[i] * rm or rs > suf_hi[i] * rm:
            return True
        dp, do = _PAIR_DEMAND[codes[i]]
        for x in range(rm + 1):
            if x:
                par = tuple(par[k] + dp[k] for k in range(3))
                opp = tuple(opp[k] + do[k] for k in range(3))
                if prune_supply and any(
                    par[k] > supply[_KINDS[k]] or opp[k] > supply[_KINDS[k]]
                    for k in range(3)
                ):
                    break
            counts[i] = x
            if not rec(i + 1, rm - x, rs - ml[i] * x, par, opp):
                return False
        counts[i] = 0
        return True

    return rec(0, matches, male_slots, (0, 0, 0), (0, 0, 0))


def shape_totals(target: dict[str, int]) -> dict[str, int]:
    """Ubah hitungan per FORMAT match jadi hitungan per BENTUK TIM.

    "LL-LP" sekali berarti satu tim LL dan satu tim LP; "LL-LL" sekali berarti
    dua tim LL.
    """
    tot = {k: 0 for k in _KINDS}
    for code, n in target.items():
        a, b = code.split("-")
        tot[a] += n
        tot[b] += n
    return tot


def shape_budget(
    men: int,
    women: int,
    matches: int,
    allowed: list[str] | None = None,
    cap: dict[str, int] | None = None,
) -> ShapeBudget:
    """Hitung jendela komposisi format yang masih memungkinkan lawan unik.

    Generik untuk keenam format dan roster apa pun; tidak ada satu pun angka
    yang khusus untuk satu setup.

    `cap` membatasi jumlah TIM per bentuk yang benar-benar bisa disediakan
    konstruktor pasangan. Muat di atas kertas tidak sama dengan terjangkau:
    komposisi paling lega sering menuntut lebih banyak tim campur daripada yang
    disediakan rotasi partner, dan target yang tak terjangkau lebih buruk
    daripada tidak punya target - penjadwalnya melesetinya tiap ronde.
    """
    supply = {
        "LL": men * (men - 1) // 2,
        "LP": men * women,
        "PP": women * (women - 1) // 2,
    }
    codes = [c for c in MATCHUPS if allowed is None or c in allowed]
    if matches <= 0 or not codes or men < 0 or women < 0 or men + women < 4:
        return ShapeBudget(None, None, supply, None, None)

    n_players = men + women
    base, extra = divmod(4 * matches, n_players)
    # rebalance_plays() menjamin selisih jumlah main maksimal 1, jadi sebagian
    # orang main base kali dan sisanya base+1. Berapa dari yang base+1 itu
    # laki-laki belum tentu - jadi semua pembagian yang mungkin ikut dicoba.
    lo = max(0, extra - women)
    hi = min(extra, men)

    best = None
    tuntas = True

    def nilai(counts, par, opp):
        nonlocal best
        slack = {}
        for k, kind in enumerate(_KINDS):
            s = supply[kind]
            if s == 0:
                if par[k] or opp[k]:
                    return
                continue
            slack[kind] = s - max(par[k], opp[k])
        if any(v < 0 for v in slack.values()):
            return
        # Yang dikejar adalah sumber daya paling mepet, bukan kelegaan
        # rata-rata: satu kolam yang pas-pasan sudah cukup membuat penjadwal
        # mentok.
        longgar = [v / supply[k] for k, v in slack.items()]
        if cap is not None:
            # Ketersediaan tim per bentuk ikut jadi sumber daya, bukan sekadar
            # batas lulus/gagal. Kalau cuma jadi batas, komposisi paling lega
            # menurut kolam pasangan menang walau menuntut TEPAT sebanyak yang
            # bisa disediakan - penjadwalnya lalu kehilangan seluruh ruang
            # gerak untuk meratakan giliran duduk, dan meleset sedikit saja
            # tidak bisa ditebus lagi.
            tim = shape_totals(dict(zip(codes, counts)))
            for kind in _KINDS:
                batas = cap.get(kind, 0)
                if tim[kind] > batas:
                    return
                if batas:
                    longgar.append((batas - tim[kind]) / batas)
        rel = min(longgar, default=1.0)
        key = (rel, sum(slack.values()))
        if best is None or key > best[0]:
            ketat = min(slack, key=lambda k: slack[k] / supply[k]) if slack else None
            best = (
                key,
                dict(zip(codes, counts)),
                {k: par[i] for i, k in enumerate(_KINDS)},
                {k: opp[i] for i, k in enumerate(_KINDS)},
                ketat,
            )

    for e in range(lo, hi + 1):
        if not _walk(codes, matches, men * base + e, supply, True, nilai):
            tuntas = False

    if best is not None:
        _, target, par, opp, ketat = best
        target = {c: n for c, n in target.items() if n}
        return ShapeBudget(True, target, supply, par, opp, ketat, 0)

    if not tuntas:
        # Tidak menemukan bukan berarti tidak ada - jangan mengaku tahu.
        return ShapeBudget(None, None, supply, None, None)

    # Tidak ada komposisi yang muat. Cari yang paling sedikit kekurangannya
    # supaya host dapat angka konkret, bukan sekadar "tidak bisa". Kali ini
    # tanpa pemangkasan suplai - justru kelebihannya yang mau diukur.
    kurang = None

    def ukur(counts, par, opp):
        nonlocal kurang
        # Kolam paling jebol pada komposisi ini menentukan kekurangannya;
        # yang dicari lalu komposisi dengan kekurangan terkecil.
        worst = max(
            (max(par[k], opp[k]) - supply[kind], kind)
            for k, kind in enumerate(_KINDS)
        )
        if kurang is None or worst[0] < kurang[0]:
            kurang = worst

    for e in range(lo, hi + 1):
        if not _walk(codes, matches, men * base + e, supply, False, ukur):
            break

    if kurang is None:
        return ShapeBudget(False, None, supply, None, None)
    return ShapeBudget(False, None, supply, None, None, kurang[1], kurang[0])


# Berapa laki-laki dan perempuan yang dihabiskan satu match dari tiap format.
# Tim LL = 2 laki-laki, LP = 1 + 1, PP = 2 perempuan, dan satu match dua tim.
_KEBUTUHAN: dict[str, tuple[int, int]] = {
    "LL-LL": (4, 0),
    "LL-LP": (3, 1),
    "LL-PP": (2, 2),
    "LP-LP": (2, 2),
    "LP-PP": (1, 3),
    "PP-PP": (0, 4),
}


def _gender_seronde(izin: list[str], men: int, women: int,
                    courts_used: int) -> dict[str, bool] | None:
    """Gender mana yang bisa muncul di satu ronde PENUH yang sah.

    None kalau ronde penuh tidak mungkin sama sekali dengan format ini.

    Yang ditelusuri himpunan total (laki-laki, perempuan) yang terpakai, bukan
    daftar komposisinya: jumlah komposisi tumbuh cepat pada meet besar,
    sedangkan totalnya tidak pernah lebih banyak dari (men+1) x (women+1) - dan
    analyze() dipanggil ulang tiap kali host mengubah satu angka.
    """
    capai: set[tuple[int, int]] = {(0, 0)}
    for _ in range(max(1, courts_used)):
        maju: set[tuple[int, int]] = set()
        for m, w in capai:
            for c in izin:
                dm, dw = _KEBUTUHAN[c]
                if m + dm <= men and w + dw <= women:
                    maju.add((m + dm, w + dw))
        capai = maju
        if not capai:
            return None
    return {"M": any(m > 0 for m, _ in capai),
            "F": any(w > 0 for _, w in capai)}


def gender_tak_terpakai(
    men: int, women: int, allowed_matchups: list[str] | None,
    courts_used: int = 1,
) -> dict[str, dict]:
    """Gender yang tidak muat di komposisi ronde sah mana pun.

    Yang dikembalikan FAKTA TENTANG FORMAT, bukan ramalan tentang jadwalnya, dan
    perbedaannya penting - lihat di bawah. Kalau sebuah gender ada di sini,
    salah satu dari dua hal pasti terjadi: peserta gender itu tidak turun sama
    sekali, atau jadwalnya melanggar format yang dipilih host. Tidak ada
    kemungkinan ketiga, dan keduanya sama-sama layak diberitahukan.

    Pertanyaannya berbeda dari shape_budget(), dan modul itu tidak bisa
    menjawabnya. shape_budget bertanya "cukupkah suplai pasangan untuk sekian
    match"; jawabannya `feasible=False` juga untuk banyak setup yang berjalan
    mulus - 10 laki-laki + 2 perempuan dinilai tidak layak padahal semua peserta
    main dan kualitasnya 96,6. Jadi ia tidak bisa dipakai sebagai tanda bahaya.

    Keadaan yang ditangkap terjadi persis pada roster dengan tepat satu orang
    dari satu gender: satu match yang memuat seorang perempuan di antara para
    laki-laki berkode LL-LP, dan pilihan "sesama bentuk saja" melarangnya,
    sedangkan LP-LP menuntut kedua tim campur - butuh dua perempuan. Diukur, 11
    laki-laki + 1 perempuan di 2 court: yang seorang itu main 0 dari 15 ronde
    sementara yang lain 11 kali, dan tidak ada satu catatan pun yang
    menyebutkannya.

    Kenapa ini BUKAN ramalan "tidak akan main". Diadu dengan 28 jadwal betulan,
    versi yang meramal begitu meleset di 4 kasus - 6L+6P di 1 court, 12L+4P di 3
    court, 8L+1P di 2 court, 20L+1P di 5 court - dan di keempatnya semua peserta
    main. Yang terjadi di situ penjadwal memilih melanggar format daripada
    mendudukkan seseorang semalaman, lalu melaporkannya lewat catatan "match
    memakai format yang Anda larang". Diperiksa satu per satu, dikotominya
    persis: keempat kasus itu punya catatan pelanggaran format, dan kedelapan
    kasus yang memang terdampar tidak punya. Jadi yang dinyatakan di sini
    percabangannya - yang benar di 28 dari 28 - bukan salah satu cabangnya.

    Yang diperiksa harus RONDE PENUH, bukan satu format saja. 6 laki-laki + 6
    perempuan dengan hanya "putra vs putra" lolos uji per-format - 4 dari 6
    laki-laki cukup untuk satu match - tapi dua court menuntut 8 laki-laki
    sekaligus, dan itu tidak ada.

    Kuncinya "M"/"F"; isinya format yang akan menyelamatkannya kalau diizinkan,
    dan berapa peserta lagi yang menyelamatkannya tanpa mengubah format.
    Keduanya diuji dengan penelusuran yang sama, bukan dengan aturan per-format
    - saran yang tidak benar-benar menolong lebih buruk daripada tidak ada.
    """
    izin = [c for c in (allowed_matchups or MATCHUPS) if c in _KEBUTUHAN]
    punya = {"M": men, "F": women}
    terpakai = _gender_seronde(izin, men, women, courts_used)
    if terpakai is None:
        return {}                        # ronde penuh tidak mungkin sama sekali

    out: dict[str, dict] = {}
    for g in ("M", "F"):
        if punya[g] <= 0:
            continue                     # tidak ada orangnya, tidak ada korban
        if terpakai[g]:
            continue                     # gender ini kebagian, aman
        # Format terlarang mana yang menolong kalau host mengizinkannya.
        penolong = []
        for c in MATCHUPS:
            if c in izin:
                continue
            coba = _gender_seronde(izin + [c], men, women, courts_used)
            if coba is not None and coba[g]:
                penolong.append(c)
        # Atau berapa peserta lagi, dengan format apa adanya. Batasnya satu
        # ronde penuh: kalau menambah sebanyak itu pun tidak menolong, yang
        # salah bukan jumlahnya.
        tambah = None
        for k in range(1, 4 * max(1, courts_used) + 1):
            coba = _gender_seronde(
                izin, men + (k if g == "M" else 0),
                women + (k if g == "F" else 0), courts_used)
            if coba is not None and coba[g]:
                tambah = k
                break
        out[g] = {"penolong": penolong, "tambah": tambah}
    return out


def kapasitas_per_kelompok(
    men: int, women: int, courts: int,
    segments: list[tuple[str, int]] | None,
) -> list[dict] | None:
    """Berapa ronde main yang didapat tiap kelompok gender di meet bersegmen.

    None kalau pembagiannya memang tidak ditentukan aturan babak - dan itu
    jawaban yang lebih jujur daripada angka yang dikarang. Lihat di bawah.

    Kenapa perlu. Rata-rata seluruh peserta menggambarkan nol orang begitu ada
    babak putra/putri: 20 putra + 4 putri dengan babak putra/putri/mixed
    dilaporkan "rata-rata main 5,0 ronde", padahal para putra main 3 dan para
    putri 10. Itu satu-satunya angka jatah main yang dilihat host di panel
    analisa, dan ia salah untuk semua orang di ruangan.

    Aritmetikanya, per babak:

      putra / putri  seluruh slot jatuh ke gender itu, dan court yang benar-
                     benar terpakai dibatasi jumlah orangnya: min(court, n // 4)
      mixed          tiap tim satu putra + satu putri, jadi satu match menghabiskan
                     2 putra + 2 putri; court terpakai min(court, putra // 2,
                     putri // 2), dan slotnya terbagi rata

    Diadu dengan jadwal betulan pada tujuh setup - termasuk yang timpang ekstrem
    (20+4, 6+14) dan yang ronde babaknya timpang (8/2/5) - angkanya cocok persis
    di ketujuhnya.

    Babak "open" dan "same_gender" sengaja membuat fungsi ini menyerah. Di sana
    slotnya satu kolam bersama dan siapa yang mengisinya ditentukan optimizer,
    bukan aturan babak; menebaknya lalu menyajikannya sebagai angka pasti akan
    jadi kesalahan yang persis sama dengan yang sedang diperbaiki.
    """
    if not segments or men <= 0 or women <= 0:
        return None
    if any(rule not in ("men", "women", "mixed") for rule, _ in segments):
        return None

    slot = {"M": 0, "P": 0}
    for rule, ronde in segments:
        if rule == "men":
            slot["M"] += ronde * 4 * min(courts, men // 4)
        elif rule == "women":
            slot["P"] += ronde * 4 * min(courts, women // 4)
        else:                                   # mixed
            c = min(courts, men // 2, women // 2)
            slot["M"] += ronde * 2 * c
            slot["P"] += ronde * 2 * c
    return [
        {"label": "putra", "size": men, "plays": round(slot["M"] / men, 1)},
        {"label": "putri", "size": women, "plays": round(slot["P"] / women, 1)},
    ]


def court_terpakai(rule: str, men: int, women: int, n_players: int,
                   courts: int) -> int:
    """Berapa court yang benar-benar bisa terisi di babak beraturan `rule`.

    Court hanya terpakai kalau ada cukup orang YANG BERHAK untuk mengisinya, dan
    aturan babak menentukan siapa mereka. Empat putri cuma cukup untuk satu
    court, berapa pun court yang disewa.
    """
    if rule == "men":
        return min(courts, men // 4)
    if rule == "women":
        return min(courts, women // 4)
    if rule == "mixed":                  # tiap tim 1 putra + 1 putri
        return min(courts, men // 2, women // 2)
    if rule == "same_gender":            # tiap match LL-LL atau PP-PP
        return min(courts, men // 4 + women // 4)
    return min(courts, n_players // 4)   # open


def duduk_per_ronde(
    n_players: int, men: int, women: int, courts: int,
    segments: list[tuple[str, int]] | None,
) -> tuple[int, int] | None:
    """Berapa peserta yang duduk tiap ronde: terkecil dan terbesar lintas babak.

    None kalau tidak ada babak, kalau data gendernya tidak lengkap padahal
    dibutuhkan, atau kalau angkanya sama di semua babak - di ketiganya satu
    angka sudah menggambarkan seluruh acara.

    Kenapa perlu. byes_per_round dihitung sekali untuk seluruh acara, dengan
    court terpakai = min(court, seluruh_peserta // 4). Di meet bersegmen itu
    memberi angka babak yang paling ramai saja: pada 20 putra + 4 putri, babak
    putri hanya bisa mengisi satu court sehingga 20 orang duduk, sementara yang
    dilaporkan 16.

    Melesetnya sedang, bukan parah - diukur pada 8 setup, angka lama selalu
    persis sama dengan yang TERKECIL dan tidak pernah keluar dari rentang
    sebenarnya; selisihnya 5 sampai 7 poin persen di tiga kasus dan nol di
    lima lainnya. Tapi arahnya selalu sama: melaporkan keadaan yang paling
    ramai, dan host memakai angka ini untuk memutuskan berapa court disewa.
    """
    if not segments:
        return None
    perlu_gender = any(r in ("men", "women", "mixed", "same_gender")
                       for r, _ in segments)
    if perlu_gender and men + women != n_players:
        return None
    nilai = [n_players - 4 * court_terpakai(r, men, women, n_players, courts)
             for r, ronde in segments if ronde > 0]
    if not nilai or min(nilai) == max(nilai):
        return None
    return min(nilai), max(nilai)


def analyze(
    n_players: int,
    courts: int,
    duration_minutes: int,
    round_minutes: int = 12,
    warmup_minutes: int = 10,
    rounds_override: int | None = None,
    men: int | None = None,
    women: int | None = None,
    allowed_matchups: list[str] | None = None,
    segments: list[tuple[str, int]] | None = None,
    roster_men: int | None = None,
    roster_women: int | None = None,
) -> CapacityReport:
    """Hitung kapasitas + batas matematis + rekomendasi konkret."""

    issues: list[Issue] = []

    # --- Kapasitas dasar -------------------------------------------------
    # Court hanya terpakai kalau ada 4 orang untuk mengisinya.
    courts_used = min(courts, n_players // 4)
    courts_idle = courts - courts_used
    slots_per_round = 4 * courts_used
    byes_per_round = max(0, n_players - slots_per_round)

    rounds = (
        rounds_override
        if rounds_override is not None
        else rounds_from_duration(duration_minutes, round_minutes, warmup_minutes)
    )

    # Slot yang benar-benar terpakai, dirata-rata tertimbang lintas babak.
    #
    # slots_per_round di atas mengandaikan tiap court bisa diisi siapa saja.
    # Begitu ada babak putra/putri itu tidak benar - empat putri cuma cukup
    # untuk satu court berapa pun yang disewa - dan yang paling mahal akibatnya
    # bukan angka di panel melainkan saran sewa court. Diukur pada 5 meet
    # bersegmen, upgrade_analysis() melebihkan manfaat court tambahan 8 sampai
    # 20 menit, dan pada 20 putra + 4 putri sarannya terbalik: diramal +20 menit
    # sehingga "worth_it", padahal yang benar-benar terjadi +6,7 menit - di
    # bawah ambang 10 menit modul itu sendiri.
    #
    # Ditimbang, bukan dijumlah per babak, supaya rumusnya tetap berlaku berapa
    # pun `rounds` yang dipakai pemanggil: panel meneruskan jumlah ronde babak
    # apa adanya, sedangkan perbandingan biaya menyapu durasi sehingga rondenya
    # dihitung dari lamanya sewa.
    slot_efektif = float(slots_per_round)
    if segments and any(r > 0 for _, r in segments):
        m_, w_ = roster_men or 0, roster_women or 0
        perlu_gender = any(r in ("men", "women", "mixed", "same_gender")
                           for r, ron in segments if ron > 0)
        if not perlu_gender or m_ + w_ == n_players:
            tot_r = sum(ron for _, ron in segments if ron > 0)
            slot_efektif = sum(
                ron * 4 * court_terpakai(rule, m_, w_, n_players, courts)
                for rule, ron in segments if ron > 0
            ) / tot_r

    total_slots = rounds * slot_efektif
    avg_plays = (total_slots / n_players) if n_players else 0.0
    rest_ratio = (byes_per_round / n_players) if n_players else 0.0
    playing_minutes = avg_plays * round_minutes

    # --- Batas matematis keunikan ---------------------------------------
    # Tiap ronde seorang pemain dapat 1 partner dan 2 lawan.
    #   partner unik  -> ronde_main <= N-1
    #   lawan unik    -> 2 * ronde_main <= N-1
    max_partner_rounds = max(0, n_players - 1)
    max_opponent_rounds = max(0, (n_players - 1) // 2)

    # Yang dibandingkan adalah ronde MAIN per pemain, bukan total ronde event.
    # Kalau banyak yang duduk, tiap orang main lebih sedikit -> keunikan justru
    # lebih mudah tercapai. Ini sering disalahpahami.
    effective_rounds = math.ceil(avg_plays)
    partner_ok = effective_rounds <= max_partner_rounds
    opponent_blind_ok = effective_rounds <= max_opponent_rounds

    # Batas di atas buta gender. Kalau host membatasi format match, kolam
    # pasangan pecah tiga dan batas sebenarnya bisa jauh lebih ketat - kadang
    # mustahil justru di setup yang menurut hitungan buta gender aman.
    shape = None
    if men is not None and women is not None and men + women == n_players:
        shape = shape_budget(men, women, rounds * courts_used, allowed_matchups)
    opponent_ok = opponent_blind_ok and (shape is None or shape.feasible is not False)

    # Jatah main per kelompok gender. Dihitung dari aturan babak, bukan dari
    # jadwal - panel ini jalan SEBELUM ada jadwal, dan justru di situ gunanya:
    # host masih bisa mengubah setupnya.
    #
    # Memakai roster_men/roster_women, BUKAN men/women. Kedua pemanggil sengaja
    # mengosongkan men/women begitu ada babak putra atau putri, karena model
    # bentuk tim di atas mengandaikan satu kolam peserta untuk seluruh meet.
    # Perhitungan ini justru cuma berarti kalau kolamnya tidak satu, jadi ia
    # butuh jumlah yang apa adanya. Dipisah alih-alih melonggarkan syarat itu:
    # cabang gender_tak_terpakai juga bergantung padanya, dan melonggarkannya
    # akan membuatnya memperingatkan "putri tidak muat di format" untuk meet
    # yang justru punya babak putri sendiri.
    groups = kapasitas_per_kelompok(roster_men or 0, roster_women or 0,
                                    courts, segments)
    if groups and len({g["plays"] for g in groups}) < 2:
        groups = None                    # semua sama, tidak ada yang perlu dipisah

    rentang_duduk = duduk_per_ronde(n_players, roster_men or 0,
                                    roster_women or 0, courts, segments)
    byes_max = rentang_duduk[1] if rentang_duduk else None

    # --- Rekomendasi -----------------------------------------------------
    ideal_players = 4 * courts
    comfortable_max = int(4 * courts / (1 - COMFORT_REST_RATIO))  # ~5.33 * courts
    courts_zero_rest = math.ceil(n_players / 4)
    courts_comfort = max(1, math.ceil(n_players * (1 - COMFORT_REST_RATIO) / 4))

    # --- Deteksi masalah -------------------------------------------------
    if n_players < 4:
        issues.append(
            Issue(
                "error",
                "Pemain kurang dari 4",
                f"{n_players} pemain tidak cukup untuk satu match padel.",
                "Minimal 4 pemain.",
            )
        )

    if rounds == 0:
        issues.append(
            Issue(
                "error",
                "Durasi tidak cukup untuk satu ronde pun",
                f"Durasi {duration_minutes} menit dikurangi pemanasan "
                f"{warmup_minutes} menit menyisakan "
                f"{max(0, duration_minutes - warmup_minutes)} menit, "
                f"sedangkan satu ronde butuh {round_minutes} menit.",
                "Perpanjang sewa, kurangi pemanasan, atau perpendek durasi ronde.",
            )
        )

    # Peserta yang formatnya sendiri melarang turun. Ini kegagalan yang paling
    # mahal dan paling sunyi di modul ini: jadwalnya tetap jadi, angka-angka
    # lain tetap terlihat wajar, dan yang bersangkutan duduk semalaman. Sengaja
    # TIDAK digabung dengan cabang shape.feasible di bawah - cabang itu
    # dipagari `opponent_blind_ok`, jadi pada roster yang pengulangan lawannya
    # memang wajib ia tidak pernah menyala, dan itu persis roster-roster tempat
    # keadaan ini terjadi.
    if men is not None and women is not None and men + women == n_players:
        for g, d in sorted(gender_tak_terpakai(
                men, women, allowed_matchups, courts_used).items()):
            label = "perempuan" if g == "F" else "laki-laki"
            jumlah = women if g == "F" else men
            daftar = ", ".join(MATCHUP_LABELS.get(c, c).lower()
                               for c in (allowed_matchups or MATCHUPS))
            saran = []
            if d["penolong"]:
                saran.append("izinkan format " + " atau ".join(
                    MATCHUP_LABELS.get(c, c).lower() for c in d["penolong"]))
            if d["tambah"]:
                saran.append(f"ajak {d['tambah']} peserta {label} lagi")
            if not saran:
                saran.append("longgarkan format match")
            obat = ", atau ".join(saran).capitalize() + "."
            issues.append(
                Issue(
                    "error",
                    f"{jumlah} peserta {label} tidak muat di format yang dipilih",
                    # Obatnya ikut ditaruh di detail, bukan cuma di `fix`.
                    # Catatan yang menempel di jadwal dirakit sebagai
                    # "judul: detail" dan membuang `fix` (scheduler.py), jadi
                    # host yang membaca jadwal jadi - bukan panel analisa -
                    # akan tahu keadaannya tanpa tahu jalan keluarnya. Untuk
                    # temuan ini jalan keluarnya justru bagian terpentingnya.
                    f"Format dibatasi ke {daftar}. Dengan {men} laki-laki dan "
                    f"{women} perempuan, tidak ada satu pun susunan ronde yang "
                    f"sah yang memuat peserta {label} - {courts_used} court "
                    f"harus terisi sekaligus. Akibatnya salah satu dari dua "
                    f"hal: mereka duduk sepanjang acara, 0 dari {rounds} "
                    f"ronde, atau jadwalnya melanggar format yang Anda pilih "
                    f"supaya mereka tetap kebagian. Menambah court atau "
                    f"memperpanjang sewa tidak mengubah apa pun; yang "
                    f"menghalangi formatnya, bukan kapasitasnya. {obat}",
                    obat,
                )
            )

    if courts_idle > 0 and n_players >= 4:
        issues.append(
            Issue(
                "warning",
                f"{courts_idle} court menganggur",
                f"{n_players} pemain hanya bisa mengisi {courts_used} court "
                f"(butuh 4 orang per court), padahal kamu sewa {courts}.",
                f"Sewa {courts_used} court saja, atau ajak "
                f"{4 * courts - n_players} pemain lagi.",
            )
        )

    if not opponent_blind_ok and n_players >= 4:
        excess = effective_rounds - max_opponent_rounds
        issues.append(
            Issue(
                "warning",
                "Lawan pasti ada yang berulang",
                f"Dengan {n_players} pemain, lawan 100% unik hanya mungkin sampai "
                f"{max_opponent_rounds} ronde main per orang "
                f"(karena 2 lawan/ronde, maks {n_players - 1} lawan berbeda). "
                f"Jadwal ini memberi ~{effective_rounds} ronde main per orang, "
                f"jadi kelebihan {excess} ronde. Ini batas matematis, bukan "
                f"kelemahan algoritma.",
                "Generator akan menyebar pengulangan serata mungkin. "
                "Kalau mau benar-benar nol: kurangi ronde, perpanjang durasi "
                "tiap ronde, atau tambah pemain.",
            )
        )

    if shape is not None and shape.feasible is False and opponent_blind_ok:
        kolam = KIND_LABELS.get(shape.binding, shape.binding or "")
        tersedia = shape.supply.get(shape.binding, 0)
        butuh = tersedia + shape.shortfall
        daftar = ", ".join(
            MATCHUP_LABELS.get(c, c).lower()
            for c in (allowed_matchups or MATCHUPS)
        )
        # Berapa ronde yang masih muat? Angka konkret jauh lebih berguna
        # daripada "kurangi ronde".
        muat = 0
        for r in range(rounds - 1, 0, -1):
            if shape_budget(men, women, r * courts_used, allowed_matchups).feasible:
                muat = r
                break
        saran = (
            f"Turunkan ke {muat} ronde"
            + (f" (dari {rounds})" if muat else "")
            if muat
            else "Longgarkan format match"
        )
        issues.append(
            Issue(
                "warning",
                "Lawan 100% unik mustahil dengan format yang dibatasi",
                f"Hitungan umum bilang aman ({effective_rounds} ronde main per "
                f"orang, batas {max_opponent_rounds}), tapi itu mengandaikan "
                f"siapa pun boleh melawan siapa pun. Format dibatasi ke "
                f"{daftar}, jadi pasangan {kolam} hanya bisa diambil dari "
                f"kolamnya sendiri: dengan {men} laki-laki dan {women} "
                f"perempuan cuma ada {tersedia} pasangan seperti itu. Susunan "
                f"format apa pun untuk {rounds * courts_used} match butuh "
                f"minimal {butuh} - kelebihan {shape.shortfall}. Ini batas "
                f"suplai, bukan kelemahan algoritma: menaikkan effort tidak "
                f"akan menolong.",
                f"{saran}, izinkan lebih banyak format match, atau ubah "
                f"komposisi peserta (menambah perempuan/laki-laki memperbesar "
                f"kolam yang mepet jauh lebih cepat daripada menambah ronde).",
            )
        )

    if not partner_ok and n_players >= 4:
        issues.append(
            Issue(
                "warning",
                "Partner pasti ada yang berulang",
                f"Dengan {n_players} pemain, tiap orang hanya punya "
                f"{max_partner_rounds} calon partner, tapi jadwal ini memberi "
                f"~{effective_rounds} ronde main per orang.",
                "Kurangi jumlah ronde atau tambah pemain.",
            )
        )

    if byes_per_round > 0:
        if rest_ratio > TOLERABLE_REST_RATIO:
            issues.append(
                Issue(
                    "warning",
                    # Rentangnya disebut kalau babak membuatnya berayun; satu
                    # angka di situ selalu ujung yang paling ramai, dan host
                    # memakai angka ini untuk memutuskan sewa court.
                    (f"{byes_per_round}-{byes_max} orang duduk tiap ronde "
                     f"({rest_ratio * 100:.0f}-"
                     f"{byes_max / n_players * 100:.0f}%)"
                     if byes_max else
                     f"{byes_per_round} orang duduk tiap ronde "
                     f"({rest_ratio * 100:.0f}%)"),
                    # Rata-rata seluruh peserta menyesatkan begitu ada babak
                    # putra/putri: pada 20 putra + 4 putri ia bilang "5,0 ronde"
                    # sementara para putra main 3 dan para putri 10 - angka yang
                    # tidak berlaku bagi satu orang pun di ruangan.
                    (f"Jatah mainnya tidak sama: "
                     + ", ".join(f"{g['size']} {g['label']} main {g['plays']} "
                                 f"ronde" for g in groups)
                     + f" dari {rounds} ronde acara. Selisihnya ditentukan "
                     f"jumlah peserta tiap gender dibanding court dan ronde "
                     f"babaknya, bukan oleh rotasi."
                     if groups else
                     f"Rata-rata tiap peserta main {avg_plays:.1f} dari {rounds} "
                     f"ronde, yaitu ~{playing_minutes:.0f} menit di lapangan dari "
                     f"{duration_minutes} menit sewa."),
                    f"Ini keputusan bisnis, bukan kesalahan setup: {courts} court "
                    f"menekan biaya, {courts_comfort} court menaikkan waktu main "
                    f"tapi menambah sewa. Buka panel Biaya & Margin untuk melihat "
                    f"selisih harga dan fee-nya sebelum memutuskan.",
                )
            )
        else:
            issues.append(
                Issue(
                    "info",
                    f"{byes_per_round} pemain istirahat tiap ronde",
                    f"Rotasi istirahat dibuat merata: tiap orang main "
                    f"~{avg_plays:.1f} dari {rounds} ronde. Generator juga "
                    f"menghindari duduk dua ronde berturut-turut.",
                    "",
                )
            )

    if n_players > 4 * courts and courts_zero_rest > courts:
        issues.append(
            Issue(
                "info",
                "Kapasitas court",
                f"{courts} court menampung {ideal_players} pemain main serentak, "
                f"nyaman sampai ~{comfortable_max} pemain. Kamu punya {n_players}.",
                f"Nol istirahat butuh {courts_zero_rest} court. Kalau itu di luar "
                f"anggaran, setup sekarang tetap sah — generator akan meratakan "
                f"giliran duduk dan menghindari duduk dua ronde beruntun.",
            )
        )

    if playing_minutes and playing_minutes < 40 and n_players >= 4:
        issues.append(
            Issue(
                "warning",
                "Waktu main per orang terasa sedikit",
                f"Tiap peserta hanya dapat ~{playing_minutes:.0f} menit di lapangan "
                f"dari {duration_minutes} menit sewa. Peserta cenderung menilai "
                f"harga dari menit main, bukan dari lama acara.",
                "Cek panel Biaya & Margin: kadang menaikkan fee sedikit untuk "
                "menambah court lebih diterima peserta daripada fee murah "
                "dengan banyak menunggu.",
            )
        )

    severities = {i.severity for i in issues}
    verdict = "error" if "error" in severities else (
        "warning" if "warning" in severities else "ok"
    )

    return CapacityReport(
        n_players=n_players,
        courts=courts,
        courts_used=courts_used,
        courts_idle=courts_idle,
        duration_minutes=duration_minutes,
        round_minutes=round_minutes,
        warmup_minutes=warmup_minutes,
        rounds=rounds,
        slots_per_round=slots_per_round,
        byes_per_round=byes_per_round,
        # Bisa pecahan kalau babaknya mengisi court berbeda-beda; yang
        # dilaporkan tetap bilangan bulat karena ia jumlah slot.
        total_slots=int(round(total_slots)),
        avg_plays_per_player=round(avg_plays, 2),
        rest_ratio=round(rest_ratio, 4),
        playing_minutes_per_player=round(playing_minutes, 1),
        max_unique_partner_rounds=max_partner_rounds,
        max_unique_opponent_rounds=max_opponent_rounds,
        partner_unique_feasible=partner_ok,
        opponent_unique_feasible=opponent_ok,
        ideal_players_for_courts=ideal_players,
        comfortable_max_players=comfortable_max,
        courts_for_zero_rest=courts_zero_rest,
        courts_for_comfort=courts_comfort,
        shape_feasible=None if shape is None else shape.feasible,
        shape_target=None if shape is None else shape.target,
        shape_supply=None if shape is None else shape.supply,
        shape_binding=None if shape is None else shape.binding,
        shape_shortfall=0 if shape is None else shape.shortfall,
        groups=groups,
        byes_per_round_max=byes_max,
        issues=issues,
        verdict=verdict,
    )


def suggest_setup(n_players: int, duration_minutes: int, round_minutes: int = 12,
                  warmup_minutes: int = 10) -> dict:
    """Rekomendasi setup ideal untuk jumlah pemain tertentu.

    Dipakai UI untuk menjawab "saya punya N orang, sebaiknya sewa berapa court
    dan berapa jam?".
    """
    courts_zero = math.ceil(n_players / 4)
    courts_comfort = max(1, math.ceil(n_players * (1 - COMFORT_REST_RATIO) / 4))
    max_unique_opp = max(0, (n_players - 1) // 2)

    # Durasi minimal agar tiap orang main sebanyak batas keunikan lawan.
    slots_needed = max_unique_opp * n_players
    per_round_slots = 4 * min(courts_zero, n_players // 4) or 1
    rounds_needed = math.ceil(slots_needed / per_round_slots) if per_round_slots else 0
    minutes_needed = rounds_needed * round_minutes + warmup_minutes

    return {
        "n_players": n_players,
        "courts_for_zero_rest": courts_zero,
        "courts_for_comfort": courts_comfort,
        "max_unique_opponent_rounds": max_unique_opp,
        "max_unique_partner_rounds": max(0, n_players - 1),
        "rounds_for_full_uniqueness": rounds_needed,
        "minutes_for_full_uniqueness": minutes_needed,
        "rounds_in_given_duration": rounds_from_duration(
            duration_minutes, round_minutes, warmup_minutes
        ),
    }
