"""Analisa kelayakan jadwal: court, durasi, dan batas matematis keunikan.

Modul ini yang menjawab pertanyaan "apakah rencana saya masuk akal?" SEBELUM
jadwal dibuat. Dipanggil UI setiap kali host mengubah angka, supaya host
langsung lihat konsekuensinya.

Dua kegagalan yang saling berlawanan, dan keduanya diperiksa di sini:

  1. Grup KECIL + durasi PANJANG  -> pengulangan partner/lawan tak terhindarkan.
  2. Grup BESAR + court SEDIKIT   -> terlalu banyak pemain duduk menunggu.

Host biasanya cuma sadar masalah (1) dan kaget kena masalah (2), atau sebaliknya.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

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


def analyze(
    n_players: int,
    courts: int,
    duration_minutes: int,
    round_minutes: int = 12,
    warmup_minutes: int = 10,
    rounds_override: int | None = None,
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

    total_slots = rounds * slots_per_round
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
    opponent_ok = effective_rounds <= max_opponent_rounds

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

    if not opponent_ok and n_players >= 4:
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
                    f"{byes_per_round} orang duduk tiap ronde "
                    f"({rest_ratio * 100:.0f}%)",
                    f"Rata-rata tiap peserta main {avg_plays:.1f} dari {rounds} "
                    f"ronde, yaitu ~{playing_minutes:.0f} menit di lapangan dari "
                    f"{duration_minutes} menit sewa.",
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
        total_slots=total_slots,
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
