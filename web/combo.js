'use strict';

/**
 * Combobox dengan autocomplete + quick-add ke master.
 *
 * Alurnya mengikuti cara host bekerja: ketik nama venue/klub, kalau sudah ada
 * di master langsung muncul sebagai saran; kalau belum ada, baris terakhir
 * menawarkan menyimpannya ke master tanpa pindah menu. Field tambahan
 * (jumlah court, harga sewa) diisi di tempat, divalidasi, baru disimpan.
 *
 * Nama yang diketik pengguna adalah data tak tepercaya - seluruh penyisipan ke
 * DOM lewat textContent, tidak pernah innerHTML.
 */

const norm = (v) => (v || '').trim().toLowerCase();

/**
 * @param {object} cfg
 * @param {HTMLInputElement} cfg.input   input teks yang jadi kotak combobox
 * @param {HTMLInputElement} cfg.hidden  input tersembunyi penampung id terpilih
 * @param {() => Array} cfg.getItems     sumber data (dipanggil tiap buka)
 * @param {(item) => void} [cfg.onSelect]
 * @param {() => void} [cfg.onClear]
 * @param {object} [cfg.quickAdd]        {entity, fields, validate, save}
 * @param {string} [cfg.emptyText]
 */
export function createCombo(cfg) {
  const { input, hidden, getItems } = cfg;
  const wrap = document.createElement('div');
  wrap.className = 'combo';
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-expanded', 'false');

  const list = document.createElement('div');
  list.className = 'combo-list';
  list.setAttribute('role', 'listbox');
  wrap.appendChild(list);

  let open = false;
  let cursor = -1;
  let rows = [];          // [{type:'item'|'add', item?, node}]
  let addForm = null;

  const close = () => {
    open = false; cursor = -1;
    list.style.display = 'none';
    input.setAttribute('aria-expanded', 'false');
    if (addForm) { addForm.remove(); addForm = null; }
  };

  const setValue = (item) => {
    hidden.value = item ? item.id : '';
    input.value = item ? item.name : '';
    if (item && cfg.onSelect) cfg.onSelect(item);
    if (!item && cfg.onClear) cfg.onClear();
  };

  function matchExact(text) {
    return getItems().find((i) => norm(i.name) === norm(text)) || null;
  }

  function render() {
    list.textContent = '';
    rows = [];
    const q = norm(input.value);
    const items = getItems();
    const hits = q ? items.filter((i) => norm(i.name).includes(q)) : items.slice(0, 50);

    hits.slice(0, 50).forEach((item) => {
      const row = document.createElement('div');
      row.className = 'combo-row';
      row.setAttribute('role', 'option');
      const nm = document.createElement('span');
      nm.textContent = item.name;
      row.appendChild(nm);
      if (cfg.meta) {
        const m = document.createElement('span');
        m.className = 'combo-meta';
        m.textContent = cfg.meta(item);
        row.appendChild(m);
      }
      row.onmousedown = (e) => { e.preventDefault(); setValue(item); close(); };
      list.appendChild(row);
      rows.push({ type: 'item', item, node: row });
    });

    const typed = input.value.trim();
    const isNew = typed && !matchExact(typed);

    if (!hits.length && !isNew) {
      const empty = document.createElement('div');
      empty.className = 'combo-empty';
      empty.textContent = cfg.emptyText || 'Belum ada data.';
      list.appendChild(empty);
    }

    // Baris quick-add hanya muncul kalau yang diketik memang belum ada.
    if (isNew && cfg.quickAdd) {
      const row = document.createElement('div');
      row.className = 'combo-row combo-add';
      row.setAttribute('role', 'option');
      const plus = document.createElement('span');
      plus.className = 'combo-plus';
      plus.textContent = '+';
      const lab = document.createElement('span');
      lab.textContent = `Simpan "${typed}" ke master`;
      row.append(plus, lab);
      row.onmousedown = (e) => { e.preventDefault(); openAddForm(typed); };
      list.appendChild(row);
      rows.push({ type: 'add', node: row });
    }

    list.style.display = 'block';
    open = true;
    input.setAttribute('aria-expanded', 'true');
    highlight(0);
  }

  function highlight(i) {
    rows.forEach((r) => r.node.classList.remove('on'));
    if (!rows.length) { cursor = -1; return; }
    cursor = Math.max(0, Math.min(i, rows.length - 1));
    const row = rows[cursor];
    row.node.classList.add('on');
    row.node.scrollIntoView({ block: 'nearest' });
  }

  /** Formulir ringkas untuk menyimpan entri baru tanpa pindah menu. */
  function openAddForm(name) {
    if (addForm) addForm.remove();
    const qa = cfg.quickAdd;
    addForm = document.createElement('div');
    addForm.className = 'combo-form';

    const title = document.createElement('div');
    title.className = 'combo-form-title';
    title.textContent = `${qa.title || 'Data baru'}: ${name}`;
    addForm.appendChild(title);

    const grid = document.createElement('div');
    grid.className = 'combo-form-grid';
    const inputs = {};
    (qa.fields || []).forEach((f) => {
      const cell = document.createElement('div');
      const lab = document.createElement('label');
      lab.textContent = f.label;
      const inp = document.createElement('input');
      inp.type = f.type || 'text';
      if (f.step) inp.step = f.step;
      if (f.min !== undefined) inp.min = f.min;
      inp.value = f.value !== undefined ? f.value : '';
      inp.placeholder = f.placeholder || '';
      inputs[f.key] = inp;
      cell.append(lab, inp);
      grid.appendChild(cell);
    });
    addForm.appendChild(grid);

    const err = document.createElement('div');
    err.className = 'combo-err';
    addForm.appendChild(err);

    const bar = document.createElement('div');
    bar.className = 'combo-form-bar';
    const save = document.createElement('button');
    save.type = 'button'; save.className = 'btn sm'; save.textContent = 'Simpan';
    const cancel = document.createElement('button');
    cancel.type = 'button'; cancel.className = 'btn ghost sm'; cancel.textContent = 'Batal';
    cancel.onmousedown = (e) => { e.preventDefault(); close(); input.focus(); };
    bar.append(save, cancel);
    addForm.appendChild(bar);

    save.onmousedown = async (e) => {
      e.preventDefault();
      const values = {};
      Object.entries(inputs).forEach(([k, el]) => { values[k] = el.value; });

      // Validasi sebelum menyentuh server.
      let message = null;
      if (!name.trim()) message = 'Nama tidak boleh kosong.';
      else if (matchExact(name)) message = 'Nama itu sudah ada di master.';
      else if (qa.validate) message = qa.validate(name, values);

      if (message) { err.textContent = message; return; }

      err.textContent = '';
      save.disabled = true; save.textContent = 'Menyimpan...';
      try {
        const item = await qa.save(name.trim(), values);
        close();
        setValue(item);
      } catch (ex) {
        err.textContent = ex.message || 'Gagal menyimpan.';
        save.disabled = false; save.textContent = 'Simpan';
      }
    };

    list.textContent = '';
    list.appendChild(addForm);
    list.style.display = 'block';
    const first = Object.values(inputs)[0];
    if (first) first.focus();
  }

  // -- kejadian --------------------------------------------------------------
  input.addEventListener('focus', render);
  input.addEventListener('input', () => {
    hidden.value = '';        // ketikan bebas berarti belum ada yang terpilih
    render();
  });

  input.addEventListener('keydown', (e) => {
    if (!open && ['ArrowDown', 'Enter'].includes(e.key)) { render(); return; }
    if (!open) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); highlight(cursor + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); highlight(cursor - 1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const row = rows[cursor];
      if (!row) return;
      if (row.type === 'add') openAddForm(input.value.trim());
      else { setValue(row.item); close(); }
    } else if (e.key === 'Escape') { close(); }
  });

  input.addEventListener('blur', () => {
    // Beri jeda agar klik pada daftar sempat terproses.
    setTimeout(() => {
      if (addForm) return;    // formulir sedang terbuka, ditutup lewat klik luar
      close();
      const exact = matchExact(input.value);
      if (exact) setValue(exact);
      else if (!input.value.trim()) setValue(null);
      // Teks yang tidak cocok dibiarkan apa adanya: dipakai sebagai isian bebas,
      // dan barisan quick-add tetap tersedia saat kotak difokuskan lagi.
    }, 150);
  });

  // Formulir quick-add menahan blur (agar tombol Simpan sempat terklik), jadi
  // klik di luar combobox yang menutupnya - kalau tidak, daftar menggantung.
  document.addEventListener('pointerdown', (e) => {
    if (open && !wrap.contains(e.target)) close();
  });

  return {
    refresh: () => { if (open) render(); },
    setById(id) {
      const item = getItems().find((i) => String(i.id) === String(id));
      if (item) setValue(item);
    },
    clear: () => setValue(null),
    value: () => hidden.value,
  };
}
