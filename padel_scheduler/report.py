"""Ekspor jadwal: teks siap-share, CSV, dan dict untuk API web."""

from __future__ import annotations

import csv
import io
from dataclasses import asdict
from datetime import date

from .models import Schedule

HARI = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
BULAN = ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember")


def format_date_id(value: str) -> str:
    """ISO (2026-08-09) -> "Sabtu, 9 Agustus 2026".

    Nilai yang bukan ISO dikembalikan apa adanya: acara lama tersimpan dengan
    tanggal berupa teks bebas dan tidak boleh rusak hanya karena formatnya
    berubah.
    """
    value = (value or "").strip()
    try:
        d = date.fromisoformat(value)
    except (ValueError, TypeError):
        return value
    return f"{HARI[d.weekday()]}, {d.day} {BULAN[d.month - 1]} {d.year}"


PREF_LABELS = {
    "women_only": "court isi 4 perempuan",
    "men_only": "court isi 4 laki-laki",
    "same_gender": "court satu gender",
    "mixed_team": "partner lawan jenis",
}


def _clock(minutes_from_start: int, start_clock: str | None) -> str:
    """Ubah offset menit jadi jam dinding kalau host mengisi jam mulai."""
    if not start_clock:
        return f"+{minutes_from_start}'"
    try:
        hh, mm = (int(x) for x in start_clock.split(":")[:2])
    except (ValueError, IndexError):
        return f"+{minutes_from_start}'"
    total = hh * 60 + mm + minutes_from_start
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def to_text(schedule: Schedule, start_clock: str | None = None,
            title: str = "JADWAL PADEL") -> str:
    """Format teks polos, enak dibaca di WhatsApp (tanpa tabel)."""
    names = {p.id: p.name for p in schedule.players}
    cfg = schedule.config
    out: list[str] = []

    out.append(f"*{title}*")
    out.append(
        f"{len(schedule.players)} pemain | {cfg.courts} court | "
        f"{cfg.duration_minutes} menit | {len(schedule.rounds)} ronde "
        f"@ {cfg.round_minutes} menit"
    )
    if cfg.segments and any(s.label for s in cfg.segments):
        fmt = " + ".join(f"{s.label} {s.rounds}" for s in cfg.segments if s.rounds)
        out.append(f"Format: {fmt}")
    out.append("")

    current_segment = None
    for rnd in schedule.rounds:
        if rnd.segment and rnd.segment != current_segment:
            current_segment = rnd.segment
            out.append(f"--- {rnd.segment.upper()} ---")

        out.append(f"*Ronde {rnd.index}* ({_clock(rnd.start_min, start_clock)})")
        refs = {r.court: names[r.player_id] for r in rnd.roles if r.role == "wasit"}
        balls = {r.court: names[r.player_id] for r in rnd.roles if r.role == "ballboy"}
        for m in rnd.matches:
            label = rnd.court_labels.get(m.court, "")
            suffix = f" [{label}]" if label else ""
            out.append(
                f"  C{m.court}{suffix}: "
                f"{names[m.team_a[0]]} & {names[m.team_a[1]]}"
                f"  vs  "
                f"{names[m.team_b[0]]} & {names[m.team_b[1]]}"
            )
            duty = []
            if m.court in refs:
                duty.append(f"wasit {refs[m.court]}")
            if m.court in balls:
                duty.append(f"ballboy {balls[m.court]}")
            if duty:
                out.append(f"      ({', '.join(duty)})")
        idle = rnd.resting_only()
        if idle:
            out.append(f"  Istirahat: {', '.join(names[b] for b in idle)}")
        out.append("")

    st = schedule.stats
    out.append("*Ringkasan*")
    out.append(f"Kualitas jadwal: {st.quality_score}/100")
    out.append(
        f"Partner berulang: {st.partner_repeat_pairs} pasang | "
        f"Lawan berulang: {st.opponent_repeat_pairs} pasang"
    )
    plays = list(st.plays_per_player.values())
    if plays:
        out.append(f"Main per orang: {min(plays)}-{max(plays)} ronde")
    # Tunggu terpanjang disebut bersama batasnya. Tanpa batasnya angkanya
    # menyesatkan: "menunggu 2 ronde" terbaca buruk padahal pada 10 peserta di
    # 1 court itu yang terbaik yang mungkin.
    out.append(
        f"Tunggu terpanjang: {st.longest_wait} ronde "
        f"(paling pendek yang mungkin {st.wait_floor})"
    )
    return "\n".join(out).rstrip() + "\n"


