"""Susunan laporan PDF jadwal meet padel.

Isi laporan:
  1. Ringkasan acara (setup, format, kualitas jadwal)
  2. Jadwal per ronde: siapa lawan siapa, di court mana, jam berapa,
     siapa wasit, siapa ballboy, siapa yang istirahat
  3. Rekap per pemain: jumlah main, istirahat, dan tugas
  4. Catatan & permintaan peserta yang tidak terpenuhi
"""

from __future__ import annotations

from .models import Schedule
from .pdf import PDF, truncate

INK = (0.10, 0.11, 0.13)
MUTED = (0.42, 0.45, 0.50)
LINE = (0.85, 0.87, 0.90)
BAND = (0.94, 0.96, 0.98)
ACCENT = (0.05, 0.35, 0.55)
WHITE = (1, 1, 1)
WARN = (0.70, 0.35, 0.05)

PREF_LABELS = {
    "women_only": "court isi 4 perempuan",
    "men_only": "court isi 4 laki-laki",
    "same_gender": "court satu gender",
    "mixed_team": "partner lawan jenis",
}


def _clock(minutes: int, start_clock: str | None) -> str:
    if not start_clock:
        return f"+{minutes}'"
    try:
        hh, mm = (int(x) for x in start_clock.split(":")[:2])
    except (ValueError, IndexError):
        return f"+{minutes}'"
    total = hh * 60 + mm + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def _header(pdf: PDF, title: str, subtitle: str) -> None:
    pdf.rect(0, 0, pdf.size[0], 64, fill=ACCENT)
    pdf.text(pdf.margin, 20, title, 17, bold=True, color=WHITE)
    pdf.text(pdf.margin, 42, subtitle, 9.5, color=(0.85, 0.90, 0.95))
    pdf.y = 84


def _section(pdf: PDF, label: str) -> None:
    pdf.ensure(48)
    pdf.text(pdf.margin, pdf.y, label.upper(), 9, bold=True, color=ACCENT)
    pdf.y += 13
    pdf.line(pdf.margin, pdf.y, pdf.size[0] - pdf.margin, pdf.y, LINE, 0.8)
    pdf.y += 9


def _kv_grid(pdf: PDF, items: list[tuple[str, str]], cols: int = 3) -> None:
    """Grid label/nilai untuk ringkasan acara."""
    col_w = pdf.content_width / cols
    row_h = 30
    for i, (k, v) in enumerate(items):
        if i % cols == 0:
            pdf.ensure(row_h + 4)
            row_top = pdf.y
        x = pdf.margin + (i % cols) * col_w
        pdf.text(x, row_top, k, 7.5, color=MUTED)
        pdf.text(x, row_top + 11, v, 11, bold=True, color=INK)
        if i % cols == cols - 1 or i == len(items) - 1:
            pdf.y = row_top + row_h
    pdf.y += 4


