"""Master data & riwayat jadwal, disimpan di SQLite (stdlib, nol dependency).

Semua data ada di satu file `padel.db` yang gampang di-backup atau dipindah.

Struktur:

    clubs      -- klub yang kamu kelola
      |- venues   -- tempat main + harga sewa (jadi default panel Biaya)
      |- players  -- anggota klub: rating, gender, kontak
      `- events   -- tiap jadwal yang pernah dibuat
           `- event_participants -- siapa ikut acara mana + rekapnya

Tabel event_participants sengaja dipisah dari blob JSON supaya pertanyaan
seperti "siapa yang paling sering kebagian duduk 3 bulan terakhir" bisa
dijawab dengan satu query, bukan dengan membongkar semua JSON.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "padel.db"

# Saat dipasang lewat installer, folder aplikasi umumnya hanya-baca (Program
# Files), jadi database tidak boleh tinggal di sebelah kodenya - pembungkus
# desktop mengarahkannya ke folder data milik pengguna lewat variabel ini.
# Dijalankan langsung dari repo, perilakunya tidak berubah.
_env_db = os.environ.get("PADELIN_DB")
if _env_db:
    DEFAULT_DB = Path(_env_db)

SCHEMA = """
CREATE TABLE IF NOT EXISTS clubs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    city        TEXT DEFAULT '',
    contact     TEXT DEFAULT '',
    wa_group    TEXT DEFAULT '',
    notes       TEXT DEFAULT '',
    -- Logo disimpan sebagai data URI (image/png atau image/jpeg) supaya ikut
    -- berpindah bersama file database dan bisa langsung ditanam di laporan.
    logo        TEXT DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS venues (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id        INTEGER REFERENCES clubs(id) ON DELETE SET NULL,
    name           TEXT NOT NULL,
    address        TEXT DEFAULT '',
    court_count    INTEGER NOT NULL DEFAULT 1,
    price_per_hour REAL NOT NULL DEFAULT 0,
    notes          TEXT DEFAULT '',
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(club_id, name)
);

CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id     INTEGER REFERENCES clubs(id) ON DELETE SET NULL,
    name        TEXT NOT NULL,
    nickname    TEXT DEFAULT '',
    contact     TEXT DEFAULT '',
    gender      TEXT,
    rating      REAL NOT NULL DEFAULT 3.0,
    level_label TEXT DEFAULT '',
    notes       TEXT DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    joined_at   TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(club_id, name)
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id       INTEGER REFERENCES clubs(id) ON DELETE SET NULL,
    venue_id      INTEGER REFERENCES venues(id) ON DELETE SET NULL,
    title         TEXT NOT NULL,
    event_date    TEXT DEFAULT '',
    start_clock   TEXT DEFAULT '',
    venue_name    TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    n_players     INTEGER NOT NULL,
    courts        INTEGER NOT NULL,
    duration_min  INTEGER NOT NULL,
    rounds        INTEGER NOT NULL,
    mode          TEXT NOT NULL,
    quality_score REAL NOT NULL DEFAULT 0,
    total_cost    REAL NOT NULL DEFAULT 0,
    revenue       REAL NOT NULL DEFAULT 0,
    profit        REAL NOT NULL DEFAULT 0,
    request_json  TEXT NOT NULL,
    schedule_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_participants (
    event_id       INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    player_id      INTEGER REFERENCES players(id) ON DELETE SET NULL,
    name           TEXT NOT NULL,
    rounds_played  INTEGER NOT NULL DEFAULT 0,
    rounds_rested  INTEGER NOT NULL DEFAULT 0,
    duties         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (event_id, name)
);

