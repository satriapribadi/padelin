'use strict';

/**
 * Grafik SVG tanpa dependency, mengikuti design system app.
 *
 * Palet sudah divalidasi terhadap surface panel (#161c24) memakai validator
 * data-viz: 2 seri kategorikal (#3987e5 biru, #199e70 aqua) lolos seluruh gate
 * — lightness band, chroma floor, pemisahan CVD (worst deutan dE 19.6),
 * ambang penglihatan normal (dE 20.9), dan kontras >= 3:1.
 *
 * Aturan yang dipegang di sini:
 *  - Marka tipis, gridline hairline solid, sumbu recessive
 *  - Jarak 2px berwarna surface memisahkan segmen bertumpuk (bukan garis tepi)
 *  - Teks memakai token teks, tidak pernah memakai warna seri
 *  - Legend selalu ada untuk >= 2 seri; label langsung dipakai secukupnya
 *  - Tooltip menambah, tidak pernah menjadi satu-satunya jalan ke sebuah angka:
 *    tiap grafik punya kembaran tabel
 *  - Nama pemain adalah data tak tepercaya -> selalu lewat textContent
 */

const VIZ = {
  s1: '#3987e5',        // seri 1: ronde main
  s2: '#199e70',        // seri 2: tugas (wasit/ballboy)
  neutral: '#5b6878',   // istirahat: hadir tapi recessive (3.02:1)
  grid: '#232d3a',
  axis: '#33404f',
  surface: '#161c24',   // dipakai sebagai jarak antar segmen
  ink: '#e8edf3',
  muted: '#8b98a9',
  dim: '#5f6b7a',
  good: '#3ec98a',
  warn: '#f0a94c',
};

const NS = 'http://www.w3.org/2000/svg';
const SEG_GAP = 2;      // jarak surface antar segmen bertumpuk
const BAR_MAX = 24;     // tebal maksimum marka batang
const HIT_MIN = 24;     // area sentuh minimum untuk titik scatter

// ---------------------------------------------------------------------------
// Primitif
// ---------------------------------------------------------------------------
function s(tag, attrs = {}) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

function txt(tag, attrs, content) {
  const n = s(tag, attrs);
  n.textContent = content;   // data tak tepercaya tidak pernah lewat innerHTML
  return n;
}

/** Angka rupiah ringkas untuk sumbu: 1.250.000 -> "1,3jt". */
function compactRp(v) {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1).replace('.', ',') + 'M';
  if (a >= 1e6) return (v / 1e6).toFixed(1).replace('.', ',') + 'jt';
  if (a >= 1e3) return Math.round(v / 1e3) + 'rb';
  return String(Math.round(v));
}

const fullRp = (v) => 'Rp ' + Math.round(v).toLocaleString('id-ID');

/**
 * Tick yang membulat ke angka enak dibaca, DAN dijamin mencakup seluruh data.
 *
 * Penjaminan itu bukan hiasan: kalau tick terakhir berhenti di bawah nilai
 * maksimum, titik data tersebut digambar di luar area plot dan melayang lepas
 * dari sumbunya.
 */
function niceTicks(min, max, count = 5) {
  if (min === max) { min -= 1; max += 1; }
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.floor(min / step) * step;
  const end = Math.ceil(max / step) * step;
  const out = [];
  // Toleransi kecil supaya galat pembulatan float tidak memakan tick terakhir.
  for (let v = start; v <= end + step * 1e-6; v += step) out.push(v);
  return out;
}

// ---------------------------------------------------------------------------
// Tooltip tunggal
// ---------------------------------------------------------------------------
let tipEl = null;
function tip() {
  if (!tipEl) {
    tipEl = document.createElement('div');
    tipEl.className = 'viz-tip';
    tipEl.setAttribute('role', 'status');
    document.body.appendChild(tipEl);
  }
  return tipEl;
}

