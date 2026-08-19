"""Laporan laba/rugi host: acara yang SUDAH dijalankan, bukan yang direncanakan.

Panel Biaya di aplikasi menjawab pertanyaan sebelum acara ("kalau saya sewa 3
court dan menagih 75.000, untung berapa?"). Pertanyaan sesudahnya berbeda, dan
tidak ada satu tempat pun yang menjawabnya: "dari meet yang sudah saya
selenggarakan, saya untung atau rugi, dan yang rugi kenapa?"

Modul ini merakit jawabannya sebagai satu dokumen siap cetak - daftar acara
dengan laba/ruginya masing-masing, rekap per venue dan per bulan, dan untuk
tiap acara yang nombok, selisih fee terhadap modal per peserta. Angkanya datang
dari kolom yang disimpan saat acara disimpan (storage.host_ledger), bukan dari
hitung ulang setup: setup boleh diubah setelahnya, uang yang sudah keluar tidak.

Kertas dan CSS-nya sama dengan laporan jadwal (html_report.CSS) supaya keduanya
terlihat satu keluarga dan aturan cetaknya - A4, header tabel berulang, baris
tidak terbelah - tidak perlu ditulis dua kali.
"""

from __future__ import annotations

from .html_report import APP_MARK, CSS, _e, _jam, _rupiah
from .report import format_date_id

BULAN = ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember")
BULAN_PENDEK = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep",
                "Okt", "Nov", "Des")

# Tambahan di atas CSS laporan jadwal. Angka uang dirata-kanan, bukan tengah:
# satu kolom berisi 45.000 dan 1.085.050 hanya bisa dibandingkan sekali lihat
# kalau digit terakhirnya sejajar - dan membandingkan itu satu-satunya alasan
# tabel ini ada.
EXTRA_CSS = """
.ledger{border:1px solid var(--line); border-radius:7px; overflow:hidden;
  margin-bottom:14px}
.ledger table{width:100%; border-collapse:collapse}
.ledger th{background:var(--band); text-align:left; padding:5px 8px;
  font-size:9px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); border-bottom:1px solid var(--line); font-weight:700}
.ledger td{padding:4px 8px; border-bottom:1px solid #f2f4f7; font-size:10.5px}
.ledger tr:last-child td{border-bottom:none}
.ledger td.num,.ledger th.num{text-align:right;
  font-variant-numeric:tabular-nums; white-space:nowrap}
.ledger td.tgl{white-space:nowrap}
.h2note{text-transform:none; letter-spacing:0; color:var(--muted);
  font-weight:400; font-size:9.5px; margin-left:5px}
.ledger td.c,.ledger th.c{text-align:center}
.ledger tfoot td{background:var(--band); font-weight:700;
  border-top:1px solid var(--line)}
.ledger .judul{font-weight:600}
.ledger .sub{color:var(--muted); font-size:9px}
/* Status selalu KATA, bukan cuma warna: laporan ini dicetak, sering hitam-putih,
   dan hijau/merah di bawah deuteranopia hanya berjarak dE 6,5. */
.st{display:inline-block; padding:0 5px; border-radius:4px; font-size:9px;
  font-weight:700; white-space:nowrap}
.st.u{color:var(--good); background:var(--good-soft)}
.st.r{color:var(--warn); background:var(--warn-soft)}
.st.i{color:var(--muted); background:var(--band)}
.money.u{color:var(--good)}
.money.r{color:var(--warn)}
.tile.good .v{color:var(--good)}
.tile.bad .v{color:var(--warn)}
.kpi{font-size:11px; color:var(--muted); margin:-8px 0 14px}
.kpi b{color:var(--ink)}
@media print{
  .ledger{margin-bottom:9px}
  .ledger td{padding:2px 7px; font-size:9.5px}
  .ledger th{padding:3px 7px}
  .kpi{font-size:9.5px; margin:-6px 0 9px}
}
"""