def to_personal_text(schedule: Schedule, start_clock: str | None = None) -> str:
    """Jadwal per orang, untuk di-share ke masing-masing peserta di grup WA.

    Tiap peserta cuma peduli "saya main ronde berapa, lawan siapa, kapan
    giliran saya wasit" -- daftar lengkap 12 ronde justru bikin bingung.
    """
    names = {p.id: p.name for p in schedule.players}
    lines: list[str] = ["*JADWAL PER PEMAIN*", ""]

    for p in sorted(schedule.players, key=lambda x: x.name.lower()):
        lines.append(f"*{p.name}*")
        for rnd in schedule.rounds:
            when = _clock(rnd.start_min, start_clock)
            found = False
            for m in rnd.matches:
                if p.id not in m.players():
                    continue
                found = True
                mate, opp = (
                    (m.team_a, m.team_b) if p.id in m.team_a else (m.team_b, m.team_a)
                )
                partner = [x for x in mate if x != p.id][0]
                lines.append(
                    f"  R{rnd.index} {when} C{m.court}: dgn {names[partner]} "
                    f"vs {names[opp[0]]} & {names[opp[1]]}"
                )
                break
            if found:
                continue
            duty = next((r for r in rnd.roles if r.player_id == p.id), None)
            if duty:
                lines.append(f"  R{rnd.index} {when}: {duty.role} court {duty.court}")
            elif p.id in rnd.byes:
                lines.append(f"  R{rnd.index} {when}: istirahat")
        st = schedule.stats
        roles = st.roles_per_player.get(p.id, {})
        summary = f"  Total: {st.plays_per_player.get(p.id, 0)} ronde main"
        if roles.get("wasit"):
            summary += f", {roles['wasit']}x wasit"
        if roles.get("ballboy"):
            summary += f", {roles['ballboy']}x ballboy"
        lines.append(summary)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def to_csv(schedule: Schedule) -> str:
    """CSV per match, siap dibuka di Excel / Google Sheets."""
    names = {p.id: p.name for p in schedule.players}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "ronde", "segmen", "mulai_menit", "court", "pool",
        "tim_a_1", "tim_a_2", "tim_b_1", "tim_b_2",
        "wasit", "ballboy", "istirahat",
    ])
    for rnd in schedule.rounds:
        idle = " | ".join(names[b] for b in rnd.resting_only())
        refs = {r.court: names[r.player_id] for r in rnd.roles if r.role == "wasit"}
        balls = {r.court: names[r.player_id] for r in rnd.roles if r.role == "ballboy"}
        if not rnd.matches:
            w.writerow([rnd.index, rnd.segment, rnd.start_min, "", "",
                        "", "", "", "", "", "", idle])
        for m in rnd.matches:
            w.writerow([
                rnd.index, rnd.segment, rnd.start_min, m.court,
                rnd.court_labels.get(m.court, ""),
                names[m.team_a[0]], names[m.team_a[1]],
                names[m.team_b[0]], names[m.team_b[1]],
                refs.get(m.court, ""), balls.get(m.court, ""),
                idle,
            ])
    return buf.getvalue()


def to_dict(schedule: Schedule) -> dict:
    """Bentuk JSON-able untuk dikonsumsi UI web."""
    names = {p.id: p.name for p in schedule.players}
    return {
        "players": [asdict(p) for p in schedule.players],
        "config": {
            **asdict(schedule.config),
            "segments": [asdict(s) for s in schedule.config.segments],
        },
        "rounds": [
            {
                "index": r.index,
                "segment": r.segment,
                "start_min": r.start_min,
                "end_min": r.end_min,
                "byes": [{"id": b, "name": names[b]} for b in r.byes],
                "roles": [
                    {
                        "player_id": a.player_id,
                        "name": names[a.player_id],
                        "role": a.role,
                        "court": a.court,
                    }
                    for a in r.roles
                ],
                "resting_only": [
                    {"id": b, "name": names[b]} for b in r.resting_only()
                ],
                "matches": [
                    {
                        "court": m.court,
                        "pool": r.court_labels.get(m.court, ""),
                        "team_a": [{"id": i, "name": names[i]} for i in m.team_a],
                        "team_b": [{"id": i, "name": names[i]} for i in m.team_b],
                    }
                    for m in r.matches
                ],
            }
            for r in schedule.rounds
        ],
        "stats": asdict(schedule.stats),
        "notes": schedule.notes,
        "violations": [
            {**asdict(v), "preference_label": PREF_LABELS.get(v.preference, v.preference)}
            for v in schedule.violations
        ],
    }