/** rows: [{key: warnaAtauNull, label, value}] — nilai memimpin, label mengikuti. */
function showTip(x, y, title, rows) {
  const t = tip();
  t.textContent = '';
  const h = document.createElement('div');
  h.className = 'viz-tip-title';
  h.textContent = title;
  t.appendChild(h);
  rows.forEach((r) => {
    const line = document.createElement('div');
    line.className = 'viz-tip-row';
    if (r.key) {
      const k = document.createElement('span');
      k.className = 'viz-tip-key';
      k.style.background = r.key;
      line.appendChild(k);
    }
    const v = document.createElement('b');
    v.textContent = r.value;
    const l = document.createElement('span');
    l.textContent = r.label;
    line.append(v, l);
    t.appendChild(line);
  });
  t.style.display = 'block';
  const pad = 14;
  const w = t.offsetWidth, hh = t.offsetHeight;
  let left = x + pad, top = y - hh - pad;
  if (left + w > window.innerWidth - 8) left = x - w - pad;
  if (top < 8) top = y + pad;
  t.style.left = left + 'px';
  t.style.top = top + 'px';
}

function hideTip() { if (tipEl) tipEl.style.display = 'none'; }

/** Pasang tooltip pada sebuah marka, sekaligus untuk keyboard focus. */
function bindTip(node, title, rows) {
  const show = (e) => {
    const r = node.getBoundingClientRect();
    const cx = e.clientX ?? r.left + r.width / 2;
    const cy = e.clientY ?? r.top;
    showTip(cx, cy, title, rows);
  };
  node.addEventListener('pointermove', show);
  node.addEventListener('pointerleave', hideTip);
  node.addEventListener('focus', show);
  node.addEventListener('blur', hideTip);
  node.setAttribute('tabindex', '0');
}

// ---------------------------------------------------------------------------
// Legend & kembaran tabel
// ---------------------------------------------------------------------------
function legend(entries, shape = 'rect') {
  const box = document.createElement('div');
  box.className = 'viz-legend';
  entries.forEach((e) => {
    const item = document.createElement('span');
    item.className = 'viz-legend-item';
    const sw = document.createElement('span');
    // `empty` = bukan segmen berwarna melainkan ruang kosong di batang. Kotaknya
    // digambar sebagai garis tepi saja; kotak berwarna surface tidak terlihat
    // sama sekali di atas panel yang warnanya persis sama.
    sw.className = 'viz-swatch ' + (shape === 'line' ? 'line ' : '')
      + (e.empty ? 'empty' : '');
    if (!e.empty) sw.style.background = e.color;
    const lab = document.createElement('span');
    lab.textContent = e.label;
    item.append(sw, lab);
    box.appendChild(item);
  });
  return box;
}

/**
 * Bungkus grafik: judul, legend, plot, dan tombol Tabel.
 * Kembaran tabel wajib — tooltip tidak boleh jadi satu-satunya jalan ke angka.
 */
function figure(caption, plotNode, legendNode, tableNode) {
  const fig = document.createElement('figure');
  fig.className = 'viz';

  const head = document.createElement('figcaption');
  head.className = 'viz-head';
  const cap = document.createElement('span');
  cap.textContent = caption;
  head.appendChild(cap);

  if (tableNode) {
    const btn = document.createElement('button');
    btn.className = 'viz-toggle';
    btn.type = 'button';
    btn.textContent = 'Tabel';
    btn.setAttribute('aria-expanded', 'false');
    btn.onclick = () => {
      const on = tableNode.style.display !== 'none';
      tableNode.style.display = on ? 'none' : '';
      plotNode.style.display = on ? '' : 'none';
      if (legendNode) legendNode.style.display = on ? '' : 'none';
      btn.textContent = on ? 'Tabel' : 'Grafik';
      btn.setAttribute('aria-expanded', String(!on));
      hideTip();
    };
    head.appendChild(btn);
  }

  fig.appendChild(head);
  if (legendNode) fig.appendChild(legendNode);
  fig.appendChild(plotNode);
  if (tableNode) { tableNode.style.display = 'none'; fig.appendChild(tableNode); }
  return fig;
}

