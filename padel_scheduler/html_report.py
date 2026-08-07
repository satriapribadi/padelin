"""Laporan jadwal dalam HTML + CSS, dirancang untuk dicetak jadi PDF.

Dipakai lewat tombol Print browser (Ctrl+P -> Save as PDF). Hasilnya jauh lebih
rapi daripada PDF yang digambar manual, tanpa perlu library converter apa pun.

Yang diurus khusus untuk cetak:
  - @page A4 dengan margin yang benar
  - kartu ronde tidak terpotong di tengah antar halaman
  - warna latar tetap tercetak (print-color-adjust)
  - elemen layar (tombol, navigasi) disembunyikan saat cetak
"""

from __future__ import annotations

import html

from .models import Schedule

PREF_LABELS = {
    "women_only": "court isi 4 perempuan",
    "men_only": "court isi 4 laki-laki",
    "same_gender": "court satu gender",
    "mixed_team": "partner lawan jenis",
}

MODE_LABELS = {
    "americano": "Americano",
    "tiered": "Pool berdasarkan rating",
    "mexicano": "Mexicano (seimbang rating)",
    "team": "Pasangan tetap",
}

CSS = """
:root{
  --ink:#12151a; --muted:#5b6472; --line:#e2e6ec; --band:#f5f7fa;
  --accent:#0d5c8c; --accent-soft:#e8f1f7; --warn:#a2560b; --warn-soft:#fdf3e7;
  --good:#1a7a4c; --good-soft:#e8f6ef;
}
*{box-sizing:border-box}
body{
  margin:0; padding:32px 28px 60px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  color:var(--ink); background:#fff; font-size:13px; line-height:1.5;
  -webkit-print-color-adjust:exact; print-color-adjust:exact;
}
.sheet{max-width:900px;margin:0 auto}

.masthead{
  border-bottom:3px solid var(--accent); padding-bottom:16px; margin-bottom:22px;
  display:flex; justify-content:space-between; align-items:flex-end; gap:24px;
}
.masthead h1{margin:0 0 6px; font-size:26px; letter-spacing:-.02em}
.masthead .meta{color:var(--muted); font-size:12.5px}
.badge{
  background:var(--accent); color:#fff; border-radius:999px;
  padding:7px 15px; font-size:12px; font-weight:600; white-space:nowrap;
}

.tiles{display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  gap:10px; margin-bottom:26px}
.tile{background:var(--band); border:1px solid var(--line); border-radius:9px;
  padding:11px 13px}
.tile .k{font-size:9.5px; text-transform:uppercase; letter-spacing:.07em;
  color:var(--muted); font-weight:600}
.tile .v{font-size:17px; font-weight:700; margin-top:3px; letter-spacing:-.01em}
.tile .s{font-size:10.5px; color:var(--muted); margin-top:1px}

h2{font-size:11px; text-transform:uppercase; letter-spacing:.09em;
  color:var(--accent); margin:30px 0 12px; padding-bottom:7px;
  border-bottom:1px solid var(--line)}

.segbar{
  background:var(--accent-soft); border-left:4px solid var(--accent);
  padding:8px 13px; border-radius:0 6px 6px 0; margin:18px 0 12px;
  font-weight:700; font-size:13px; color:var(--accent);
}
.round{border:1px solid var(--line); border-radius:9px; margin-bottom:11px;
  overflow:hidden; break-inside:avoid; page-break-inside:avoid}
.round-head{background:var(--band); padding:7px 13px; display:flex;
  justify-content:space-between; align-items:center; border-bottom:1px solid var(--line)}
.round-head .n{font-weight:700; font-size:13px}
.round-head .t{color:var(--muted); font-size:11.5px; font-variant-numeric:tabular-nums}

table{width:100%; border-collapse:collapse}
.matches td{padding:7px 13px; border-bottom:1px solid #f0f2f5; vertical-align:middle}
.matches tr:last-child td{border-bottom:none}
.court{font-weight:700; color:var(--accent); width:34px; font-size:12px}
.team{font-size:12.5px}
.vs{color:var(--muted); font-size:10.5px; text-align:center; width:26px;
  font-style:italic}
.duty{color:var(--muted); font-size:10.5px; white-space:nowrap; text-align:right}
.duty b{color:var(--ink); font-weight:600}
.pool{display:inline-block; background:var(--accent-soft); color:var(--accent);
  border-radius:4px; padding:1px 6px; font-size:9.5px; font-weight:600;
  margin-left:6px}
.resting{padding:6px 13px; background:#fafbfc; color:var(--muted);
  font-size:11px; border-top:1px solid #f0f2f5}

.recap{border:1px solid var(--line); border-radius:9px; overflow:hidden}
.recap th{background:var(--band); text-align:left; padding:8px 11px;
  font-size:9.5px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); border-bottom:1px solid var(--line)}
.recap td{padding:7px 11px; border-bottom:1px solid #f2f4f7; font-size:12px}
.recap tr:last-child td{border-bottom:none}
.recap td.num{font-variant-numeric:tabular-nums; text-align:center}
.recap tbody tr:nth-child(even){background:#fcfdfe}

.note{background:var(--band); border-left:3px solid var(--muted);
  padding:9px 13px; border-radius:0 6px 6px 0; margin-bottom:8px; font-size:12px}
.note.warn{background:var(--warn-soft); border-left-color:var(--warn)}
.foot{margin-top:34px; padding-top:12px; border-top:1px solid var(--line);
  color:var(--muted); font-size:10.5px; display:flex; justify-content:space-between}

.toolbar{position:sticky; top:0; background:#fff; padding:10px 0 16px;
  margin:-32px auto 8px; max-width:900px; display:flex; gap:9px; z-index:5}
.toolbar button{
  background:var(--accent); color:#fff; border:0; border-radius:7px;
  padding:9px 17px; font-size:13px; font-weight:600; cursor:pointer;
  font-family:inherit;
}
.toolbar button.ghost{background:#fff; color:var(--accent);
  border:1px solid var(--accent)}
.toolbar button:hover{opacity:.9}

@media print{
  body{padding:0; font-size:11.5px}
  .toolbar{display:none !important}
  .sheet{max-width:none}
  h2{margin-top:20px}
  .round{border-color:#d8dde4}
  .masthead{margin-bottom:16px}
  .tiles{margin-bottom:18px; gap:7px}
  thead{display:table-header-group}
  tr{break-inside:avoid; page-break-inside:avoid}
}
@page{size:A4 portrait; margin:14mm 12mm}
"""


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _clock(minutes: int, start_clock: str | None) -> str:
    if not start_clock:
        return f"+{minutes} mnt"
    try:
        hh, mm = (int(x) for x in start_clock.split(":")[:2])
    except (ValueError, IndexError):
        return f"+{minutes} mnt"
    total = hh * 60 + mm + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def build_html(
    schedule: Schedule,
    title: str = "Jadwal Meet Padel",
    event_date: str = "",
    venue: str = "",
    start_clock: str | None = None,
    include_toolbar: bool = True,
) -> str:
    """Rakit laporan HTML lengkap sebagai satu dokumen mandiri."""
    names = {p.id: p.name for p in schedule.players}
    cfg = schedule.config
    st = schedule.stats
    show_roles = bool(cfg.referees_per_court or cfg.ballboys_per_court)

    fmt = MODE_LABELS.get(cfg.mode, cfg.mode)
    if cfg.segments and any(s.label for s in cfg.segments):
        fmt = " + ".join(f"{s.label} {s.rounds}r" for s in cfg.segments if s.rounds)

    plays = list(st.plays_per_player.values()) or [0]
    meta_bits = [b for b in (event_date, venue) if b]
    meta_bits.append(f"{len(schedule.players)} peserta")
    meta_bits.append(f"{cfg.courts} court")
    meta_bits.append(f"{cfg.duration_minutes} menit")

    parts: list[str] = []
    parts.append("<!doctype html><html lang='id'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append(f"<title>{_e(title)}</title><style>{CSS}</style></head><body>")

    if include_toolbar:
        parts.append(
            "<div class='toolbar'>"
            "<button onclick='window.print()'>Simpan sebagai PDF</button>"
            "<button class='ghost' onclick='window.close()'>Tutup</button>"
            "</div>"
        )

    parts.append("<div class='sheet'>")

    # Kepala
    parts.append(
        f"<div class='masthead'><div>"
        f"<h1>{_e(title)}</h1>"
        f"<div class='meta'>{_e('  ·  '.join(meta_bits))}</div></div>"
        f"<div class='badge'>{_e(fmt)}</div></div>"
    )

    # Kartu angka
    tiles = [
        ("Ronde", str(len(schedule.rounds)), f"{cfg.round_minutes} menit / ronde"),
        ("Main per orang", f"{min(plays)}-{max(plays)}", "ronde"),
        ("Partner berulang", str(st.partner_repeat_pairs), "pasang"),
        ("Lawan berulang", str(st.opponent_repeat_pairs), "pasang"),
        ("Kualitas", f"{st.quality_score}", "dari 100"),
    ]
    if show_roles:
        duties = sum(v.get("total", 0) for v in st.roles_per_player.values())
        tiles.append(("Tugas dibagikan", str(duties), "wasit + ballboy"))
    parts.append("<div class='tiles'>")
    for k, v, s in tiles:
        parts.append(
            f"<div class='tile'><div class='k'>{_e(k)}</div>"
            f"<div class='v'>{_e(v)}</div><div class='s'>{_e(s)}</div></div>"
        )
    parts.append("</div>")

    # Jadwal
    parts.append("<h2>Jadwal pertandingan</h2>")
    current_segment = None
    for rnd in schedule.rounds:
        if rnd.segment and rnd.segment != current_segment:
            current_segment = rnd.segment
            parts.append(f"<div class='segbar'>{_e(rnd.segment)}</div>")

        refs = {r.court: names[r.player_id] for r in rnd.roles if r.role == "wasit"}
        balls = {r.court: names[r.player_id] for r in rnd.roles if r.role == "ballboy"}

        parts.append("<div class='round'>")
        parts.append(
            f"<div class='round-head'><span class='n'>Ronde {rnd.index}</span>"
            f"<span class='t'>{_e(_clock(rnd.start_min, start_clock))}</span></div>"
        )
        parts.append("<table class='matches'>")
        for m in rnd.matches:
            pool = rnd.court_labels.get(m.court, "")
            pool_html = f"<span class='pool'>{_e(pool)}</span>" if pool else ""
            duty_bits = []
            if m.court in refs:
                duty_bits.append(f"wasit <b>{_e(refs[m.court])}</b>")
            if m.court in balls:
                duty_bits.append(f"ballboy <b>{_e(balls[m.court])}</b>")
            duty_html = (f"<td class='duty'>{' &nbsp; '.join(duty_bits)}</td>"
                         if duty_bits else "<td class='duty'></td>")
            parts.append(
                f"<tr><td class='court'>C{m.court}</td>"
                f"<td class='team'>{_e(names[m.team_a[0]])} &amp; "
                f"{_e(names[m.team_a[1]])}{pool_html}</td>"
                f"<td class='vs'>vs</td>"
                f"<td class='team'>{_e(names[m.team_b[0]])} &amp; "
                f"{_e(names[m.team_b[1]])}</td>{duty_html}</tr>"
            )
        parts.append("</table>")
        idle = rnd.resting_only()
        if idle:
            parts.append(
                f"<div class='resting'>Istirahat: "
                f"{_e(', '.join(names[b] for b in idle))}</div>"
            )
        parts.append("</div>")

    # Rekap pemain
    parts.append("<h2>Rekap per pemain</h2>")
    parts.append("<table class='recap'><thead><tr>")
    headers = ["Nama", "Rating", "L/P", "Main", "Istirahat"]
    if show_roles:
        headers += ["Wasit", "Ballboy"]
    parts.append("".join(f"<th>{_e(h)}</th>" for h in headers))
    parts.append("</tr></thead><tbody>")
    for p in sorted(schedule.players, key=lambda x: x.name.lower()):
        roles = st.roles_per_player.get(p.id, {})
        cells = [
            f"<td>{_e(p.name)}</td>",
            f"<td class='num'>{p.rating:g}</td>",
            f"<td class='num'>{_e({'M': 'L', 'F': 'P'}.get(p.gender or '', '-'))}</td>",
            f"<td class='num'>{st.plays_per_player.get(p.id, 0)}</td>",
            f"<td class='num'>{st.byes_per_player.get(p.id, 0)}</td>",
        ]
        if show_roles:
            cells += [
                f"<td class='num'>{roles.get('wasit', 0)}</td>",
                f"<td class='num'>{roles.get('ballboy', 0)}</td>",
            ]
        parts.append("<tr>" + "".join(cells) + "</tr>")
    parts.append("</tbody></table>")

    # Catatan
    if schedule.notes or schedule.violations:
        parts.append("<h2>Catatan</h2>")
        for note in schedule.notes:
            parts.append(f"<div class='note'>{_e(note)}</div>")
        for v in schedule.violations[:25]:
            parts.append(
                f"<div class='note warn'>Ronde {v.round_index} - "
                f"{_e(v.player_name)} minta "
                f"{_e(PREF_LABELS.get(v.preference, v.preference))}, "
                f"tapi komposisi court tidak memungkinkan.</div>"
            )

    parts.append(
        f"<div class='foot'><span>{_e(title)}</span>"
        f"<span>Dibuat dengan generator jadwal padel</span></div>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)
