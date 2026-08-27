"""Ekspor jadwal: teks siap-share, CSV, dan dict untuk API web."""

from __future__ import annotations

import csv
import io
from dataclasses import asdict, fields
from datetime import date

from .models import (
    MATCHUPS,
    Config,
    Match,
    Player,
    PreferenceViolation,
    Round,
    RoleAssignment,
    Schedule,
    ScheduleStats,
    Segment,
)

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


def _bentuk(g1: str | None, g2: str | None) -> str | None:
    """Susunan gender satu tim; None kalau gendernya belum terisi."""
    if g1 is None or g2 is None:
        return None
    return "LL" if g1 == g2 == "M" else "PP" if g1 == g2 == "F" else "LP"


def kolam_partner(players, allowed_matchups) -> dict[int, int]:
    """Berapa calon partner yang SAH untuk tiap peserta menurut format.

    Menerima daftar peserta dan format, bukan Schedule: penjadwal memanggilnya
    saat catatan disusun, dan di titik itu jadwalnya belum dirakit.

    Format yang dibatasi memotong kolam partner jauh lebih dalam daripada yang
    terlihat dari jumlah peserta. Contoh nyata dari host: 5 putra + 3 putri
    dengan format "putra vs putra" dan "campur vs campur" saja. Tim dua putri
    tidak pernah sah - PP-PP tidak diizinkan dan di LL-LL tidak ada putri sama
    sekali - jadi tiap putri hanya bisa berpasangan dengan putra, dan calonnya
    cuma lima. Dengan main 6 ronde, tiap putri WAJIB mengulang satu partner.
    Tiga pasang berulang di jadwal itu persis batas bawahnya, bukan kelalaian.

    Sepasang dianggap sah kalau bentuk timnya muncul di salah satu format yang
    diizinkan DAN lawannya masih bisa dibentuk dari sisa roster - tim campur
    tidak ada gunanya kalau tidak ada dua orang lagi yang bisa jadi lawannya.

    Kosong kalau gender tidak lengkap atau format tidak dibatasi: di situ kolam
    partnernya seluruh peserta lain, dan hitungan per-babak yang biasa sudah
    menjawabnya.
    """
    izin = set(allowed_matchups or ())
    if not izin or izin == set(MATCHUPS):
        return {}
    g = {p.id: p.gender for p in players}
    if any(v not in ("M", "F") for v in g.values()):
        return {}

    n_m = sum(1 for v in g.values() if v == "M")
    n_f = sum(1 for v in g.values() if v == "F")

    def bisa_dibentuk(m: int, f: int) -> set[str]:
        out = set()
        if m >= 2:
            out.add("LL")
        if m >= 1 and f >= 1:
            out.add("LP")
        if f >= 2:
            out.add("PP")
        return out

    out: dict[int, int] = {}
    for p, gp in g.items():
        n = 0
        for q, gq in g.items():
            if q == p:
                continue
            bentuk = _bentuk(gp, gq)
            sisa_m = n_m - ((gp == "M") + (gq == "M"))
            sisa_f = n_f - ((gp == "F") + (gq == "F"))
            if any("-".join(sorted((bentuk, lawan))) in izin
                   for lawan in bisa_dibentuk(sisa_m, sisa_f)):
                n += 1
        out[p] = n
    return out


