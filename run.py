#!/usr/bin/env python3
"""Web app lokal untuk menyusun jadwal meet padel.

Jalankan:

    python run.py

Lalu buka http://127.0.0.1:8770 (browser dibuka otomatis).

Tanpa dependency eksternal — cukup Python 3.10+.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from padel_scheduler import (
    Config,
    Economics,
    Player,
    Segment,
    analyze,
    build_schedule,
)
from padel_scheduler import storage
from padel_scheduler.economics import compare, fee_for_target_margin, upgrade_analysis
from padel_scheduler.html_report import build_html
from padel_scheduler.models import COURT_PREFERENCES
from padel_scheduler.presets import PRESETS
from padel_scheduler.report import (
    format_date_id,
    to_csv,
    to_dict,
    to_personal_text,
    to_text,
)
from padel_scheduler.scheduler import ScheduleError

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 8 * 1024 * 1024

# Logo klub disimpan sebagai data URI. Hanya PNG/JPEG, dengan batas ukuran
# supaya database tetap ringan dan laporan tidak membengkak.
LOGO_MAX_BYTES = 400 * 1024
LOGO_PREFIXES = ("data:image/png;base64,", "data:image/jpeg;base64,")


def _clean_logo(value):
    """Validasi data URI logo. None = jangan diubah, '' = hapus."""
    if value is None:
        return None
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith(LOGO_PREFIXES):
        raise ValueError("Logo harus berupa gambar PNG atau JPEG.")
    head, _, b64 = value.partition(",")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Data logo rusak atau tidak lengkap.") from exc
    if len(raw) > LOGO_MAX_BYTES:
        raise ValueError(
            f"Ukuran logo {len(raw) // 1024} KB melebihi batas "
            f"{LOGO_MAX_BYTES // 1024} KB. Perkecil gambarnya dulu."
        )
    # Cek angka ajaib supaya ekstensi yang diganti nama tidak lolos.
    if head.startswith("data:image/png") and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Berkas ini bukan PNG yang sah.")
    if head.startswith("data:image/jpeg") and not raw.startswith(b"\xff\xd8\xff"):
        raise ValueError("Berkas ini bukan JPEG yang sah.")
    return value


# ---------------------------------------------------------------------------
# Parsing payload
# ---------------------------------------------------------------------------

def _players_from(payload: dict) -> list[Player]:
    raw = payload.get("players") or []
    players: list[Player] = []
    for i, item in enumerate(raw):
        gender = item.get("gender") or None
        if gender not in ("M", "F", None):
            gender = None
        pref = item.get("court_preference") or None
        if pref not in COURT_PREFERENCES:
            pref = None
        partner = item.get("partner_id")
        players.append(
            Player(
                id=int(item.get("id", i)),
                name=(item.get("name") or f"Pemain {i + 1}").strip(),
                rating=float(item.get("rating", 3.0)),
                gender=gender,
                partner_id=int(partner) if partner not in (None, "", -1) else None,
                court_preference=pref,
            )
        )
    return players


def _config_from(payload: dict) -> Config:
    segs = [
        Segment(
            label=s.get("label", ""),
            rounds=int(s.get("rounds", 0)),
            rule=s.get("rule", "open"),
        )
        for s in (payload.get("segments") or [])
        if int(s.get("rounds", 0)) > 0
    ]
    ro = payload.get("rounds_override")
    return Config(
        courts=int(payload.get("courts", 1)),
        duration_minutes=int(payload.get("duration_minutes", 120)),
        round_minutes=int(payload.get("round_minutes", 12)),
        warmup_minutes=int(payload.get("warmup_minutes", 10)),
        mode=payload.get("mode", "americano"),
        rounds_override=int(ro) if ro not in (None, "", 0) else None,
        tier_count=int(payload.get("tier_count", 2)),
        seed=int(payload.get("seed", 42)),
        effort=max(1000, min(200_000, int(payload.get("effort", 30_000)))),
        referees_per_court=max(0, min(2, int(payload.get("referees_per_court", 0)))),
        ballboys_per_court=max(0, min(3, int(payload.get("ballboys_per_court", 0)))),
        segments=segs,
    )


def _econ_from(payload: dict) -> Economics:
    e = payload.get("economics") or {}
    return Economics(
        court_price_per_hour=float(e.get("court_price_per_hour", 0) or 0),
        fee_per_player=float(e.get("fee_per_player", 0) or 0),
        other_costs=float(e.get("other_costs", 0) or 0),
    )


# ---------------------------------------------------------------------------
# Endpoint handlers
# ---------------------------------------------------------------------------

def api_analyze(payload: dict) -> dict:
    players = _players_from(payload)
    cfg = _config_from(payload)
    n = len(players) or int(payload.get("n_players", 0))

    total_seg_rounds = cfg.total_segment_rounds()
    rounds_override = cfg.rounds_override or (total_seg_rounds or None)
    round_minutes = cfg.round_minutes
    if total_seg_rounds > 0:
        usable = cfg.duration_minutes - cfg.warmup_minutes
        if usable > 0:
            round_minutes = max(1, usable // total_seg_rounds)

    rep = analyze(
        n_players=n,
        courts=cfg.courts,
        duration_minutes=cfg.duration_minutes,
        round_minutes=round_minutes,
        warmup_minutes=cfg.warmup_minutes,
        rounds_override=rounds_override,
    )

    men = sum(1 for p in players if p.gender == "M")
    women = sum(1 for p in players if p.gender == "F")
    return {
        "report": {
            k: v for k, v in vars(rep).items() if k != "issues"
        },
        "issues": [vars(i) for i in rep.sorted_issues()],
        "roster": {"men": men, "women": women, "unspecified": n - men - women},
        "effective_round_minutes": round_minutes,
    }


def api_economics(payload: dict) -> dict:
    cfg = _config_from(payload)
    econ = _econ_from(payload)
    n = len(_players_from(payload)) or int(payload.get("n_players", 0))
    if n < 4:
        return {"error": "Butuh minimal 4 pemain untuk hitung biaya."}

    hours = cfg.duration_minutes / 60.0

    # Daftar pembanding harus berpusat pada pilihan host, bukan pada jumlah
    # court "ideal". Host sering sengaja menyewa court lebih sedikit demi
    # margin; kalau court itu tidak masuk daftar, skenarionya sendiri hilang
    # dari perbandingan dan grafik kehilangan titik acuannya.
    courts = cfg.courts
    court_options = sorted({
        c for c in range(max(1, courts - 2), courts + 3) if c >= 1
    } | {courts})

    options = compare(
        n_players=n,
        econ=econ,
        court_options=court_options,
        hour_options=sorted({1.0, 1.5, 2.0, 2.5, 3.0, round(hours, 2)}),
        round_minutes=cfg.round_minutes,
        warmup_minutes=cfg.warmup_minutes,
    )
    # Skenario yang sedang dipakai tidak boleh terpangkas oleh batas tampilan.
    def _is_current(o):
        return o.courts == courts and abs(o.hours - hours) < 0.01

    shown = options[:24]
    if not any(_is_current(o) for o in shown):
        current = next((o for o in options if _is_current(o)), None)
        if current is not None:
            shown = [current] + shown[:23]
    up = upgrade_analysis(n, cfg.courts, hours, econ,
                          cfg.round_minutes, cfg.warmup_minutes)

    return {
        "current": vars(up["base"]),
        "plus_one_court": vars(up["plus_one_court"]),
        "upgrade": {
            k: v for k, v in up.items()
            if k not in ("base", "plus_one_court")
        },
        "options": [vars(o) for o in shown],
        "fee_suggestions": {
            str(int(m)): fee_for_target_margin(n, cfg.courts, hours, econ, m)
            for m in (20, 30, 40, 50)
        },
    }


def _generate(payload: dict):
    return build_schedule(_players_from(payload), _config_from(payload))


def api_schedule(payload: dict) -> dict:
    sch = _generate(payload)
    clock = payload.get("start_clock") or None
    data = to_dict(sch)
    data["text"] = to_text(sch, start_clock=clock,
                           title=payload.get("title") or "JADWAL PADEL")
    data["personal_text"] = to_personal_text(sch, start_clock=clock)
    data["csv"] = to_csv(sch)
    return data


# -- database ---------------------------------------------------------------

def api_event_save(payload: dict) -> dict:
    sch = _generate(payload)
    clock = payload.get("start_clock") or None
    data = to_dict(sch)
    data["text"] = to_text(sch, start_clock=clock,
                           title=payload.get("title") or "JADWAL PADEL")
    data["personal_text"] = to_personal_text(sch, start_clock=clock)
    data["csv"] = to_csv(sch)

    request = {k: v for k, v in payload.items() if k != "event_id"}
    with storage.session() as conn:
        eid = storage.save_event(conn, request, data,
                                 event_id=payload.get("event_id") or None)
    return {"id": eid, "ok": True}


def api_event_delete(payload: dict) -> dict:
    with storage.session() as conn:
        storage.delete_event(conn, int(payload["id"]))
    return {"ok": True}


def api_players_bulk(payload: dict) -> dict:
    with storage.session() as conn:
        club_id = payload.get("club_id") or storage.ensure_default_club(conn)
        n = storage.bulk_save_players(conn, int(club_id), payload.get("players") or [])
    return {"saved": n, "club_id": club_id}


def api_club_save(payload: dict) -> dict:
    data = dict(payload)
    data["logo"] = _clean_logo(payload.get("logo"))
    with storage.session() as conn:
        return {"id": storage.save_club(conn, data), "ok": True}


def _club_logo(club_id) -> str:
    """Logo klub untuk ditanam di laporan. Kosong kalau tidak ada."""
    if not club_id:
        return ""
    try:
        with storage.session() as conn:
            club = storage.get_club(conn, int(club_id))
        return (club or {}).get("logo") or ""
    except (ValueError, TypeError):
        return ""


def _master_routes():
    """Endpoint CRUD seragam untuk venue dan pemain."""
    specs = {
        "venues": (storage.save_venue, storage.delete_venue),
        "players": (storage.save_player, storage.delete_player),
    }
    routes = {}
    for entity, (saver, deleter) in specs.items():
        def make_save(fn=saver):
            def handler(payload: dict) -> dict:
                with storage.session() as conn:
                    return {"id": fn(conn, payload), "ok": True}
            return handler

        def make_delete(fn=deleter):
            def handler(payload: dict) -> dict:
                with storage.session() as conn:
                    fn(conn, int(payload["id"]))
                return {"ok": True}
            return handler

        routes[f"/api/{entity}/save"] = make_save()
        routes[f"/api/{entity}/delete"] = make_delete()
    return routes


# Endpoint daftar berhalaman. Semua mengembalikan bentuk yang sama:
# {items, total, page, pages, per_page}
PAGED_ROUTES = {
    "/api/clubs/list": storage.page_clubs,
    "/api/venues/list": storage.page_venues,
    "/api/players/list": storage.page_players,
    "/api/events/list": storage.page_events,
}


ROUTES = {
    "/api/analyze": api_analyze,
    "/api/economics": api_economics,
    "/api/schedule": api_schedule,
    "/api/events/save": api_event_save,
    "/api/events/delete": api_event_delete,
    "/api/players/bulk": api_players_bulk,
    "/api/clubs/save": api_club_save,
    "/api/clubs/delete": lambda pl: _delete_club(pl),
    **_master_routes(),
}


def _delete_club(payload: dict) -> dict:
    with storage.session() as conn:
        storage.delete_club(conn, int(payload["id"]))
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "PadelScheduler/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - senyapkan log per request
        pass

    # -- util -------------------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, ctype: str, filename: str | None = None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path):
        if not path.is_file():
            self.send_error(404, "Tidak ditemukan")
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8"
                         if ctype.startswith("text/") or "javascript" in ctype
                         else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html")
        elif path == "/api/presets":
            self._send_json({
                "presets": PRESETS,
                "court_preferences": list(COURT_PREFERENCES),
            })
        elif path == "/api/master":
            # Satu panggilan memuat semua master data yang dibutuhkan UI.
            with storage.session() as conn:
                club_id = storage.ensure_default_club(conn)
                self._send_json({
                    "clubs": storage.list_clubs(conn),
                    "venues": storage.list_venues(conn),
                    "players": storage.list_players(conn),
                    "default_club_id": club_id,
                })
        elif path == "/api/stats/players":
            cid = (query.get("club_id") or [""])[0]
            with storage.session() as conn:
                self._send_json({
                    "stats": storage.player_stats(
                        conn, int(cid) if cid.isdigit() else None
                    )
                })
        elif path == "/api/stats/club":
            cid = (query.get("club_id") or [""])[0]
            if not cid.isdigit():
                self._send_json({"error": "club_id wajib diisi."}, 400)
                return
            with storage.session() as conn:
                self._send_json({"summary": storage.club_summary(conn, int(cid))})
        elif path in PAGED_ROUTES:
            fn = PAGED_ROUTES[path]
            cid = (query.get("club_id") or [""])[0]
            page = (query.get("page") or ["1"])[0]
            per = (query.get("per_page") or [""])[0]
            kwargs = {
                "search": (query.get("search") or [""])[0],
                "page": int(page) if page.lstrip("-").isdigit() else 1,
                "per_page": (int(per) if per.isdigit()
                             else storage.DEFAULT_PAGE_SIZE),
            }
            # Klub tidak disaring per klub - itu daftar induknya.
            if path != "/api/clubs/list":
                kwargs["club_id"] = int(cid) if cid.isdigit() else None
            with storage.session() as conn:
                self._send_json(fn(conn, **kwargs))
        elif path == "/api/events/get":
            try:
                eid = int((query.get("id") or ["0"])[0])
            except ValueError:
                self._send_json({"error": "id tidak valid"}, 400)
                return
            with storage.session() as conn:
                ev = storage.get_event(conn, eid)
            if ev is None:
                self._send_json({"error": "Jadwal tidak ditemukan."}, 404)
            else:
                self._send_json({"event": ev})
        elif path.startswith("/web/"):
            target = (WEB_DIR / path[len("/web/"):]).resolve()
            if WEB_DIR.resolve() in target.parents or target.parent == WEB_DIR.resolve():
                self._send_file(target)
            else:
                self.send_error(403, "Terlarang")
        else:
            self.send_error(404, "Tidak ditemukan")

    def do_POST(self):
        path = urlparse(self.path).path

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send_json({"error": "Payload terlalu besar."}, 413)
            return
        raw = self.rfile.read(length) if length else b""

        # /api/report dikirim lewat form submit (bukan fetch) supaya hasilnya
        # bisa dibuka sebagai tab baru dan langsung di-print jadi PDF.
        if path == "/api/report":
            try:
                fields = parse_qs(raw.decode("utf-8"))
                payload = json.loads(fields.get("payload", ["{}"])[0])
                sch = _generate(payload)
                html = build_html(
                    sch,
                    title=payload.get("title") or "Jadwal Meet Padel",
                    event_date=payload.get("event_date", ""),
                    venue=payload.get("venue", ""),
                    start_clock=payload.get("start_clock") or None,
                    logo=_club_logo(payload.get("club_id")),
                )
                self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
            except ScheduleError as exc:
                self._send_bytes(
                    f"<p style='font-family:sans-serif;padding:30px'>{exc}</p>"
                    .encode("utf-8"), "text/html; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                self._send_bytes(
                    f"<p style='font-family:sans-serif;padding:30px'>"
                    f"Gagal membuat laporan: {exc}</p>".encode("utf-8"),
                    "text/html; charset=utf-8")
            return

        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "JSON tidak valid."}, 400)
            return


        handler = ROUTES.get(path)
        if handler is None:
            self.send_error(404, "Tidak ditemukan")
            return

        try:
            self._send_json(handler(payload))
        except ScheduleError as exc:
            self._send_json({"error": str(exc)}, 400)
        except (ValueError, KeyError, TypeError) as exc:
            self._send_json({"error": f"Input tidak valid: {exc}"}, 400)
        except Exception as exc:  # noqa: BLE001 - jangan sampai server mati
            self._send_json({"error": f"Kesalahan internal: {exc}"}, 500)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generator jadwal meet padel")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Generator jadwal padel berjalan di {url}")
    print("Tekan Ctrl+C untuk berhenti.")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDihentikan.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