function dataTable(headers, rows) {
  const wrap = document.createElement('div');
  wrap.className = 'viz-table';
  const t = document.createElement('table');
  t.className = 'data';
  const thead = document.createElement('thead');
  const htr = document.createElement('tr');
  headers.forEach((h) => { const th = document.createElement('th'); th.textContent = h; htr.appendChild(th); });
  thead.appendChild(htr);
  const tb = document.createElement('tbody');
  rows.forEach((r) => {
    const tr = document.createElement('tr');
    r.forEach((c, i) => {
      const td = document.createElement('td');
      if (i > 0) td.className = 'num';
      td.textContent = c;
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
  t.append(thead, tb);
  wrap.appendChild(t);
  return wrap;
}

// ---------------------------------------------------------------------------
// Grafik 1 — Trade-off court x durasi (bentuk: emphasis scatter)
// ---------------------------------------------------------------------------
/**
 * Menjawab pertanyaan yang tidak terjawab oleh satu angka: apa yang kamu
 * korbankan saat menahan jumlah court demi margin.
 *
 * Sumbu X = menit main per peserta, sumbu Y = untung. Pilihan yang sedang
 * dipakai diberi warna aksen, sisanya abu-abu (bentuk emphasis, bukan
 * kategorikal — ceritanya satu titik, bukan identitas tiap titik).
 */
export function tradeoffChart(options, current, width = 640) {
  const W = Math.max(420, Math.round(width) || 640), H = 320;
  const M = { t: 14, r: 20, b: 42, l: 66 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;

  const pts = options.filter((o) => o.play_minutes_per_player > 0);
  if (!pts.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = 'Belum ada skenario untuk dibandingkan.';
    return p;
  }

  const xs = pts.map((o) => o.play_minutes_per_player);
  const ys = pts.map((o) => o.profit);
  const xTicks = niceTicks(Math.min(...xs), Math.max(...xs), 5);
  const yTicks = niceTicks(Math.min(0, ...ys), Math.max(0, ...ys), 5);
  const x0 = xTicks[0], x1 = xTicks[xTicks.length - 1];
  const y0 = yTicks[0], y1 = yTicks[yTicks.length - 1];
  const sx = (v) => M.l + ((v - x0) / (x1 - x0 || 1)) * iw;
  const sy = (v) => M.t + ih - ((v - y0) / (y1 - y0 || 1)) * ih;

  const svg = s('svg', {
    viewBox: `0 0 ${W} ${H}`, class: 'viz-svg', role: 'img',
    'aria-label': 'Perbandingan waktu main per peserta terhadap keuntungan '
      + 'untuk tiap kombinasi jumlah court dan durasi sewa',
  });

  // Gridline: hairline solid, satu langkah dari surface.
  yTicks.forEach((v) => {
    svg.appendChild(s('line', {
      x1: M.l, x2: M.l + iw, y1: sy(v), y2: sy(v),
      stroke: VIZ.grid, 'stroke-width': 1,
    }));
    svg.appendChild(txt('text', {
      x: M.l - 10, y: sy(v) + 4, fill: VIZ.muted,
      'font-size': 10.5, 'text-anchor': 'end', class: 'viz-tick',
    }, compactRp(v)));
  });

  // Garis impas: baseline bermakna, bukan sekadar grid.
  if (y0 < 0 && y1 > 0) {
    svg.appendChild(s('line', {
      x1: M.l, x2: M.l + iw, y1: sy(0), y2: sy(0),
      stroke: VIZ.axis, 'stroke-width': 1,
    }));
    svg.appendChild(txt('text', {
      x: M.l + iw, y: sy(0) - 6, fill: VIZ.muted,
      'font-size': 10, 'text-anchor': 'end',
    }, 'impas'));
  }

  xTicks.forEach((v) => {
    svg.appendChild(txt('text', {
      x: sx(v), y: M.t + ih + 18, fill: VIZ.muted,
      'font-size': 10.5, 'text-anchor': 'middle', class: 'viz-tick',
    }, Math.round(v)));
  });

  svg.appendChild(s('line', {
    x1: M.l, x2: M.l + iw, y1: M.t + ih, y2: M.t + ih,
    stroke: VIZ.axis, 'stroke-width': 1,
  }));
  svg.appendChild(txt('text', {
    x: M.l + iw / 2, y: H - 6, fill: VIZ.muted,
    'font-size': 10.5, 'text-anchor': 'middle',
  }, 'menit main per peserta'));
  svg.appendChild(txt('text', {
    x: 12, y: M.t + ih / 2, fill: VIZ.muted, 'font-size': 10.5,
    'text-anchor': 'middle', transform: `rotate(-90 12 ${M.t + ih / 2})`,
  }, 'untung'));

  const isCurrent = (o) => current
    && o.courts === current.courts
    && Math.abs(o.hours - current.hours) < 0.01;

  // Titik konteks dulu, titik utama terakhir supaya berada di atas.
  [...pts].sort((a, b) => isCurrent(a) - isCurrent(b)).forEach((o) => {
    const cur = isCurrent(o);
    const cx = sx(o.play_minutes_per_player), cy = sy(o.profit);
    const g = s('g', { class: 'viz-pt' });

    // Cincin surface 2px agar titik tetap terbaca saat bertumpuk.
    g.appendChild(s('circle', {
      cx, cy, r: cur ? 8 : 5.5,
      fill: cur ? VIZ.s1 : VIZ.neutral,
      stroke: VIZ.surface, 'stroke-width': SEG_GAP,
    }));
    // Area sentuh jauh lebih besar dari marka.
    const hit = s('circle', { cx, cy, r: HIT_MIN / 2, fill: 'transparent', class: 'viz-hit' });
    bindTip(hit, `${o.courts} court x ${o.hours} jam`, [
      { key: cur ? VIZ.s1 : VIZ.neutral, value: `${o.play_minutes_per_player} menit`, label: 'main / peserta' },
      { value: fullRp(o.profit), label: `untung (margin ${o.margin_pct}%)` },
      { value: `${o.byes_per_round} orang`, label: 'duduk tiap ronde' },
    ]);
    g.appendChild(hit);

    // Label langsung hanya untuk titik yang jadi pokok cerita. Diletakkan ke
    // kanan-atas karena sebaran skenario selalu menurun ke kanan, jadi sisi itu
    // yang paling lapang; kalau mepet tepi kanan, label dipantulkan ke kiri.
    if (cur) {
      const label = `${o.courts} court · ${o.hours} jam`;
      const near_right = cx > M.l + iw * 0.68;
      g.appendChild(txt('text', {
        x: cx + (near_right ? -12 : 12), y: cy - 9, fill: VIZ.ink,
        'font-size': 11.5, 'font-weight': 700,
        'text-anchor': near_right ? 'end' : 'start',
      }, label));
    }
    svg.appendChild(g);
  });

  const plot = document.createElement('div');
  plot.className = 'viz-plot';
  plot.appendChild(svg);

  const leg = legend([
    { color: VIZ.s1, label: 'Pilihan sekarang' },
    { color: VIZ.neutral, label: 'Alternatif' },
  ]);

  const table = dataTable(
    ['Skenario', 'Menit main', 'Duduk/ronde', 'Biaya', 'Untung', 'Margin'],
    [...pts]
      .sort((a, b) => b.play_minutes_per_player - a.play_minutes_per_player)
      .map((o) => [
        `${o.courts} court x ${o.hours} jam${isCurrent(o) ? ' (sekarang)' : ''}`,
        o.play_minutes_per_player, o.byes_per_round,
        fullRp(o.total_cost), fullRp(o.profit), o.margin_pct + '%',
      ])
  );

  return figure('Waktu main vs keuntungan per skenario', plot, leg, table);
}

// ---------------------------------------------------------------------------
// Grafik 2 — Keterlibatan tiap peserta (bentuk: stacked bar 100%)
// ---------------------------------------------------------------------------
/**
 * Total tiap batang selalu sama (jumlah ronde), jadi yang dibaca adalah
 * komposisinya: berapa ronde main, berapa bertugas, berapa benar-benar duduk.
 * Inilah cara tercepat melihat apakah pembagiannya adil untuk 26 orang -
 * tabel 26 baris menyembunyikan pencilan, batang menampakkannya.
 */
export function engagementChart(players, stats, totalRounds, width = 640) {
  const rows = players.map((p) => {
    const roles = stats.roles_per_player[p.id] || {};
    const play = stats.plays_per_player[p.id] || 0;
    const duty = roles.total || 0;
    // Istirahat dihitung dari daftar bye, bukan dari (totalRonde - main - tugas).
    // Peserta yang datang telat atau pulang duluan tidak sedang istirahat di
    // ronde yang belum ia ikuti, dan rumus lama menghitungnya begitu - batangnya
    // lalu penuh sampai ujung untuk orang yang belum sampai venue.
    const idle = Math.max(0, (stats.byes_per_player
      ? (stats.byes_per_player[p.id] || 0) : (totalRounds - play)) - duty);
    // Sisanya = ronde yang ia tidak hadiri. Sengaja tidak diberi warna seri
    // sendiri: batangnya berakhir lebih awal, dan ruang kosong sampai ujung
    // itulah yang membedakannya dari "istirahat".
    const absent = Math.max(0, totalRounds - play - duty - idle);
    return { name: p.name, play, duty, idle, absent };
  }).sort((a, b) => b.play - a.play || a.name.localeCompare(b.name));

  const barH = Math.min(BAR_MAX, Math.max(9, Math.floor(300 / rows.length)));
  const gap = Math.max(4, Math.round(barH * 0.45));
  const W = Math.max(360, Math.round(width) || 640);
  const labelW = Math.min(140, Math.max(78, W * 0.2)), valueW = 40;
  const iw = W - labelW - valueW - 12;
  const H = rows.length * (barH + gap) + 26;

  const svg = s('svg', {
    viewBox: `0 0 ${W} ${H}`, class: 'viz-svg', role: 'img',
    'aria-label': `Komposisi ${totalRounds} ronde untuk tiap peserta: `
      + 'jumlah ronde bermain, bertugas, dan istirahat',
  });

  const showDuty = rows.some((r) => r.duty > 0);

  rows.forEach((r, i) => {
    const y = i * (barH + gap) + 4;
    svg.appendChild(txt('text', {
      x: labelW - 10, y: y + barH * 0.5 + 4, fill: VIZ.muted,
      'font-size': 11, 'text-anchor': 'end', class: 'viz-name',
    }, r.name));

    const segs = [
      { v: r.play, c: VIZ.s1, label: 'ronde main' },
      { v: r.duty, c: VIZ.s2, label: 'ronde bertugas' },
      { v: r.idle, c: VIZ.neutral, label: 'ronde istirahat' },
    ].filter((sg) => sg.v > 0);

    let x = labelW;
    segs.forEach((sg, k) => {
      const raw = (sg.v / totalRounds) * iw;
      // Jarak 2px berwarna surface memisahkan segmen - bukan garis tepi.
      const w = Math.max(2, raw - (k < segs.length - 1 ? SEG_GAP : 0));
      const last = k === segs.length - 1;
      const rect = s('rect', {
        x, y, width: w, height: barH, fill: sg.c,
        rx: last ? 4 : 0,   // ujung data membulat 4px, pangkal tetap siku
      });
      svg.appendChild(rect);

      const hit = s('rect', {
        x, y: y - gap / 2, width: w, height: barH + gap,
        fill: 'transparent', class: 'viz-hit',
      });
      bindTip(hit, r.name, [
        { key: VIZ.s1, value: String(r.play), label: 'ronde main' },
        ...(showDuty ? [{ key: VIZ.s2, value: String(r.duty), label: 'ronde bertugas' }] : []),
        { key: VIZ.neutral, value: String(r.idle), label: 'ronde istirahat' },
        ...(r.absent ? [{ key: VIZ.dim, value: String(r.absent),
                          label: 'ronde tidak hadir' }] : []),
      ]);
      svg.appendChild(hit);
      x += raw;
    });

    // Nilai pokok diletakkan di luar batang: selalu muat, tidak pernah terpotong.
    svg.appendChild(txt('text', {
      x: labelW + iw + 10, y: y + barH * 0.5 + 4, fill: VIZ.ink,
      'font-size': 11, 'font-weight': 600, class: 'viz-val',
    }, String(r.play)));
  });

  svg.appendChild(txt('text', {
    x: labelW + iw + 10, y: H - 6, fill: VIZ.muted, 'font-size': 10,
  }, 'main'));

  const plot = document.createElement('div');
  plot.className = 'viz-plot scroll';
  plot.appendChild(svg);

  const showAbsent = rows.some((r) => r.absent > 0);
  const entries = [{ color: VIZ.s1, label: 'Main' }];
  if (showDuty) entries.push({ color: VIZ.s2, label: 'Bertugas' });
  entries.push({ color: VIZ.neutral, label: 'Istirahat' });
  // Ruang kosong sampai ujung batang. Warnanya surface, jadi kotak legendanya
  // memang terlihat kosong - dan itu persis yang harus dikenali mata.
  if (showAbsent) entries.push({ color: VIZ.surface, label: 'Tidak hadir',
                                empty: true });

  const table = dataTable(
    ['Nama', 'Main', 'Bertugas', 'Istirahat'].concat(
      showAbsent ? ['Tidak hadir'] : []),
    rows.map((r) => [r.name, r.play, r.duty, r.idle].concat(
      showAbsent ? [r.absent] : []))
  );

  return figure(`Komposisi ${totalRounds} ronde per peserta`, plot,
                legend(entries), table);
}

// ---------------------------------------------------------------------------
// Grafik 3 — Porsi istirahat lintas acara (bentuk: bar + garis acuan)
// ---------------------------------------------------------------------------
/**
 * Satu seri, satu warna. Yang memberi makna bukan warna melainkan garis acuan
 * rata-rata: siapa yang duduk jauh di atas porsi wajarnya selama ini.
 */
export function restShareChart(stats, width = 640) {
  const rows = stats.filter((r) => (r.rounds_played + r.rounds_rested) > 0)
    .sort((a, b) => b.rest_pct - a.rest_pct);
  if (!rows.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = 'Belum ada acara tersimpan.';
    return p;
  }

  const avg = rows.reduce((t, r) => t + r.rest_pct, 0) / rows.length;
  const maxV = Math.max(...rows.map((r) => r.rest_pct), avg) || 1;

  const barH = Math.min(BAR_MAX, Math.max(9, Math.floor(300 / rows.length)));
  const gap = Math.max(4, Math.round(barH * 0.45));
  const W = Math.max(360, Math.round(width) || 640);
  const labelW = Math.min(140, Math.max(78, W * 0.2)), valueW = 46;
  const iw = W - labelW - valueW - 12;
  const H = rows.length * (barH + gap) + 34;
  const sx = (v) => (v / maxV) * iw;

  const svg = s('svg', {
    viewBox: `0 0 ${W} ${H}`, class: 'viz-svg', role: 'img',
    'aria-label': 'Persentase ronde istirahat tiap peserta di seluruh acara '
      + 'tersimpan, dibandingkan rata-rata',
  });

  rows.forEach((r, i) => {
    const y = i * (barH + gap) + 18;
    svg.appendChild(txt('text', {
      x: labelW - 10, y: y + barH * 0.5 + 4, fill: VIZ.muted,
      'font-size': 11, 'text-anchor': 'end', class: 'viz-name',
    }, r.name));

    const w = Math.max(2, sx(r.rest_pct));
    svg.appendChild(s('rect', {
      x: labelW, y, width: w, height: barH, fill: VIZ.s1, rx: 4,
    }));

    const hit = s('rect', {
      x: labelW, y: y - gap / 2, width: Math.max(w, 8), height: barH + gap,
      fill: 'transparent', class: 'viz-hit',
    });
    bindTip(hit, r.name, [
      { key: VIZ.s1, value: r.rest_pct + '%', label: 'porsi istirahat' },
      { value: String(r.rounds_played), label: 'ronde main' },
      { value: String(r.events), label: 'acara diikuti' },
    ]);
    svg.appendChild(hit);

    svg.appendChild(txt('text', {
      x: labelW + iw + 10, y: y + barH * 0.5 + 4, fill: VIZ.ink,
      'font-size': 11, 'font-weight': 600, class: 'viz-val',
    }, r.rest_pct + '%'));
  });

  // Garis acuan rata-rata - inti dari grafik ini. Labelnya diletakkan di ATAS:
  // wadah plot bisa ter-scroll, dan label di dasar grafik terpotong sehingga
  // garisnya jadi garis vertikal tanpa keterangan apa pun.
  const ax = labelW + sx(avg);
  svg.appendChild(s('line', {
    x1: ax, x2: ax, y1: 14, y2: H - 8, stroke: VIZ.warn, 'stroke-width': 1,
  }));
  const avgLabel = `rata-rata ${avg.toFixed(0)}%`;
  const nearRight = ax > labelW + iw * 0.75;
  svg.appendChild(txt('text', {
    x: ax + (nearRight ? -5 : 5), y: 10, fill: VIZ.warn, 'font-size': 10,
    'text-anchor': nearRight ? 'end' : 'start',
  }, avgLabel));

  const plot = document.createElement('div');
  plot.className = 'viz-plot scroll';
  plot.appendChild(svg);

  const table = dataTable(
    ['Nama', 'Acara', 'Ronde main', 'Ronde duduk', '% duduk'],
    rows.map((r) => [r.name, r.events, r.rounds_played, r.rounds_rested,
                     r.rest_pct + '%'])
  );

  return figure('Porsi istirahat tiap peserta lintas acara', plot, null, table);
}

export { VIZ, hideTip };
