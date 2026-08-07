"""Format meet yang sering dipakai, siap pilih di UI.

Menyimpan struktur SEGMEN saja (siapa turun & aturan pasangan). Jumlah court,
durasi, dan mode tetap ditentukan host, karena itu berubah tiap acara.
"""

from __future__ import annotations

from .models import Segment

PRESETS: dict[str, dict] = {
    "single": {
        "label": "Satu babak (Americano biasa)",
        "description": "Semua peserta dalam satu pool, ronde mengalir sampai waktu habis.",
        "segments": [],
        "needs_gender": False,
    },
    "gender_3_3_6": {
        "label": "Putra 3 - Putri 3 - Mixed 6",
        "description": (
            "Format 12 ronde: 3 ronde sesama putra, 3 ronde sesama putri, "
            "lalu 6 ronde mixed (tiap tim 1 putra + 1 putri). Cocok untuk "
            "8 pemain / 1 court / 2 jam."
        ),
        "segments": [
            {"label": "Putra", "rounds": 3, "rule": "men"},
            {"label": "Putri", "rounds": 3, "rule": "women"},
            {"label": "Mixed", "rounds": 6, "rule": "mixed"},
        ],
        "needs_gender": True,
    },
    "gender_2_2_4": {
        "label": "Putra 2 - Putri 2 - Mixed 4",
        "description": "Versi lebih pendek (8 ronde) untuk sewa 1,5 jam.",
        "segments": [
            {"label": "Putra", "rounds": 2, "rule": "men"},
            {"label": "Putri", "rounds": 2, "rule": "women"},
            {"label": "Mixed", "rounds": 4, "rule": "mixed"},
        ],
        "needs_gender": True,
    },
    "mixed_only": {
        "label": "Mixed sepanjang acara",
        "description": "Setiap tim wajib 1 putra + 1 putri di semua ronde.",
        "segments": [{"label": "Mixed", "rounds": 8, "rule": "mixed"}],
        "needs_gender": True,
    },
    "same_gender_only": {
        "label": "Tim sesama gender",
        "description": (
            "Tiap tim harus satu gender (putra+putra atau putri+putri), "
            "tapi boleh saling berhadapan."
        ),
        "segments": [{"label": "Sesama gender", "rounds": 8, "rule": "same_gender"}],
        "needs_gender": True,
    },
    "warmup_then_open": {
        "label": "Pemanasan mixed 2 - Bebas sisanya",
        "description": "Dua ronde mixed untuk mencairkan suasana, sisanya Americano bebas.",
        "segments": [
            {"label": "Mixed", "rounds": 2, "rule": "mixed"},
            {"label": "Bebas", "rounds": 8, "rule": "open"},
        ],
        "needs_gender": True,
    },
}


def preset_segments(key: str) -> list[Segment]:
    """Ubah preset jadi objek Segment. Key tak dikenal -> satu babak biasa."""
    spec = PRESETS.get(key)
    if not spec:
        return []
    return [Segment(**s) for s in spec["segments"]]