def _round_block(pdf: PDF, schedule: Schedule, rnd, names: dict[int, str],
                 start_clock: str | None, show_roles: bool) -> None:
    cw = pdf.content_width
    # Kolom: court | tim A | vs | tim B | wasit | ballboy
    if show_roles:
        w_court, w_vs, w_role = 26, 18, 66
        w_team = (cw - w_court - w_vs - 2 * w_role) / 2
    else:
        w_court, w_vs, w_role = 26, 18, 0
        w_team = (cw - w_court - w_vs) / 2

    x_court = pdf.margin
    x_a = x_court + w_court
    x_vs = x_a + w_team
    x_b = x_vs + w_vs
    x_ref = x_b + w_team
    x_ball = x_ref + w_role

    block_h = 22 + len(rnd.matches) * 15 + 16
    pdf.ensure(block_h + 10)

    # Bilah judul ronde.
    pdf.rect(pdf.margin, pdf.y, cw, 18, fill=BAND)
    label = f"Ronde {rnd.index}"
    if rnd.segment:
        label += f"  -  {rnd.segment}"
    pdf.text(x_court + 5, pdf.y + 4.5, label, 9.5, bold=True, color=INK)
    pdf.text_right(pdf.margin + cw - 6, pdf.y + 4.5,
                   _clock(rnd.start_min, start_clock), 9, color=MUTED)
    pdf.y += 22

    if show_roles:
        pdf.text(x_ref, pdf.y, "WASIT", 6.5, bold=True, color=MUTED)
        pdf.text(x_ball, pdf.y, "BALLBOY", 6.5, bold=True, color=MUTED)
        pdf.y += 9

    ref_by_court = {r.court: names[r.player_id]
                    for r in rnd.roles if r.role == "wasit"}
    ball_by_court = {r.court: names[r.player_id]
                     for r in rnd.roles if r.role == "ballboy"}

    for m in rnd.matches:
        pool = rnd.court_labels.get(m.court, "")
        pdf.text(x_court, pdf.y, f"C{m.court}", 9, bold=True, color=ACCENT)
        team_a = f"{names[m.team_a[0]]} & {names[m.team_a[1]]}"
        team_b = f"{names[m.team_b[0]]} & {names[m.team_b[1]]}"
        pdf.text(x_a, pdf.y, truncate(team_a, w_team - 6, 9), 9, color=INK)
        pdf.text(x_vs, pdf.y, "vs", 8, color=MUTED)
        pdf.text(x_b, pdf.y, truncate(team_b, w_team - 6, 9), 9, color=INK)
        if show_roles:
            pdf.text(x_ref, pdf.y,
                     truncate(ref_by_court.get(m.court, "-"), w_role - 6, 8), 8,
                     color=MUTED)
            pdf.text(x_ball, pdf.y,
                     truncate(ball_by_court.get(m.court, "-"), w_role - 6, 8), 8,
                     color=MUTED)
        if pool:
            pdf.text_right(pdf.margin + cw, pdf.y, pool, 7, color=MUTED)
        pdf.y += 15

    idle = rnd.resting_only()
    if idle:
        text = "Istirahat: " + ", ".join(names[b] for b in idle)
        pdf.text(x_court, pdf.y, truncate(text, cw, 8), 8, color=MUTED)
        pdf.y += 12
    pdf.y += 6