def _tgl(iso: str) -> str:
    """"2026-08-15" -> "15 Agu 2026".

    Bukan format_date_id(): di tabel sebelas kolom, "Sabtu, 15 Agustus 2026"
    memakan dua baris dan mendorong kolom Laba keluar dari lebar isi A4 - hari
    dan nama bulan panjang tidak membantu siapa pun yang sedang membandingkan
    empat baris angka. Tanggal panjang tetap dipakai di kepala laporan dan di
    catatan per acara, tempat ia justru dibaca sebagai kalimat.
    """
    try:
        y, m, d = (iso or "").split("-")
        return f"{int(d)} {BULAN_PENDEK[int(m) - 1]} {y}"
    except (ValueError, IndexError):
        return iso or "-"


def _uang(value: float) -> str:
    """Angka uang TANPA "Rp" - satuannya disebut sekali di judul kolom.

    "Rp" di setiap sel menambah ~26px per kolom uang; dengan lima kolom uang itu
    130px, dan itulah selisih antara tabel yang muat di A4 dan tabel yang
    kolom terakhirnya terpotong di cetakan.
    """
    return f"{round(value):,}".replace(",", ".")


def _bulan_id(ym: str) -> str:
    """"2026-08" -> "Agustus 2026". Nilai lain dikembalikan apa adanya."""
    try:
        y, m = ym.split("-")
        return f"{BULAN[int(m) - 1]} {y}"
    except (ValueError, IndexError):
        return ym


def _kelas(profit: float) -> str:
    return "u" if profit > 0 else "r" if profit < 0 else "i"


def _kata(profit: float) -> str:
    """Status satu acara sebagai KATA - "laba" mengikuti judul laporannya.

    Dipakai pil status di tabel, badge di kepala laporan, dan baris total. Satu
    fungsi supaya ketiganya tidak bisa menyebut hal yang sama dengan istilah yang
    berbeda.
    """
    return "laba" if profit > 0 else "rugi" if profit < 0 else "impas"


def _periode(ledger: dict) -> str:
    """Kalimat periode yang disebut di kepala laporan.

    Diambil dari acara yang benar-benar masuk, bukan dari filternya: host yang
    memilih 1 Januari - 31 Desember tapi cuma punya tiga meet di Agustus lebih
    perlu tahu yang kedua. Filter yang diminta tetap disebut kalau memang
    mempersempit, supaya jelas ada yang tidak ikut dihitung.
    """
    ev = ledger.get("events") or []
    if not ev:
        return ""
    tanggal = sorted(e.get("tanggal") or "" for e in ev if e.get("tanggal"))
    if not tanggal:
        return ""
    if tanggal[0] == tanggal[-1]:
        return format_date_id(tanggal[0])
    return f"{format_date_id(tanggal[0])} - {format_date_id(tanggal[-1])}"