def batas_keunikan(schedule: Schedule) -> dict:
    """Batas ronde main untuk partner/lawan 100% unik, per kolam babak.

    Angka pengulangan tanpa konteks terbaca seperti cacat jadwal, padahal sering
    kali itu batas matematis: tiap ronde seorang pemain dapat 1 partner dan 2
    lawan, jadi keunikan mentok di (kolam-1) dan (kolam-1)//2 ronde.

    Yang menentukan KOLAM YANG BENAR-BENAR DIHADAPI, bukan jumlah seluruh
    peserta. Babak putra/putri memecah kolamnya: pada 8 putra + 8 putri dengan
    babak putra lalu putri, tiap putra cuma pernah berhadapan dengan 7 putra
    lain, jadi 6 ronde main sudah jauh melewati batas 3 ronde dan 15 pasang
    berulang itu wajib terjadi. Dihitung dari 16 peserta, batasnya terbaca 7
    ronde dan penjelasannya tidak pernah muncul - persis di kasus yang paling
    membutuhkannya.

    Kolamnya dibaca dari jadwal jadi, bukan dimodelkan dari aturan babak: siapa
    yang benar-benar muncul di ronde-ronde babak itu sudah menjawabnya persis,
    termasuk untuk babak "sesama gender" dan "mixed" yang komposisinya berbeda.

    Kembaliannya {"partner": ..., "lawan": ...}; tiap nilai None kalau keunikan
    memang masih mungkin - di situ tidak ada yang perlu dijelaskan - atau
    {"batas": ronde, "babak": nama} untuk babak yang paling mengikat. `babak`
    kosong kalau meetnya cuma satu babak.

    SYARAT CUKUP, BUKAN SYARAT LENGKAP, dan bedanya penting bagi yang membaca
    keluarannya. Kalau fungsi ini menyebut sebuah batas, pengulangannya memang
    tak terhindarkan - itu prinsip laci merpati pada kolam yang terukur. Tapi
    diamnya BUKAN berarti pengulangan bisa dihindari: babak "sesama gender"
    memuat kedua gender di kolam yang sama padahal seorang putra tidak pernah
    berhadapan dengan putri di situ, jadi kolam yang sebenarnya lebih kecil
    daripada yang terbaca. Diukur pada 8 putra + 8 putri dengan babak sesama
    gender + mixed, 8 pasang lawan berulang lewat tanpa catatan. Menahan diri di
    situ disengaja: mengaku tahu sesuatu tak terhindarkan padahal belum tentu
    lebih buruk daripada tidak berkomentar.
    """
    kolam: dict[str, set[int]] = {}
    main_babak: dict[str, dict[int, int]] = {}
    for rnd in schedule.rounds:
        lab = rnd.segment or ""
        turun = {p for m in rnd.matches for p in m.players()}
        if not turun:
            continue
        kolam.setdefault(lab, set()).update(turun)
        hit = main_babak.setdefault(lab, {})
        for p in turun:
            hit[p] = hit.get(p, 0) + 1

    banyak = len(kolam) > 1
    hasil: dict[str, dict | None] = {"partner": None, "lawan": None}
    # Yang dilaporkan babak dengan KELEBIHAN terbesar di atas batasnya, bukan
    # batas terkecil: babak yang batasnya rendah tapi rondenya sedikit tidak
    # memaksa pengulangan apa pun.
    lebih = {"partner": -1, "lawan": -1}
    for lab, anggota in kolam.items():
        r = max(main_babak[lab].values(), default=0)
        for kunci, batas in (("partner", max(0, len(anggota) - 1)),
                             ("lawan", max(0, (len(anggota) - 1) // 2))):
            if r > batas and r - batas > lebih[kunci]:
                lebih[kunci] = r - batas
                hasil[kunci] = {"batas": batas, "babak": lab if banyak else "",
                                "kelompok": ""}

    # Format yang dibatasi bisa memotong kolam partner lebih dalam daripada
    # jumlah peserta di babaknya - lihat kolam_partner(). Diperiksa belakangan
    # dan hanya menggantikan kalau kelebihannya lebih besar, supaya yang
    # dilaporkan tetap kendala yang paling mengikat.
    main = schedule.stats.plays_per_player
    gender = {p.id: p.gender for p in schedule.players}
    for p, muat in kolam_partner(schedule.players,
                                 schedule.config.allowed_matchups).items():
        r = main.get(p, 0)
        if r > muat and r - muat > lebih["partner"]:
            lebih["partner"] = r - muat
            hasil["partner"] = {
                "batas": muat, "babak": "",
                "kelompok": ("putri" if gender.get(p) == "F" else
                             "putra" if gender.get(p) == "M" else ""),
            }
    return hasil


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
    # Court yang benar-benar dipakai tiap ronde. Teks ini yang ditempel ke grup
    # peserta, jadi "2 court" untuk acara yang ronde belakangnya cuma satu court
    # akan langsung dibantah oleh daftar ronde di bawahnya.
    # Berlaku dua arah: court boleh berkurang di tengah acara (sewa yang tidak
    # sama panjang) maupun bertambah (court sebelah baru kosong jam berikutnya).
    #
    # Titik pergantiannya dibaca dari SETUP, bukan ditebak dari jumlah match per
    # ronde. Sejak peserta boleh datang telat, jumlah match bisa berubah tanpa
    # court-nya berubah sama sekali - dan kalimat "jadi 1 court dari ronde 5"
    # untuk acara yang court-nya tidak pernah dikurangi adalah kesalahan yang
    # dibantah langsung oleh tagihan venue. Angkanya tetap dari match yang
    # BENAR-BENAR berjalan: 10 peserta di 4 court cuma mengisi 2.
    court_ronde = [len(r.matches) for r in schedule.rounds]
    court_txt = f"{cfg.courts} court"
    if court_ronde:
        ubah = cfg.courts_from_round if cfg.courts_after is not None else None
        if ubah is not None and 1 < ubah <= len(court_ronde):
            sebelum = max(court_ronde[:ubah - 1])
            sesudah = max(court_ronde[ubah - 1:])
            court_txt = (f"{sebelum} court (jadi {sesudah} dari ronde {ubah})"
                         if sebelum != sesudah else f"{sebelum} court")
        elif len(set(court_ronde)) > 1:
            court_txt = f"{min(court_ronde)}-{max(court_ronde)} court"
        else:
            court_txt = f"{court_ronde[0]} court"
    out.append(
        f"{len(schedule.players)} pemain | {court_txt} | "
        f"{cfg.duration_minutes} menit | {len(schedule.rounds)} ronde "
        f"@ {cfg.round_minutes} menit"
    )
    if cfg.segments and any(s.label for s in cfg.segments):
        fmt = " + ".join(f"{s.label} {s.rounds}" for s in cfg.segments if s.rounds)
        out.append(f"Format: {fmt}")
    # Peserta yang tidak ikut sepanjang acara. Teks inilah yang ditempel ke grup,
    # jadi di sinilah yang bersangkutan mengecek jam datangnya sendiri - dan di
    # sini pula peserta lain melihat kenapa ada nama yang hilang di ronde awal.
    total_ronde = len(schedule.rounds)
    sebagian = [p for p in sorted(schedule.players, key=lambda x: x.name.lower())
                if p.kehadiran_label(total_ronde)]
    if sebagian:
        out.append("Ikut sebagian: " + ", ".join(
            f"{p.name} ({p.kehadiran_label(total_ronde)})" for p in sebagian))
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
                f"  {cfg.court_label(m.court)}{suffix}: "
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
    # Batasnya ikut disebut, sama seperti di laporan cetak. Teks inilah yang
    # ditempel ke grup dan dibaca semua peserta, jadi justru di sini angka
    # pengulangan telanjang paling mudah terbaca sebagai kegagalan jadwal -
    # "Lawan berulang: 15 pasang" pada meet berbabak putra/putri adalah angka
    # yang tidak mungkin lebih kecil.
    batas = batas_keunikan(schedule)

    def _dengan_batas(nama, jumlah, kunci):
        b = batas[kunci]
        if not jumlah or b is None:
            return f"{nama}: {jumlah} pasang"
        di_mana = (f" di babak {b['babak']}" if b["babak"]
                   else f" bagi peserta {b['kelompok']}" if b.get("kelompok")
                   else "")
        return (f"{nama}: {jumlah} pasang (tak terhindarkan - unik cuma "
                f"mungkin sampai {b['batas']} ronde main{di_mana})")

    out.append(_dengan_batas("Partner berulang", st.partner_repeat_pairs,
                             "partner"))
    out.append(_dengan_batas("Lawan berulang", st.opponent_repeat_pairs,
                             "lawan"))
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
    cfg = schedule.config
    lines: list[str] = ["*JADWAL PER PEMAIN*", ""]

    total_ronde = len(schedule.rounds)
    for p in sorted(schedule.players, key=lambda x: x.name.lower()):
        lines.append(f"*{p.name}*")
        # Rentang kehadirannya disebut lebih dulu. Tanpa itu daftar di bawahnya
        # cuma "hilang" di ronde-ronde awal, dan yang membacanya tidak bisa
        # membedakan "belum datang" dari "tidak kebagian main".
        rentang = p.kehadiran_label(total_ronde)
        if rentang:
            lines.append(f"  (ikut {rentang} dari {total_ronde} ronde)")
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
                    f"  R{rnd.index} {when} {cfg.court_label(m.court)}: "
                    f"dgn {names[partner]} "
                    f"vs {names[opp[0]]} & {names[opp[1]]}"
                )
                break
            if found:
                continue
            duty = next((r for r in rnd.roles if r.player_id == p.id), None)
            if duty:
                lines.append(f"  R{rnd.index} {when}: {duty.role} di "
                             f"{cfg.court_label(duty.court)}")
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
        "ronde", "segmen", "mulai_menit", "court", "nama_court", "pool",
        "tim_a_1", "tim_a_2", "tim_b_1", "tim_b_2",
        "wasit", "ballboy", "istirahat",
    ])
    for rnd in schedule.rounds:
        idle = " | ".join(names[b] for b in rnd.resting_only())
        refs = {r.court: names[r.player_id] for r in rnd.roles if r.role == "wasit"}
        balls = {r.court: names[r.player_id] for r in rnd.roles if r.role == "ballboy"}
        if not rnd.matches:
            w.writerow([rnd.index, rnd.segment, rnd.start_min, "", "", "",
                        "", "", "", "", "", "", idle])
        for m in rnd.matches:
            w.writerow([
                rnd.index, rnd.segment, rnd.start_min, m.court,
                schedule.config.court_label(m.court),
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
                "rule": r.rule,
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


def _milik(cls, raw: dict | None) -> dict:
    """Hanya field yang memang dimiliki dataclass-nya.

    Jadwal tersimpan bisa datang dari versi lain: membawa field yang sudah
    dihapus, atau belum punya field yang baru ditambahkan. Menyaringnya di sini
    membuat acara lama tetap bisa dibuka alih-alih mati karena satu kunci asing.
    """
    nama = {f.name for f in fields(cls)}
    return {k: v for k, v in (raw or {}).items() if k in nama}


def _kunci_int(raw: dict | None) -> dict:
    """Kunci dict yang sebenarnya id pemain, dikembalikan menjadi int.

    JSON tidak punya kunci angka: sekali jadwal melewati json.dumps, id 7
    berubah jadi "7". Kalau dibiarkan, st.plays_per_player.get(p.id) di laporan
    mencari 7 di antara kunci berupa string dan selalu meleset - dan yang keluar
    bukan error, melainkan kolom Main/Duduk/Tugas yang isinya nol semua.
    """
    return {int(k): v for k, v in (raw or {}).items()}


def from_dict(data: dict) -> Schedule:
    """Kebalikan to_dict: rakit ulang Schedule dari bentuk JSON-nya.

    Dibutuhkan karena Schedule sebelumnya hanya bisa lahir dari solver, padahal
    laporan dan penyimpanan butuh jadwal yang PERSIS sedang dilihat host.
    Tanpa ini, satu-satunya cara mendapatkan objeknya adalah menjalankan ulang
    seluruh optimasi - lambat, dan hasilnya belum tentu sama.

    Dua hal yang tidak simetris dengan to_dict dan sengaja diurus di sini:

      - court_labels dibongkar to_dict menjadi field 'pool' di tiap match, jadi
        di sini dirakit balik dari match-nya. Label untuk court yang tidak punya
        match tidak ikut kembali; court seperti itu tidak pernah ada karena
        labelnya memang diberikan per court yang bermain.
      - resting_only diserialkan sebagai data padahal aslinya turunan dari byes
        dan roles. Yang dibaca di sini byes dan roles-nya; resting_only dihitung
        ulang sendiri oleh Round.

    Melempar TypeError/ValueError/KeyError kalau datanya tidak utuh - pemanggil
    yang memutuskan apakah itu berarti gagal atau jatuh ke generate ulang.
    """
    players = [Player(**_milik(Player, p)) for p in data.get("players") or []]

    craw = dict(data.get("config") or {})
    segments = [Segment(**_milik(Segment, s)) for s in craw.get("segments") or []]
    config = Config(**{**_milik(Config, craw), "segments": segments})

    rounds: list[Round] = []
    for r in data.get("rounds") or []:
        matches: list[Match] = []
        labels: dict[int, str] = {}
        for m in r.get("matches") or []:
            court = int(m["court"])
            matches.append(
                Match(
                    court=court,
                    team_a=tuple(int(x["id"]) for x in m["team_a"]),
                    team_b=tuple(int(x["id"]) for x in m["team_b"]),
                )
            )
            if m.get("pool"):
                labels[court] = m["pool"]
        rounds.append(
            Round(
                index=int(r.get("index", len(rounds) + 1)),
                matches=matches,
                byes=[int(b["id"]) for b in r.get("byes") or []],
                start_min=int(r.get("start_min", 0)),
                end_min=int(r.get("end_min", 0)),
                segment=r.get("segment", ""),
                rule=r.get("rule", ""),
                court_labels=labels,
                roles=[
                    RoleAssignment(player_id=int(a["player_id"]), role=a["role"],
                                   court=int(a["court"]))
                    for a in r.get("roles") or []
                ],
            )
        )

    sraw = _milik(ScheduleStats, data.get("stats") or {})
    for k in ("plays_per_player", "byes_per_player", "roles_per_player"):
        if k in sraw:
            sraw[k] = _kunci_int(sraw[k])

    return Schedule(
        players=players,
        config=config,
        rounds=rounds,
        stats=ScheduleStats(**sraw),
        notes=list(data.get("notes") or []),
        # preference_label ikut dikirim to_dict untuk UI, tapi bukan field
        # PreferenceViolation - _milik yang membuangnya.
        violations=[PreferenceViolation(**_milik(PreferenceViolation, v))
                    for v in data.get("violations") or []],
    )