CREATE INDEX IF NOT EXISTS idx_events_club ON events(club_id);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_players_club ON players(club_id);
CREATE INDEX IF NOT EXISTS idx_parts_name ON event_participants(name);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Buka koneksi. Penelepon bertanggung jawab menutupnya - pakai session()."""
    # Folder tujuan bisa belum ada kalau database diarahkan ke luar repo
    # (mis. folder data pengguna pada versi desktop). sqlite3 tidak membuatnya
    # sendiri; tanpa ini aplikasinya gagal jalan saat pertama kali dipasang.
    parent = Path(db_path).expanduser().parent
    if str(parent) and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def session(db_path: Path | str = DEFAULT_DB):
    """Koneksi yang dijamin tertutup.

    Jangan pakai `with connect(...)` langsung: context manager bawaan sqlite3
    hanya mengurus commit/rollback dan TIDAK menutup koneksi. Di Windows file
    database jadi terkunci dan handle-nya bocor tiap request.
    """
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Sesuaikan database lama dengan skema terbaru."""
    # Kolom yang ditambahkan setelah rilis awal. CREATE TABLE IF NOT EXISTS
    # tidak menyentuh tabel yang sudah ada, jadi kolom baru ditambah di sini.
    added = False
    for table, column, decl in (("clubs", "logo", "TEXT DEFAULT ''"),):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added = True
    if added:
        conn.commit()

    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='roster'"
    ).fetchone()
    if not exists:
        return
    club_id = ensure_default_club(conn)
    for row in conn.execute("SELECT * FROM roster"):
        conn.execute(
            """
            INSERT INTO players (club_id, name, gender, rating, notes, active,
                                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(club_id, name) DO NOTHING
            """,
            (club_id, row["name"], row["gender"], row["rating"],
             row["notes"] if "notes" in row.keys() else "", 1, _now(), _now()),
        )
    conn.execute("DROP TABLE roster")
    conn.commit()


