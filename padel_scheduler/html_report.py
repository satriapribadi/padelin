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
from .report import format_date_id

PREF_LABELS = {
    "women_only": "court isi 4 perempuan",
    "men_only": "court isi 4 laki-laki",
    "same_gender": "court satu gender",
    "mixed_team": "partner lawan jenis",
}

APP_MARK = (
    '<svg viewBox="0 0 40 40" role="img" aria-label="Padelin"><rect x="6.5" y="3" width="27" height="25" rx="11.5" fill="none" stroke="currentColor" stroke-width="2.4"/><path d="M13.5 27.2 L17.5 32.4 M26.5 27.2 L22.5 32.4" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><rect x="17.8" y="31.4" width="4.4" height="6.4" rx="2.2" fill="currentColor"/><g fill="#3d9be9"><circle cx="14" cy="11.5" r="1.9"/><circle cx="20" cy="11.5" r="1.9"/><circle cx="26" cy="11.5" r="1.9"/><circle cx="14" cy="19.5" r="1.9"/><circle cx="20" cy="19.5" r="1.9"/><circle cx="26" cy="19.5" r="1.9"/></g></svg>'
)

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
  margin:0; padding:24px 26px 40px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  color:var(--ink); background:#fff; font-size:13px; line-height:1.5;
  -webkit-print-color-adjust:exact; print-color-adjust:exact;
}
.sheet{max-width:900px;margin:0 auto}

/* Format babak panjang ("Sesama gender 8r + Mixed 4r + ...") dulu memaksa
   masthead melar: badge-nya nowrap dan blok judul tidak boleh menyusut, jadi
   keduanya saling dorong sampai badge menembus keluar garis dan menimpa judul
   serta logo. Sekarang badge boleh turun baris (flex-wrap) dan teksnya boleh
   membungkus, sedangkan blok judul diberi min-width:0 supaya ikut menyusut. */
.masthead{
  border-bottom:2px solid var(--accent); padding-bottom:10px; margin-bottom:14px;
  display:flex; flex-wrap:wrap; justify-content:space-between;
  align-items:flex-end; gap:8px 24px;
}
.masthead h1{margin:0 0 3px; font-size:21px; letter-spacing:-.02em}
/* 440px kira-kira lebar logo + judul satu baris. Dijadikan basis flex supaya
   badge yang panjang turun ke barisnya sendiri, bukan menyempitkan judul sampai
   membelah dua baris; badge pendek tetap duduk sebaris seperti biasa. */
.brand{display:flex; align-items:center; gap:14px; flex:1 1 440px; min-width:0}
.brand>div{min-width:0}
.logo{width:40px; height:40px; object-fit:contain; flex:none}
.masthead .meta{color:var(--muted); font-size:12.5px}
.badge{
  background:var(--accent); color:#fff; border-radius:999px;
  padding:7px 15px; font-size:12px; font-weight:600;
  max-width:100%; text-align:right; overflow-wrap:anywhere;
}

/* 94px supaya tujuh kartu (dengan fee) muat sebaris di lebar A4; dengan 104px
   kartu terakhir jatuh sendirian ke baris kedua dan terlihat belum rampung. */
.tiles{display:grid; grid-template-columns:repeat(auto-fit,minmax(94px,1fr));
  gap:5px; margin-bottom:14px}
.tile{background:var(--band); border:1px solid var(--line); border-radius:7px;
  padding:6px 9px}
.tile .k{font-size:8.5px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); font-weight:600}
.tile .v{font-size:15px; font-weight:700; margin-top:1px; letter-spacing:-.01em}
.tile .s{font-size:9px; color:var(--muted); margin-top:0}

h2{font-size:10px; text-transform:uppercase; letter-spacing:.09em;
  color:var(--accent); margin:16px 0 7px; padding-bottom:4px;
  border-bottom:1px solid var(--line)}

