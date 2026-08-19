"""Sisi bisnis penyelenggaraan meet: biaya sewa, fee peserta, dan margin.

Alasan modul ini ada: saran "sewa 5 court biar tidak ada yang duduk" itu benar
secara penjadwalan tapi buta secara ekonomi. Court tambahan menambah biaya tetap
yang harus ditanggung jumlah peserta yang sama, sehingga menekan margin host
atau menaikkan fee peserta.

Jadi keputusan yang sebenarnya bukan "berapa court yang ideal", melainkan:

    berapa court yang memberi waktu main layak DENGAN margin yang masih masuk?

Modul ini menyajikan angka kedua sisi sekaligus supaya host memutuskan sadar,
bukan menebak.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .capacity import analyze

# Ambang waktu main per peserta (menit) untuk menilai "worth it"-nya fee.
DECENT_PLAY_MINUTES = 60.0


@dataclass
class Economics:
    """Parameter biaya & harga. Satuan bebas (pakai Rupiah apa adanya)."""

    court_price_per_hour: float = 0.0
    fee_per_player: float = 0.0
    # Bola, air, hadiah, dokumentasi, dsb. Total untuk satu acara.
    other_costs: float = 0.0


@dataclass
class Option:
    """Satu skenario setup, lengkap dengan konsekuensi main dan uangnya."""

    courts: int
    hours: float
    n_players: int

    # Penjadwalan
    rounds: int
    byes_per_round: int
    rest_ratio: float
    play_minutes_per_player: float
    opponent_unique_feasible: bool
    partner_unique_feasible: bool

    # Keuangan
    court_cost: float
    other_costs: float
    total_cost: float
    revenue: float
    profit: float
    margin_pct: float
    cost_per_player: float
    break_even_fee: float
    # Fee minimal agar tiap peserta membayar <= sekian per menit main.
    cost_per_play_minute: float

    labels: list[str] = field(default_factory=list)


def evaluate(
    n_players: int,
    courts: int,
    hours: float,
    econ: Economics,
    round_minutes: int = 12,
    warmup_minutes: int = 10,
    segments: list[tuple[str, int]] | None = None,
    men: int | None = None,
    women: int | None = None,
    court_hours: float | None = None,
    matches_per_round: list[int] | None = None,
) -> Option:
    """Hitung satu skenario: berapa main, berapa untung.

    Babak dan komposisi gender ikut diteruskan karena keduanya menentukan berapa
    court yang benar-benar bisa terisi. Tanpa itu tiap court dianggap bisa diisi
    siapa saja, dan justru pertanyaan termahal host - "kalau saya tambah 1
    court, peserta dapat tambahan berapa menit?" - yang paling salah dijawab.
    Lihat capacity.court_terpakai() untuk angkanya.

    `court_hours` dan `matches_per_round` untuk acara yang court-nya dilepas di
    tengah jam sewa: yang pertama court-jam yang benar-benar dibayar, yang kedua
    match yang berjalan di tiap ronde. Keduanya harus diberikan bersama - court
    yang berkurang menurunkan ongkos DAN waktu main sekaligus, dan mengoreksi
    satu saja menghasilkan harga-per-menit yang lebih menyesatkan daripada
    sebelum dikoreksi.
    """
    duration = int(round(hours * 60))
    cap = analyze(
        n_players=n_players,
        courts=courts,
        duration_minutes=duration,
        round_minutes=round_minutes,
        warmup_minutes=warmup_minutes,
        segments=segments,
        roster_men=men,
        roster_women=women,
        matches_per_round=matches_per_round,
    )

    court_cost = (court_hours if court_hours is not None else courts * hours) \
        * econ.court_price_per_hour
    total_cost = court_cost + econ.other_costs
    revenue = n_players * econ.fee_per_player
    profit = revenue - total_cost
    margin = (profit / revenue * 100.0) if revenue > 0 else 0.0
    cost_pp = (total_cost / n_players) if n_players else 0.0
    play_min = cap.playing_minutes_per_player
    cost_per_min = (cost_pp / play_min) if play_min > 0 else 0.0

    # Urutan penting: penampil hanya menunjukkan beberapa label pertama, jadi
    # yang berstatus (rugi, court menganggur) harus mendahului yang deskriptif.
    labels: list[str] = []
    if econ.fee_per_player > 0 and profit < 0:
        labels.append("rugi")
    if cap.courts_idle > 0:
        labels.append(f"{cap.courts_idle} court menganggur")
    if cap.rest_ratio > 1 / 3:
        labels.append("banyak yang duduk")
    elif cap.byes_per_round == 0:
        labels.append("semua main terus")
    if not cap.opponent_unique_feasible:
        labels.append("lawan berulang")
    if play_min >= DECENT_PLAY_MINUTES:
        labels.append("waktu main layak")

    return Option(
        courts=courts,
        hours=hours,
        n_players=n_players,
        rounds=cap.rounds,
        byes_per_round=cap.byes_per_round,
        rest_ratio=round(cap.rest_ratio, 4),
        play_minutes_per_player=play_min,
        opponent_unique_feasible=cap.opponent_unique_feasible,
        partner_unique_feasible=cap.partner_unique_feasible,
        court_cost=round(court_cost, 2),
        other_costs=round(econ.other_costs, 2),
        total_cost=round(total_cost, 2),
        revenue=round(revenue, 2),
        profit=round(profit, 2),
        margin_pct=round(margin, 1),
        cost_per_player=round(cost_pp, 2),
        # Modal per peserta boleh pecahan - itu memang biaya. Tapi titik impas
        # adalah AMBANG yang ditagihkan ke peserta, jadi dibulatkan ke atas:
        # menagih 38.324 padahal modalnya 38.324,125 membuat host nombok.
        # Sama semangatnya dengan fee_for_target_margin yang juga ceil.
        break_even_fee=float(math.ceil(cost_pp)),
        cost_per_play_minute=round(cost_per_min, 2),
        labels=labels,
    )


def compare(
    n_players: int,
    econ: Economics,
    court_options: list[int] | None = None,
    hour_options: list[float] | None = None,
    round_minutes: int = 12,
    warmup_minutes: int = 10,
    segments: list[tuple[str, int]] | None = None,
    men: int | None = None,
    women: int | None = None,
) -> list[Option]:
    """Bandingkan beberapa kombinasi court x durasi untuk jumlah peserta ini.

    Hasilnya diurut dari waktu main terbanyak, supaya host melihat langsung
    berapa harga dari setiap kenaikan kenyamanan.
    """
    if court_options is None:
        need = math.ceil(n_players / 4)
        lo = max(1, need - 2)
        court_options = list(range(lo, need + 2))
    if hour_options is None:
        hour_options = [1.0, 1.5, 2.0, 2.5, 3.0]

    out: list[Option] = []
    for c in court_options:
        for h in hour_options:
            opt = evaluate(n_players, c, h, econ, round_minutes,
                           warmup_minutes, segments, men, women)
            if opt.rounds <= 0:
                continue
            out.append(opt)

    out.sort(key=lambda o: (-o.play_minutes_per_player, o.total_cost))
    return out


def fee_for_target_margin(
    n_players: int,
    courts: int,
    hours: float,
    econ: Economics,
    target_margin_pct: float,
    round_to: int = 5000,
    court_hours: float | None = None,
) -> float:
    """Fee per peserta agar margin mencapai target.

    fee = biaya_per_peserta / (1 - margin), lalu dibulatkan ke atas ke kelipatan
    yang wajar untuk diumumkan (default Rp 5.000).

    `court_hours` sama artinya dengan di evaluate(): court-jam yang benar-benar
    dibayar, untuk acara yang court-nya dilepas di tengah jam sewa. Tanpa itu
    fungsi ini menagih court x durasi penuh, dan panel "fee untuk target margin"
    berdiri di atas biaya yang lebih besar daripada kartu "biaya total" di
    sebelahnya - host membaca dua angka yang tidak bisa dua-duanya benar. Pada
    2 court 2 jam yang satu court-nya dilepas di menit ke-60 (90.000/jam,
    8 peserta), selisihnya bukan pembulatan: margin 20% disarankan 60.000
    padahal modalnya cuma menuntut 45.000.
    """
    billed = court_hours if court_hours is not None else courts * hours
    total_cost = billed * econ.court_price_per_hour + econ.other_costs
    if n_players <= 0:
        return 0.0
    cost_pp = total_cost / n_players
    m = max(0.0, min(95.0, target_margin_pct)) / 100.0
    fee = cost_pp / (1.0 - m) if m < 1.0 else cost_pp
    if round_to > 0:
        fee = math.ceil(fee / round_to) * round_to
    return float(fee)


def upgrade_analysis(
    n_players: int,
    courts: int,
    hours: float,
    econ: Economics,
    round_minutes: int = 12,
    warmup_minutes: int = 10,
    segments: list[tuple[str, int]] | None = None,
    men: int | None = None,
    women: int | None = None,
    court_hours: float | None = None,
    matches_per_round: list[int] | None = None,
    court_hours_plus: float | None = None,
    matches_per_round_plus: list[int] | None = None,
) -> dict:
    """Berapa harga sebenarnya dari menambah satu court?

    Menjawab pertanyaan praktis host: "kalau saya tambah 1 court, peserta dapat
    tambahan berapa menit main, dan fee harus naik berapa supaya margin saya
    tidak turun?"

    Diukur pada 5 meet bersegmen, versi yang buta babak melebihkan manfaatnya 8
    sampai 20 menit - dan pada 20 putra + 4 putri ia membalik sarannya: diramal
    +20 menit sehingga worth_it, padahal yang benar-benar terjadi +6,7 menit.
    Court tambahan tidak menolong babak yang gendernya memang tidak cukup untuk
    mengisinya.

    Empat parameter court_hours/matches_per_round untuk acara yang court-nya dilepas
    di tengah jam sewa: sepasang untuk setup host apa adanya, sepasang untuk
    setup yang sama plus satu court. Pemanggil yang menghitungnya, bukan fungsi
    ini - ia tidak tahu pola sewanya, dan menebaknya ("court tambahan disewa
    penuh") akan mencampur dua perubahan sekaligus ke dalam satu selisih menit
    yang lalu dibaca host sebagai manfaat court saja.
    """
    # Skenario host apa adanya, termasuk court yang dilepas di tengah acara.
    base = evaluate(n_players, courts, hours, econ, round_minutes,
                    warmup_minutes, segments, men, women,
                    court_hours=court_hours,
                    matches_per_round=matches_per_round)
    plus = evaluate(n_players, courts + 1, hours, econ, round_minutes,
                    warmup_minutes, segments, men, women,
                    court_hours=court_hours_plus,
                    matches_per_round=matches_per_round_plus)

    extra_cost = plus.total_cost - base.total_cost
    extra_minutes = plus.play_minutes_per_player - base.play_minutes_per_player
    fee_bump = (extra_cost / n_players) if n_players else 0.0

    keep_margin_fee = 0.0
    if econ.fee_per_player > 0 and base.revenue > 0:
        keep_margin_fee = fee_for_target_margin(
            n_players, courts + 1, hours, econ, base.margin_pct,
            # Skenario pembandingnya court_hours_plus, bukan (courts+1) x jam:
            # margin yang dijaga diambil dari base yang SUDAH dikoreksi, jadi
            # menagihnya dengan biaya yang belum dikoreksi menaikkan fee untuk
            # margin yang tidak pernah diminta.
            court_hours=court_hours_plus,
        )

    return {
        "base": base,
        "plus_one_court": plus,
        "extra_cost": round(extra_cost, 2),
        "extra_play_minutes_per_player": round(extra_minutes, 1),
        # Ambang lagi ("agar tidak nombok"), jadi ke atas juga.
        "fee_bump_to_break_even": float(math.ceil(fee_bump)),
        "fee_to_keep_same_margin": round(keep_margin_fee, 2),
        "worth_it": extra_minutes >= 10 and plus.courts <= math.ceil(n_players / 4),
        "note": (
            f"Menambah 1 court menaikkan biaya {extra_cost:,.0f} "
            f"dan waktu main tiap peserta {extra_minutes:+.0f} menit."
            if extra_minutes else
            "Court tambahan tidak menambah waktu main (pemain tidak cukup mengisinya)."
        ),
    }