def ensure_default_club(conn: sqlite3.Connection) -> int:
    """Klub bawaan, supaya host bisa langsung pakai tanpa setup dulu."""
    row = conn.execute("SELECT id FROM clubs ORDER BY id LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO clubs (name, created_at, updated_at) VALUES (?,?,?)",
        ("Klub Saya", _now(), _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Helper generik untuk master data
# ---------------------------------------------------------------------------

# Kolom yang boleh dicari per tabel. Dipakai untuk menyusun SQL, jadi daftarnya
# ditulis eksplisit di sini dan tidak pernah diambil dari input pengguna.
SEARCH_COLUMNS = {
    "clubs": ("name", "city", "contact"),
    "venues": ("name", "address"),
    "players": ("name", "nickname", "level_label"),
}

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


def _find_by_name(conn, table: str, club_id, name: str):
    """Cari baris berdasarkan nama, TANPA membedakan huruf besar-kecil.

    UNIQUE di SQLite membandingkan teks secara persis, jadi "Nisa" dan "NISA"
    lolos sebagai dua orang berbeda - kejadian yang sangat mungkin kalau host
    mengetik nama yang sama di waktu berbeda. Pencarian ini yang mencegahnya.
    """
    name = (name or "").strip()
    if not name:
        return None
    if table == "clubs":
        row = conn.execute(
            "SELECT id FROM clubs WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE club_id IS ? AND name = ? COLLATE NOCASE",
            (club_id, name),
        ).fetchone()
    return int(row["id"]) if row else None


def _where(table: str, club_id: int | None, include_inactive: bool,
           search: str) -> tuple[str, list]:
    sql = " WHERE 1=1"
    params: list = []
    if not include_inactive:
        sql += " AND active = 1"
    if club_id is not None and table != "clubs":
        sql += " AND club_id = ?"
        params.append(club_id)
    if search:
        cols = SEARCH_COLUMNS.get(table, ("name",))
        sql += " AND (" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")"
        params += [f"%{search}%"] * len(cols)
    return sql, params


def _list(conn, table: str, club_id: int | None, include_inactive: bool,
          order: str, search: str = "") -> list[dict]:
    where, params = _where(table, club_id, include_inactive, search)
    return [dict(r) for r in
            conn.execute(f"SELECT * FROM {table}{where} ORDER BY {order}", params)]


def _list_paged(conn, table: str, club_id: int | None, include_inactive: bool,
                order: str, search: str = "", page: int = 1,
                per_page: int = DEFAULT_PAGE_SIZE) -> dict:
    """Halaman data + total, supaya tabel master tetap ringan saat data banyak."""
    per_page = max(1, min(MAX_PAGE_SIZE, per_page))
    where, params = _where(table, club_id, include_inactive, search)

    total = int(conn.execute(
        f"SELECT COUNT(*) AS n FROM {table}{where}", params
    ).fetchone()["n"])

    pages = max(1, -(-total // per_page))       # pembulatan ke atas
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page

    items = [dict(r) for r in conn.execute(
        f"SELECT * FROM {table}{where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, per_page, offset],
    )]
    return {"items": items, "total": total, "page": page,
            "pages": pages, "per_page": per_page}


def _soft_delete(conn, table: str, row_id: int) -> None:
    conn.execute(f"UPDATE {table} SET active = 0, updated_at = ? WHERE id = ?",
                 (_now(), row_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Klub
# ---------------------------------------------------------------------------

def list_clubs(conn, include_inactive: bool = False) -> list[dict]:
    return _list(conn, "clubs", None, include_inactive, "name COLLATE NOCASE")


def page_clubs(conn, search: str = "", page: int = 1,
               per_page: int = DEFAULT_PAGE_SIZE) -> dict:
    return _list_paged(conn, "clubs", None, False, "name COLLATE NOCASE",
                       search, page, per_page)


def save_club(conn, data: dict) -> int:
    fields = ("name", "city", "contact", "wa_group", "notes")
    vals = [(data.get(f) or "").strip() for f in fields]
    if not vals[0]:
        raise ValueError("Nama klub wajib diisi.")
    cid = data.get("id")
    if not cid:
        cid = _find_by_name(conn, "clubs", None, vals[0])
        if cid:
            row = conn.execute("SELECT name FROM clubs WHERE id=?",
                               (cid,)).fetchone()
            vals[0] = row["name"]

    # logo tidak disertakan -> pertahankan yang lama; string kosong -> hapus.
    logo = data.get("logo")
    if cid:
        if logo is None:
            conn.execute(
                "UPDATE clubs SET name=?, city=?, contact=?, wa_group=?, notes=?, "
                "active=1, updated_at=? WHERE id=?",
                (*vals, _now(), int(cid)),
            )
        else:
            conn.execute(
                "UPDATE clubs SET name=?, city=?, contact=?, wa_group=?, notes=?, "
                "logo=?, active=1, updated_at=? WHERE id=?",
                (*vals, logo, _now(), int(cid)),
            )
        conn.commit()
        return int(cid)

    # Baris yang pernah dihapus masih memegang UNIQUE(nama), jadi insert
    # dengan nama sama harus MENGHIDUPKAN KEMBALI baris itu, bukan gagal.
    conn.execute(
        """
        INSERT INTO clubs (name, city, contact, wa_group, notes, logo,
                           created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(name) DO UPDATE SET
            city       = excluded.city,
            contact    = excluded.contact,
            wa_group   = excluded.wa_group,
            notes      = excluded.notes,
            logo       = excluded.logo,
            active     = 1,
            updated_at = excluded.updated_at
        """,
        (*vals, logo or "", _now(), _now()),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM clubs WHERE name = ?", (vals[0],)).fetchone()
    return int(row["id"])


def get_club(conn, club_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM clubs WHERE id = ?", (club_id,)).fetchone()
    return dict(row) if row else None


def delete_club(conn, club_id: int) -> None:
    _soft_delete(conn, "clubs", club_id)


# ---------------------------------------------------------------------------
# Venue
# ---------------------------------------------------------------------------

def list_venues(conn, club_id: int | None = None,
                include_inactive: bool = False) -> list[dict]:
    return _list(conn, "venues", club_id, include_inactive, "name COLLATE NOCASE")


def page_venues(conn, club_id: int | None = None, search: str = "",
                page: int = 1, per_page: int = DEFAULT_PAGE_SIZE) -> dict:
    return _list_paged(conn, "venues", club_id, False, "name COLLATE NOCASE",
                       search, page, per_page)


def save_venue(conn, data: dict) -> int:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Nama venue wajib diisi.")
    vals = (
        data.get("club_id") or None,
        name,
        (data.get("address") or "").strip(),
        max(1, int(data.get("court_count") or 1)),
        max(0.0, float(data.get("price_per_hour") or 0)),
        (data.get("notes") or "").strip(),
    )
    vid = data.get("id")
    if not vid:
        vid = _find_by_name(conn, "venues", vals[0], name)
        if vid:
            row = conn.execute("SELECT name FROM venues WHERE id=?",
                               (vid,)).fetchone()
            vals = (vals[0], row["name"], *vals[2:])
    if vid:
        conn.execute(
            "UPDATE venues SET club_id=?, name=?, address=?, court_count=?, "
            "price_per_hour=?, notes=?, active=1, updated_at=? WHERE id=?",
            (*vals, _now(), int(vid)),
        )
        conn.commit()
        return int(vid)
    # Baris yang pernah dihapus masih memegang UNIQUE(nama), jadi insert
    # dengan nama sama harus MENGHIDUPKAN KEMBALI baris itu, bukan gagal.
    conn.execute(
        """
        INSERT INTO venues (club_id, name, address, court_count, price_per_hour,
                            notes, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(club_id, name) DO UPDATE SET
            address        = excluded.address,
            court_count    = excluded.court_count,
            price_per_hour = excluded.price_per_hour,
            notes          = excluded.notes,
            active         = 1,
            updated_at     = excluded.updated_at
        """,
        (*vals, _now(), _now()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM venues WHERE club_id IS ? AND name = ?", (vals[0], vals[1])
    ).fetchone()
    return int(row["id"])


def delete_venue(conn, venue_id: int) -> None:
    _soft_delete(conn, "venues", venue_id)


# ---------------------------------------------------------------------------
# Pemain
# ---------------------------------------------------------------------------

def list_players(conn, club_id: int | None = None,
                 include_inactive: bool = False) -> list[dict]:
    return _list(conn, "players", club_id, include_inactive, "name COLLATE NOCASE")


def page_players(conn, club_id: int | None = None, search: str = "",
                 page: int = 1, per_page: int = DEFAULT_PAGE_SIZE) -> dict:
    return _list_paged(conn, "players", club_id, False, "name COLLATE NOCASE",
                       search, page, per_page)


def save_player(conn, data: dict) -> int:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Nama pemain wajib diisi.")
    gender = data.get("gender") if data.get("gender") in ("M", "F") else None
    vals = (
        data.get("club_id") or None,
        name,
        (data.get("nickname") or "").strip(),
        (data.get("contact") or "").strip(),
        gender,
        float(data.get("rating") or 3.0),
        (data.get("level_label") or "").strip(),
        (data.get("notes") or "").strip(),
        (data.get("joined_at") or "").strip(),
    )
    pid = data.get("id")
    if not pid:
        # Ditemukan lewat nama berarti host hanya salah kapital, bukan sedang
        # mengganti nama - ejaan yang sudah dipakai dipertahankan.
        pid = _find_by_name(conn, "players", vals[0], name)
        if pid:
            row = conn.execute("SELECT name FROM players WHERE id=?",
                               (pid,)).fetchone()
            vals = (vals[0], row["name"], *vals[2:])
    if pid:
        conn.execute(
            "UPDATE players SET club_id=?, name=?, nickname=?, contact=?, gender=?, "
            "rating=?, level_label=?, notes=?, joined_at=?, active=1, updated_at=? "
            "WHERE id=?",
            (*vals, _now(), int(pid)),
        )
        conn.commit()
        return int(pid)
    # Baris yang pernah dihapus masih memegang UNIQUE(nama), jadi insert
    # dengan nama sama harus MENGHIDUPKAN KEMBALI baris itu, bukan gagal.
    conn.execute(
        """
        INSERT INTO players (club_id, name, nickname, contact, gender, rating,
                             level_label, notes, joined_at, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(club_id, name) DO UPDATE SET
            nickname    = excluded.nickname,
            contact     = excluded.contact,
            gender      = excluded.gender,
            rating      = excluded.rating,
            level_label = excluded.level_label,
            notes       = excluded.notes,
            joined_at   = excluded.joined_at,
            active      = 1,
            updated_at  = excluded.updated_at
        """,
        (*vals, _now(), _now()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM players WHERE club_id IS ? AND name = ?", (vals[0], vals[1])
    ).fetchone()
    return int(row["id"])


def bulk_save_players(conn, club_id: int, people: list[dict]) -> int:
    """Tambah/perbarui banyak pemain sekaligus berdasarkan nama."""
    n = 0
    for p in people:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        gender = p.get("gender") if p.get("gender") in ("M", "F") else None
        rating = float(p.get("rating", 3.0))
        existing = _find_by_name(conn, "players", club_id, name)
        if existing:
            # Nama yang sama beda kapital tetap orang yang sama; jangan bikin
            # baris kedua. Nama aslinya dipertahankan supaya ejaan yang sudah
            # dipakai host tidak berubah diam-diam.
            conn.execute(
                "UPDATE players SET rating=?, gender=COALESCE(?, gender), "
                "active=1, updated_at=? WHERE id=?",
                (rating, gender, _now(), existing),
            )
        else:
            conn.execute(
                "INSERT INTO players (club_id, name, gender, rating, active, "
                "created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                (club_id, name, gender, rating, _now(), _now()),
            )
        n += 1
    conn.commit()
    return n


def delete_player(conn, player_id: int) -> None:
    _soft_delete(conn, "players", player_id)


# ---------------------------------------------------------------------------
# Acara
# ---------------------------------------------------------------------------

def save_event(conn, request: dict, schedule: dict,
               event_id: int | None = None) -> int:
    """Simpan (atau perbarui) satu acara beserta daftar pesertanya."""
    cfg = schedule.get("config", {})
    stats = schedule.get("stats", {})
    econ = request.get("economics") or {}

    hours = float(cfg.get("duration_minutes", 0)) / 60.0
    courts = int(cfg.get("courts", 0))
    n_players = len(schedule.get("players", []))
    # Court-jam yang benar-benar disewa. Bukan court x jam: acara yang melepas
    # court kedua di tengah jalan membayar lebih sedikit, dan angka inilah yang
    # muncul di daftar acara dan rekap klub - salah di sini berarti laporan laba
    # klub ikut salah, jauh setelah acaranya lewat.
    court_hours = courts * hours
    if cfg.get("courts_after") and cfg.get("courts_from_round"):
        menit = float(cfg.get("duration_minutes", 0))
        rm = float(cfg.get("round_minutes", 0) or 0)
        awal = min(menit, float(cfg.get("warmup_minutes", 0) or 0)
                   + (int(cfg["courts_from_round"]) - 1) * rm)
        court_hours = (courts * awal
                       + int(cfg["courts_after"]) * (menit - awal)) / 60.0
    total_cost = (court_hours * float(econ.get("court_price_per_hour") or 0)
                  + float(econ.get("other_costs") or 0))
    revenue = n_players * float(econ.get("fee_per_player") or 0)

    meta = (
        request.get("club_id") or None,
        request.get("venue_id") or None,
        request.get("title") or "Meet Padel",
        request.get("event_date", ""),
        request.get("start_clock", ""),
        request.get("venue", ""),
        n_players,
        courts,
        int(cfg.get("duration_minutes", 0)),
        len(schedule.get("rounds", [])),
        cfg.get("mode", ""),
        float(stats.get("quality_score", 0)),
        round(total_cost, 2),
        round(revenue, 2),
        round(revenue - total_cost, 2),
        json.dumps(request, ensure_ascii=False),
        json.dumps(schedule, ensure_ascii=False, default=str),
    )

    if event_id:
        conn.execute(
            """
            UPDATE events SET club_id=?, venue_id=?, title=?, event_date=?,
                start_clock=?, venue_name=?, n_players=?, courts=?, duration_min=?,
                rounds=?, mode=?, quality_score=?, total_cost=?, revenue=?, profit=?,
                request_json=?, schedule_json=?, updated_at=?
            WHERE id=?
            """,
            (*meta, _now(), event_id),
        )
        eid = int(event_id)
    else:
        cur = conn.execute(
            """
            INSERT INTO events (club_id, venue_id, title, event_date, start_clock,
                venue_name, n_players, courts, duration_min, rounds, mode,
                quality_score, total_cost, revenue, profit, request_json,
                schedule_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (*meta, _now(), _now()),
        )
        eid = int(cur.lastrowid)

    _save_participants(conn, eid, request, schedule)
    conn.commit()
    return eid


def _save_participants(conn, event_id: int, request: dict, schedule: dict) -> None:
    conn.execute("DELETE FROM event_participants WHERE event_id = ?", (event_id,))
    stats = schedule.get("stats", {})
    plays = stats.get("plays_per_player", {})
    byes = stats.get("byes_per_player", {})
    roles = stats.get("roles_per_player", {})

    club_id = request.get("club_id")
    known: dict[str, int] = {}
    if club_id:
        for row in conn.execute(
            "SELECT id, name FROM players WHERE club_id = ?", (club_id,)
        ):
            known[row["name"].lower()] = int(row["id"])

    for p in schedule.get("players", []):
        pid = str(p["id"])
        role = roles.get(pid) or roles.get(p["id"]) or {}
        conn.execute(
            """
            INSERT INTO event_participants
                (event_id, player_id, name, rounds_played, rounds_rested, duties)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(event_id, name) DO UPDATE SET
                rounds_played=excluded.rounds_played,
                rounds_rested=excluded.rounds_rested,
                duties=excluded.duties
            """,
            (
                event_id,
                known.get((p.get("name") or "").lower()),
                p.get("name", ""),
                int(plays.get(pid, plays.get(p["id"], 0)) or 0),
                int(byes.get(pid, byes.get(p["id"], 0)) or 0),
                int(role.get("total", 0) if isinstance(role, dict) else 0),
            ),
        )


def list_events(conn, club_id: int | None = None, limit: int = 200,
                search: str = "") -> list[dict]:
    sql = (
        "SELECT e.id, e.title, e.event_date, e.venue_name, e.created_at, "
        "e.n_players, e.courts, e.duration_min, e.rounds, e.mode, "
        "e.quality_score, e.total_cost, e.revenue, e.profit, c.name AS club_name "
        "FROM events e LEFT JOIN clubs c ON c.id = e.club_id WHERE 1=1"
    )
    params: list = []
    if club_id:
        sql += " AND e.club_id = ?"
        params.append(club_id)
    if search:
        sql += " AND (e.title LIKE ? OR e.venue_name LIKE ? OR e.event_date LIKE ?)"
        params += [f"%{search}%"] * 3
    sql += (" ORDER BY COALESCE(NULLIF(e.event_date,''), e.created_at) DESC, "
            "e.id DESC LIMIT ?")
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def page_events(conn, club_id: int | None = None, search: str = "",
                page: int = 1, per_page: int = DEFAULT_PAGE_SIZE) -> dict:
    """Halaman riwayat acara + total, untuk tabel Riwayat."""
    per_page = max(1, min(MAX_PAGE_SIZE, per_page))
    where = " WHERE 1=1"
    params: list = []
    if club_id:
        where += " AND e.club_id = ?"
        params.append(club_id)
    if search:
        where += " AND (e.title LIKE ? OR e.venue_name LIKE ? OR e.event_date LIKE ?)"
        params += [f"%{search}%"] * 3

    total = int(conn.execute(
        f"SELECT COUNT(*) AS n FROM events e{where}", params
    ).fetchone()["n"])
    pages = max(1, -(-total // per_page))
    page = max(1, min(page, pages))

    rows = conn.execute(
        "SELECT e.id, e.title, e.event_date, e.venue_name, e.created_at, "
        "e.n_players, e.courts, e.duration_min, e.rounds, e.mode, "
        "e.quality_score, e.total_cost, e.revenue, e.profit, c.name AS club_name "
        "FROM events e LEFT JOIN clubs c ON c.id = e.club_id"
        + where
        + " ORDER BY COALESCE(NULLIF(e.event_date,''), e.created_at) DESC, "
          "e.id DESC LIMIT ? OFFSET ?",
        [*params, per_page, (page - 1) * per_page],
    )
    return {"items": [dict(r) for r in rows], "total": total, "page": page,
            "pages": pages, "per_page": per_page}


def get_event(conn, event_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["request"] = json.loads(data.pop("request_json"))
    data["schedule"] = json.loads(data.pop("schedule_json"))
    return data


def delete_event(conn, event_id: int) -> None:
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Statistik lintas acara
# ---------------------------------------------------------------------------

def player_stats(conn, club_id: int | None = None) -> list[dict]:
    """Rekap keikutsertaan tiap orang di seluruh acara tersimpan.

    Menjawab pertanyaan host: siapa yang rajin datang, siapa yang selama ini
    paling sering kebagian duduk, dan apakah pembagian tugas sudah adil.
    """
    sql = """
        SELECT p.name                       AS name,
               COUNT(DISTINCT p.event_id)   AS events,
               SUM(p.rounds_played)         AS rounds_played,
               SUM(p.rounds_rested)         AS rounds_rested,
               SUM(p.duties)                AS duties,
               MAX(COALESCE(NULLIF(e.event_date,''), e.created_at)) AS last_seen
        FROM event_participants p
        JOIN events e ON e.id = p.event_id
        WHERE 1=1
    """
    params: list = []
    if club_id:
        sql += " AND e.club_id = ?"
        params.append(club_id)
    sql += " GROUP BY p.name COLLATE NOCASE ORDER BY events DESC, name COLLATE NOCASE"

    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        total = (d["rounds_played"] or 0) + (d["rounds_rested"] or 0)
        d["rest_pct"] = round((d["rounds_rested"] or 0) / total * 100, 1) if total else 0.0
        out.append(d)
    return out


def club_summary(conn, club_id: int) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS events, COALESCE(SUM(profit),0) AS profit,
               COALESCE(SUM(revenue),0) AS revenue,
               COALESCE(SUM(n_players),0) AS attendances,
               COALESCE(AVG(quality_score),0) AS avg_quality
        FROM events WHERE club_id = ?
        """,
        (club_id,),
    ).fetchone()
    members = conn.execute(
        "SELECT COUNT(*) AS n FROM players WHERE club_id = ? AND active = 1",
        (club_id,),
    ).fetchone()
    return {
        "events": int(row["events"]),
        "revenue": float(row["revenue"]),
        "profit": float(row["profit"]),
        "attendances": int(row["attendances"]),
        "avg_quality": round(float(row["avg_quality"]), 1),
        "members": int(members["n"]),
    }