def build_pdf(
    schedule: Schedule,
    title: str = "Jadwal Meet Padel",
    event_date: str = "",
    venue: str = "",
    start_clock: str | None = None,
) -> bytes:
    """Rakit laporan PDF lengkap dan kembalikan byte-nya."""
    names = {p.id: p.name for p in schedule.players}
    cfg = schedule.config
    st = schedule.stats
    show_roles = bool(cfg.referees_per_court or cfg.ballboys_per_court)

    pdf = PDF()
    subtitle_bits = [b for b in (event_date, venue) if b]
    subtitle_bits.append(f"{len(schedule.players)} peserta")
    _header(pdf, title, "  |  ".join(subtitle_bits))

    # -- Ringkasan --------------------------------------------------------
    _section(pdf, "Ringkasan acara")
    fmt = "Americano"
    mode_names = {"americano": "Americano", "tiered": "Berdasarkan pool rating",
                  "mexicano": "Mexicano (seimbang rating)", "team": "Pasangan tetap"}
    fmt = mode_names.get(cfg.mode, cfg.mode)
    if cfg.segments and any(s.label for s in cfg.segments):
        fmt = " + ".join(f"{s.label} {s.rounds}r" for s in cfg.segments if s.rounds)

    plays = list(st.plays_per_player.values()) or [0]
    _kv_grid(pdf, [
        ("FORMAT", fmt),
        ("COURT", str(cfg.courts)),
        ("DURASI", f"{cfg.duration_minutes} menit"),
        ("JUMLAH RONDE", str(len(schedule.rounds))),
        ("MENIT / RONDE", str(cfg.round_minutes)),
        ("MAIN PER ORANG", f"{min(plays)}-{max(plays)} ronde"),
        ("PARTNER BERULANG", f"{st.partner_repeat_pairs} pasang"),
        ("LAWAN BERULANG", f"{st.opponent_repeat_pairs} pasang"),
        ("KUALITAS JADWAL", f"{st.quality_score}/100"),
    ])

    # -- Jadwal -----------------------------------------------------------
    _section(pdf, "Jadwal pertandingan")
    for rnd in schedule.rounds:
        _round_block(pdf, schedule, rnd, names, start_clock, show_roles)

    # -- Rekap pemain -----------------------------------------------------
    pdf.new_page()
    _section(pdf, "Rekap per pemain")
    cw = pdf.content_width
    cols = [("NO", 26), ("NAMA", 150), ("RATING", 48), ("G", 24),
            ("MAIN", 42), ("ISTIRAHAT", 62)]
    if show_roles:
        cols += [("WASIT", 48), ("BALLBOY", 52)]

    def draw_row(values, bold=False, color=INK, size=8.5):
        x = pdf.margin
        for (label, w), val in zip(cols, values):
            pdf.text(x, pdf.y, truncate(str(val), w - 5, size, bold), size,
                     bold=bold, color=color)
            x += w
        pdf.y += 14

    pdf.rect(pdf.margin, pdf.y - 3, cw, 15, fill=BAND)
    draw_row([c[0] for c in cols], bold=True, color=MUTED, size=7.5)
    pdf.line(pdf.margin, pdf.y - 4, pdf.margin + cw, pdf.y - 4, LINE)

    for i, p in enumerate(sorted(schedule.players, key=lambda x: x.name.lower()), 1):
        pdf.ensure(20)
        roles = st.roles_per_player.get(p.id, {})
        row = [
            i, p.name, f"{p.rating:g}",
            {"M": "L", "F": "P"}.get(p.gender or "", "-"),
            st.plays_per_player.get(p.id, 0),
            st.byes_per_player.get(p.id, 0),
        ]
        if show_roles:
            row += [roles.get("wasit", 0), roles.get("ballboy", 0)]
        draw_row(row)
        pdf.line(pdf.margin, pdf.y - 4, pdf.margin + cw, pdf.y - 4,
                 (0.94, 0.95, 0.96))

    # -- Catatan ----------------------------------------------------------
    if schedule.notes or schedule.violations:
        pdf.y += 10
        _section(pdf, "Catatan")
        for note in schedule.notes:
            pdf.ensure(24)
            pdf.text(pdf.margin, pdf.y, "-", 8.5, bold=True, color=MUTED)
            # Pecah catatan panjang jadi beberapa baris.
            words, line = note.split(), ""
            for w in words:
                trial = f"{line} {w}".strip()
                if len(trial) > 118:
                    pdf.text(pdf.margin + 10, pdf.y, line, 8.5, color=INK)
                    pdf.y += 11
                    pdf.ensure(20)
                    line = w
                else:
                    line = trial
            if line:
                pdf.text(pdf.margin + 10, pdf.y, line, 8.5, color=INK)
                pdf.y += 14

        if schedule.violations:
            pdf.y += 4
            pdf.ensure(30)
            pdf.text(pdf.margin, pdf.y, "Permintaan yang tidak terpenuhi:", 8.5,
                     bold=True, color=WARN)
            pdf.y += 13
            for v in schedule.violations[:20]:
                pdf.ensure(16)
                pdf.text(pdf.margin + 10, pdf.y,
                         truncate(f"Ronde {v.round_index} - {v.player_name}: "
                                  f"{PREF_LABELS.get(v.preference, v.preference)}",
                                  cw - 20, 8), 8, color=INK)
                pdf.y += 11

    # -- Nomor halaman ----------------------------------------------------
    total = len(pdf.pages)
    for i, page in enumerate(pdf.pages, 1):
        saved_y, saved_pages = pdf.y, pdf.pages
        pdf.pages = [page]
        pdf.text_right(pdf.size[0] - pdf.margin, pdf.size[1] - 26,
                       f"Halaman {i} dari {total}", 7.5, color=MUTED)
        pdf.text(pdf.margin, pdf.size[1] - 26,
                 "Dibuat dengan generator jadwal padel", 7.5, color=MUTED)
        pdf.pages, pdf.y = saved_pages, saved_y

    return pdf.output(title=title)
