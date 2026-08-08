"""Tipe data inti untuk penjadwalan padel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["americano", "tiered", "mexicano", "team"]

# Semua mode yang didukung generator.
MODES: tuple[str, ...] = ("americano", "tiered", "mexicano", "team")

# Aturan komposisi pemain dalam satu segmen jadwal.
#   open        -> siapa saja boleh main & berpasangan dengan siapa saja
#   men         -> hanya pemain putra yang turun
#   women       -> hanya pemain putri yang turun
#   same_gender -> tiap tim harus satu gender (putra+putra / putri+putri)
#   mixed       -> tiap tim wajib 1 putra + 1 putri
SEGMENT_RULES: tuple[str, ...] = ("open", "men", "women", "same_gender", "mixed")

# Susunan gender satu tim: LL = dua putra, PP = dua putri, LP = campur.
TEAM_SHAPES: tuple[str, ...] = ("LL", "LP", "PP")

# Semua format match yang mungkin, dilihat dari susunan kedua tim. Kodenya
# ditulis urut abjad ("LL-PP", bukan "PP-LL") supaya satu pertandingan hanya
# punya satu nama - tanpa itu, melarang "LL-PP" tidak ikut melarang "PP-LL".
MATCHUPS: tuple[str, ...] = (
    "LL-LL",   # putra vs putra
    "LL-LP",   # dua putra vs campur
    "LL-PP",   # dua putra vs dua putri
    "LP-LP",   # campur vs campur
    "LP-PP",   # campur vs dua putri
    "PP-PP",   # putri vs putri
)

MATCHUP_LABELS: dict[str, str] = {
    "LL-LL": "Putra vs putra",
    "LL-LP": "Dua putra vs campur",
    "LL-PP": "Dua putra vs dua putri",
    "LP-LP": "Campur vs campur",
    "LP-PP": "Campur vs dua putri",
    "PP-PP": "Putri vs putri",
}


def team_shape(g1: str | None, g2: str | None) -> str | None:
    """Susunan gender satu tim. None kalau ada gender yang belum diisi."""
    if g1 is None or g2 is None:
        return None
    return "LL" if g1 == g2 == "M" else "PP" if g1 == g2 == "F" else "LP"


def matchup_code(shape_a: str | None, shape_b: str | None) -> str | None:
    """Nama format match dari susunan kedua tim, urut abjad."""
    if shape_a is None or shape_b is None:
        return None
    return "-".join(sorted((shape_a, shape_b)))


@dataclass
class Player:
    """Satu peserta meet.

    rating memakai skala bebas (mis. 1.0-7.0 ala padel rating, atau 1-5).
    Yang dipakai algoritma hanya urutan & selisihnya, bukan nilai absolutnya.
    """

    id: int
    name: str
    rating: float = 3.0
    # "M" | "F" | None. Wajib diisi kalau ada segmen putra/putri/mixed.
    gender: str | None = None
    # Diisi generator untuk mode tiered; None kalau tidak relevan.
    tier: int | None = None
    # Id rekan tetap, kalau peserta ini minta partner dikunci. Berlaku di mode
    # apa pun dan boleh sebagian: peserta lain tetap rotasi bebas.
    partner_id: int | None = None
    # Permintaan komposisi court dari peserta ini. Bersifat lunak: kalau tidak
    # bisa dipenuhi, jadwal tetap jadi dan pelanggarannya dilaporkan.
    #   None | "women_only" | "men_only" | "same_gender" | "mixed_team"
    court_preference: str | None = None


# Nilai yang sah untuk Player.court_preference.
COURT_PREFERENCES: tuple[str, ...] = (
    "women_only", "men_only", "same_gender", "mixed_team",
)


@dataclass
class PreferenceViolation:
    """Permintaan peserta yang tidak bisa dipenuhi di ronde tertentu."""

    round_index: int
    player_id: int
    player_name: str
    preference: str
    reason: str


@dataclass
class Segment:
    """Satu babak dalam meet, dengan aturan komposisinya sendiri.

    Contoh format nyata: 3 ronde putra, 3 ronde putri, lalu 6 ronde mixed.
    Keunikan partner & lawan tetap dihitung LINTAS segmen, jadi orang yang
    sudah jadi lawanmu di babak putra dihindari lagi di babak mixed.
    """

    label: str
    rounds: int
    rule: str = "open"

    def __post_init__(self) -> None:
        if self.rule not in SEGMENT_RULES:
            raise ValueError(f"Aturan segmen tidak dikenal: {self.rule}")
        if self.rounds < 0:
            raise ValueError("Jumlah ronde segmen tidak boleh negatif.")


@dataclass
class Match:
    court: int
    team_a: tuple[int, int]
    team_b: tuple[int, int]

    def players(self) -> tuple[int, int, int, int]:
        return (*self.team_a, *self.team_b)


@dataclass
class RoleAssignment:
    """Tugas untuk peserta yang sedang tidak main."""

    player_id: int
    role: str  # "wasit" | "ballboy"
    court: int


@dataclass
class Round:
    index: int  # 1-based, untuk tampilan
    matches: list[Match] = field(default_factory=list)
    byes: list[int] = field(default_factory=list)
    start_min: int = 0
    end_min: int = 0
    # Label segmen ("Putra" / "Putri" / "Mixed"), kosong kalau meet satu babak.
    segment: str = ""
    # Mode tiered: label pool per court, supaya bisa ditampilkan di UI.
    court_labels: dict[int, str] = field(default_factory=dict)
    # Wasit & ballboy ronde ini, diambil dari yang istirahat.
    roles: list[RoleAssignment] = field(default_factory=list)

    def resting_only(self) -> list[int]:
        """Yang istirahat TANPA tugas apa pun."""
        busy = {r.player_id for r in self.roles}
        return [b for b in self.byes if b not in busy]


@dataclass
class Config:
    """Parameter yang dipilih host sebelum generate."""

    courts: int
    duration_minutes: int
    round_minutes: int = 12
    warmup_minutes: int = 10
    mode: str = "americano"
    # Kalau diisi, meng-override jumlah ronde hasil hitungan durasi.
    rounds_override: int | None = None
    # Mode tiered: berapa pool rating yang dibentuk.
    tier_count: int = 2
    seed: int = 42
    # Iterasi optimasi. Lebih tinggi = lebih rapi, tapi lebih lama.
    effort: int = 30_000
    # Tugas untuk yang istirahat. 0 = nonaktif.
    referees_per_court: int = 0
    ballboys_per_court: int = 0
    # Meet bersegmen (mis. putra/putri/mixed). Kosong = satu babak biasa.
    segments: list[Segment] = field(default_factory=list)
    # Sebarkan ronde tiap babak merata sepanjang acara, bukan berurutan sebagai
    # blok. Tanpa ini "Putri 4" lalu "Putra 4" berarti para putri main 4 ronde
    # beruntun sementara para putra duduk 4 ronde beruntun.
    interleave_segments: bool = False
    # Kalau True, durasi per ronde dihitung otomatis dari total ronde segmen
    # agar pas dengan jam sewa.
    fit_rounds_to_duration: bool = True
    # Format match yang boleh muncul, dilihat dari susunan gender kedua tim.
    # Kosong/None = semua boleh (perilaku lama, dan tetap jadi default).
    #
    # Ini bukan aturan yang sama dengan Segment.rule. Segment mengatur SIAPA
    # yang turun dan bagaimana satu tim disusun; ini mengatur tim seperti apa
    # boleh berhadapan dengan tim seperti apa. Host memakainya untuk mencegah
    # pertandingan yang timpang, mis. dua putra melawan dua putri.
    allowed_matchups: list[str] | None = None

    def __post_init__(self) -> None:
        if self.courts < 1:
            raise ValueError("Jumlah court minimal 1.")
        if self.allowed_matchups is not None:
            tidak_dikenal = set(self.allowed_matchups) - set(MATCHUPS)
            if tidak_dikenal:
                raise ValueError(
                    f"Format match tidak dikenal: {', '.join(sorted(tidak_dikenal))}")
            if not self.allowed_matchups:
                raise ValueError(
                    "Minimal satu format match harus diizinkan, kalau tidak "
                    "tidak ada satu pun susunan yang sah.")
        if self.round_minutes < 1:
            raise ValueError("Durasi per ronde minimal 1 menit.")
        if self.mode not in MODES:
            raise ValueError(f"Mode tidak dikenal: {self.mode}")

    def total_segment_rounds(self) -> int:
        return sum(s.rounds for s in self.segments)


@dataclass
class PairStat:
    """Ringkasan satu pasang pemain: berapa kali partner, berapa kali lawan."""

    a: int
    b: int
    as_partner: int
    as_opponent: int


@dataclass
class ScheduleStats:
    rounds: int
    players: int
    # Berapa pasang yang partner-an lebih dari sekali, dan maksimum pengulangannya.
    partner_repeat_pairs: int
    partner_repeat_max: int
    opponent_repeat_pairs: int
    opponent_repeat_max: int
    # Berapa pasang yang sama sekali belum pernah ketemu (partner maupun lawan).
    never_met_pairs: int
    plays_per_player: dict[int, int]
    byes_per_player: dict[int, int]
    back_to_back_byes: int
    # Rata-rata selisih total rating antar tim dalam satu match.
    avg_rating_gap: float
    max_rating_gap: float
    # 0-100, ringkasan kualitas jadwal untuk ditampilkan ke host.
    quality_score: float
    # id pemain -> {"total": n, "wasit": n, "ballboy": n}
    roles_per_player: dict[int, dict[str, int]] = field(default_factory=dict)


@dataclass
class Schedule:
    players: list[Player]
    config: Config
    rounds: list[Round]
    stats: ScheduleStats
    notes: list[str] = field(default_factory=list)
    # Permintaan peserta yang tidak terpenuhi. Dilaporkan apa adanya supaya
    # host bisa memberi tahu yang bersangkutan sebelum acara.
    violations: list[PreferenceViolation] = field(default_factory=list)
