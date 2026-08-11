"""Optimasi jadwal lewat simulated annealing dengan evaluasi delta O(1).

Konstruksi eksak (1-factorization / Latin square) hanya menjamin PARTNER unik.
Siapa lawan siapa, siapa yang duduk, dan keseimbangan rating masih harus dicari.
Di situlah optimizer ini bekerja.

Fungsi biaya:

    cost = w_partner  * SUM pc*(pc-1)          # pengulangan partner
         + w_opponent * SUM oc*(oc-1)          # pengulangan lawan
         + w_opp_cap  * #{pasang dengan oc>=2} # denda "pernah ketemu 2x"
         + w_bye      * SUM bye^2              # ketimpangan istirahat
         + w_b2b      * (duduk 2 ronde beruntun)
         + w_wait     * SUM L*(L-1)            # rentetan duduk yang menumpuk
         + w_rating   * SUM |rating_tim_A - rating_tim_B|
         + w_spread   * SUM max(0, jarak_rating - ambang)^2
         + w_repeat   * SUM 1/jarak_ronde (match yang persis sama terulang)

Bentuk c*(c-1) itu disengaja: fungsinya konveks, jadi optimizer otomatis lebih
memilih "4 orang mengulang 1x" daripada "1 orang mengulang 4x". Pengulangan yang
tidak terhindarkan pun jadi tersebar rata.

Aturan komposisi (putra/putri/mixed/partner terkunci) TIDAK dimasukkan ke fungsi
biaya sebagai penalti — melainkan ditegakkan sebagai batas keras: gerakan yang
melanggar langsung ditolak. Dengan begitu jadwal yang keluar mustahil melanggar
aturan, seberapa pun agresifnya optimasi.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .models import matchup_code, team_shape


@dataclass
class Weights:
    partner: float = 1000.0
    opponent: float = 120.0
    # Denda sekali-bayar begitu sepasang orang berhadapan untuk KEDUA kalinya.
    #
    # Bentuk c*(c-1) saja tidak cukup untuk mengejar nol. Ia konveks, jadi
    # justru pengulangan PERTAMA yang paling murah - bagus untuk menyebar
    # pengulangan yang memang tak terhindarkan, buruk untuk membuang beberapa
    # sisa terakhir. Denda ini menambal persis lubang itu: ia hanya menyala di
    # perbatasan 1->2, jadi jadwal yang sudah mustahil nol tidak ikut terhukum
    # berulang-ulang, sementara jadwal yang tinggal sedikit lagi jadi punya
    # dorongan kuat untuk menutupnya. Padanan "batas 1x" yang aman untuk
    # pencarian lokal: hard constraint akan membekukan annealing, karena
    # keadaan awalnya memang sudah melanggar - tiap gerakan akan ditolak dan
    # pencarian tidak pernah bergerak sama sekali.
    #
    # 900 dari pengukuran pada setup 26 orang / 4 court / format sesama-bentuk.
    #
    #   effort 30000 (default), 10 seed : 4.4 -> 2.0 pasang berulang,
    #                                     duduk-beruntun 18.6 -> 18.2,
    #                                     kualitas 92.20 -> 92.71
    #   effort 160000,          24 seed : 1.17 -> 0.71 rata-rata, TERBURUK
    #                                     3 -> 1, dan seluruh 24 seed mendarat
    #                                     di <=1 (sebelumnya cuma 17 dari 24),
    #                                     duduk-beruntun 16.8 -> 17.3,
    #                                     kualitas 93.37 -> 93.22
    #
    # Yang paling berharga bukan rata-ratanya melainkan sebarannya: simpangan
    # baku 0.87 -> 0.46. Host tidak menjalankan 24 seed lalu memilih yang
    # terbaik - ia menjalankan sekali. Menghapus ekor "3 pasang berulang" jauh
    # lebih berarti daripada menurunkan rata-rata. Ongkosnya, duduk-beruntun
    # +0.5 dari simpangan baku 2.6, masih di dalam derau.
    #
    # Kenapa menaikkan `opponent` saja tidak dipilih: pada 450 ia menekan
    # pengulangan serupa, tapi ikut merusak jadwal yang pengulangannya SUDAH
    # nol (26 orang format bebas: duduk-beruntun 16.2 -> 19.2), karena
    # gradiennya bekerja di semua keadaan. Denda ini padam sendiri begitu tidak
    # ada pasangan yang berulang.
    opponent_cap: float = 900.0
    # Kerataan jumlah main mengalahkan variasi lawan, dan itu disengaja.
    # Peserta membayar fee yang sama; kehilangan satu ronde main itu kerugian
    # nyata, sedangkan sekali bertemu lawan yang sama hampir tak terasa. Dengan
    # bobot lama (60) satu gerakan yang menambah pengulangan lawan dinilai lebih
    # mahal daripada meratakan giliran, sehingga optimasi yang lebih lama justru
    # membuat jumlah main makin timpang.
    bye: float = 500.0
    b2b_bye: float = 400.0
    # Menunggu yang MENUMPUK, bukan sekadar menunggu.
    #
    # b2b_bye di atas menghitung tiap pasang ronde-duduk yang bersebelahan,
    # jadi harganya linear terhadap panjang rentetan: duduk 5 ronde beruntun
    # (4 pasang bersebelahan) dinilai sama persis dengan dua kali duduk 3 ronde
    # (2 + 2). Buat peserta keduanya sama sekali tidak sama, dan yang pertama
    # itulah yang terbaca sebagai "kok saya belum main padahal dia sudah dua
    # kali". Denda ini berbentuk L*(L-1) per rentetan, jadi konveks: 5 beruntun
    # = 20, dua kali 3 beruntun = 12. Optimizer otomatis memecah rentetan
    # panjang jadi beberapa yang pendek.
    #
    # Ia juga mengurus rentetan PEMBUKA - ronde-ronde sebelum seseorang main
    # pertama kali. Itu bagian yang paling terasa: menunggu di awal terasa
    # seperti dilupakan, menunggu di tengah terasa seperti jeda.
    #
    # NOL di annealing utama, dan itu keputusan yang diukur - bukan berarti
    # giliran tidak penting.
    #
    # Sebagai suku biaya berbobot, denda ini selalu bisa MEMBELI pengulangan
    # lawan: memecah satu rentetan menunggu bernilai lebih besar daripada denda
    # satu pasangan yang berhadapan dua kali. Pada sapuan 324 kasus, bobot 250
    # di annealing utama membuat 36 jadwal yang tadinya nol lawan berulang
    # kehilangannya - satu melompat dari 0 ke 9 pasang. Bobot yang cukup kecil
    # untuk aman (60) hampir tidak memperbaiki gilirannya lagi. Tidak ada satu
    # bobot yang benar untuk kedua keadaan.
    #
    # Jadi annealing utama dibiarkan mengejar keunikan sepenuhnya, seperti
    # sebelum giliran jadi urusan sama sekali, lalu anneal_giliran() mengurus
    # giliran sebagai tahap sendiri - dengan bobot kuat, tapi dengan jumlah
    # pasang berulang yang sudah dicapai dipasang sebagai batas keras. Di sana
    # bobot inilah yang dipakai (lihat argumen `bobot`), dan di sini nol supaya
    # tahap pertama tidak pernah menawar.
    long_wait: float = 60.0
    rating: float = 0.0
    spread: float = 0.0
    spread_threshold: float = 1.5
    # Match yang terulang PERSIS (empat orang sama, tim sama). Kadang tak
    # terhindarkan: 4 orang cuma punya 3 susunan match, jadi ronde ke-4 mereka
    # pasti mengulang salah satunya. Yang bisa diatur adalah JARAKNYA - "lagi,
    # sekarang juga" terasa seperti bug, "lagi, satu jam kemudian" tidak.
    # Biayanya 1/jarak, jadi mengulang di ronde berikutnya jauh lebih mahal
    # daripada mengulang di ujung acara. Sengaja lebih kecil dari satu
    # pengulangan partner (2000) supaya perannya cuma memutus seri: tidak akan
    # ada partner unik yang dikorbankan demi menggeser jarak pengulangan.
    repeat_gap: float = 200.0
    # Preferensi per-peserta (mis. "saya maunya court isi 4 perempuan").
    # Lunak, bukan keras: kalau tidak bisa dipenuhi, jadwal tetap jadi dan
    # pelanggarannya dilaporkan ke host apa adanya.
    preference: float = 3000.0

    @staticmethod
    def for_mode(mode: str) -> "Weights":
        if mode == "mexicano":
            # Keseimbangan rating jadi tujuan utama, keunikan tetap dijaga.
            return Weights(opponent=90.0, rating=150.0, spread=60.0,
                           spread_threshold=1.0)
        if mode == "team":
            # Partner terkunci; yang dioptimasi hanya lawan & istirahat.
            return Weights(partner=0.0, opponent=300.0, rating=20.0)
        # americano & tiered (di dalam pool) -> murni keunikan.
        return Weights()


@dataclass
class Rules:
    """Batas keras yang tidak boleh dilanggar optimizer.

    Dibedakan dari preferensi lunak (lihat Weights.preference): yang di sini
    ditegakkan dengan menolak gerakan, jadi jadwal hasilnya mustahil melanggar.
    """

    gender: dict[int, str | None] = field(default_factory=dict)
    # Aturan komposisi per ronde: open | men | women | same_gender | mixed
    round_rule: list[str] = field(default_factory=list)
    # Siapa yang boleh turun di ronde tertentu.
    round_eligible: list[set[int]] = field(default_factory=list)
    # Pemain -> id rekan tetapnya. Boleh sebagian saja: peserta yang minta
    # partner tetap dikunci, sisanya tetap rotasi bebas.
    locked_partner: dict[int, int] = field(default_factory=dict)
    # Mode tiered: pemain -> nomor pool. Satu court wajib satu pool.
    tier_of: dict[int, int] = field(default_factory=dict)
    # Preferensi lunak per peserta:
    #   women_only  -> maunya court berisi 4 perempuan
    #   men_only    -> maunya court berisi 4 laki-laki
    #   same_gender -> maunya court satu gender (yang mana saja)
    #   mixed_team  -> maunya partner lawan jenis
    court_pref: dict[int, str] = field(default_factory=dict)
    # Format match yang diizinkan (kode dari models.MATCHUPS). Kosong = semua
    # boleh. Beda dari round_rule: itu mengatur bagaimana satu tim disusun, ini
    # mengatur tim seperti apa boleh berhadapan dengan tim seperti apa.
    allowed_matchups: set[str] = field(default_factory=set)

    def pref_violations(self, quad: list[int]) -> list[tuple[int, str]]:
        """Preferensi mana saja yang dilanggar susunan court ini."""
        if not self.court_pref:
            return []
        g = self.gender
        genders = [g.get(p) for p in quad]
        all_f = all(x == "F" for x in genders)
        all_m = all(x == "M" for x in genders)
        out: list[tuple[int, str]] = []
        for slot, p in enumerate(quad):
            pref = self.court_pref.get(p)
            if pref is None:
                continue
            if pref == "women_only" and not all_f:
                out.append((p, pref))
            elif pref == "men_only" and not all_m:
                out.append((p, pref))
            elif pref == "same_gender" and not (all_f or all_m):
                out.append((p, pref))
            elif pref == "mixed_team":
                mate = quad[slot ^ 1]  # 0<->1, 2<->3
                if g.get(p) is None or g.get(mate) is None or g[p] == g[mate]:
                    out.append((p, pref))
        return out

    @property
    def locked(self) -> bool:
        return bool(self.locked_partner)

    def active_mate(self, p: int, r: int) -> int | None:
        """Rekan tetap p, kalau kuncinya masih mungkin ditegakkan di ronde r.

        Kunci partner berlaku lintas babak, tapi tidak setiap babak sanggup
        menampungnya: pasangan putra-putri mustahil di babak "sesama gender",
        pasangan sesama gender mustahil di babak "mixed", dan rekan yang tidak
        turun di babak itu jelas tidak bisa dipasangkan.

        Kalau kunci ditegakkan buta di babak seperti itu, TIDAK ADA susunan yang
        lolos - orangnya bukan cuma kehilangan partner tetapnya, tapi hilang
        dari babak itu sama sekali. Jadi di babak yang mustahil kuncinya
        dilonggarkan; pelonggarannya dilaporkan ke host lewat catatan jadwal.
        """
        mate = self.locked_partner.get(p)
        if mate is None:
            return None
        eligible = self.round_eligible[r] if r < len(self.round_eligible) else None
        if eligible is not None and mate not in eligible:
            return None
        rule = self.round_rule[r] if r < len(self.round_rule) else "open"
        gp, gm = self.gender.get(p), self.gender.get(mate)
        if rule == "mixed" and (gp is None or gm is None or gp == gm):
            return None
        if rule == "same_gender" and (gp is None or gp != gm):
            return None
        return mate

    def matchup_ok(self, quad: list[int]) -> bool:
        """Apakah format match ini termasuk yang diizinkan host.

        Gender yang belum diisi membuat aturan ini tidak bisa dinilai, dan
        dalam keadaan itu jadwal TIDAK diblokir - meet tanpa data gender harus
        tetap bisa jalan. Aturannya menyaring yang jelas melanggar, bukan
        menuntut data yang mungkin tidak dimiliki host.
        """
        if not self.allowed_matchups:
            return True
        a, b, c, d = quad
        g = self.gender
        kode = matchup_code(team_shape(g.get(a), g.get(b)),
                            team_shape(g.get(c), g.get(d)))
        return kode is None or kode in self.allowed_matchups

    def quad_ok(self, quad: list[int], r: int) -> bool:
        if not self.matchup_ok(quad):
            return False
        a, b, c, d = quad
        eligible = self.round_eligible[r] if r < len(self.round_eligible) else None
        if eligible is not None:
            if a not in eligible or b not in eligible or c not in eligible or d not in eligible:
                return False

        if self.locked_partner:
            # Hanya pemain yang minta partner tetap yang diperiksa; peserta lain bebas.
            for x, mate_slot in ((a, b), (b, a), (c, d), (d, c)):
                m = self.active_mate(x, r)
                if m is not None and m != mate_slot:
                    return False

        if self.tier_of:
            t = self.tier_of
            if not (t.get(a) == t.get(b) == t.get(c) == t.get(d)):
                return False

        rule = self.round_rule[r] if r < len(self.round_rule) else "open"
        if rule == "mixed":
            g = self.gender
            if g.get(a) is None or g.get(b) is None or g.get(c) is None or g.get(d) is None:
                return False
            return g[a] != g[b] and g[c] != g[d]
        if rule == "same_gender":
            # KEEMPAT pemain satu gender - putra lawan putra, putri lawan putri.
            # Dulu syaratnya hanya "tiap tim satu gender", yang membolehkan tim
            # putri melawan tim putra; itu bukan yang dimaksud "sesama gender".
            g = self.gender
            ga = g.get(a)
            return ga is not None and ga == g.get(b) == g.get(c) == g.get(d)
        # open / men / women sudah dijamin lewat round_eligible.
        return True


class ScheduleState:
    """State jadwal + count matrix, semua di-maintain inkremental."""

    __slots__ = (
        "n", "ratings", "w", "rules", "n_rounds", "matches", "byes",
        "pc", "oc", "bye_count", "play_count", "rep_pc", "rep_oc", "seen_at",
        "cost_pair", "cost_bye", "cost_b2b", "cost_wait", "cost_rating",
        "cost_pref", "cost_repeat",
    )

    def __init__(self, n: int, ratings: list[float], w: Weights,
                 n_rounds: int, rules: Rules | None = None):
        self.n = n
        self.ratings = ratings
        self.w = w
        self.rules = rules or Rules()
        self.n_rounds = n_rounds
        # matches[r] = list of [a, b, c, d]  -> tim (a,b) lawan (c,d)
        self.matches: list[list[list[int]]] = [[] for _ in range(n_rounds)]
        self.byes: list[set[int]] = [set() for _ in range(n_rounds)]
        self.pc = [0] * (n * n)
        self.oc = [0] * (n * n)
        self.bye_count = [0] * n
        # Berapa ronde tiap pemain sudah benar-benar turun. Dipelihara
        # inkremental supaya konstruksi bisa memakainya sebagai antrean giliran
        # tanpa menyapu ulang seluruh jadwal tiap kali memilih pasangan.
        self.play_count = [0] * n
        # Berapa PASANG yang sudah partner-an / berhadapan lebih dari sekali.
        # Ini angka yang dilihat host dan yang dipakai memilih di antara
        # beberapa percobaan, jadi ia pula yang harus dijaga saat membetulkan
        # giliran - bukan biaya konveksnya. Bedanya nyata: begitu pengulangan
        # memang tak terhindarkan, menggeser pengulangan dari satu pasangan ke
        # pasangan lain mengubah biaya konveks tapi tidak mengubah angka ini,
        # dan gerakan seperti itu memang tidak merugikan siapa pun.
        self.rep_pc = 0
        self.rep_oc = 0
        # susunan match -> ronde-ronde tempat ia muncul (untuk biaya jarak ulang)
        self.seen_at: dict[tuple, list[int]] = {}
        self.cost_pair = 0.0
        self.cost_bye = 0.0
        self.cost_b2b = 0.0
        self.cost_wait = 0.0
        self.cost_rating = 0.0
        self.cost_pref = 0.0
        self.cost_repeat = 0.0

    # -- indeks simetris --------------------------------------------------
    def _k(self, i: int, j: int) -> int:
        return i * self.n + j if i < j else j * self.n + i

    @staticmethod
    def _match_key(quad: list[int]) -> tuple:
        a, b, c, d = quad
        ta = (a, b) if a < b else (b, a)
        tb = (c, d) if c < d else (d, c)
        return (ta, tb) if ta < tb else (tb, ta)

    def _pref_cost(self, quad: list[int]) -> float:
        if not self.rules.court_pref or self.w.preference == 0.0:
            return 0.0
        return self.w.preference * len(self.rules.pref_violations(quad))

    # -- biaya rating satu match -----------------------------------------
    def _rating_cost(self, quad: list[int]) -> float:
        w = self.w
        if w.rating == 0.0 and w.spread == 0.0:
            return 0.0
        r = self.ratings
        a, b, c, d = quad
        gap = abs((r[a] + r[b]) - (r[c] + r[d]))
        cost = w.rating * gap
        if w.spread:
            vals = (r[a], r[b], r[c], r[d])
            spread = max(vals) - min(vals)
            over = spread - w.spread_threshold
            if over > 0:
                cost += w.spread * over * over
        return cost

    # -- biaya jarak pengulangan match ------------------------------------
    def _touch_repeat(self, quad: list[int], sign: int, r: int) -> None:
        """Biaya 1/jarak terhadap kemunculan lain dari susunan match yang sama."""
        w = self.w.repeat_gap
        key = self._match_key(quad)
        seen = self.seen_at
        if sign > 0:
            others = seen.get(key)
            if others:
                if w:
                    self.cost_repeat += w * sum(1.0 / max(1, abs(r - o))
                                                for o in others)
                others.append(r)
            else:
                seen[key] = [r]
        else:
            others = seen[key]
            others.remove(r)
            if others:
                if w:
                    self.cost_repeat -= w * sum(1.0 / max(1, abs(r - o))
                                                for o in others)
            else:
                del seen[key]

    # -- tambah / hapus match --------------------------------------------
    def _touch_match(self, quad: list[int], sign: int, r: int) -> None:
        """sign=+1 pasang match, sign=-1 lepas match. Update count + cost."""
        a, b, c, d = quad
        self._touch_repeat(quad, sign, r)
        for p in quad:
            self.play_count[p] += sign
        wp, wo = self.w.partner, self.w.opponent
        pc, oc, k = self.pc, self.oc, self._k

        for (i, j) in ((a, b), (c, d)):
            idx = k(i, j)
            cur = pc[idx]
            if sign > 0:
                self.cost_pair += wp * 2 * cur       # (c+1)c - c(c-1) = 2c
                if cur == 1:                        # baru saja jadi berulang
                    self.rep_pc += 1
                pc[idx] = cur + 1
            else:
                pc[idx] = cur - 1
                self.cost_pair -= wp * 2 * (cur - 1)
                if cur == 2:                        # kembali jadi sekali saja
                    self.rep_pc -= 1

        wcap = self.w.opponent_cap
        for (i, j) in ((a, c), (a, d), (b, c), (b, d)):
            idx = k(i, j)
            cur = oc[idx]
            if sign > 0:
                self.cost_pair += wo * 2 * cur
                if cur == 1:                        # baru saja jadi berulang
                    self.cost_pair += wcap
                    self.rep_oc += 1
                oc[idx] = cur + 1
            else:
                oc[idx] = cur - 1
                self.cost_pair -= wo * 2 * (cur - 1)
                if cur == 2:                        # kembali jadi sekali saja
                    self.cost_pair -= wcap
                    self.rep_oc -= 1

        self.cost_rating += sign * self._rating_cost(quad)
        self.cost_pref += sign * self._pref_cost(quad)

    # -- rentetan duduk ---------------------------------------------------
    def _run_kiri(self, r: int, p: int) -> int:
        """Berapa ronde beruntun p sudah duduk PERSIS sebelum ronde r."""
        n = 0
        while r - 1 - n >= 0 and p in self.byes[r - 1 - n]:
            n += 1
        return n

    def recompute_wait(self) -> None:
        """Hitung ulang biaya menunggu dari nol.

        Perlu setiap kali bobot long_wait diubah di tengah jalan: biayanya
        dipelihara inkremental sebagai bobot x jumlah, jadi mengganti bobotnya
        saja akan meninggalkan angka lama yang tidak lagi berarti apa-apa.
        """
        total = 0
        for p in range(self.n):
            r = 0
            while r < self.n_rounds:
                if p not in self.byes[r]:
                    r += 1
                    continue
                mulai = r
                while r < self.n_rounds and p in self.byes[r]:
                    r += 1
                panjang = r - mulai
                total += panjang * (panjang - 1)
        self.cost_wait = self.w.long_wait * total

    def wait_before(self, r: int, p: int) -> int:
        """Sudah berapa ronde beruntun p menunggu saat ronde r akan disusun.

        Dipakai konstruksi, yang mengisi ronde dari depan ke belakang: saat
        ronde r disusun, ronde sesudahnya masih kosong, jadi rentetan kiri
        memang tepat sama dengan "sudah menunggu berapa lama". Pemain yang belum
        pernah turun sama sekali otomatis mendapat angka r - paling besar yang
        mungkin, jadi ia selalu di depan antrean.
        """
        return self._run_kiri(r, p)

    def _run_kanan(self, r: int, p: int) -> int:
        """Berapa ronde beruntun p duduk PERSIS setelah ronde r."""
        n = 0
        while r + 1 + n < self.n_rounds and p in self.byes[r + 1 + n]:
            n += 1
        return n

    def _delta_wait(self, r: int, p: int, on: bool) -> float:
        """Perubahan biaya menunggu kalau status duduk p di ronde r digeser.

        Biayanya SUM L*(L-1) atas tiap rentetan duduk. Menyalakan duduk di r
        menyatukan rentetan kiri (L) dan kanan (R) menjadi satu rentetan
        L+R+1; mematikannya memecahnya kembali. Deltanya dihitung dari keadaan
        yang sedang berlaku saat ini, jadi totalnya tetap tepat tanpa peduli
        urutan bongkar-pasangnya - sama seperti biaya duduk-beruntun.
        """
        w = self.w.long_wait
        if not w:
            return 0.0
        kiri = self._run_kiri(r, p)
        kanan = self._run_kanan(r, p)
        gabung = kiri + kanan + 1
        pecah = kiri * (kiri - 1) + kanan * (kanan - 1)
        selisih = gabung * (gabung - 1) - pecah
        return w * selisih if on else -w * selisih

    # -- status bye -------------------------------------------------------
    def _set_bye(self, r: int, p: int, on: bool) -> None:
        """Ubah status istirahat pemain p di ronde r, update cost inkremental."""
        wb, wbb = self.w.bye, self.w.b2b_bye
        cur = self.bye_count[p]

        # Tetangga ronde sebelum & sesudah (untuk deteksi duduk beruntun).
        prev_bye = p in self.byes[r - 1] if r > 0 else False
        next_bye = p in self.byes[r + 1] if r + 1 < self.n_rounds else False
        neighbours = (1 if prev_bye else 0) + (1 if next_bye else 0)

        # Dihitung SEBELUM set byes[r] diubah: rentetan kiri & kanan harus
        # dibaca dari keadaan yang masih utuh, baik saat menyalakan maupun saat
        # mematikan.
        d_wait = self._delta_wait(r, p, on)

        if on:
            self.byes[r].add(p)
            self.cost_bye += wb * (2 * cur + 1)      # (c+1)^2 - c^2
            self.bye_count[p] = cur + 1
            self.cost_b2b += wbb * neighbours
        else:
            self.byes[r].discard(p)
            self.bye_count[p] = cur - 1
            self.cost_bye -= wb * (2 * cur - 1)
            self.cost_b2b -= wbb * neighbours
        self.cost_wait += d_wait

    # -- total ------------------------------------------------------------
    def cost(self) -> float:
        return (self.cost_pair + self.cost_bye + self.cost_b2b + self.cost_wait
                + self.cost_rating + self.cost_pref + self.cost_repeat)

    def round_legal(self, r: int) -> bool:
        return all(self.rules.quad_ok(q, r) for q in self.matches[r])

    # -- pembangunan awal -------------------------------------------------
    def place_round(self, r: int, quads: list[list[int]], byes: list[int]) -> None:
        for q in quads:
            self.matches[r].append(list(q))
            self._touch_match(q, +1, r)
        for p in byes:
            self._set_bye(r, p, True)

    def snapshot(self) -> tuple[list[list[list[int]]], list[set[int]]]:
        return ([[q[:] for q in rnd] for rnd in self.matches],
                [set(b) for b in self.byes])

    def restore(self, snap: tuple[list[list[list[int]]], list[set[int]]]) -> None:
        """Bangun ulang dari snapshot (dipakai saat annealing gagal membaik)."""
        quads, byes = snap
        self.matches = [[] for _ in range(self.n_rounds)]
        self.byes = [set() for _ in range(self.n_rounds)]
        self.pc = [0] * (self.n * self.n)
        self.oc = [0] * (self.n * self.n)
        self.bye_count = [0] * self.n
        self.play_count = [0] * self.n
        self.rep_pc = self.rep_oc = 0
        self.seen_at = {}
        self.cost_pair = self.cost_bye = self.cost_b2b = self.cost_wait = 0.0
        self.cost_rating = self.cost_pref = self.cost_repeat = 0.0
        for r, rnd in enumerate(quads):
            self.place_round(r, rnd, sorted(byes[r]))


# ---------------------------------------------------------------------------
# Gerakan (moves)
# ---------------------------------------------------------------------------

def _swap_within_match(st: ScheduleState, r: int, rng: random.Random) -> bool:
    """Tukar susunan tim dalam satu match: (a,b|c,d) -> (a,c|b,d) atau (a,d|c,b).

    Partner berubah, jadi ini gerakan kuat untuk mode mexicano.
    """
    if not st.matches[r]:
        return False
    mi = rng.randrange(len(st.matches[r]))
    quad = st.matches[r][mi]
    st._touch_match(quad, -1, r)
    a, b, c, d = quad
    new = [a, c, b, d] if rng.random() < 0.5 else [a, d, c, b]
    st.matches[r][mi] = new
    st._touch_match(new, +1, r)
    return True


def _swap_between_matches(st: ScheduleState, r: int, rng: random.Random) -> bool:
    """Tukar dua pemain dari dua match berbeda di ronde yang sama."""
    ms = st.matches[r]
    if len(ms) < 2:
        return False
    i, j = rng.sample(range(len(ms)), 2)
    pi, pj = rng.randrange(4), rng.randrange(4)
    st._touch_match(ms[i], -1, r)
    st._touch_match(ms[j], -1, r)
    ms[i][pi], ms[j][pj] = ms[j][pj], ms[i][pi]
    st._touch_match(ms[i], +1, r)
    st._touch_match(ms[j], +1, r)
    return True


def _swap_teams_between_matches(st: ScheduleState, r: int, rng: random.Random) -> bool:
    """Mode team: tukar satu tim utuh antar match, partner tetap menempel."""
    ms = st.matches[r]
    if len(ms) < 2:
        return False
    i, j = rng.sample(range(len(ms)), 2)
    si, sj = rng.randrange(2) * 2, rng.randrange(2) * 2
    st._touch_match(ms[i], -1, r)
    st._touch_match(ms[j], -1, r)
    ms[i][si], ms[j][sj] = ms[j][sj], ms[i][si]
    ms[i][si + 1], ms[j][sj + 1] = ms[j][sj + 1], ms[i][si + 1]
    st._touch_match(ms[i], +1, r)
    st._touch_match(ms[j], +1, r)
    return True


def _swap_with_bye(st: ScheduleState, r: int, rng: random.Random) -> bool:
    """Tukar seorang pemain di lapangan dengan seorang yang sedang istirahat."""
    if not st.byes[r] or not st.matches[r]:
        return False
    resting = rng.choice(tuple(st.byes[r]))
    mi = rng.randrange(len(st.matches[r]))
    pi = rng.randrange(4)
    quad = st.matches[r][mi]
    playing = quad[pi]

    st._touch_match(quad, -1, r)
    quad[pi] = resting
    st._touch_match(quad, +1, r)
    st._set_bye(r, resting, False)
    st._set_bye(r, playing, True)
    return True


def _swap_team_with_bye(st: ScheduleState, r: int, rng: random.Random) -> bool:
    """Mode team: turunkan tim yang istirahat, naikkan tim yang sedang main."""
    if not st.byes[r] or not st.matches[r]:
        return False
    lp = st.rules.locked_partner
    resting = rng.choice(tuple(st.byes[r]))
    mate = lp.get(resting)
    if mate is None or mate not in st.byes[r]:
        return False

    mi = rng.randrange(len(st.matches[r]))
    slot = rng.randrange(2) * 2
    quad = st.matches[r][mi]
    out_a, out_b = quad[slot], quad[slot + 1]

    st._touch_match(quad, -1, r)
    quad[slot], quad[slot + 1] = resting, mate
    st._touch_match(quad, +1, r)
    st._set_bye(r, resting, False)
    st._set_bye(r, mate, False)
    st._set_bye(r, out_a, True)
    st._set_bye(r, out_b, True)
    return True


def _swap_rounds(st: ScheduleState, r: int, r2: int) -> bool:
    """Tukar ISI dua ronde secara utuh.

    Gerakan lain hanya menyentuh satu ronde, dan itu meninggalkan satu jenis
    perbaikan di luar jangkauan: menggeser LETAK pengulangan. Susunan A-B-A-C
    seharusnya jadi A-B-C-A supaya jaraknya jauh, tapi untuk sampai ke sana
    ronde ke-3 dan ke-4 harus berubah bersamaan - dan keadaan antaranya
    (A-B-C-C) lebih mahal, jadi annealing per-ronde terjebak di lembah.
    Menukar dua ronde sekaligus melompati lembah itu dalam satu langkah.
    """
    if r == r2:
        return False
    # Salin dulu: _set_bye mengosongkan set aslinya, jadi membaca setelah
    # pembongkaran akan mengembalikan daftar istirahat yang kosong.
    keep_r = [q[:] for q in st.matches[r]]
    keep_r2 = [q[:] for q in st.matches[r2]]
    keep_br = sorted(st.byes[r])
    keep_br2 = sorted(st.byes[r2])

    for q in st.matches[r]:
        st._touch_match(q, -1, r)
    for q in st.matches[r2]:
        st._touch_match(q, -1, r2)
    for p in keep_br:
        st._set_bye(r, p, False)
    for p in keep_br2:
        st._set_bye(r2, p, False)

    st.matches[r], st.matches[r2] = keep_r2, keep_r
    for q in st.matches[r]:
        st._touch_match(q, +1, r)
    for q in st.matches[r2]:
        st._touch_match(q, +1, r2)
    for p in keep_br2:
        st._set_bye(r, p, True)
    for p in keep_br:
        st._set_bye(r2, p, True)
    return True


def swap_groups(st: ScheduleState) -> dict[int, list[int]]:
    """Ronde -> ronde lain yang isinya boleh ditukar dengannya.

    Hanya ronde dengan aturan komposisi DAN daftar pemain yang sah sama persis;
    menukar ronde putra dengan ronde putri jelas melanggar batas keras.
    """
    rules = st.rules
    buckets: dict[tuple, list[int]] = {}
    for r in range(st.n_rounds):
        if not st.matches[r]:
            continue
        rule = rules.round_rule[r] if r < len(rules.round_rule) else "open"
        elig = (rules.round_eligible[r]
                if r < len(rules.round_eligible) else None)
        key = (rule, frozenset(elig) if elig is not None else None,
               len(st.matches[r]))
        buckets.setdefault(key, []).append(r)
    return {r: peers for peers in buckets.values() if len(peers) > 1
            for r in peers}


def play_counts(st: ScheduleState) -> list[int]:
    """Berapa ronde tiap pemain benar-benar turun."""
    counts = [0] * st.n
    for rnd in st.matches:
        for quad in rnd:
            for p in quad:
                counts[p] += 1
    return counts


def _try_swap(st: ScheduleState, r: int, mi: int, pi: int, incoming: int):
    """Turunkan `incoming` menggantikan penghuni slot; kembalikan fungsi pembatal."""
    quad = st.matches[r][mi]
    outgoing = quad[pi]
    st._touch_match(quad, -1, r)
    quad[pi] = incoming
    st._touch_match(quad, +1, r)
    st._set_bye(r, incoming, False)
    st._set_bye(r, outgoing, True)

    def undo() -> None:
        st._touch_match(quad, -1, r)
        quad[pi] = outgoing
        st._touch_match(quad, +1, r)
        st._set_bye(r, outgoing, False)
        st._set_bye(r, incoming, True)

    return undo


def _try_reorder(st: ScheduleState, r: int, mi: int, varian: int):
    """Susun ulang tim dalam satu match; kembalikan fungsi pembatal.

    Keempat orangnya tetap, cuma pembagian timnya yang berubah - jadi jumlah
    main, siapa yang duduk, dan duduk-beruntun semuanya tidak tersentuh. Yang
    berubah hanya siapa berpartner dengan siapa dan siapa melawan siapa.
    """
    quad = st.matches[r][mi]
    lama = list(quad)
    a, b, c, d = lama
    baru = [a, c, b, d] if varian == 1 else [a, d, c, b]
    st._touch_match(quad, -1, r)
    quad[:] = baru
    st._touch_match(quad, +1, r)

    def undo() -> None:
        st._touch_match(quad, -1, r)
        quad[:] = lama
        st._touch_match(quad, +1, r)

    return undo


def _try_cross(st: ScheduleState, r: int, i: int, pi: int, j: int, pj: int):
    """Tukar dua pemain yang sama-sama main, antar match di ronde yang sama."""
    ms = st.matches[r]
    st._touch_match(ms[i], -1, r)
    st._touch_match(ms[j], -1, r)
    ms[i][pi], ms[j][pj] = ms[j][pj], ms[i][pi]
    st._touch_match(ms[i], +1, r)
    st._touch_match(ms[j], +1, r)

    def undo() -> None:
        st._touch_match(ms[i], -1, r)
        st._touch_match(ms[j], -1, r)
        ms[i][pi], ms[j][pj] = ms[j][pj], ms[i][pi]
        st._touch_match(ms[i], +1, r)
        st._touch_match(ms[j], +1, r)

    return undo


def _repeating_players(st: ScheduleState) -> set[int]:
    """Siapa saja yang terlibat pertemuan berulang.

    Cuma mereka yang perlu digeser, dan itu biasanya segelintir orang - jadi
    pencarian gerakan berpasangan yang mahal bisa dipersempit ke mereka saja
    alih-alih menjajal seluruh jadwal.
    """
    out: set[int] = set()
    n = st.n
    for i in range(n):
        base = i * n
        for j in range(i + 1, n):
            if st.pc[base + j] > 1 or st.oc[base + j] > 1:
                out.add(i)
                out.add(j)
    return out


def wait_thresholds(st: ScheduleState) -> list[int]:
    """Rentetan duduk terpanjang yang masih wajar untuk tiap pemain.

    Ini membedakan "algoritmanya kurang rapi" dari "court memang tidak cukup".
    Pemain yang main m dari R ronde punya R-m ronde duduk yang harus dibagi ke
    paling banyak m+1 sela (sebelum match pertama, di antara tiap dua match,
    sesudah match terakhir). Sebaran paling merata memberi rentetan terpanjang
    ceil((R-m) / (m+1)), dan tidak ada susunan yang bisa lebih pendek dari itu.

    10 orang di 1 court: tiap orang main 6 dari 15 ronde, jadi 9 ronde duduk di
    7 sela - menunggu 2 ronde memang tak terhindarkan, menunggu 4 tidak.

    Ronde tempat seorang pemain memang tidak boleh turun (babak putra untuk
    peserta putri) ikut terhitung sebagai duduk, jadi ambangnya jadi lebih
    longgar di meet bersegmen. Itu arah yang aman: yang dikejar cuma rentetan
    yang jelas berlebihan, bukan setiap rentetan.
    """
    out = []
    for p in range(st.n):
        duduk = st.n_rounds - st.play_count[p]
        out.append(math.ceil(duduk / (st.play_count[p] + 1)) if duduk > 0 else 0)
    return out


def wait_runs(st: ScheduleState) -> list[int]:
    """Rentetan duduk terpanjang yang BENAR-BENAR dialami tiap pemain."""
    out = [0] * st.n
    for p in range(st.n):
        r = 0
        while r < st.n_rounds:
            if p not in st.byes[r]:
                r += 1
                continue
            mulai = r
            while r < st.n_rounds and p in st.byes[r]:
                r += 1
            out[p] = max(out[p], r - mulai)
    return out


def turn_skips(st: ScheduleState) -> int:
    """Berapa kali antrean giliran diserobot sepanjang jadwal.

    Satu serobotan = seseorang turun untuk kali ke-(k+1) padahal ada peserta
    lain yang sedang duduk, boleh turun di ronde itu, dan baru main kurang dari
    k kali.

    Ini ukuran yang BERBEDA dari panjang rentetan duduk, dan keduanya perlu.
    Rentetan mengukur berapa lama satu orang menunggu; serobotan mengukur
    apakah urutannya adil dibanding orang lain. Jadwal bisa punya rentetan
    pendek merata - tidak ada yang menunggu lebih dari 3 ronde - sambil tetap
    membiarkan satu orang main di ronde 1 dan 3 sementara orang lain baru turun
    di ronde 4. Diukur pada satu roster nyata: rentetan terpanjang tetap 3 di
    lima nilai effort, sementara serobotan berayun 2, 4, 5, 15, 4 - jadi ada
    yang bergerak besar tanpa satu pun angka lama menunjukkannya.

    Ayunan itu SEBARAN ANTAR LINTASAN ACAK, bukan akibat effort, dan bedanya
    penting supaya tidak ada yang mengejar perbaikan ke arah yang salah. Pernah
    ditulis di sini bahwa menaikkan effort memperburuk giliran; dibandingkan
    berpasangan pada 6 konfigurasi x 12 seed, itu tidak benar - effort 160.000
    lawan 30.000 memberi 38 seed membaik, 31 memburuk, 3 sama, dan rata-rata
    serobotannya justru turun. Yang nyata simpangannya: pada 16 putra + 10 putri
    di 4 court, serobotan berkisar 37 sampai 98 pada effort yang sama, simpangan
    baku 18 - jauh lebih besar daripada selisih antar level effort, yang 5,3.
    attempts pun tidak menyempitkannya (lihat scheduler._lebih_baik). Jadi yang
    membuat fungsi ini perlu ada bukan effort, melainkan kenyataan bahwa tidak
    satu tahap pun menilai urutan giliran.

    Peserta yang memang tidak boleh turun di ronde itu (peserta putri di babak
    putra) tidak dihitung sedang menunggu - ia tidak sedang dilewati, ia sedang
    tidak berhak.
    """
    sudah = [0] * st.n
    total = 0
    for r in range(st.n_rounds):
        turun = [p for q in st.matches[r] for p in q]
        if turun:
            main_r = set(turun)
            elig = (st.rules.round_eligible[r]
                    if r < len(st.rules.round_eligible) else None)
            duduk = [sudah[p] for p in range(st.n)
                     if p not in main_r and (elig is None or p in elig)]
            if duduk:
                paling_tertinggal = min(duduk)
                total += sum(1 for p in turun if sudah[p] > paling_tertinggal)
        for p in turun:
            sudah[p] += 1
    return total


def _terserobot(st: ScheduleState) -> set[int]:
    """Siapa yang terlibat serobotan: yang menyerobot dan yang diserobot."""
    sudah = [0] * st.n
    out: set[int] = set()
    for r in range(st.n_rounds):
        turun = [p for q in st.matches[r] for p in q]
        if turun:
            main_r = set(turun)
            elig = (st.rules.round_eligible[r]
                    if r < len(st.rules.round_eligible) else None)
            nganggur = [p for p in range(st.n)
                        if p not in main_r and (elig is None or p in elig)]
            if nganggur:
                lo = min(sudah[p] for p in nganggur)
                lewat = [p for p in turun if sudah[p] > lo]
                if lewat:
                    out.update(lewat)
                    out.update(p for p in nganggur if sudah[p] == lo)
        for p in turun:
            sudah[p] += 1
    return out


def _menunggu_lama(st: ScheduleState) -> set[int]:
    """Siapa saja yang menunggu lebih lama daripada yang seharusnya perlu."""
    ambang = wait_thresholds(st)
    nyata = wait_runs(st)
    return {p for p in range(st.n) if nyata[p] > ambang[p]}


def _tolok(st: ScheduleState) -> tuple[float, float, float, int, int]:
    """Patokan sebelum sebuah gerakan.

    (biaya total, biaya pasangan, biaya menunggu, pasang partner berulang,
    pasang lawan berulang). Dua yang terakhir bukan biaya melainkan hitungan
    kepala - lihat ScheduleState.rep_pc.
    """
    return (st.cost(), st.cost_pair, st.cost_wait, st.rep_pc, st.rep_oc)


def _tolok_serobot(st: ScheduleState) -> tuple:
    """Patokan untuk sapuan serobotan; hitungan serobotannya ikut dibawa.

    Dipisah dari _tolok karena turn_skips() menyapu seluruh jadwal (O(ronde x
    peserta)), sedangkan patokan biasa cuma membaca angka yang sudah
    dipelihara. Hanya sapuan yang memang membutuhkannya yang membayar itu.
    """
    return (st.cost(), st.cost_pair, st.cost_wait, st.rep_pc, st.rep_oc,
            turn_skips(st))


def _serobotan_membaik(st: ScheduleState, sebelum: tuple) -> bool:
    """Terima kalau serobotan berkurang tanpa membayar apa pun.

    Tiga hal dijaga sekaligus, dan semuanya sebagai batas - bukan bobot:
    jumlah pasang berulang tidak boleh bertambah, dan biaya menunggu tidak
    boleh naik. Jadi sapuan ini hanya merapikan URUTAN giliran, dan tidak bisa
    menukarnya dengan keunikan maupun dengan rentetan duduk yang lebih panjang.
    """
    return (turn_skips(st) < sebelum[5]
            and st.rep_pc <= sebelum[3]
            and st.rep_oc <= sebelum[4]
            and st.cost_wait <= sebelum[2] + 1e-9)


def _biaya_turun(st: ScheduleState, sebelum: tuple) -> bool:
    """Terima kalau biaya TOTAL turun. Dipakai membersihkan pertemuan berulang."""
    return st.cost() < sebelum[0] - 1e-9


def _giliran_membaik(st: ScheduleState, sebelum: tuple) -> bool:
    """Terima kalau giliran membaik DAN keunikan tidak dibayar sedikit pun.

    Ini bukan penurunan biaya total, dan bedanya penting. Biaya total adalah
    penjumlahan berbobot, jadi ia selalu bisa menukar: satu rentetan menunggu
    yang dipecah cukup mahal untuk membeli satu pertemuan lawan yang berulang.
    Diukur pada 324 kasus, itu bukan kekhawatiran teoretis - dengan biaya
    menunggu sekuat 250, tiga puluh enam jadwal yang tadinya nol lawan berulang
    kehilangannya, termasuk 20 putra + 6 putri di 1 court yang melompat ke 9
    pasang berulang.
    Menurunkan bobotnya bukan jawaban: pada bobot yang cukup kecil untuk aman,
    giliran hampir tidak diperbaiki lagi.

    Jadi syaratnya dibuat leksikografis, bukan berbobot: berapa PASANG yang
    berulang tidak boleh bertambah sama sekali, berapa pun besar perbaikan
    gilirannya.

    Yang dijaga hitungan pasangnya, bukan biaya konveksnya, dan itu perbedaan
    yang menentukan. Biaya konveks berubah setiap kali pengulangan digeser dari
    satu pasangan ke pasangan lain, jadi menjaganya akan menolak juga gerakan
    yang tidak merugikan siapa pun - dan pada meet yang pengulangannya memang
    tak terhindarkan, itu berarti hampir semua gerakan ditolak dan gilirannya
    tidak pernah membaik. Hitungan pasang adalah angka yang benar-benar
    dipertaruhkan: ia yang dilihat host di ringkasan, dan ia yang dipakai
    _lebih_baik untuk memilih di antara beberapa percobaan.

    APA YANG DIBAYAR OLEH KEPUTUSAN INI, diukur supaya tidak dikira terlewat.
    Pada 282 kasus lintas roster, urutan gender, dan effort produksi, 98 di
    antaranya punya peserta yang match pertamanya datang lebih telat daripada
    yang sebenarnya perlu - 87 telat satu ronde, 9 telat dua, 2 telat tiga.
    Itu sisa yang tidak bisa dibereskan tanpa menaikkan pengulangan.

    Sudah dicoba dan dibatalkan: sapuan khusus yang menyasar putaran pertama,
    dengan pagar yang sama persis (pasang berulang tidak naik, biaya menunggu
    tidak naik). Hasilnya 1 kasus membaik dari 282 - tidak sepadan dengan satu
    sapuan O(ronde x peserta) tambahan per kandidat gerakan.

    Diagnosisnya jelas dan pagar mana yang mengikat sudah diukur satu per satu:
    melepas pagar biaya menunggu tidak mengubah apa pun (identik di kelima kasus
    uji), sedangkan melepas pagar lawan membereskan 3 dari 5 - dan ongkosnya 12
    putra + 8 putri di 2 court kehilangan rekor nol lawan berulangnya, jadi 3
    pasang. Jadi telatnya match pertama BUKAN kelemahan sapuan; ia harga dari
    keputusan di fungsi ini. Dijaga uji test_keunikan_menang_atas_giliran.
    """
    return (st.cost_wait < sebelum[2] - 1e-9
            and st.rep_pc <= sebelum[3]
            and st.rep_oc <= sebelum[4])


def _paired_bye_swaps(st: ScheduleState, max_steps: int = 60,
                      terima=_biaya_turun, panas_fn=_repeating_players,
                      tolok_fn=_tolok) -> int:
    """Tukar dengan yang istirahat, lalu tukar balik di ronde lain.

    Ada pengulangan yang tidak bisa dibuang oleh gerakan mana pun di dalam satu
    ronde: satu-satunya lawan segar yang tersisa buat seseorang sedang duduk di
    ronde itu. Menariknya turun berarti menukar dengan yang istirahat, dan itu
    menggeser jumlah main - persis yang baru saja diratakan rebalance_plays().

    Jadi gerakannya dipasangkan. `keluar` turun di r1 digantikan `masuk`, lalu
    di ronde r2 - tempat `masuk` main dan `keluar` kebetulan duduk - keduanya
    ditukar balik. Bersihnya: keduanya cuma bertukar ronde. Jumlah main
    keduanya, dan semua orang lain, sama persis seperti sebelumnya.

    Yang bergeser cuma LETAK istirahatnya, jadi duduk-beruntun bisa memburuk -
    itu sudah terhitung di biaya total, dan gerakan hanya diterima kalau biaya
    totalnya tetap turun.

    Justru karena ia menggeser LETAK istirahat tanpa menyentuh jumlah main,
    gerakan ini pula satu-satunya yang bisa membetulkan giliran yang terlewat.
    "P4 main di ronde 1 dan 2 sementara P10 baru turun di ronde 4" cuma bisa
    diperbaiki dengan menukar keduanya di dua ronde sekaligus - menariknya
    sendirian akan membuat salah satu kehilangan satu ronde main.

    Karena itu fungsi ini dipakai untuk dua tujuan, dan yang membedakannya cuma
    dua argumen: siapa yang dicari (`panas_fn`) dan gerakan seperti apa yang
    diterima (`terima`). Membersihkan pertemuan berulang memakai patokan biaya
    total; membetulkan giliran memakai patokan leksikografis yang melarang
    keunikan ikut terbayar. Lihat _giliran_membaik untuk alasannya.
    """
    swaps = 0
    for _ in range(max_steps):
        panas = panas_fn(st)
        if not panas:
            break
        dapat = False
        for r1 in range(st.n_rounds):
            if dapat:
                break
            duduk1 = sorted(st.byes[r1])
            if not duduk1:
                continue
            elig1 = (st.rules.round_eligible[r1]
                     if r1 < len(st.rules.round_eligible) else None)
            for mi in range(len(st.matches[r1])):
                if dapat:
                    break
                for pi in range(4):
                    keluar = st.matches[r1][mi][pi]
                    for masuk in duduk1:
                        # Cukup salah satu sisi yang bermasalah. Untuk
                        # pertemuan berulang yang perlu digeser adalah yang
                        # SEDANG MAIN; untuk giliran yang terlewat yang perlu
                        # ditarik turun adalah yang SEDANG DUDUK. Menyaring
                        # dari satu sisi saja akan menutup separuh perbaikan.
                        if keluar not in panas and masuk not in panas:
                            continue
                        if elig1 is not None and masuk not in elig1:
                            continue
                        before = tolok_fn(st)
                        undo1 = _try_swap(st, r1, mi, pi, masuk)
                        if not st.rules.quad_ok(st.matches[r1][mi], r1):
                            undo1()
                            continue
                        if _balas(st, r1, keluar, masuk, before, terima):
                            swaps += 1
                            dapat = True
                            break
                        undo1()
                    if dapat:
                        break
        if not dapat:
            break
    return swaps


def _balas(st: ScheduleState, r1: int, keluar: int, masuk: int,
           before: tuple, terima=_biaya_turun) -> bool:
    """Cari ronde yang bisa membalas pertukaran di r1 supaya jumlah main utuh."""
    for r2 in range(st.n_rounds):
        if r2 == r1 or masuk in st.byes[r2] or keluar not in st.byes[r2]:
            continue
        elig2 = (st.rules.round_eligible[r2]
                 if r2 < len(st.rules.round_eligible) else None)
        if elig2 is not None and keluar not in elig2:
            continue
        for mj, quad in enumerate(st.matches[r2]):
            if masuk not in quad:
                continue
            undo2 = _try_swap(st, r2, mj, quad.index(masuk), keluar)
            if (st.rules.quad_ok(st.matches[r2][mj], r2)
                    and terima(st, before)):
                return True
            undo2()
            break
    return False


def polish_pairs(st: ScheduleState, max_steps: int = 200) -> int:
    """Sapu deterministik terakhir: buang pengulangan yang masih bisa dibuang.

    Kenapa masih ada yang tersisa padahal annealing baru saja jalan 160.000
    iterasi? Karena keadaan akhir BUKAN optimum lokal annealing. Annealing
    memulihkan keadaan terbaiknya, lalu rebalance_plays() menukar-nukar lagi
    demi meratakan jumlah main - dan pertukaran itu bisa melahirkan pertemuan
    berulang baru yang tidak pernah dinilai siapa pun sesudahnya.

    Sapuan ini memakai dua gerakan yang sama sekali tidak menyentuh jumlah
    main maupun siapa yang duduk, jadi ia tidak bisa merusak kerataan yang baru
    saja dijamin rebalance_plays(): menyusun ulang tim di dalam satu match, dan
    menukar dua pemain yang sama-sama main. Yang diterima hanya gerakan yang
    menurunkan biaya total secara tegas, jadi tidak ada komponen mana pun -
    rating, preferensi, jarak pengulangan - yang digadaikan diam-diam.

    Bedanya dengan annealing: menyeluruh dan deterministik. Annealing menjajal
    tetangga secara acak dan berhenti saat suhunya habis; di sini setiap
    kemungkinan diperiksa sampai satu sapuan penuh tidak menemukan apa pun.

    Perbaikan diterapkan begitu ditemukan, bukan dikumpulkan dulu lalu dipilih
    yang terbaik. Memilih yang terbaik menuntut satu sapuan penuh per satu
    pertukaran, dan pada meet besar (60 orang, 15 court, 20 ronde) itu sendiri
    menghabiskan 9 detik - lebih lama daripada seluruh sisa penjadwalan.

    Dua fase, yang murah dulu. Fase satu cuma menggeser orang di dalam satu
    ronde. Fase dua (_paired_bye_swaps) juga menyentuh yang istirahat, dan itu
    perlu dua pertukaran sekaligus supaya jumlah main tetap utuh - lebih mahal,
    jadi baru dijalankan setelah yang murah kehabisan langkah. Selama fase dua
    masih berbuah, fase satu diulang: menggeser seseorang ke ronde lain sering
    membuka perbaikan sederhana yang tadinya tertutup.
    """
    swaps = 0
    for _ in range(10):
        swaps += _local_sweeps(st, max_steps)
        lanjut = _paired_bye_swaps(st)
        swaps += lanjut
        if not lanjut:
            break
    return swaps


def _tukar_antar_ronde(st: ScheduleState, r1: int, r2: int,
                       rng: random.Random) -> bool:
    """Tukar seorang yang main di r1 dengan seorang yang main di r2.

    Syaratnya masing-masing sedang duduk di ronde yang lain, jadi bersihnya
    keduanya cuma BERTUKAR RONDE: jumlah main setiap orang, dan kerataannya,
    persis sama sesudahnya. Itu yang membuat gerakan ini boleh dipakai
    annealing tahap kedua tanpa membatalkan rebalance_plays().

    Mengembalikan True kalau pertukaran terjadi. Pembatalannya diserahkan ke
    pemanggil, yang sudah menyimpan salinan kedua ronde untuk rollback murah.
    """
    duduk2 = st.byes[r2]
    duduk1 = st.byes[r1]
    # Yang main di r1 tapi duduk di r2, dan sebaliknya.
    calon_a = [(mi, pi) for mi, q in enumerate(st.matches[r1])
               for pi, p in enumerate(q) if p in duduk2]
    calon_b = [(mj, pj) for mj, q in enumerate(st.matches[r2])
               for pj, p in enumerate(q) if p in duduk1]
    if not calon_a or not calon_b:
        return False
    mi, pi = calon_a[rng.randrange(len(calon_a))]
    mj, pj = calon_b[rng.randrange(len(calon_b))]
    a = st.matches[r1][mi][pi]
    b = st.matches[r2][mj][pj]
    if a == b:
        return False

    _try_swap(st, r1, mi, pi, b)
    _try_swap(st, r2, mj, pj, a)
    return True


def anneal_giliran(st: ScheduleState, iterations: int, rng: random.Random,
                   bobot: float = 250.0, progress=None) -> None:
    """Annealing tahap kedua: hanya mengurus giliran, keunikan dikunci.

    Kenapa tahap terpisah, dan bukan satu suku biaya di annealing utama.
    Sebagai suku biaya, perbaikan giliran selalu bisa MEMBELI pengulangan
    lawan - memecah satu rentetan menunggu bernilai lebih besar daripada denda
    satu pasangan yang berhadapan dua kali. Diukur pada 324 kasus, bobot 250 di
    annealing utama membuat 36 jadwal yang tadinya nol lawan berulang
    kehilangannya, satu di antaranya melompat dari 0 ke 9 pasang. Menurunkan
    bobotnya sampai aman membuat gilirannya nyaris tidak membaik. Tidak ada satu
    bobot yang benar untuk kedua keadaan.

    Di sini keunikan berhenti jadi harga dan menjadi BATAS: jumlah pasang yang
    berulang - hasil kerja seluruh tahap sebelumnya - tidak boleh bertambah,
    berapa pun bagusnya perbaikan giliran. Batas keras seperti ini aman justru
    karena keadaan awalnya sudah memenuhi syarat; gerakan yang melanggar
    ditolak, dan pencarian tetap punya banyak ruang untuk bergerak.

    Gerakannya pun dibatasi yang tidak mengubah jumlah main sama sekali:
    menukar ronde antara dua orang, menyusun ulang tim di dalam satu match, dan
    menukar dua orang yang sama-sama main. Jadi kerataan jumlah main yang baru
    dijamin rebalance_plays() tidak bisa rusak di sini, dan tidak perlu
    diratakan ulang setelahnya.

    Berbeda dari ratakan_giliran(), yang hanya mengambil perbaikan yang PERSIS
    gratis dan karena itu cepat mentok: banyak perbaikan giliran menuntut satu
    langkah yang sementara memburuk sebelum membaik, dan hanya annealing yang
    bisa melewatinya.
    """
    ronde = [r for r in range(st.n_rounds) if st.matches[r]]
    if len(ronde) < 2:
        return

    batas_pc, batas_oc = st.rep_pc, st.rep_oc
    bobot_lama = st.w.long_wait
    st.w.long_wait = bobot
    st.recompute_wait()

    current = st.cost()
    best = current
    best_snap = st.snapshot()
    t0 = max(50.0, st.cost_wait * 0.05 + 50.0)
    t_end = 0.05
    tick = max(1, iterations // 10)

    for it in range(iterations):
        if progress is not None and it % tick == 0:
            progress(it / iterations, f"Meratakan giliran {it * 100 // iterations}%")
        temp = t0 * math.pow(t_end / t0, it / iterations)
        before = current

        r1 = rng.choice(ronde)
        roll = rng.random()
        if roll < 0.6:
            r2 = rng.choice(ronde)
            if r2 == r1:
                continue
            saved = [([q[:] for q in st.matches[t]], set(st.byes[t]))
                     for t in (r1, r2)]
            if not _tukar_antar_ronde(st, r1, r2, rng):
                continue
            touched = (r1, r2)
        else:
            # Gerakan di dalam satu ronde: keempat orangnya tetap turun, jadi
            # jumlah main jelas tidak tersentuh. Ini yang membuka jalan bagi
            # pertukaran ronde berikutnya dengan menata ulang lawannya.
            saved = [([q[:] for q in st.matches[r1]], set(st.byes[r1]))]
            ms = st.matches[r1]
            if len(ms) >= 2 and roll < 0.8:
                i, j = rng.sample(range(len(ms)), 2)
                _try_cross(st, r1, i, rng.randrange(4), j, rng.randrange(4))
            else:
                _try_reorder(st, r1, rng.randrange(len(ms)), rng.randrange(2) + 1)
            touched = (r1,)

        boleh = (st.rep_pc <= batas_pc and st.rep_oc <= batas_oc
                 and all(st.round_legal(t) for t in touched))
        if boleh:
            current = st.cost()
            delta = current - before
            boleh = delta <= 0 or rng.random() < math.exp(-delta / max(temp, 1e-9))

        if boleh:
            if current < best - 1e-9:
                best = current
                best_snap = st.snapshot()
        else:
            for t in touched:
                for q in st.matches[t]:
                    st._touch_match(q, -1, t)
                for p in list(st.byes[t]):
                    st._set_bye(t, p, False)
            for t, (quads, byes) in zip(touched, saved):
                st.matches[t] = [q[:] for q in quads]
                for q in st.matches[t]:
                    st._touch_match(q, +1, t)
                for p in sorted(byes):
                    st._set_bye(t, p, True)
            current = st.cost()

    if best < current - 1e-9:
        st.restore(best_snap)
    st.w.long_wait = bobot_lama
    st.recompute_wait()


def ratakan_giliran(st: ScheduleState, max_steps: int = 60,
                    bobot: float = 250.0) -> int:
    """Betulkan giliran yang terlewat, tanpa membayarnya dengan keunikan.

    Dijalankan paling akhir, dan sengaja sebagai tahap sendiri alih-alih sebagai
    suku biaya yang kuat di annealing.

    Alasannya diukur, bukan diperkirakan. Sebagai suku biaya, perbaikan giliran
    selalu bisa membeli pengulangan lawan: memecah satu rentetan menunggu yang
    panjang bernilai lebih besar daripada denda satu pasangan yang berhadapan
    dua kali, jadi annealing akan mengambilnya. Pada sapuan 324 kasus, biaya
    menunggu sekuat 250 membuat 36 jadwal yang tadinya nol lawan berulang
    kehilangannya - satu di antaranya melompat dari 0 ke 9 pasang. Menurunkan
    bobotnya sampai aman membuat gilirannya nyaris tidak diperbaiki lagi; tidak
    ada satu bobot yang benar untuk kedua keadaan sekaligus.

    Di sini pertanyaannya tidak lagi "berapa harganya" melainkan "gratis atau
    tidak": gerakan diterima hanya kalau biaya menunggu turun DAN biaya
    pasangan - partner, lawan, denda pernah-ketemu-2x - tidak naik sedikit pun.
    Jadi giliran diperbaiki di semua tempat yang tidak menuntut tebusan, dan
    keunikan yang sudah dicapai tidak pernah tergerus.

    Gerakannya berpasangan (lihat _paired_bye_swaps), jadi jumlah main tiap
    orang persis sama seperti sebelum sapuan ini - kerataan yang baru dijamin
    rebalance_plays() tidak bisa rusak di sini.

    Bobot menunggu dipasang sendiri di sini karena Weights.long_wait sengaja nol
    di annealing utama; tanpa ini biaya menunggu selalu nol dan sapuan ini tidak
    punya apa pun untuk diperbaiki.
    """
    bobot_lama = st.w.long_wait
    if not bobot_lama:
        st.w.long_wait = bobot
        st.recompute_wait()
    try:
        # Dua sapuan bergantian, karena keduanya mengukur hal yang berbeda:
        # yang pertama memendekkan rentetan menunggu, yang kedua merapikan
        # urutan giliran antar peserta. Memperbaiki salah satunya sering membuka
        # perbaikan yang tadinya tertutup di satu lagi, jadi diulang sampai
        # keduanya tidak menemukan apa pun.
        total = 0
        for _ in range(10):
            langkah = _paired_bye_swaps(st, max_steps, terima=_giliran_membaik,
                                        panas_fn=_menunggu_lama)
            langkah += _paired_bye_swaps(st, max_steps, terima=_serobotan_membaik,
                                         panas_fn=_terserobot,
                                         tolok_fn=_tolok_serobot)
            total += langkah
            if not langkah:
                break
        return total
    finally:
        if st.w.long_wait != bobot_lama:
            st.w.long_wait = bobot_lama
            st.recompute_wait()


def _local_sweeps(st: ScheduleState, max_steps: int) -> int:
    """Sapuan yang hanya menggeser orang di dalam satu ronde."""
    swaps = 0
    for _ in range(max_steps):
        moved = 0
        for r in range(st.n_rounds):
            ms = st.matches[r]
            for mi in range(len(ms)):
                for varian in (1, 2):
                    before = st.cost()
                    undo = _try_reorder(st, r, mi, varian)
                    if st.rules.quad_ok(ms[mi], r) and st.cost() < before - 1e-9:
                        moved += 1
                        break                       # susunan ini sudah dipakai
                    undo()
            for i in range(len(ms)):
                for j in range(i + 1, len(ms)):
                    sudah = False
                    for pi in range(4):
                        for pj in range(4):
                            before = st.cost()
                            undo = _try_cross(st, r, i, pi, j, pj)
                            if (st.rules.quad_ok(ms[i], r)
                                    and st.rules.quad_ok(ms[j], r)
                                    and st.cost() < before - 1e-9):
                                moved += 1
                                sudah = True
                                break
                            undo()
                        if sudah:
                            break

        swaps += moved
        if not moved:
            break

    return swaps


def rebalance_plays(st: ScheduleState, max_steps: int = 500) -> int:
    """Ratakan jumlah main sampai selisihnya mencapai minimum yang mungkin.

    Annealing meminimalkan biaya gabungan, jadi kerataan main selalu bisa
    tergadai demi tujuan lain - dan makin lama optimasinya, makin sering
    tergadai. Ini penjamin deterministiknya, dijalankan setelah annealing.

    Aturannya sederhana: selama ada pemain yang main dua ronde lebih banyak
    daripada pemain lain DAN ada pertukaran sah yang memperbaikinya, tukar -
    pilih yang paling murah. Tiap langkah menurunkan jumlah kuadrat jumlah-main,
    jadi prosesnya pasti berhenti. Hasil akhirnya selisih maksimal 1; dan kalau
    total slot habis dibagi jumlah pemain, selisih 1 mustahil (jumlahnya tidak
    akan cocok), sehingga hasilnya rata sempurna.
    """
    swaps = 0
    for _ in range(max_steps):
        counts = play_counts(st)
        order = sorted(range(st.n), key=lambda p: counts[p])
        best = None

        # Pasangan paling timpang lebih dulu; berhenti begitu ada yang bisa.
        pairs = [(counts[hi] - counts[lo], hi, lo)
                 for hi in reversed(order) for lo in order
                 if counts[hi] - counts[lo] >= 2]
        if not pairs:
            break
        pairs.sort(key=lambda t: -t[0])

        for _gap, hi, lo in pairs:
            for r in range(st.n_rounds):
                if lo not in st.byes[r]:
                    continue
                eligible = (st.rules.round_eligible[r]
                            if r < len(st.rules.round_eligible) else None)
                if eligible is not None and lo not in eligible:
                    continue
                for mi, quad in enumerate(st.matches[r]):
                    if hi not in quad:
                        continue
                    pi = quad.index(hi)
                    before = st.cost()
                    undo = _try_swap(st, r, mi, pi, lo)
                    ok = st.rules.quad_ok(st.matches[r][mi], r)
                    delta = st.cost() - before
                    undo()
                    if ok and (best is None or delta < best[0]):
                        best = (delta, r, mi, pi, lo)
            if best is not None:
                break

        if best is None:
            break
        _, r, mi, pi, lo = best
        _try_swap(st, r, mi, pi, lo)
        swaps += 1

    return swaps


def anneal(
    st: ScheduleState,
    iterations: int,
    rng: random.Random,
    start_temp: float | None = None,
    progress=None,
) -> float:
    """Simulated annealing. Mengembalikan biaya akhir (state dimodifikasi in-place).

    `progress(frac, pesan)` dipanggil sesekali kalau diberikan - angkanya nyata
    (iterasi yang sudah dijalani dan biaya terbaik saat ini), bukan animasi.
    """
    rounds_with_matches = [r for r in range(st.n_rounds) if st.matches[r]]
    if not rounds_with_matches:
        return st.cost()

    current = st.cost()
    best = current
    best_snap = st.snapshot()

    t0 = start_temp if start_temp is not None else max(50.0, current * 0.05 + 50.0)
    t_end = 0.05
    locked = st.rules.locked

    peers = swap_groups(st)

    tick = max(1, iterations // 25)
    for it in range(iterations):
        if progress is not None and it % tick == 0:
            progress(it / iterations,
                     f"Optimasi {it * 100 // iterations}% - biaya terbaik "
                     f"{best:,.0f}".replace(",", "."))
        temp = t0 * math.pow(t_end / t0, it / iterations)
        r = rng.choice(rounds_with_matches)

        before = current
        roll = rng.random()

        # Tukar ronde utuh: satu-satunya gerakan yang menyentuh dua ronde, jadi
        # dipilih lebih dulu supaya rollback tahu apa saja yang harus disimpan.
        r2 = None
        if roll >= 0.92:
            group = peers.get(r)
            if group:
                r2 = rng.choice([x for x in group if x != r])

        touched = (r, r2) if r2 is not None else (r,)
        # Simpan kondisi ronde untuk rollback murah.
        saved = [([q[:] for q in st.matches[t]], set(st.byes[t]))
                 for t in touched]

        if r2 is not None:
            moved = _swap_rounds(st, r, r2)
        elif locked:
            # Ada peserta berpartner tetap. Gerakan tim utuh dipakai untuk mereka,
            # gerakan per-pemain tetap dipakai untuk peserta yang rotasi bebas
            # (gerakan yang memecah pasangan terkunci ditolak quad_ok).
            if roll < 0.35:
                moved = _swap_teams_between_matches(st, r, rng)
            elif roll < 0.55:
                moved = _swap_team_with_bye(st, r, rng)
            elif roll < 0.70:
                moved = _swap_within_match(st, r, rng)
            elif roll < 0.88:
                moved = _swap_between_matches(st, r, rng)
            else:
                moved = _swap_with_bye(st, r, rng)
        elif roll < 0.40:
            moved = _swap_within_match(st, r, rng)
        elif roll < 0.75:
            moved = _swap_between_matches(st, r, rng)
        else:
            moved = _swap_with_bye(st, r, rng)

        if not moved:
            continue

        # Batas keras: gerakan ilegal langsung dibatalkan, tanpa masuk cost.
        accept = all(st.round_legal(t) for t in touched)
        if accept:
            current = st.cost()
            delta = current - before
            accept = delta <= 0 or rng.random() < math.exp(-delta / max(temp, 1e-9))

        if accept:
            if current < best - 1e-9:
                best = current
                best_snap = st.snapshot()
        else:
            # Bongkar semua ronde yang tersentuh dulu, baru pasang kembali.
            # Biaya duduk-beruntun membaca tetangga, jadi kalau dua ronde yang
            # bersebelahan dibongkar-pasang bergantian, hitungannya melenceng.
            for t in touched:
                for q in st.matches[t]:
                    st._touch_match(q, -1, t)
                for p in list(st.byes[t]):
                    st._set_bye(t, p, False)
            for t, (quads, byes) in zip(touched, saved):
                st.matches[t] = [q[:] for q in quads]
                for q in st.matches[t]:
                    st._touch_match(q, +1, t)
                for p in sorted(byes):
                    st._set_bye(t, p, True)
            current = st.cost()

    if best < current - 1e-9:
        st.restore(best_snap)
    return st.cost()
