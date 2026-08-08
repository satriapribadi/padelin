"""Optimasi jadwal lewat simulated annealing dengan evaluasi delta O(1).

Konstruksi eksak (1-factorization / Latin square) hanya menjamin PARTNER unik.
Siapa lawan siapa, siapa yang duduk, dan keseimbangan rating masih harus dicari.
Di situlah optimizer ini bekerja.

Fungsi biaya:

    cost = w_partner  * SUM pc*(pc-1)          # pengulangan partner
         + w_opponent * SUM oc*(oc-1)          # pengulangan lawan
         + w_bye      * SUM bye^2              # ketimpangan istirahat
         + w_b2b      * (duduk 2 ronde beruntun)
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


@dataclass
class Weights:
    partner: float = 1000.0
    opponent: float = 120.0
    # Kerataan jumlah main mengalahkan variasi lawan, dan itu disengaja.
    # Peserta membayar fee yang sama; kehilangan satu ronde main itu kerugian
    # nyata, sedangkan sekali bertemu lawan yang sama hampir tak terasa. Dengan
    # bobot lama (60) satu gerakan yang menambah pengulangan lawan dinilai lebih
    # mahal daripada meratakan giliran, sehingga optimasi yang lebih lama justru
    # membuat jumlah main makin timpang.
    bye: float = 500.0
    b2b_bye: float = 400.0
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

    def quad_ok(self, quad: list[int], r: int) -> bool:
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
        "pc", "oc", "bye_count", "seen_at", "cost_pair", "cost_bye",
        "cost_b2b", "cost_rating", "cost_pref", "cost_repeat",
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
        # susunan match -> ronde-ronde tempat ia muncul (untuk biaya jarak ulang)
        self.seen_at: dict[tuple, list[int]] = {}
        self.cost_pair = 0.0
        self.cost_bye = 0.0
        self.cost_b2b = 0.0
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
        wp, wo = self.w.partner, self.w.opponent
        pc, oc, k = self.pc, self.oc, self._k

        for (i, j) in ((a, b), (c, d)):
            idx = k(i, j)
            cur = pc[idx]
            if sign > 0:
                self.cost_pair += wp * 2 * cur       # (c+1)c - c(c-1) = 2c
                pc[idx] = cur + 1
            else:
                pc[idx] = cur - 1
                self.cost_pair -= wp * 2 * (cur - 1)

        for (i, j) in ((a, c), (a, d), (b, c), (b, d)):
            idx = k(i, j)
            cur = oc[idx]
            if sign > 0:
                self.cost_pair += wo * 2 * cur
                oc[idx] = cur + 1
            else:
                oc[idx] = cur - 1
                self.cost_pair -= wo * 2 * (cur - 1)

        self.cost_rating += sign * self._rating_cost(quad)
        self.cost_pref += sign * self._pref_cost(quad)

    # -- status bye -------------------------------------------------------
    def _set_bye(self, r: int, p: int, on: bool) -> None:
        """Ubah status istirahat pemain p di ronde r, update cost inkremental."""
        wb, wbb = self.w.bye, self.w.b2b_bye
        cur = self.bye_count[p]

        # Tetangga ronde sebelum & sesudah (untuk deteksi duduk beruntun).
        prev_bye = p in self.byes[r - 1] if r > 0 else False
        next_bye = p in self.byes[r + 1] if r + 1 < self.n_rounds else False
        neighbours = (1 if prev_bye else 0) + (1 if next_bye else 0)

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

    # -- total ------------------------------------------------------------
    def cost(self) -> float:
        return (self.cost_pair + self.cost_bye + self.cost_b2b
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
        self.seen_at = {}
        self.cost_pair = self.cost_bye = self.cost_b2b = 0.0
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