def build_host_report(
    ledger: dict,
    title: str = "Laporan laba/rugi",
    club_name: str = "",
    logo: str = "",
    include_toolbar: bool = True,
) -> str:
    """Rakit laporan laba/rugi sebagai satu dokumen HTML mandiri."""
    s = ledger.get("summary") or {}
    events = ledger.get("events") or []
    f = ledger.get("filter") or {}

    parts: list[str] = []
    parts.append("<!doctype html><html lang='id'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append(f"<title>{_e(title)}</title>"
                 f"<style>{CSS}{EXTRA_CSS}</style></head><body>")

    if include_toolbar:
        # Sama persis dengan laporan jadwal: satu tombol, dua jalur (browser
        # biasa vs jembatan window.padelin di Electron). Lihat html_report.
        parts.append(
            "<div class='toolbar'>"
            "<button id='pdf'>Simpan sebagai PDF</button>"
            "<button class='ghost' onclick='window.close()'>Tutup</button>"
            "</div>"
            "<script>(function(){"
            "var j=window.padelin, b=document.getElementById('pdf');"
            "if(j){ b.textContent='Pratinjau cetak'; }"
            "b.onclick=function(){ j ? j.pratinjau() : window.print(); };"
            "})();</script>"
        )

    parts.append("<div class='sheet'>")

    logo_html = ""
    if logo.startswith(("data:image/png;base64,", "data:image/jpeg;base64,")):
        logo_html = f"<img class='logo' src='{_e(logo)}' alt=''>"

    meta = [b for b in (club_name, _periode(ledger)) if b]
    meta.append(f"{s.get('events', 0)} acara")
    if f.get("since") or f.get("until"):
        meta.append("disaring: "
                    + " s.d. ".join(x for x in (f.get("since"), f.get("until")) if x))
    parts.append(
        f"<div class='masthead'><div class='brand'>{logo_html}<div>"
        f"<h1>{_e(title)}</h1>"
        f"<div class='meta'>{_e('  ·  '.join(meta))}</div></div></div>"
        f"<div class='badge'>{_e(_kata(s.get('profit', 0)).upper())}</div></div>"
    )

    if not events:
        parts.append(
            "<div class='note'>Belum ada acara tersimpan untuk rentang ini. "
            "Laporan ini dirakit dari acara yang disimpan lewat tombol "
            "<b>Simpan ke database</b> di tab Jadwal - acara yang cuma "
            "di-generate lalu ditutup tidak punya angka untuk dihitung.</div>")
        parts.append(f"<div class='foot'><span>{_e(title)}</span>"
                     f"<span class='madeby'>{APP_MARK} Dibuat dengan Padelin"
                     f"</span></div></div></body></html>")
        return "".join(parts)

    # --- Ringkasan --------------------------------------------------------
    laba = s.get("profit", 0.0)
    tiles = [
        ("Acara", str(s.get("events", 0)),
         f"{s.get('attendances', 0)} kehadiran"),
        ("Pemasukan", _rupiah(s.get("revenue", 0)), "dari fee peserta"),
        ("Biaya", _rupiah(s.get("total_cost", 0)), "sewa + biaya lain"),
        ("Laba", _rupiah(laba), f"margin {s.get('margin_pct', 0)}%",
         "good" if laba >= 0 else "bad"),
        ("Laba / acara", _rupiah(s.get("profit_per_event", 0)),
         "rata-rata", "good" if s.get("profit_per_event", 0) >= 0 else "bad"),
        ("Laba / peserta", _rupiah(s.get("profit_per_attendance", 0)),
         "per kehadiran", "good" if s.get("profit_per_attendance", 0) >= 0
         else "bad"),
    ]
    parts.append("<h2>Ringkasan</h2>")
    parts.append(f"<div class='tiles' style='--n:{len(tiles)}'>")
    for t in tiles:
        cls = f"tile {t[3]}" if len(t) > 3 else "tile"
        parts.append(f"<div class='{cls}'><div class='k'>{_e(t[0])}</div>"
                     f"<div class='v'>{_e(t[1])}</div>"
                     f"<div class='s'>{_e(t[2])}</div></div>")
    parts.append("</div>")

    # Hitungan laba/rugi/impas dipisah. "8 acara laba" yang sebenarnya berisi
    # 3 acara impas membuat host menyimpulkan feenya sudah pas, padahal impas
    # berarti seluruh kerjanya tidak dibayar.
    parts.append(
        f"<div class='kpi'><b>{s.get('laba', 0)}</b> acara laba &middot; "
        f"<b>{s.get('rugi', 0)}</b> rugi &middot; "
        f"<b>{s.get('impas', 0)}</b> impas (laba persis nol).</div>")

    # --- Daftar acara -----------------------------------------------------
    parts.append("<h2>Laba / rugi per acara"
                 "<span class='h2note'>angka uang dalam Rupiah</span></h2>")
    parts.append("<div class='ledger'><table><thead><tr>"
                 "<th>Tanggal</th><th>Acara</th><th class='c'>Peserta</th>"
                 "<th class='num'>Court-jam</th><th class='num'>Fee</th>"
                 "<th class='num'>Modal / org</th><th class='num'>Biaya</th>"
                 "<th class='num'>Pemasukan</th><th class='num'>Laba</th>"
                 "<th class='num'>Margin</th><th class='c'>Status</th>"
                 "</tr></thead><tbody>")
    for e in events:
        k = _kelas(e["profit"])
        venue = e.get("venue_name") or ""
        parts.append(
            f"<tr><td class='tgl'>{_e(_tgl(e.get('tanggal') or ''))}</td>"
            f"<td><span class='judul'>{_e(e.get('title') or '-')}</span>"
            + (f"<div class='sub'>{_e(venue)}</div>" if venue else "")
            + f"</td>"
            f"<td class='c'>{int(e.get('n_players') or 0)}</td>"
            f"<td class='num'>{_e(_jam(e.get('court_hours') or 0))}</td>"
            f"<td class='num'>{_e(_uang(e.get('fee_per_player') or 0))}</td>"
            f"<td class='num'>{_e(_uang(e.get('cost_per_player') or 0))}</td>"
            f"<td class='num'>{_e(_uang(e.get('total_cost') or 0))}</td>"
            f"<td class='num'>{_e(_uang(e.get('revenue') or 0))}</td>"
            f"<td class='num money {k}'>{_e(_uang(e['profit']))}</td>"
            f"<td class='num'>{e.get('margin_pct', 0)}%</td>"
            f"<td class='c'><span class='st {k}'>{_kata(e['profit'])}</span></td>"
            "</tr>")
    parts.append(
        "</tbody><tfoot><tr>"
        f"<td colspan='2'>Total {s.get('events', 0)} acara</td>"
        f"<td class='c'>{s.get('attendances', 0)}</td>"
        "<td class='num'></td><td class='num'></td><td class='num'></td>"
        f"<td class='num'>{_e(_uang(s.get('total_cost', 0)))}</td>"
        f"<td class='num'>{_e(_uang(s.get('revenue', 0)))}</td>"
        f"<td class='num money {_kelas(laba)}'>{_e(_uang(laba))}</td>"
        f"<td class='num'>{s.get('margin_pct', 0)}%</td>"
        f"<td class='c'><span class='st {_kelas(laba)}'>"
        f"{_kata(laba)}</span></td>"
        "</tr></tfoot></table></div>")

    # --- Yang nombok, dan kenapa -----------------------------------------
    rugi = [e for e in events if e["profit"] < 0]
    if rugi:
        parts.append("<h2>Acara yang nombok</h2>")
        for e in rugi:
            kurang = (e.get("cost_per_player") or 0) - (e.get("fee_per_player") or 0)
            parts.append(
                f"<div class='note warn'><b>{_e(e.get('title') or '-')}</b>"
                f" &middot; {_e(format_date_id(e.get('tanggal') or '') or '-')}"
                f" &mdash; rugi {_e(_rupiah(-e['profit']))}. "
                f"Fee {_e(_rupiah(e.get('fee_per_player') or 0))} sementara modal "
                f"per peserta {_e(_rupiah(e.get('cost_per_player') or 0))}, jadi "
                f"fee kurang {_e(_rupiah(kurang))} per orang."
                # Court-jam disebut karena itu tuas yang paling sering dipakai:
                # menaikkan fee bukan satu-satunya jalan keluar, mengurangi court
                # atau jam sewa juga.
                f" Sewanya {_e(_jam(e.get('court_hours') or 0))} court-jam"
                + (f" x {_e(_rupiah(e['court_price_per_hour']))}"
                   if e.get("court_price_per_hour") else "")
                + (f" + biaya lain {_e(_rupiah(e['other_costs']))}"
                   if e.get("other_costs") else "")
                + ".</div>")

    # --- Terbaik & terburuk ----------------------------------------------
    best, worst = s.get("best"), s.get("worst")
    if best and worst and best is not worst and len(events) > 1:
        parts.append("<h2>Ujung ke ujung</h2>")
        # "Laba tertipis" hanya benar kalau ujung bawahnya masih di atas nol.
        # Untuk acara yang nombok Rp 300.000, label itu memperhalus satu-satunya
        # angka yang justru harus terbaca apa adanya.
        label_bawah = ("Rugi terbesar" if worst["profit"] < 0
                       else "Laba tertipis" if worst["profit"] > 0 else "Impas")
        for label, e in (("Laba terbesar", best), (label_bawah, worst)):
            k = _kelas(e["profit"])
            parts.append(
                f"<div class='note'><b>{_e(label)}</b> &middot; "
                f"{_e(e.get('title') or '-')} "
                f"({_e(format_date_id(e.get('tanggal') or '') or '-')}) &mdash; "
                f"<span class='money {k}'>{_e(_rupiah(e['profit']))}</span> "
                f"dari {int(e.get('n_players') or 0)} peserta, margin "
                f"{e.get('margin_pct', 0)}%.</div>")

    # --- Rekap per venue --------------------------------------------------
    per_venue = ledger.get("per_venue") or []
    if len(per_venue) > 1:
        parts.append("<h2>Per venue<span class='h2note'>angka uang dalam Rupiah</span></h2>")
        parts.append("<div class='ledger'><table><thead><tr><th>Venue</th>"
                     "<th class='c'>Acara</th><th class='num'>Court-jam</th>"
                     "<th class='num'>Biaya</th><th class='num'>Pemasukan</th>"
                     "<th class='num'>Laba</th><th class='num'>Margin</th>"
                     "</tr></thead><tbody>")
        for v in per_venue:
            k = _kelas(v["profit"])
            parts.append(
                f"<tr><td>{_e(v['venue_name'])}</td>"
                f"<td class='c'>{v['events']}</td>"
                f"<td class='num'>{_e(_jam(v.get('court_hours') or 0))}</td>"
                f"<td class='num'>{_e(_uang(v['total_cost']))}</td>"
                f"<td class='num'>{_e(_uang(v['revenue']))}</td>"
                f"<td class='num money {k}'>{_e(_uang(v['profit']))}</td>"
                f"<td class='num'>{v['margin_pct']}%</td></tr>")
        parts.append("</tbody></table></div>")

    # --- Rekap per bulan --------------------------------------------------
    per_month = ledger.get("per_month") or []
    if len(per_month) > 1:
        parts.append("<h2>Per bulan<span class='h2note'>angka uang dalam Rupiah</span></h2>")
        parts.append("<div class='ledger'><table><thead><tr><th>Bulan</th>"
                     "<th class='c'>Acara</th><th class='c'>Kehadiran</th>"
                     "<th class='num'>Biaya</th><th class='num'>Pemasukan</th>"
                     "<th class='num'>Laba</th><th class='num'>Margin</th>"
                     "</tr></thead><tbody>")
        for m in per_month:
            k = _kelas(m["profit"])
            parts.append(
                f"<tr><td>{_e(_bulan_id(m['month']))}</td>"
                f"<td class='c'>{m['events']}</td>"
                f"<td class='c'>{m['attendances']}</td>"
                f"<td class='num'>{_e(_uang(m['total_cost']))}</td>"
                f"<td class='num'>{_e(_uang(m['revenue']))}</td>"
                f"<td class='num money {k}'>{_e(_uang(m['profit']))}</td>"
                f"<td class='num'>{m['margin_pct']}%</td></tr>")
        parts.append("</tbody></table></div>")

    parts.append(
        "<div class='note'>Angkanya dari data yang tersimpan saat acara "
        "disimpan: biaya = court-jam yang benar-benar disewa x harga sewa + "
        "biaya lain, pemasukan = jumlah peserta x fee. Acara yang court-nya "
        "dilepas di tengah jalan sudah dihitung dengan court-jam yang lebih "
        "kecil, bukan court x durasi.</div>")

    parts.append(f"<div class='foot'><span>{_e(title)}</span>"
                 f"<span class='madeby'>{APP_MARK} Dibuat dengan Padelin"
                 f"</span></div>")
    parts.append("</div></body></html>")
    return "".join(parts)