.segbar{
  background:var(--accent-soft); border-left:4px solid var(--accent);
  padding:4px 10px; border-radius:0 5px 5px 0;
  font-weight:700; font-size:11px; color:var(--accent);
}
/* Card ronde disusun grid. Jumlah kolom mengikuti lebar isi card: 1 court
   berarti satu match per card, jadi 3 kolom masih lega; 3+ court butuh selebar
   halaman. Tanpa ini laporan 12 ronde memakan 3 halaman. */
.rounds{display:grid; gap:7px; align-items:start}
.rounds.cols-1{grid-template-columns:1fr}
.rounds.cols-2{grid-template-columns:repeat(2,1fr)}
.rounds.cols-3{grid-template-columns:repeat(3,1fr)}
.rounds .segbar{grid-column:1 / -1; margin:10px 0 3px}

.round{border:1px solid var(--line); border-radius:7px;
  overflow:hidden; break-inside:avoid; page-break-inside:avoid}
.round-head{background:var(--band); padding:3px 9px; display:flex;
  justify-content:space-between; align-items:baseline; gap:8px;
  border-bottom:1px solid var(--line)}
.round-head .n{font-weight:700; font-size:11px}
.round-head .t{color:var(--muted); font-size:10px; font-variant-numeric:tabular-nums}

table{width:100%; border-collapse:collapse}

/* Baris match memakai GRID, bukan flex. Dengan flex, lebar kolom tugas berbeda
   tiap baris (nama petugas panjang-pendek) sehingga posisi "vs" ikut bergeser
   dan kolomnya terlihat bergoyang. Grid mengunci kolomnya:

     court | tim A (rata kiri) | vs | tim B (rata kanan) | tugas (lebar tetap)

   Tugas ditaruh di kolom kanan sendiri - bukan di tengah - supaya blok
   "A & B vs C & D" tetap sejajar di semua baris dan mata bisa menyusuri satu
   kolom saja saat mencari lawan. */
