"""Mesin penjadwal eksak berbasis CP-SAT (OR-Tools).

Ini ALTERNATIF dari optimizer.anneal(), bukan penggantinya. Mode "americano"
yang lama tidak menyentuh file ini sama sekali; yang memakainya cuma mode
"americano_cpsat".

Bedanya dengan simulated annealing:

  * SA mencari dengan gerakan acak dan berhenti di optimum lokal. Ia cepat dan
    selalu memberi jawaban, tapi tidak pernah bisa mengatakan apakah masih ada
    yang lebih baik.
  * CP-SAT mencari dengan cabang-dan-batas. Ia bisa MEMBUKTIKAN sebuah jadwal
    optimal - atau membuktikan nol pengulangan memang mustahil - tapi waktunya
    tidak bisa diramalkan.

Karena itu keduanya dipakai bersama, bukan bergantian: SELURUH rangkaian yang
sudah ada tetap jalan lebih dulu sampai selesai, dan hasilnya dipakai dua kali -
sebagai HINT supaya solver mulai dari tempat yang bagus, dan sebagai pembanding
di akhir. Kalau hasil solver tidak melampauinya, jadwal lama yang dipertahankan.

Pembanding itulah yang memegang janji "tidak pernah lebih buruk", dan janjinya
hanya sekuat UKURAN yang dipakai membandingkan - lihat parameter `nilai` di
optimize(). Model di file ini tidak memuat semua yang dinilai host.


MODEL

Unit dasarnya PASANGAN, bukan pemain. Untuk tiap ronde didaftar semua pasangan
yang sah menurut Rules (gender babak, partner terkunci, pool rating), lalu:

    y[r][i]      pasangan i turun di ronde r
    pt[r][t][i]  pasangan i main di court t ronde r
    at[r][t][p]  pemain p ada di court t ronde r

dengan tepat 2 pasangan per court, dan tiap pemain paling banyak 1 pasangan per
ronde. Sisanya - siapa lawan siapa, siapa yang duduk - diturunkan dari situ.

Identitas court sebenarnya tidak berarti apa-apa (court 1 dan court 2 bisa
ditukar tanpa mengubah jadwal), dan simetri itu racun untuk solver eksak: ia
menelusuri ulang jadwal yang sama berkali-kali dengan nomor court berbeda.
Karena itu court diurutkan lewat pemain ber-indeks terkecil di tiap court -
lihat `lead` di bawah.


YANG TIDAK DIMODELKAN

Denda `repeat_gap` (match yang persis sama terulang, biayanya 1/jarak ronde)
tidak ikut. Ia butuh variabel per pasang-ronde untuk sesuatu yang perannya cuma
memutus seri, dan ongkos modelnya jauh lebih besar daripada nilainya. Suku itu
tetap ikut terhitung saat hasilnya dinilai ulang lewat ScheduleState, jadi yang
hilang cuma kemampuan CP-SAT untuk sengaja mengejarnya.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .models import matchup_code

# Semua biaya dikalikan ini sebelum jadi bilangan bulat. CP-SAT hanya bekerja
# dengan integer, sementara bobot di Weights bisa pecahan.
SKALA = 100

# Jadwal apa yang dipegang host kalau solver gagal. Bunyinya berbeda menurut
# peran solver: sebagai penyempurna, yang tersisa adalah jadwal utuh dari mesin
# biasa; sebagai mesin dasar, yang tersisa cuma konstruksi awal yang belum
# dioptimasi siapa pun - dan itu keadaan yang jauh lebih perlu diketahui host.
_CADANGAN = {
    False: "Jadwal dari mesin biasa yang dipakai.",
    True: ("Yang dipakai jadwal konstruksi awal, yang belum dioptimasi sama "
           "sekali. Naikkan batas waktu solver, atau pakai mode Americano "
           "biasa yang mengoptimasinya dengan annealing."),
}

# Berapa satuan waktu deterministik yang kira-kira sepadan dengan satu detik
# jam-dinding, DI SATU WORKER. Dipakai hanya kalau host menyalakan
# Config.cpsat_deterministic - lihat blok panjang di bawah soal kenapa satu.
#
# Batas waktu deterministik CP-SAT tidak dihitung dalam detik melainkan dalam
# satuan kerja solver - itu justru intinya, karena detik tidak bisa
# direproduksi. Konsekuensinya angka "batas waktu (detik)" di UI berhenti
# berarti detik, dan yang bisa dilakukan cuma menerjemahkannya sebaik mungkin.
#
# Diukur di mesin pengembangan pada 12 orang / 2 court / 9 ronde dengan satu
# worker: 6 satuan memakan 4,1 detik, 12 satuan 10,7 detik, 24 satuan 22,0
# detik - jadi sekitar 1,1 satuan per detik di dua pengukuran yang lebih besar.
#
# Ini PERKIRAAN dan sengaja disebut begitu: mesin yang lebih lambat memakan lebih
# banyak detik untuk satuan yang sama. Itulah kenapa ada FAKTOR_CADANGAN_DET.
UNIT_DET_PER_DETIK = 1.1

# Batas jam-dinding yang tetap dipasang di mode deterministik, sebagai kelipatan
# dari yang diminta host. Bukan pengganti batas deterministik - penjaga supaya
# mesin yang jauh lebih lambat tidak membuat host menunggu tanpa ujung.
#
# Kalau penjaga ini yang menggigit, hasilnya TIDAK lagi deterministik, dan itu
# harus dikatakan alih-alih dibiarkan jadi janji yang diam-diam batal. Lihat
# Hasil.deterministik.
FAKTOR_CADANGAN_DET = 3.0

# Rating dipetakan ke satuan sepersepuluh (3.5 -> 35). Presisi 0.1 jauh lebih
# halus daripada yang dipakai host mana pun, dan menahan angka tetap kecil.
SKALA_RATING = 10

# Susunan gender satu tim, plus satu kategori untuk gender yang belum diisi.
# "XX" sengaja ada: aturan format match tidak boleh memblokir meet yang data
# gendernya belum lengkap, persis seperti Rules.matchup_ok.
_BENTUK = ("LL", "LP", "PP", "XX")


def tersedia() -> bool:
    """Apakah OR-Tools terpasang di interpreter ini."""
    try:
        from ortools.sat.python import cp_model  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class Hasil:
    """Apa yang benar-benar terjadi di dalam solver.

    Dilaporkan apa adanya ke host. "Optimal" dan "yang terbaik dalam 30 detik"
    adalah dua klaim yang sangat berbeda, dan menyamarkan keduanya jadi "selesai"
    berarti host tidak pernah tahu kapan menaikkan batas waktu itu ada gunanya.
    """

    status: str = "tidak jalan"
    terbukti_optimal: bool = False
    objective: float | None = None
    batas_bawah: float | None = None
    detik: float = 0.0
    # Jadwal solver yang dipakai (bukan jadwal sebelumnya yang dipertahankan).
    dipakai: bool = False
    # Benar-benar LEBIH BAIK dari jadwal sebelumnya, bukan cuma setara.
    membaik: bool = False
    n_variabel: int = 0
    # Apakah pencarian ini benar-benar bisa diulang. Hanya benar kalau host
    # memintanya DAN penjaga jam-dinding tidak menggigit di tengah jalan.
    deterministik: bool = False
    catatan: list[str] = field(default_factory=list)


def _pasangan_sah(st, r: int) -> list[tuple[int, int]]:
    """Semua pasangan yang boleh terbentuk di ronde r.

    Menyaring dengan aturan yang sama persis dengan Rules.quad_ok, tapi hanya
    bagian yang bisa dinilai dari dua orang: kelayakan babak, partner terkunci,
    pool rating, dan komposisi tim. Yang butuh empat orang - format match dan
    babak "sesama gender" - ditegakkan di tingkat court.
    """
    rules = st.rules
    layak = (sorted(rules.round_eligible[r])
             if r < len(rules.round_eligible) else list(range(st.n)))
    rule = rules.round_rule[r] if r < len(rules.round_rule) else "open"
    g = rules.gender
    tier = rules.tier_of

    out: list[tuple[int, int]] = []
    for ii, p in enumerate(layak):
        mate_p = rules.active_mate(p, r) if rules.locked_partner else None
        for q in layak[ii + 1:]:
            if mate_p is not None and mate_p != q:
                continue
            if rules.locked_partner:
                mate_q = rules.active_mate(q, r)
                if mate_q is not None and mate_q != p:
                    continue
            if tier and tier.get(p) != tier.get(q):
                continue
            gp, gq = g.get(p), g.get(q)
            if rule == "mixed":
                if gp is None or gq is None or gp == gq:
                    continue
            elif rule == "same_gender":
                if gp is None or gp != gq:
                    continue
            out.append((p, q))
    return out


def _bentuk(st, pair: tuple[int, int]) -> str:
    g = st.rules.gender
    ga, gb = g.get(pair[0]), g.get(pair[1])
    if ga is None or gb is None:
        return "XX"
    return "LL" if ga == gb == "M" else "PP" if ga == gb == "F" else "LP"


def _kombinasi_bentuk_sah(st, r: int) -> list[tuple[int, int, int, int]]:
    """Cacah bentuk tim per court yang diizinkan, sebagai tuple (LL, LP, PP, XX).

    Tepat 2 pasangan per court, jadi yang mungkin cuma tujuh cacahan. Yang
    memuat "XX" selalu lolos: gender yang belum diisi membuat aturan format
    tidak bisa dinilai, dan dalam keadaan itu jadwal tidak diblokir.
    """
    izin = st.rules.allowed_matchups
    rule = (st.rules.round_rule[r]
            if r < len(st.rules.round_rule) else "open")

    out = []
    for i, a in enumerate(_BENTUK):
        for b in _BENTUK[i:]:
            cacah = [0, 0, 0, 0]
            cacah[_BENTUK.index(a)] += 1
            cacah[_BENTUK.index(b)] += 1
            if cacah[3]:                      # ada gender yang belum diisi
                out.append(tuple(cacah))
                continue
            # Babak "sesama gender": KEEMPAT pemain satu gender, jadi tim putri
            # melawan tim putra ikut ditolak - sama seperti Rules.quad_ok.
            if rule == "same_gender" and not (a == b and a in ("LL", "PP")):
                continue
            if izin and matchup_code(a, b) not in izin:
                continue
            out.append(tuple(cacah))
    return out


def _nilai_bawaan(st) -> tuple:
    """Ukuran cadangan kalau pemanggil tidak menyediakan penilainya sendiri.

    Sengaja bukan yang dipakai scheduler - lihat catatan pada parameter `nilai`
    di optimize() untuk kenapa ukuran ini saja tidak cukup.
    """
    return (st.rep_pc, st.rep_oc, st.cost())


def optimize(st, courts_r: list[int], *,
             time_limit: float = 30.0,
             workers: int = 8,
             nilai=None,
             progress=None,
             beku=None,
             dasar: bool = False,
             deterministic: bool = False,
             seed: int = 0) -> Hasil:
    """Cari jadwal terbaik untuk `st`, lalu tulis hasilnya kembali ke `st`.

    `st` harus SUDAH berisi jadwal layak. Jadwal itu dipakai dua kali: sebagai
    hint supaya solver mulai dari tempat yang bagus, dan sebagai pembanding di
    akhir - kalau hasil solver tidak melampauinya, jadwal lama yang dipertahankan.

    `nilai(st)` mengembalikan kunci pembanding (makin kecil makin baik), dan
    HARUS ukuran yang sama dengan yang dipakai memilih jadwal di tempat lain.

    Itu bukan formalitas. Model di sini tidak memuat "giliran" - berapa kali
    antrean main diserobot, dan seberapa jauh tunggu terpanjang melewati yang
    tak terhindarkan - sementara skor kualitas yang dilihat host memuatnya.
    Dengan pembanding bawaan (yang juga buta giliran), solver bisa menurunkan
    biaya modelnya sendiri sambil merusak giliran, dan hasilnya tetap diterima:
    diukur pada 26 orang / 4 court, kualitas turun 96,8 -> 96,5 padahal partner
    dan lawan sama-sama sudah nol. Jadi janji "tidak pernah lebih buruk" hanya
    berlaku sejauh ukuran yang dipakai di sini.

    `courts_r` cuma dipakai untuk melaporkan; jumlah court yang dipakai model
    diambil dari jadwal yang sudah ada, supaya jumlah slot main tidak berubah
    diam-diam dari yang sudah disepakati tahap sebelumnya.

    `dasar=True` menjalankan solver sebagai MESIN DASAR, bukan penyempurna:
    hint tidak dipasang sama sekali, jadi solver mencari dari nol dan jadwal
    yang keluar benar-benar hasil pencariannya sendiri - bukan jadwal annealing
    yang dipungut ulang. Yang TIDAK ikut dilepas adalah pembandingnya: `st`
    tetap harus berisi jadwal layak (hasil konstruksi awal), dan kalau solver
    tidak sanggup melampauinya dalam batas waktu, jadwal itulah yang
    dipertahankan. Tanpa jaring itu batas waktu yang terlalu pendek berarti host
    memegang jadwal yang lebih buruk daripada yang sudah ada di tangan.

    `deterministic=True` menukar perlombaan antar worker dengan pembagian giliran
    yang tertib, dan `time_limit` dibaca sebagai satuan kerja solver alih-alih
    detik - dua-duanya syarat supaya jalan yang sama memberi jadwal yang sama.
    `seed` dipakai sebagai benih solver di mode itu, jadi mengganti seed host
    tetap memberi variasi lain yang sama-sama bisa diulang. Ongkos mutunya nyata;
    lihat Config.cpsat_deterministic.
    """
    nilai = nilai or _nilai_bawaan
    hasil = Hasil()
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        hasil.status = "OR-Tools tidak terpasang"
        hasil.catatan.append(
            "Mode CP-SAT butuh OR-Tools, dan paket itu tidak ada di Python "
            "yang menjalankan aplikasi ini. Jadwal disusun dengan mesin biasa."
        )
        return hasil

    mulai = time.perf_counter()
    R, n, w = st.n_rounds, st.n, st.w

    # Berapa court yang benar-benar terisi tiap ronde, dibaca dari jadwal yang
    # ada - bukan dari courts_r. Kalau pesertanya kurang dari 4 x court, tahap
    # konstruksi sudah memutuskan berapa court yang realistis, dan model ini
    # tidak berhak menganulirnya.
    meja = [len(st.matches[r]) for r in range(R)]
    if not any(meja):
        hasil.status = "tidak ada match untuk dioptimasi"
        return hasil

    m = cp_model.CpModel()

    # --- Variabel ---------------------------------------------------------
    pairs: list[list[tuple[int, int]]] = []
    idx_pair: list[dict[tuple[int, int], int]] = []
    y: list[list] = []          # y[r][i]        pasangan i turun di ronde r
    pt: list[list[list]] = []   # pt[r][t][i]    pasangan i di court t
    at: list[list[list]] = []   # at[r][t][p]    pemain p di court t

    for r in range(R):
        ps = _pasangan_sah(st, r) if meja[r] else []
        pairs.append(ps)
        idx_pair.append({pr: i for i, pr in enumerate(ps)})
        y.append([m.new_bool_var(f"y{r}_{i}") for i in range(len(ps))])
        pt.append([[m.new_bool_var(f"pt{r}_{t}_{i}") for i in range(len(ps))]
                   for t in range(meja[r])])
        at.append([[m.new_bool_var(f"at{r}_{t}_{p}") for p in range(n)]
                   for t in range(meja[r])])

    for r in range(R):
        if not meja[r]:
            continue
        # Satu pasangan turun paling banyak di satu court.
        for i in range(len(pairs[r])):
            m.add(sum(pt[r][t][i] for t in range(meja[r])) == y[r][i])
        # Tiap court berisi tepat dua tim.
        for t in range(meja[r]):
            m.add(sum(pt[r][t]) == 2)
        # Tiap pemain paling banyak satu pasangan per ronde.
        milik: list[list[int]] = [[] for _ in range(n)]
        for i, (a, b) in enumerate(pairs[r]):
            milik[a].append(i)
            milik[b].append(i)
        for p in range(n):
            if milik[p]:
                m.add(sum(y[r][i] for i in milik[p]) <= 1)
        # at[] diturunkan dari pt[]: pemain ada di court t kalau salah satu
        # pasangan yang memuatnya ada di court t.
        for t in range(meja[r]):
            for p in range(n):
                if milik[p]:
                    m.add(at[r][t][p] == sum(pt[r][t][i] for i in milik[p]))
                else:
                    m.add(at[r][t][p] == 0)

        # Komposisi tim per court: format match yang dilarang host, dan babak
        # "sesama gender" yang menuntut keempat pemain satu gender.
        sah = _kombinasi_bentuk_sah(st, r)
        if len(sah) < 10:  # 10 = semua kombinasi mungkin, jadi tidak membatasi
            bentuk_i = [_BENTUK.index(_bentuk(st, pr)) for pr in pairs[r]]
            for t in range(meja[r]):
                cacah = []
                for s in range(4):
                    v = m.new_int_var(0, 2, f"bentuk{r}_{t}_{s}")
                    m.add(v == sum(pt[r][t][i] for i in range(len(pairs[r]))
                                   if bentuk_i[i] == s))
                    cacah.append(v)
                m.add_allowed_assignments(cacah, sah)

        # Pool rating: satu court wajib satu pool.
        if st.rules.tier_of:
            tiers = sorted(set(st.rules.tier_of.values()))
            if len(tiers) > 1:
                tier_pair = [st.rules.tier_of.get(pr[0]) for pr in pairs[r]]
                for t in range(meja[r]):
                    for k in tiers:
                        anggota = [pt[r][t][i] for i in range(len(pairs[r]))
                                   if tier_pair[i] == k]
                        if anggota:
                            v = m.new_int_var(0, 2, f"pool{r}_{t}_{k}")
                            m.add(v == sum(anggota))
                            m.add_allowed_assignments([v], [(0,), (2,)])

        # Pemutus simetri: court tidak punya identitas, jadi tanpa ini solver
        # menelusuri jadwal yang sama berulang kali dengan nomor court ditukar.
        # Court diurutkan lewat pemain ber-indeks terkecil di dalamnya.
        if meja[r] > 1:
            lead = []
            for t in range(meja[r]):
                indeks = []
                for p in range(n):
                    v = m.new_int_var(0, n, f"lead{r}_{t}_{p}")
                    m.add(v == p).only_enforce_if(at[r][t][p])
                    m.add(v == n).only_enforce_if(at[r][t][p].negated())
                    indeks.append(v)
                lo = m.new_int_var(0, n, f"min{r}_{t}")
                m.add_min_equality(lo, indeks)
                lead.append(lo)
            for t in range(meja[r] - 1):
                m.add(lead[t] < lead[t + 1])

    # --- Ronde yang dipaku (dipakai penyempurnaan jendela) ----------------
    # `beku` berisi ronde yang isinya TIDAK boleh diubah solver. Dengan itu satu
    # panggilan optimize() jadi menyelesaikan submasalah 3 ronde alih-alih
    # seluruh jadwal, dan submasalah itu selalu kecil - di situlah solver eksak
    # kuat, sementara model utuh mati di 12 ronde ke atas.
    #
    # Court diurutkan naik menurut pemain ber-indeks terkecil oleh pemutus
    # simetri di atas, jadi quad yang ada harus dipasang ke court dalam urutan
    # yang SAMA. Kalau tidak, pakunya bertentangan dengan pemutus simetri dan
    # modelnya jadi mustahil - bukan cuma lambat, dan kegagalannya tidak
    # terlihat sebagai kesalahan pemrograman.
    if beku:
        for r in sorted(beku):
            if not meja[r]:
                continue
            quads = sorted((list(q) for q in st.matches[r]), key=lambda q: min(q))
            if len(quads) != meja[r]:
                raise ValueError(
                    f"Ronde {r + 1} punya {len(quads)} match tapi {meja[r]} court.")
            aktif = set()
            for t, q in enumerate(quads):
                for a, b in ((q[0], q[1]), (q[2], q[3])):
                    pr = (min(a, b), max(a, b))
                    i = idx_pair[r].get(pr)
                    if i is None:
                        raise ValueError(
                            f"Ronde {r + 1}: pasangan yang sedang dipakai tidak "
                            f"ada di daftar pasangan sah.")
                    m.add(pt[r][t][i] == 1)
                    aktif.add(i)
            for i in range(len(pairs[r])):
                if i not in aktif:
                    m.add(y[r][i] == 0)

    # --- Pertemuan antar pemain -------------------------------------------
    # partner[r][{p,q}] : jadi satu tim.  lawan[r][{p,q}] : berhadapan.
    # Keduanya dipakai untuk menghitung berapa kali sepasang orang bertemu
    # sepanjang acara - inti dari seluruh fungsi biaya.
    partner_r: list[dict[tuple[int, int], object]] = []
    lawan_r: list[dict[tuple[int, int], object]] = []
    for r in range(R):
        pmap: dict[tuple[int, int], object] = {}
        lmap: dict[tuple[int, int], object] = {}
        if meja[r]:
            for i, pr in enumerate(pairs[r]):
                pmap[pr] = y[r][i]
            # Sepasang orang berhadapan kalau mereka di court yang sama tapi
            # bukan satu tim.
            hadir = sorted({p for pr in pairs[r] for p in pr})
            se: list[list] = [[] for _ in range(meja[r])]
            for ii, p in enumerate(hadir):
                for q in hadir[ii + 1:]:
                    sekutu = []
                    for t in range(meja[r]):
                        b = m.new_bool_var(f"ct{r}_{t}_{p}_{q}")
                        m.add_bool_and([at[r][t][p], at[r][t][q]]
                                       ).only_enforce_if(b)
                        m.add_bool_or([at[r][t][p].negated(),
                                       at[r][t][q].negated()]
                                      ).only_enforce_if(b.negated())
                        sekutu.append(b)
                        se[t].append(b)
                    lv = m.new_bool_var(f"lw{r}_{p}_{q}")
                    tim = pmap.get((p, q))
                    if tim is None:
                        m.add(lv == sum(sekutu))
                    else:
                        m.add(lv == sum(sekutu) - tim)
                    lmap[(p, q)] = lv

            # Batasan berlebih, dan justru inilah yang membuat mode ini ada
            # gunanya. Empat orang di satu court berarti TEPAT enam pertemuan -
            # fakta yang jelas bagi manusia tapi tidak terbaca oleh relaksasi
            # linear, karena "p dan q satu court" dilinearkan jadi
            # b >= at_p + at_q - 1 dan itu nol begitu keduanya pecahan.
            #
            # Tanpa baris ini batas bawah pertemuan runtuh ke nol, dan solver
            # tidak pernah bisa membuktikan apa pun: pada 8 orang / 2 court / 8
            # ronde ia menemukan optimum (18.560) dalam sedetik lalu berhenti di
            # celah 43% setelah 60 detik penuh. Dengan baris ini celahnya
            # tertutup - jawaban yang sama, tapi kali ini TERBUKTI.
            for t in range(meja[r]):
                if se[t]:
                    m.add(sum(se[t]) == 6)
            # Turunannya untuk seluruh ronde: tiap court menyumbang 2 tim dan 4
            # pasang yang berhadapan.
            if lmap:
                m.add(sum(lmap.values()) == 4 * meja[r])
            if pmap:
                m.add(sum(pmap.values()) == 2 * meja[r])
        partner_r.append(pmap)
        lawan_r.append(lmap)

    # --- Fungsi biaya ------------------------------------------------------
    biaya = []

    def tangga(cacah, batas: int, f, nama: str) -> list:
        """Biaya konveks f(cacah) sebagai deret indikator berantai.

        f dipecah jadi kenaikannya - f(c) = SUM_k (f(k) - f(k-1)) x [c >= k] -
        lalu indikator [c >= k] dipasang BUKAN sebagai reifikasi melainkan
        sebagai dekomposisi: cacahnya sendiri didefinisikan sebagai jumlah
        indikatornya, dengan rantai menurun ge[k] >= ge[k+1].

        Bedanya besar, dan bukan soal gaya. Dua bentuk sebelumnya memberi
        JAWABAN yang sama persis tapi BATAS BAWAH yang jauh lebih longgar,
        karena keduanya dilinearkan lewat big-M:

            add_element pada tabel [0, 0, 2b, 6b, ...]
            reifikasi "c >= k" lewat only_enforce_if

        Diukur pada 8 orang / 2 court / 8 ronde - kasus terkecil yang ada -
        keduanya MENEMUKAN optimum (18.560) dalam sedetik lalu menghabiskan 60
        detik penuh tanpa pernah bisa membuktikannya, mentok di celah 43%.
        Bentuk berantai ini relaksasi linearnya persis menyinggung f, jadi
        batas bawahnya langsung bertemu dengan solusinya.

        Dan itu bukan perbaikan kosmetik: "terbukti optimal" adalah
        satu-satunya hal yang bisa diberikan mode ini dan tidak bisa diberikan
        annealing. Tanpanya, seluruh mode ini cuma annealing yang lebih lambat.
        """
        naik = [f(k) - f(k - 1) for k in range(1, batas + 1)]
        ge = [m.new_bool_var(f"{nama}_ge{k}") for k in range(1, batas + 1)]
        m.add(sum(ge) == cacah)
        for k in range(len(ge) - 1):
            m.add(ge[k] >= ge[k + 1])
        return [n * g for n, g in zip(naik, ge) if n]

    # Pengulangan partner & lawan.
    for peta, bobot, cap in ((partner_r, w.partner, 0.0),
                             (lawan_r, w.opponent, w.opponent_cap)):
        if bobot == 0.0 and cap == 0.0:
            continue
        semua: dict[tuple[int, int], list] = {}
        for r in range(R):
            for key, v in peta[r].items():
                semua.setdefault(key, []).append(v)
        wb, wc = round(bobot * SKALA), round(cap * SKALA)
        # Denda sekali-bayar begitu sepasang orang bertemu untuk KEDUA kalinya
        # menyatu rapi ke dalam deret yang sama: ia persis kenaikan di k = 2.
        # Bentuk c*(c-1) sendiri justru paling murah di pengulangan pertama,
        # dan ini menambal persis lubang itu.
        def f(k: int, wb=wb, wc=wc) -> int:
            return wb * k * (k - 1) + (wc if k >= 2 else 0)

        for key, vs in semua.items():
            biaya.extend(tangga(sum(vs), len(vs), f, f"p{key[0]}_{key[1]}"))

    # Istirahat: main[p][r] dipakai ulang oleh tiga suku di bawah.
    main = [[None] * R for _ in range(n)]
    for p in range(n):
        for r in range(R):
            milik = [y[r][i] for i, pr in enumerate(pairs[r]) if p in pr]
            if milik:
                v = m.new_bool_var(f"main{p}_{r}")
                m.add(v == sum(milik))
                main[p][r] = v
            else:
                main[p][r] = None       # tidak mungkin turun di ronde ini

    if w.bye:
        wbye = round(w.bye * SKALA)
        for p in range(n):
            turun = [v for v in main[p] if v is not None]
            biaya.extend(tangga(R - sum(turun), R,
                                lambda k: wbye * k * k, f"bye{p}"))

    # Duduk dua ronde beruntun. Hanya dihitung kalau pemainnya memang BERHAK
    # turun di kedua ronde: duduk di babak yang bukan babaknya tidak bisa
    # dihindari gerakan apa pun, jadi mendendanya cuma menambah tetapan.
    if w.b2b_bye:
        for p in range(n):
            for r in range(R - 1):
                if not (st.rules.eligible_at(r, p)
                        and st.rules.eligible_at(r + 1, p)):
                    continue
                a, b = main[p][r], main[p][r + 1]
                if a is None and b is None:
                    continue
                v = m.new_bool_var(f"b2b{p}_{r}")
                sisi = [x.negated() for x in (a, b) if x is not None]
                m.add_bool_and(sisi).only_enforce_if(v)
                m.add_bool_or([x.negated() for x in sisi]
                              ).only_enforce_if(v.negated())
                biaya.append(round(w.b2b_bye * SKALA) * v)

    # Rentetan duduk yang MENUMPUK, bentuk L*(L-1) per rentetan. Identitasnya:
    # SUM L*(L-1) = 2 x #{(i,j), i<j, seluruh ronde i..j duduk}. Jadi cukup satu
    # bool per jendela, dirantai supaya murah.
    if w.long_wait:
        koef = 2 * round(w.long_wait * SKALA)
        for p in range(n):
            for i in range(R - 1):
                jendela = None
                for j in range(i + 1, R):
                    v = m.new_bool_var(f"tunggu{p}_{i}_{j}")
                    isi = []
                    if jendela is not None:
                        isi.append(jendela)
                    else:
                        if main[p][i] is not None:
                            isi.append(main[p][i].negated())
                    if main[p][j] is not None:
                        isi.append(main[p][j].negated())
                    if isi:
                        m.add_bool_and(isi).only_enforce_if(v)
                        m.add_bool_or([x.negated() for x in isi]
                                      ).only_enforce_if(v.negated())
                    else:
                        m.add(v == 1)
                    biaya.append(koef * v)
                    jendela = v

    # Keseimbangan rating antar tim dalam satu court, dan sebaran rating di
    # dalam court. Dua-duanya nol di mode americano, jadi modelnya tidak ikut
    # dibangun kecuali host memakai bobot yang menyalakannya.
    if w.rating or w.spread:
        skala_r = [round(x * SKALA_RATING) for x in st.ratings]
        rmin, rmax = min(skala_r), max(skala_r)
        for r in range(R):
            for t in range(meja[r]):
                if w.rating:
                    jum = [skala_r[a] + skala_r[b] for a, b in pairs[r]]
                    hi = m.new_int_var(2 * rmin, 2 * rmax, f"rhi{r}_{t}")
                    lo = m.new_int_var(2 * rmin, 2 * rmax, f"rlo{r}_{t}")
                    for i in range(len(pairs[r])):
                        m.add(hi >= jum[i]).only_enforce_if(pt[r][t][i])
                        m.add(lo <= jum[i]).only_enforce_if(pt[r][t][i])
                    biaya.append(round(w.rating * SKALA_RATING) * (hi - lo))
                if w.spread:
                    hi = m.new_int_var(rmin, rmax, f"shi{r}_{t}")
                    lo = m.new_int_var(rmin, rmax, f"slo{r}_{t}")
                    for p in range(n):
                        m.add(hi >= skala_r[p]).only_enforce_if(at[r][t][p])
                        m.add(lo <= skala_r[p]).only_enforce_if(at[r][t][p])
                    ambang = round(w.spread_threshold * SKALA_RATING)
                    lebih = m.new_int_var(0, rmax - rmin, f"sov{r}_{t}")
                    m.add(lebih >= hi - lo - ambang)
                    kuadrat = m.new_int_var(0, (rmax - rmin) ** 2, f"sq{r}_{t}")
                    m.add_multiplication_equality(kuadrat, [lebih, lebih])
                    biaya.append(round(w.spread) * kuadrat)

    # Permintaan komposisi court dari peserta. Lunak: kalau tidak bisa dipenuhi,
    # jadwal tetap jadi dan pelanggarannya masuk laporan.
    if st.rules.court_pref and w.preference:
        biaya.extend(_biaya_preferensi(m, st, pairs, y, at, meja, main))

    m.minimize(sum(biaya))

    # --- Hint dari jadwal yang sudah ada ----------------------------------
    # Tanpa ini solver mulai dari nol dan sering menghabiskan seluruh batas
    # waktu cuma untuk mencapai mutu yang sudah dipegang konstruksi greedy.
    #
    # Justru itu yang diminta mode "solver sebagai dasar": di sana jadwal harus
    # benar-benar keluar dari pencarian solver, jadi hint-nya sengaja tidak
    # dipasang. Ongkosnya nyata dan sudah diukur - lihat blok "Solver eksak
    # sebagai mesin dasar" di scheduler.py.
    if not dasar:
        _pasang_hint(m, st, pairs, idx_pair, y, pt, at, meja)
    else:
        # Fakta tentang CARA solver dijalankan, bukan klaim tentang hasilnya:
        # hasilnya belum diketahui di titik ini, dan solver masih bisa kalah dari
        # konstruksi awal. Yang menilai hasil adalah catatan di scheduler.py.
        hasil.catatan.append(
            "Solver dijalankan sebagai mesin dasar: tanpa hint dari annealing, "
            "jadi pencariannya mulai dari nol."
        )

    # --- Jalankan ---------------------------------------------------------
    solver = cp_model.CpSolver()
    if deterministic:
        # SATU worker, dan itu keputusan yang sudah diukur dua kali.
        #
        # Yang dicoba lebih dulu adalah cara yang dianjurkan dokumentasi
        # OR-Tools: delapan worker dengan `interleave_search` - worker berbagi
        # giliran alih-alih berlomba - plus batas waktu deterministik. Cara itu
        # jauh lebih cepat (sekitar 10 detik lawan 22 detik untuk anggaran yang
        # sama) dan HAMPIR selalu memberi jadwal yang sama. "Hampir" itulah
        # masalahnya: pada empat jalan berturut-turut dengan input yang sama
        # persis, satu jalan mendarat di objective 61.200 sementara tiga lainnya
        # di 61.120. Sapuan berikutnya di mesin yang lebih senggang memberi empat
        # dari empat sama, jadi penyimpangannya bergantung beban mesin - persis
        # sifat yang membuat janji "bisa diulang" tidak bisa dipegang.
        #
        # Sakelar yang kadang-kadang berhasil lebih buruk daripada tidak ada
        # sakelar: host memakai seed dari laporan, mendapat jadwal lain, lalu
        # menyimpulkan seed-nya salah dicatat. Dengan satu worker tidak ada
        # perlombaan maupun pembagian giliran antar thread, jadi determinismenya
        # datang dari algoritmanya - bukan dari mesin yang kebetulan senggang.
        #
        # `workers` yang diminta pemanggil sengaja diabaikan di sini, dan itu
        # dilaporkan ke host lewat catatan di bawah.
        solver.parameters.num_workers = 1
        solver.parameters.interleave_search = True
        solver.parameters.max_deterministic_time = (
            max(1.0, float(time_limit)) * UNIT_DET_PER_DETIK)
        solver.parameters.random_seed = int(seed)
        # Penjaga jam-dinding, bukan batas yang diharapkan menggigit. Kalau ia
        # menggigit, hasilnya tidak deterministik lagi - diperiksa di bawah.
        batas_cadangan = max(1.0, float(time_limit)) * FAKTOR_CADANGAN_DET
        solver.parameters.max_time_in_seconds = batas_cadangan
        if int(workers) > 1:
            # Host memilih 8 thread lalu mendapat 1. Kalau itu tidak disebut,
            # yang ia lihat cuma solver yang tiba-tiba menemukan lebih sedikit,
            # dan ia akan mencari sebabnya di setupnya sendiri.
            hasil.catatan.append(
                f"Sakelar 'hasil bisa diulang' menyala, jadi solver dijalankan "
                f"di 1 thread alih-alih {int(workers)}: perlombaan antar thread "
                f"itulah yang membuat hasilnya tidak bisa diulang. Solver "
                f"memeriksa lebih sedikit kemungkinan dalam waktu yang sama."
            )
    else:
        batas_cadangan = None
        solver.parameters.num_workers = max(1, int(workers))
        solver.parameters.max_time_in_seconds = max(1.0, float(time_limit))
    # Hint yang tidak layak DIBUANG DIAM-DIAM oleh CP-SAT. Tidak ada peringatan,
    # tidak ada status berbeda - solver cuma mulai dari nol, dan seluruh
    # keunggulan mode ini hilang tanpa jejak.
    #
    # Itu sudah pernah terjadi di sini: pemutus simetri menuntut court terurut
    # menurut pemain ber-indeks terkecil, sementara jadwal dari annealing tidak
    # tahu apa-apa soal urutan itu, jadi setiap hint ditolak. Gejalanya cuma
    # "hasilnya jelek", yang bisa berarti seratus hal lain. Perbaikannya ada di
    # _pasang_hint; yang di bawah ini cara memeriksanya lagi kalau suatu saat
    # ada batasan baru ditambahkan ke model:
    #
    #     solver.parameters.debug_crash_on_bad_hint = True
    #
    # Dibiarkan mati di jalur normal karena efeknya membunuh proses, dan host
    # tidak boleh kehilangan jadwalnya gara-gara sebuah hint.
    solver.parameters.debug_crash_on_bad_hint = False

    cb = _pelapor(progress) if progress else None
    status = solver.solve(m, cb) if cb else solver.solve(m)

    hasil.detik = time.perf_counter() - mulai
    hasil.n_variabel = len(m.proto.variables)
    hasil.terbukti_optimal = status == cp_model.OPTIMAL

    # Janji "bisa diulang" hanya berlaku kalau yang menghentikan pencarian adalah
    # batas deterministiknya, bukan penjaga jam-dinding. Kalau penjaga yang
    # menggigit, mesin ini terlalu lambat untuk anggaran yang diminta, dan
    # diamnya soal itu berarti host mengulang lalu mendapat jadwal lain tanpa
    # tahu kenapa.
    if deterministic:
        hasil.deterministik = (batas_cadangan is None
                               or solver.wall_time < batas_cadangan * 0.98)
        if not hasil.deterministik:
            hasil.catatan.append(
                f"Hasil solver ini TIDAK bisa diulang walau sakelarnya menyala: "
                f"pencariannya dihentikan penjaga waktu di "
                f"{solver.wall_time:.1f} detik sebelum anggaran deterministiknya "
                f"habis. Turunkan batas waktu solver supaya anggaran itu selesai "
                f"di dalam penjaganya."
            )

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        hasil.status = {
            cp_model.INFEASIBLE: "mustahil",
            cp_model.MODEL_INVALID: "model tidak sah",
        }.get(status, "tidak ketemu solusi")
        hasil.catatan.append(
            f"CP-SAT tidak menemukan jadwal dalam {hasil.detik:.1f} detik "
            f"({hasil.status}). {_CADANGAN[dasar]}"
        )
        return hasil

    hasil.objective = solver.objective_value / SKALA
    hasil.batas_bawah = solver.best_objective_bound / SKALA
    hasil.status = "optimal" if hasil.terbukti_optimal else "terbaik sejauh ini"

    # --- Tulis balik -------------------------------------------------------
    lama = st.snapshot()
    nilai_lama = nilai(st)

    baru: list[list[list[int]]] = []
    for r in range(R):
        rnd = []
        for t in range(meja[r]):
            tim = [pairs[r][i] for i in range(len(pairs[r]))
                   if solver.boolean_value(pt[r][t][i])]
            if len(tim) != 2:
                baru = []
                break
            rnd.append([tim[0][0], tim[0][1], tim[1][0], tim[1][1]])
        if not rnd and meja[r]:
            baru = []
            break
        baru.append(rnd)

    if not baru:
        st.restore(lama)
        hasil.status = "solusi tidak utuh"
        hasil.catatan.append(
            "Solusi CP-SAT tidak bisa dibaca kembali jadi jadwal utuh. "
            + _CADANGAN[dasar]
        )
        return hasil

    st.restore(([[] for _ in range(R)], [set() for _ in range(R)]))
    for r in range(R):
        turun = {p for q in baru[r] for p in q}
        # Yang belum datang / sudah pulang bukan "istirahat" - lihat
        # Rules.round_present. Tanpa fitur kehadiran sebagian, hadir_at()
        # mengembalikan seluruh peserta dan baris ini persis seperti sebelumnya.
        st.place_round(r, baru[r], sorted(st.rules.hadir_at(r, n) - turun))

    # CP-SAT meminimalkan MODELNYA, dan model itu tidak memuat semua yang dinilai
    # host (repeat_gap dan giliran di luar jangkauannya). Jadi hasilnya wajib
    # dibandingkan ulang dengan ukuran yang sebenarnya dipakai menilai jadwal -
    # tanpa langkah ini solver bisa "menang" menurut dirinya sendiri sambil
    # menyerahkan jadwal yang lebih buruk.
    nilai_baru = nilai(st)
    if nilai_baru <= nilai_lama:
        hasil.dipakai = True
        hasil.membaik = nilai_baru < nilai_lama
    else:
        st.restore(lama)
    return hasil


def _biaya_preferensi(m, st, pairs, y, at, meja, main) -> list:
    """Denda untuk permintaan komposisi court yang tidak terpenuhi.

    Menirukan Rules.pref_violations: satu denda per peserta per ronde, bukan
    per court, jadi angkanya sebanding dengan yang dilaporkan ke host.
    """
    w = round(st.w.preference * SKALA)
    g = st.rules.gender
    out = []

    # "Court ini semua perempuan" / "semua laki-laki", dihitung sekali per court
    # dan dipakai bersama oleh semua peserta yang memintanya.
    semua_f: dict[tuple[int, int], object] = {}
    semua_m: dict[tuple[int, int], object] = {}

    def penuh(r: int, t: int, gender: str):
        simpan = semua_f if gender == "F" else semua_m
        if (r, t) not in simpan:
            anggota = [at[r][t][q] for q in range(st.n) if g.get(q) == gender]
            v = m.new_bool_var(f"penuh{gender}{r}_{t}")
            if len(anggota) < 4:
                m.add(v == 0)               # tidak akan pernah terisi empat
            else:
                c = m.new_int_var(0, 4, f"cnt{gender}{r}_{t}")
                m.add(c == sum(anggota))
                m.add(c == 4).only_enforce_if(v)
                m.add(c <= 3).only_enforce_if(v.negated())
            simpan[(r, t)] = v
        return simpan[(r, t)]

    for p, pref in sorted(st.rules.court_pref.items()):
        for r in range(len(meja)):
            if not meja[r] or main[p][r] is None:
                continue

            if pref == "mixed_team":
                # Cukup dilihat dari pasangannya sendiri; siapa lagi yang ada di
                # court itu tidak berpengaruh.
                buruk = [y[r][i] for i, pr in enumerate(pairs[r])
                         if p in pr
                         and (g.get(pr[0]) is None or g.get(pr[1]) is None
                              or g[pr[0]] == g[pr[1]])]
                if buruk:
                    v = m.new_bool_var(f"pref{p}_{r}")
                    m.add(v == sum(buruk))
                    out.append(w * v)
                continue

            # Sisanya soal komposisi court. Peserta terlanggar kalau ia MAIN di
            # court yang komposisinya bukan yang dimintanya.
            kena = []
            for t in range(meja[r]):
                if pref == "women_only":
                    ok = [penuh(r, t, "F")]
                elif pref == "men_only":
                    ok = [penuh(r, t, "M")]
                else:                        # same_gender: satu gender apa pun
                    ok = [penuh(r, t, "F"), penuh(r, t, "M")]
                v = m.new_bool_var(f"kena{p}_{r}_{t}")
                # v <=> p di court t DAN tak satu pun syarat komposisi terpenuhi
                m.add_bool_and([at[r][t][p]] + [x.negated() for x in ok]
                               ).only_enforce_if(v)
                m.add_bool_or([at[r][t][p].negated()] + ok
                              ).only_enforce_if(v.negated())
                kena.append(v)
            if kena:
                v = m.new_bool_var(f"pref{p}_{r}")
                m.add(v == sum(kena))
                out.append(w * v)
    return out


def _pasang_hint(m, st, pairs, idx_pair, y, pt, at, meja) -> None:
    """Suapkan jadwal yang sudah ada sebagai titik awal solver."""
    for r in range(len(meja)):
        if not meja[r]:
            continue
        hadir_pair: dict[int, int] = {}     # index pasangan -> court
        hadir_orang: dict[int, int] = {}    # pemain -> court
        # Court diurutkan lewat pemain ber-indeks terkecil, sama seperti
        # pemutus simetri di model. Tanpa ini hint-nya melanggar batasan itu dan
        # DIBUANG DIAM-DIAM - solver lalu mulai dari nol, yang persis membuang
        # satu-satunya keunggulan yang dipunyai mode ini.
        urut = sorted(st.matches[r][:meja[r]], key=min)
        for t, q in enumerate(urut):
            for tim in ((q[0], q[1]), (q[2], q[3])):
                key = tim if tim[0] < tim[1] else (tim[1], tim[0])
                i = idx_pair[r].get(key)
                if i is None:
                    # Konstruksi menghasilkan pasangan yang model ini anggap
                    # tidak sah. Hint-nya dibuang seluruh ronde daripada
                    # sebagian - hint separuh justru menyesatkan solver.
                    hadir_pair.clear()
                    hadir_orang.clear()
                    break
                hadir_pair[i] = t
                hadir_orang[tim[0]] = t
                hadir_orang[tim[1]] = t
            if not hadir_pair:
                break
        if not hadir_pair:
            continue
        for i in range(len(pairs[r])):
            m.add_hint(y[r][i], 1 if i in hadir_pair else 0)
            for t in range(meja[r]):
                m.add_hint(pt[r][t][i], 1 if hadir_pair.get(i) == t else 0)
        for t in range(meja[r]):
            for p in range(st.n):
                m.add_hint(at[r][t][p], 1 if hadir_orang.get(p) == t else 0)


def _pelapor(progress):
    """Callback yang melaporkan tiap solusi yang membaik ke UI, apa adanya.

    Dibuat lewat fungsi, bukan kelas modul, karena kelas induknya ada di dalam
    ortools - dan modul ini harus tetap bisa di-import walau ortools tidak
    terpasang.
    """
    from ortools.sat.python import cp_model

    class _Impl(cp_model.CpSolverSolutionCallback):
        def __init__(self):
            cp_model.CpSolverSolutionCallback.__init__(self)
            self.hitung = 0

        def on_solution_callback(self):
            self.hitung += 1
            # Angkanya nyata: seberapa dekat solusi sekarang ke batas bawah yang
            # sudah terbukti, bukan animasi.
            atas = self.objective_value / SKALA
            bawah = self.best_objective_bound / SKALA
            frac = 0.0 if atas <= 0 else max(0.0, min(0.95, bawah / atas))
            progress(frac,
                     f"CP-SAT: solusi ke-{self.hitung}, biaya {atas:,.0f} "
                     f"(batas bawah {bawah:,.0f})")

    return _Impl()


# ---------------------------------------------------------------------------
# Penyempurnaan jendela (LNS): solver eksak di 3 ronde, sisanya dipaku
# ---------------------------------------------------------------------------

# Berapa ronde yang dibuka sekaligus. Diukur pada 26 orang / 4 court / 11 ronde:
# kesembilan jendela 3-ronde TERBUKTI optimal, masing-masing 1,6-2,9 detik,
# sementara model utuh pada setup yang sama tidak selesai dalam 15 detik. Dinding
# "12 ronde ke atas" yang mematikan model utuh tidak berlaku untuk submodel.
JENDELA = 3

# Batas waktu satu jendela. Angka ini bukan yang membatasi total - anggaran
# totalnya milik host (Config.lns_seconds) - melainkan penjaga supaya satu
# jendela yang ternyata keras tidak menelan seluruh anggaran sendirian.
#
# CP-SAT TIDAK selalu menghormatinya, dan itu terukur: presolve model besar sudah
# melewati batasnya sebelum pemeriksaan waktu menggigit. Satu jendela dengan
# batas 3,0 detik memakai 2,10 detik pada 26 orang / 4 court (47 ribu variabel),
# 3,62 detik pada 40 orang / 4 court (110 ribu), dan 4,82 detik pada 60 orang / 6
# court (325 ribu). Perakitan modelnya sendiri bukan penyebabnya - itu cuma
# 0,02-0,37 detik. Karena itu anggaran total di sempurnakan() tidak boleh
# mengandalkan angka ini sebagai biaya sebenarnya; lihat `biaya_jendela` di sana.
DETIK_PER_JENDELA = 3.0

# Berapa jendela terpanas yang dicoba per sapuan. Diukur: 3 jendela x 2 sapuan
# memberi mutu rata-rata yang sama dengan menyapu SELURUH jendela (+0,57 lawan
# +0,56 pada 18 kasus) dengan seperempat waktunya.
JENDELA_PER_SAPUAN = 3

# Sapuan diulang karena satu perbaikan bisa membuka perbaikan di jendela lain.
MAKS_SAPUAN = 2


# Apa yang sebenarnya membatasi penyempurnaan, menurut mode yang dipakai.
_ANGGARAN = {False: "Anggaran waktunya", True: "Jatah jendelanya"}


@dataclass
class HasilSempurna:
    """Apa yang terjadi selama penyempurnaan, apa adanya.

    Host menekan tombol dan menunggu; kalau tidak ada yang berubah ia berhak
    tahu apakah itu karena jadwalnya sudah rapi atau karena waktunya habis.
    """

    dijalankan: bool = False
    # Apakah seluruh rangkaian ini bisa diulang. Hanya benar kalau host
    # memintanya DAN tidak ada satu jendela pun yang jatuh ke penjaga waktu.
    deterministik: bool = False
    # Tidak ada satu pun ronde yang punya pelanggaran - jadwal sudah rapi.
    gerbang_tutup: bool = False
    jendela_dicoba: int = 0
    jendela_membaik: int = 0
    detik: float = 0.0
    anggaran_habis: bool = False
    catatan: list[str] = field(default_factory=list)


def _panas_per_ronde(st, ambang: list[int]) -> list[int]:
    """Seberapa banyak pelanggaran yang dimuat tiap ronde.

    Dua sumber, dan keduanya perlu:

      * rentetan duduk yang MELEBIHI ambang pemiliknya sendiri. Ambang itu
        dihitung per pemain dari jumlah mainnya (optimizer.wait_thresholds),
        jadi orang yang memang jarang turun tidak dituduh menunggu terlalu lama.
      * ronde tempat sepasang orang berhadapan untuk kali kedua atau lebih.

    Ronde tetangga sebuah rentetan ikut dihitung panas: mengeluarkan seseorang
    dari rentetan butuh tempat untuk memasukkannya, dan tempat itu ada di ronde
    sebelum atau sesudahnya. Tanpa itu jendela yang terpilih tidak punya ruang
    gerak dan solver cuma memastikan keadaan yang sama.

    Rentetan diukur HANYA di ronde tempat peserta itu berhak turun, aturan yang
    sama yang dipakai statistik di scheduler._telaah_giliran. Tanpa itu peserta
    putri "menunggu" sepanjang babak putra, padahal ia bukan sedang dilewati -
    ia sedang tidak berhak, dan tidak ada susunan yang bisa mengubahnya. Diukur
    pada 26 orang dengan babak Putra 3 / Putri 3 / Mixed 5 berselang-seling,
    skornya membengkak lima kali lipat: total 145-150 lawan 28-31 yang
    sebenarnya, dan bengkaknya menumpuk di ronde batas babak - jadi jendela yang
    terpilih adalah jendela yang tunggunya paling mustahil diperbaiki. Di meet
    tanpa babak kedua hitungan itu identik.
    """
    R, n = st.n_rounds, st.n
    panas = [0] * R
    for p in range(n):
        # Ronde milik p sendiri. Tetangga rentetan juga diambil dari daftar ini,
        # bukan dari ronde di sebelahnya secara mentah: tempat untuk menukar p
        # masuk cuma ada di ronde tempat ia berhak turun.
        milik = [r for r in range(R) if st.rules.eligible_at(r, p)]
        mulai = None
        for i, r in enumerate(milik):
            if p in st.byes[r]:
                if mulai is None:
                    mulai = i
                continue
            if mulai is not None:
                if (i - mulai) > ambang[p]:
                    for rr in milik[max(0, mulai - 1):i + 1]:
                        panas[rr] += 1
                mulai = None
        if mulai is not None and (len(milik) - mulai) > ambang[p]:
            for rr in milik[max(0, mulai - 1):]:
                panas[rr] += 1
    for r in range(R):
        for q in st.matches[r]:
            a, b, c, d = q
            for i, j in ((a, c), (a, d), (b, c), (b, d)):
                if st.oc[st._k(i, j)] > 1:
                    panas[r] += 1
    return panas


def sempurnakan(st, courts_r: list[int], *, anggaran: float,
                workers: int = 8, nilai=None, progress=None,
                deterministic: bool = False,
                seed: int = 0) -> HasilSempurna:
    """Perbaiki jadwal yang SUDAH JADI, jendela demi jendela, secara eksak.

    Berbeda dari optimize(): yang ini tidak pernah menyerahkan seluruh jadwal ke
    solver. Ia memilih jendela 3 ronde yang memuat pelanggaran terbanyak,
    memakukan sisanya, dan menyelesaikan submasalah itu sampai TERBUKTI optimal.

    Kalau tidak ada satu pun ronde yang memuat pelanggaran, ia tidak menjalankan
    solver sama sekali - gerbangnya tutup dan ongkosnya nol. Diukur pada 18
    kasus, gerbang itu menutup di setengahnya, dan tidak satu pun dari yang
    tertutup memang punya sesuatu untuk dikejar.

    Penjaga "tidak pernah lebih buruk" milik optimize() dipakai apa adanya: tiap
    jendela hanya diterima kalau `nilai` membaik. Pada 54 percobaan di tiga
    varian penyetelan, tidak ada satu pun jadwal yang memburuk.

    `deterministic=True` mengubah arti `anggaran`: dari "berapa detik host mau
    menunggu" menjadi "berapa jendela yang dicoba". Itu bukan pilihan gaya -
    lingkaran di bawah ini memutuskan kapan berhenti dengan MELIHAT JAM, jadi
    membuat tiap jendela deterministik saja tidak cukup: mesin yang sedang sibuk
    akan mencoba lebih sedikit jendela dan mendarat di jadwal lain. Anggarannya
    dibagi DETIK_PER_JENDELA, dan jumlah jendela tetap terbatas oleh
    MAKS_SAPUAN x JENDELA_PER_SAPUAN seperti biasa.
    """
    hasil = HasilSempurna()
    if not tersedia():
        hasil.catatan.append(
            "Penyempurnaan butuh OR-Tools, dan paket itu tidak terpasang.")
        return hasil

    from .optimizer import wait_thresholds

    R = st.n_rounds
    if R < JENDELA:
        hasil.catatan.append(
            f"Acara ini cuma {R} ronde, sementara penyempurnaan membuka "
            f"{JENDELA} ronde sekaligus.")
        return hasil

    mulai = time.perf_counter()
    hasil.dijalankan = True
    nilai = nilai or _nilai_bawaan

    # Berapa lama satu jendela BENAR-BENAR memakan waktu, bukan berapa lama ia
    # dijanjikan. Dimulai dari batas per jendela lalu dinaikkan ke biaya nyata
    # yang terpantau, dan sebuah jendela baru hanya dimulai kalau sisa anggaran
    # masih cukup membayarnya.
    #
    # Ini pengamanan terhadap batas atas, bukan perbaikan atas pelanggaran yang
    # terpantau: diukur pada 60 orang / 6 court, tahap ini memakai 14,3 detik
    # dari anggaran 20 - patuh. Yang diamankan adalah keadaan yang belum
    # terpantau tapi mungkin: satu jendela di sana berbiaya 4,5-5,6 detik
    # meskipun batasnya 3, jadi enam jendela (dua sapuan penuh yang keduanya
    # menemukan perbaikan) bisa menembus anggaran. Memakai biaya yang terpantau
    # membuat kelebihan terburuknya tinggal satu jendela, dan angkanya
    # menyesuaikan sendiri ke ukuran acara tanpa tabel ukuran yang harus
    # dirawat.
    biaya_jendela = DETIK_PER_JENDELA

    # Mode deterministik menghitung jendela, bukan detik - lihat docstring.
    det = bool(deterministic)
    maks_jendela = max(1, int(anggaran / DETIK_PER_JENDELA)) if det else 0
    hasil.deterministik = det

    def kehabisan() -> bool:
        if det:
            return hasil.jendela_dicoba >= maks_jendela
        return (anggaran - (time.perf_counter() - mulai)) < biaya_jendela

    for sapuan in range(MAKS_SAPUAN):
        if kehabisan():
            hasil.anggaran_habis = True
            break
        panas = _panas_per_ronde(st, wait_thresholds(st))
        if not any(panas):
            # Sapuan pertama: memang sudah rapi. Sapuan berikutnya: baru saja
            # dirapikan. Keduanya berarti berhenti, tapi hanya yang pertama
            # berarti tombolnya tidak perlu ditekan.
            hasil.gerbang_tutup = (sapuan == 0)
            break
        urut = sorted(
            ((sum(panas[a:a + JENDELA]), a) for a in range(R - JENDELA + 1)),
            key=lambda x: (-x[0], x[1]))
        kandidat = [a for skor, a in urut if skor > 0][:JENDELA_PER_SAPUAN]
        ada = False
        for ke, a in enumerate(kandidat):
            if kehabisan():
                hasil.anggaran_habis = True
                break
            if progress is not None:
                # Kemajuan diukur dengan jendela di mode deterministik: memakai
                # jam di situ akan melaporkan angka yang tidak sejalan dengan apa
                # yang benar-benar menghentikan lingkaran ini.
                frac = (hasil.jendela_dicoba / maks_jendela if det
                        else (time.perf_counter() - mulai) / max(anggaran, 1e-9))
                progress(min(1.0, frac),
                         f"Menyempurnakan ronde {a + 1}-{a + JENDELA}")
            hasil.jendela_dicoba += 1
            t_jendela = time.perf_counter()
            batas = DETIK_PER_JENDELA if det else min(
                DETIK_PER_JENDELA, anggaran - (time.perf_counter() - mulai))
            lapor = optimize(
                st, courts_r,
                time_limit=batas,
                workers=workers, nilai=nilai,
                beku=set(range(R)) - set(range(a, a + JENDELA)),
                deterministic=det, seed=seed,
            )
            biaya_jendela = max(biaya_jendela, time.perf_counter() - t_jendela)
            # Satu jendela yang jatuh ke penjaga waktu membatalkan janji untuk
            # SELURUH rangkaian: jendela berikutnya bekerja di atas jadwal yang
            # sudah berbeda.
            if det and not lapor.deterministik:
                hasil.deterministik = False
                hasil.catatan.extend(lapor.catatan)
            if lapor.membaik:
                hasil.jendela_membaik += 1
                ada = True
        if not ada:
            break

    hasil.detik = time.perf_counter() - mulai
    return hasil


def catatan_sempurna(h: HasilSempurna) -> str:
    """Satu kalimat untuk host, menyebut angkanya - bukan cuma "selesai".

    Kata "anggaran" berganti bunyi di mode deterministik, dan itu bukan
    kosmetik: di sana yang membatasi memang jumlah jendela, bukan detik, jadi
    "tambah waktunya" adalah saran yang salah - yang menolong adalah menaikkan
    angka batasnya.
    """
    if not h.dijalankan:
        return h.catatan[0] if h.catatan else "Penyempurnaan tidak dijalankan."
    if h.gerbang_tutup:
        return ("Penyempurnaan tidak menemukan apa pun untuk dikerjakan: tidak "
                "ada peserta yang menunggu lebih lama daripada batas jumlah "
                "mainnya, dan tidak ada pasangan yang berhadapan dua kali. "
                "Jadwalnya tidak diubah.")
    if not h.jendela_membaik:
        pokok = (f"Penyempurnaan memeriksa {h.jendela_dicoba} kelompok ronde "
                 f"dalam {h.detik:.1f} detik dan tidak menemukan susunan yang "
                 f"lebih baik. Jadwalnya tidak diubah.")
        return pokok + ((f" {_ANGGARAN[h.deterministik]} habis lebih dulu, jadi "
                         f"menambahnya masih mungkin menolong.")
                        if h.anggaran_habis else "")
    pokok = (f"Penyempurnaan memperbaiki {h.jendela_membaik} dari "
             f"{h.jendela_dicoba} kelompok ronde yang diperiksa, dalam "
             f"{h.detik:.1f} detik.")
    if h.anggaran_habis:
        pokok += (f" {_ANGGARAN[h.deterministik]} habis sebelum pemeriksaan "
                  f"selesai, jadi menambahnya masih mungkin menolong.")
    return pokok