.m{display:grid; grid-template-columns:22px 1fr 16px 1fr 112px;
  align-items:baseline; gap:0 6px;
  padding:4px 9px; border-bottom:1px solid #f0f2f5; font-size:11px}
.m:last-of-type{border-bottom:none}
.court{font-weight:700; color:var(--accent); font-size:10px}
.team{min-width:0}
.team.b{text-align:right}
.vs{color:var(--muted); font-size:9px; font-style:italic; text-align:center}

/* Card sempit (1-2 court): tugas turun ke baris sendiri di bawah matchnya. */
.cols-2 .m,.cols-3 .m{grid-template-columns:22px 1fr 16px 1fr}
/* Di card lebar tugas muat sebaris dengan matchnya; hanya di kolom sempit
   ia turun ke baris sendiri. Memaksanya selalu turun membuat tiap match makan
   dua baris dan laporan 4 court membengkak dua kali lipat. */
.duty{color:var(--muted); font-size:9.5px; text-align:right;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.cols-2 .duty,.cols-3 .duty{grid-column:1 / -1; white-space:normal}
.pool{display:inline-block; background:var(--accent-soft); color:var(--accent);
  border-radius:4px; padding:0 5px; font-size:9px; font-weight:600;
  margin-left:5px}
.resting{padding:3px 9px; background:#fafbfc; color:var(--muted);
  font-size:9.5px; border-top:1px solid #f0f2f5}

.recap-wrap{display:grid; gap:8px; align-items:start}
.recap-wrap.split{grid-template-columns:1fr 1fr}
.recap{border:1px solid var(--line); border-radius:7px; overflow:hidden}
.recap th{background:var(--band); text-align:left; padding:4px 9px;
  font-size:8.5px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); border-bottom:1px solid var(--line)}
.recap td{padding:3px 9px; border-bottom:1px solid #f2f4f7; font-size:10.5px}
.recap tr:last-child td{border-bottom:none}
.recap td.num,.recap th.num{text-align:center}
.recap td.num{font-variant-numeric:tabular-nums}
.recap tbody tr:nth-child(even){background:#fcfdfe}

/* Matriks pertemuan. Lebarnya tumbuh kuadrat terhadap jumlah peserta, jadi
   selnya dibuat sekecil mungkin yang masih terbaca dan kolomnya diberi NOMOR,
   bukan nama - nama peserta sering berbagi kata depan sehingga singkatannya
   jadi kembar semua. Nomornya dicetak di depan nama tiap baris. */
.mx{border:1px solid var(--line); border-radius:7px; overflow:hidden;
  margin-bottom:10px}
/* table-layout:fixed + width:100% supaya kolom MEMBAGI lebar halaman, bukan
   menuntutnya. Dengan lebar otomatis, roster 40 orang membuat tabel melebihi
   lebar A4 lalu terpotong diam-diam oleh overflow:hidden - di layar itu cuma
   jelek, di kertas itu data yang hilang tanpa memberi tahu. */
.mx table{table-layout:fixed; width:100%}
.mx th.nm{width:84px; overflow:hidden; text-overflow:ellipsis}
/* Roster besar: kolomnya makin sempit, jadi angkanya ikut dikecilkan supaya
   tetap muat utuh alih-alih terpotong. */
.mx.dense th,.mx.dense td{padding:1px 2px; font-size:7.5px}
.mx.dense th.nm{width:70px; font-size:8px}
.mx caption{caption-side:top; text-align:left; padding:4px 9px;
  background:var(--band); border-bottom:1px solid var(--line);
  font-size:10px; font-weight:700; text-transform:uppercase;
  letter-spacing:.06em; color:var(--muted)}
.mx th,.mx td{padding:2px 4px; font-size:9px; text-align:center;
  border-bottom:1px solid #f2f4f7; font-variant-numeric:tabular-nums}
.mx th.nm{text-align:left; white-space:nowrap; font-weight:600;
  font-size:9.5px; border-right:1px solid var(--line)}
.mx th.nm .no{color:var(--muted); font-weight:400; margin-right:5px}
.mx thead th{background:var(--band); color:var(--muted); font-weight:700}
.mx tbody tr:last-child td,.mx tbody tr:last-child th{border-bottom:none}
.mx td.self{color:#c8ced8}
.mx td.zero{color:#aeb6c2}
.mx td.once{color:var(--good); background:var(--good-soft); font-weight:700}
.mx td.many{color:var(--warn); background:var(--warn-soft); font-weight:700}
.mx-key{font-size:10px; color:var(--muted); margin-bottom:7px;
  display:flex; gap:14px; flex-wrap:wrap}
.mx-key b{font-weight:700; padding:0 4px; border-radius:3px}
.mx-key .k0{color:#aeb6c2; border:1px solid var(--line)}
.mx-key .k1{color:var(--good); background:var(--good-soft)}
.mx-key .k2{color:var(--warn); background:var(--warn-soft)}

.note{background:var(--band); border-left:3px solid var(--muted);
  padding:5px 10px; border-radius:0 5px 5px 0; margin-bottom:5px; font-size:10px}
.note.warn{background:var(--warn-soft); border-left-color:var(--warn)}
.madeby{display:inline-flex; align-items:center; gap:6px}
.madeby svg{width:14px; height:14px; color:var(--muted)}
.foot{margin-top:16px; padding-top:8px; border-top:1px solid var(--line);
  color:var(--muted); font-size:9px; display:flex; justify-content:space-between}

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
  body{padding:0; font-size:10px}
  .toolbar{display:none !important}
  .sheet{max-width:none}
  h2{margin-top:11px}
  .round{border-color:#d8dde4}
  .masthead{margin-bottom:10px}
  .tiles{margin-bottom:10px; gap:5px}
  .rounds{gap:5px}
  .m{padding:2px 7px; font-size:9.5px; line-height:1.35}
  .resting{padding:1px 7px; font-size:8.5px; line-height:1.35}
  .round-head{padding:1px 7px}
  .recap td{padding:1.5px 8px; font-size:9.5px; line-height:1.35}
  .recap th{padding:2px 8px}
  .note{padding:4px 9px; font-size:9px; margin-bottom:4px}
  thead{display:table-header-group}
  tr{break-inside:avoid; page-break-inside:avoid}
}
@page{size:A4 portrait; margin:14mm 12mm}
"""


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _rupiah(value: float) -> str:
    """Rp dengan pemisah ribuan gaya Indonesia (titik)."""
    return "Rp " + f"{round(value):,}".replace(",", ".")


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
    logo: str = "",
    fee: float = 0.0,
) -> str:
    """Rakit laporan HTML lengkap sebagai satu dokumen mandiri."""
    names = {p.id: p.name for p in schedule.players}
    cfg = schedule.config
    st = schedule.stats
    show_roles = bool(cfg.referees_per_court or cfg.ballboys_per_court)

    fmt = MODE_LABELS.get(cfg.mode, cfg.mode)
    if cfg.segments and any(s.label for s in cfg.segments):
        # Babak berlabel sama dijumlahkan rondenya, bukan disebut berulang.
        # Babak "Sesama gender" bisa terpecah jadi banyak potongan (putra/putri,
        # dan lebih parah lagi kalau selang-seling memecahnya per ronde), yang
        # membuat badge berbunyi "Sesama gender 1r + Sesama gender 1r + ..."
        # enam kali - panjangnya berlipat tanpa satu pun informasi tambahan,
        # dan itulah yang menabrak judul. Urutan main tetap terbaca lengkap di
        # daftar ronde di bawah; badge ini memang cuma ringkasan format.
        totals: dict[str, int] = {}
        for s in cfg.segments:
            if s.rounds:
                totals[s.label] = totals.get(s.label, 0) + s.rounds
        fmt = " + ".join(f"{label} {rounds}r" for label, rounds in totals.items())

    plays = list(st.plays_per_player.values()) or [0]
    meta_bits = [b for b in (format_date_id(event_date), venue) if b]
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
    # Hanya data URI gambar yang diterima; nilai lain diabaikan diam-diam
    # supaya laporan tetap terbit.
    logo_html = ""
    if logo.startswith(("data:image/png;base64,", "data:image/jpeg;base64,")):
        logo_html = f"<img class='logo' src='{_e(logo)}' alt=''>"

    parts.append(
        f"<div class='masthead'><div class='brand'>{logo_html}<div>"
        f"<h1>{_e(title)}</h1>"
        f"<div class='meta'>{_e('  ·  '.join(meta_bits))}</div></div></div>"
        f"<div class='badge'>{_e(fmt)}</div></div>"
    )

    # Kartu angka
    # Angka pengulangan tanpa konteks terbaca seperti cacat jadwal, padahal
    # sering kali itu batas matematis: tiap ronde seorang pemain dapat 1 partner
    # dan 2 lawan, jadi lawan unik mentok di (N-1)/2 ronde. Batasnya disebut di
    # kartunya sendiri, bukan hanya di catatan yang jauh di bawah.
    n_players = len(schedule.players)
    max_partner_rounds = max(0, n_players - 1)
    max_opp_rounds = max(0, (n_players - 1) // 2)
    played = max(plays)

    partner_note = ("pasang" if played <= max_partner_rounds
                    else f"pasang · batas {max_partner_rounds} ronde")
    opp_note = ("pasang" if played <= max_opp_rounds
                else f"pasang · batas matematis {max_opp_rounds} ronde")

    tiles = []

    # Fee ditaruh paling depan: itu hal pertama yang dicari peserta saat
    # laporannya dibagikan. Keterangannya memakai harga per menit main, bukan
    # per acara - peserta menilai harga dari waktu di lapangan.
    if fee and fee > 0:
        minutes = min(plays) * cfg.round_minutes
        sub = (f"{_rupiah(fee / minutes)} / menit main" if minutes
               else "per peserta")
        tiles.append(("Fee per peserta", _rupiah(fee), sub))

    tiles += [
        ("Ronde", str(len(schedule.rounds)), f"{cfg.round_minutes} menit / ronde"),
        ("Main per orang", f"{min(plays)}-{max(plays)}", "ronde"),
        ("Partner berulang", str(st.partner_repeat_pairs), partner_note),
        ("Lawan berulang", str(st.opponent_repeat_pairs), opp_note),
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

    # Jadwal - satu tabel padat, bukan satu kartu per ronde. Tujuannya muat
    # sehalaman saat dicetak: kartu memakan ~4 baris per ronde untuk bingkai dan
    # judulnya sendiri, tabel memakai satu baris per match.
    #
    # Tiap ronde dibungkus <tbody> sendiri supaya `break-inside: avoid` menjaga
    # satu ronde tidak terbelah antar halaman - itu satu-satunya pengelompokan
    # yang dihormati mesin cetak pada tabel panjang.
    parts.append("<h2>Jadwal pertandingan</h2>")

    # Card tetap dipakai, tapi disusun dalam grid. Dengan 1 court tiap card
    # hanya memuat satu match, jadi satu kolom membuang ruang horizontal dan
    # laporan jadi berhalaman-halaman. Jumlah kolom mengikuti lebar isi card.
    max_matches = max((len(r.matches) for r in schedule.rounds), default=1)
    cols = 3 if max_matches == 1 else 2 if max_matches == 2 else 1
    parts.append(f"<div class='rounds cols-{cols}'>")

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
        for m in rnd.matches:
            pool = rnd.court_labels.get(m.court, "")
            pool_html = f"<span class='pool'>{_e(pool)}</span>" if pool else ""
            duty_bits = []
            if m.court in refs:
                duty_bits.append(f"W {_e(refs[m.court])}")
            if m.court in balls:
                duty_bits.append(f"B {_e(balls[m.court])}")
            duty_html = (f"<span class='duty'>{' · '.join(duty_bits)}</span>"
                         if duty_bits else "")
            parts.append(
                f"<div class='m'><span class='court'>C{m.court}</span>"
                f"<span class='team'>{_e(names[m.team_a[0]])} &amp; "
                f"{_e(names[m.team_a[1]])}{pool_html}</span>"
                f"<span class='vs'>vs</span>"
                f"<span class='team b'>{_e(names[m.team_b[0]])} &amp; "
                f"{_e(names[m.team_b[1]])}</span>{duty_html}</div>"
            )
        idle = rnd.resting_only()
        if idle:
            parts.append(
                f"<div class='resting'>Istirahat: "
                f"{_e(', '.join(names[b] for b in idle))}</div>"
            )
        parts.append("</div>")
    parts.append("</div>")

    # Rekap pemain. Dipecah dua kolom kalau pesertanya banyak: satu kolom
    # panjang membuang separuh lebar halaman dan bisa menambah satu halaman
    # penuh sendirian.
    parts.append("<h2>Rekap per pemain</h2>")
    roster = sorted(schedule.players, key=lambda x: x.name.lower())
    split = len(roster) > 14
    chunks = ([roster[: (len(roster) + 1) // 2], roster[(len(roster) + 1) // 2:]]
              if split else [roster])

    # (judul, apakah kolom angka). Judul kolom angka harus rata tengah juga -
    # kalau judulnya kiri sementara isinya tengah, keduanya terlihat meleset.
    # Kolom dibuat aditif: main + wasit + ballboy + istirahat = jumlah ronde.
    # "Duduk" yang lama menghitung ronde bertugas juga, jadi angkanya tidak bisa
    # dijumlah dan peserta yang membacanya bingung.
    headers = [("Nama", False), ("Rating", True), ("L/P", True), ("Main", True)]
    if show_roles:
        headers += [("W", True), ("B", True)]
    headers.append(("Istirahat", True))

    parts.append(f"<div class='recap-wrap{' split' if split else ''}'>")
    for chunk in chunks:
        parts.append("<table class='recap'><thead><tr>")
        parts.append("".join(
            "<th class='num'>" + _e(h) + "</th>" if is_num
            else "<th>" + _e(h) + "</th>"
            for h, is_num in headers))
        parts.append("</tr></thead><tbody>")
        for p in chunk:
            roles = st.roles_per_player.get(p.id, {})
            idle = max(0, st.byes_per_player.get(p.id, 0)
                       - int(roles.get("total", 0) or 0))
            cells = [
                f"<td>{_e(p.name)}</td>",
                f"<td class='num'>{p.rating:g}</td>",
                f"<td class='num'>"
                f"{_e({'M': 'L', 'F': 'P'}.get(p.gender or '', '-'))}</td>",
                f"<td class='num'>{st.plays_per_player.get(p.id, 0)}</td>",
            ]
            if show_roles:
                cells += [
                    f"<td class='num'>{roles.get('wasit', 0)}</td>",
                    f"<td class='num'>{roles.get('ballboy', 0)}</td>",
                ]
            cells.append(f"<td class='num'>{idle}</td>")
            parts.append("<tr>" + "".join(cells) + "</tr>")
        parts.append("</tbody></table>")
    parts.append("</div>")

    # Matriks pertemuan: siapa berpartner & melawan siapa, berapa kali.
    # Dihitung dari susunan ronde, bukan dari statistik, supaya laporan lama
    # yang dibuka ulang pun tetap terlayani.
    if len(schedule.players) >= 2:
        partner_n: dict[tuple[int, int], int] = {}
        oppo_n: dict[tuple[int, int], int] = {}

        def _bump(store, a, b):
            key = (a, b) if a < b else (b, a)
            store[key] = store.get(key, 0) + 1

        for rnd in schedule.rounds:
            for m in rnd.matches:
                _bump(partner_n, *m.team_a)
                _bump(partner_n, *m.team_b)
                for x in m.team_a:
                    for y in m.team_b:
                        _bump(oppo_n, x, y)

        order = sorted(schedule.players, key=lambda x: x.name.lower())
        seat = {p.id: i + 1 for i, p in enumerate(order)}

        dense = " dense" if len(order) > 24 else ""

        def _matrix(store, caption):
            out = [f"<div class='mx{dense}'><table><caption>{_e(caption)}</caption>",
                   "<thead><tr><th class='nm'>Nama</th>"]
            out += [f"<th>{seat[p.id]}</th>" for p in order]
            out.append("</tr></thead><tbody>")
            for a in order:
                out.append(f"<tr><th class='nm'><span class='no'>{seat[a.id]}</span>"
                           f"{_e(a.name)}</th>")
                for b in order:
                    if a.id == b.id:
                        out.append("<td class='self'>&middot;</td>")
                        continue
                    key = (a.id, b.id) if a.id < b.id else (b.id, a.id)
                    n = store.get(key, 0)
                    cls = "zero" if n == 0 else "once" if n == 1 else "many"
                    out.append(f"<td class='{cls}'>{n}</td>")
                out.append("</tr>")
            out.append("</tbody></table></div>")
            return "".join(out)

        parts.append("<h2>Matriks pertemuan</h2>")
        parts.append(
            "<div class='mx-key'>"
            "<span><b class='k0'>0</b> belum pernah</span>"
            "<span><b class='k1'>1</b> tepat sekali</span>"
            "<span><b class='k2'>2+</b> berulang</span>"
            "<span>Angka kolom = nomor di depan nama pada baris.</span>"
            "</div>"
        )
        parts.append(_matrix(partner_n, "Berpartner dengan"))
        parts.append(_matrix(oppo_n, "Melawan"))

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
        f"<span class='madeby'>{APP_MARK} Dibuat dengan Padelin</span></div>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)
