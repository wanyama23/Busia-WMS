// static/js/app.js
// Global app helpers and modal submit wired to csrfFetch used by templates

// Read CSRF token from meta tag
function getCsrfToken(){
  const m = document.querySelector('meta[name="csrf-token"]');
  return m ? m.getAttribute('content') : null;
}

// csrfFetch: automatically JSON.stringify body and add CSRF header for unsafe methods
async function csrfFetch(url, options = {}){
  options = Object.assign({}, options);
  options.headers = options.headers || {};

  // If body is a plain object (not FormData), stringify it and set JSON header
  if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData)) {
    options.body = JSON.stringify(options.body);
    options.headers['Content-Type'] = options.headers['Content-Type'] || 'application/json';
  }

  const method = (options.method || 'GET').toUpperCase();
  if (['POST','PUT','PATCH','DELETE'].includes(method)) {
    const token = getCsrfToken();
    if (token) options.headers['X-CSRFToken'] = token;
  }

  return fetch(url, options);
}

// Small DOM helpers
function escapeHtml(str){
  if (!str) return '';
  return String(str).replace(/[&<>"']/g, function (m) {
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[m];
  });
}

// Create card DOM element for an item (matches items.html structure)
function createCardHtml(item){
  const col = document.createElement('div');
  col.className = 'col-12 col-sm-6 col-md-4 item-card';
  col.dataset.sku = (item.sku || '').toLowerCase();
  col.dataset.name = (item.name || '').toLowerCase();
  col.dataset.category = (item.category || '').toLowerCase();

  const on_hand = item.on_hand || 0;
  const reorder = (item.reorder_level !== undefined && item.reorder_level !== null) ? item.reorder_level : null;
  const below = (reorder !== null && on_hand <= reorder);
  const pct = reorder && reorder > 0 ? Math.min(100, Math.round(on_hand / reorder * 100)) : 0;

  col.innerHTML = `
    <div class="card h-100 shadow-sm">
      <div class="card-body d-flex flex-column">
        <div class="d-flex justify-content-between align-items-start">
          <div class="me-2">
            <div class="fw-bold text-truncate" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
            <div class="text-muted small">${escapeHtml(item.sku)}${item.unit ? ' · ' + escapeHtml(item.unit) : ''}${item.category ? ' · ' + escapeHtml(item.category) : ''}</div>
          </div>
          ${below ? '<span class="badge badge-low small ms-2">Low</span>' : (reorder !== null ? '<span class="badge badge-ok small ms-2">OK</span>' : '')}
        </div>

        <div class="mt-3 mb-2 d-flex justify-content-between align-items-end mt-auto">
          <div>
            <div class="h5 mb-0">${on_hand} <span class="text-muted small">${escapeHtml(item.unit || '')}</span></div>
            ${reorder !== null ? '<div class="small text-secondary">Reorder: ' + (reorder || 0) + '</div>' : ''}
            ${reorder !== null ? `<div class="progress mt-1" style="height:8px;"><div class="progress-bar ${below ? 'bg-danger' : 'bg-success'}" role="progressbar" style="width:${pct}%" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"></div></div>` : ''}
          </div>

          <div class="text-end">
            <a class="btn btn-sm btn-outline-primary mb-1" href="/item/${encodeURIComponent(item.sku)}">Open</a>
            <div>
              <a class="btn btn-sm btn-outline-success me-1" href="/receive/${encodeURIComponent(item.sku)}">Receive</a>
              <a class="btn btn-sm btn-outline-warning" href="/issue/${encodeURIComponent(item.sku)}">Issue</a>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
  return col;
}

// Modal and filter behavior (runs on DOMContentLoaded)
document.addEventListener('DOMContentLoaded', function () {
  // Filter form elements
  const filterForm = document.getElementById('itemsFilterForm');
  const categorySelect = document.getElementById('categoryFilter');
  const clearBtn = document.getElementById('clearFilters');
  const searchInput = document.getElementById('itemSearch');
  const itemsGrid = document.getElementById('itemsList');

  if (categorySelect && filterForm) {
    categorySelect.addEventListener('change', () => filterForm.submit());
  }

  if (clearBtn && filterForm) {
    clearBtn.addEventListener('click', () => {
      const q = filterForm.querySelector('[name="q"]');
      const cat = filterForm.querySelector('[name="category"]');
      if (q) q.value = '';
      if (cat) cat.value = '';
      filterForm.submit();
    });
  }

  // Client-side instant search
  function filterCardsByQuery(q) {
    const query = (q || '').trim().toLowerCase();
    Array.from(document.querySelectorAll('.item-card')).forEach(c => {
      const sku = c.dataset.sku || '';
      const name = c.dataset.name || '';
      c.style.display = (query === '' || sku.includes(query) || name.includes(query)) ? '' : 'none';
    });
  }
  if (searchInput) {
    searchInput.addEventListener('input', (e) => filterCardsByQuery(e.target.value));
  }

  // Modal elements
  const itemModalEl = document.getElementById('itemModal');
  const itemForm = document.getElementById('itemForm');
  const unitSelect = document.getElementById('i_unit_select');
  const unitOther = document.getElementById('i_unit_other');
  const catSelect = document.getElementById('i_cat_select');
  const catOther = document.getElementById('i_cat_other');
  const alertBox = document.getElementById('itemFormAlert');
  const bsModal = itemModalEl ? bootstrap.Modal.getOrCreateInstance(itemModalEl) : null;

  function toggleOther(selectEl, otherEl) {
    if (!selectEl || !otherEl) return;
    otherEl.classList.toggle('d-none', selectEl.value !== '__other__');
    if (selectEl.value !== '__other__') otherEl.value = '';
  }

  if (unitSelect) unitSelect.addEventListener('change', () => toggleOther(unitSelect, unitOther));
  if (catSelect) catSelect.addEventListener('change', () => toggleOther(catSelect, catOther));

  if (itemModalEl) {
    itemModalEl.addEventListener('show.bs.modal', function () {
      if (alertBox) { alertBox.classList.add('d-none'); alertBox.textContent = ''; }
      if (itemForm) itemForm.reset();
      toggleOther(unitSelect, unitOther);
      toggleOther(catSelect, catOther);
    });
  }

  // Submit new item via csrfFetch
  if (itemForm) {
    itemForm.addEventListener('submit', function (e) {
      e.preventDefault();
      if (!alertBox) return;
      alertBox.classList.add('d-none');
      alertBox.textContent = '';

      const skuEl = itemForm.querySelector('[name="sku"]');
      const nameEl = itemForm.querySelector('[name="name"]');
      const reorderEl = itemForm.querySelector('[name="reorder_level"]');

      const sku = (skuEl && skuEl.value || '').trim();
      const name = (nameEl && nameEl.value || '').trim();
      const unit = (unitSelect && unitSelect.value === '__other__') ? (unitOther.value || '').trim() : (unitSelect ? unitSelect.value : 'pcs');
      const category = (catSelect && catSelect.value === '__other__') ? (catOther.value || '').trim() : (catSelect ? catSelect.value : '');
      const reorder_raw = reorderEl ? reorderEl.value : '';
      const reorder_level = reorder_raw === '' ? null : Number(reorder_raw);

      if (!sku || !name) {
        alertBox.textContent = 'SKU and Name are required.';
        alertBox.classList.remove('d-none');
        return;
      }
      if (!unit) {
        alertBox.textContent = 'Please select or enter a Unit.';
        alertBox.classList.remove('d-none');
        return;
      }
      if (!category) {
        alertBox.textContent = 'Please select or enter a Category.';
        alertBox.classList.remove('d-none');
        return;
      }

      const payload = { sku, name, unit, category, reorder_level };

      csrfFetch('/api/items', {
        method: 'POST',
        body: payload,
        headers: { 'Accept': 'application/json' }
      })
      .then(async res => {
        const text = await res.text();
        let data = null;
        try { data = JSON.parse(text); } catch (e) { data = null; }
        if (!res.ok) {
          const msg = data && data.message ? data.message : (text || 'Failed to create item');
          throw new Error(msg);
        }
        return data;
      })
      .then(result => {
        if (result && result.status === 'ok' && result.item) {
          const newCard = createCardHtml(result.item);
          if (itemsGrid) itemsGrid.insertBefore(newCard, itemsGrid.firstChild);
          if (bsModal) bsModal.hide();
        } else {
          throw new Error((result && result.message) || 'Unexpected response from server');
        }
      })
      .catch(err => {
        alertBox.textContent = err.message || 'Server error';
        alertBox.classList.remove('d-none');
      });
    });
  }
});


// static/js/app.js

function getCsrfToken(){
  const m = document.querySelector('meta[name="csrf-token"]');
  return m ? m.getAttribute('content') : null;
}

async function csrfFetch(url, options = {}){
  options = Object.assign({}, options);
  options.headers = options.headers || {};

  if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData)) {
    options.body = JSON.stringify(options.body);
    options.headers['Content-Type'] = options.headers['Content-Type'] || 'application/json';
  }

  const method = (options.method || 'GET').toUpperCase();
  if (['POST','PUT','PATCH','DELETE'].includes(method)) {
    const token = getCsrfToken();
    if (token) options.headers['X-CSRFToken'] = token;
  }

  return fetch(url, options);
}

function escapeHtml(str){
  if (!str) return '';
  return String(str).replace(/[&<>"']/g, function (m) {
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[m];
  });
}

async function refreshDashboardOnce() {
  try {
    const res = await csrfFetch('/api/dashboard', { method: 'GET', headers: { 'Accept': 'application/json' } });
    if (!res.ok) return;
    const data = await res.json();

    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    setText('dash_active_skus', data.active_skus);
    setText('dash_units_on_hand', data.units_on_hand);
    setText('dash_low_stock_alerts', data.low_stock_alerts);
    setText('dash_recent_count', (data.recent_movements || []).length);

    const ra = document.getElementById('dash_reorder_alerts');
    if (ra) {
      ra.innerHTML = '';
      if (data.reorder_alerts && data.reorder_alerts.length) {
        data.reorder_alerts.forEach(item => {
          const li = document.createElement('li');
          li.className = 'list-group-item d-flex justify-content-between align-items-start';
          li.innerHTML = `<div><strong>${escapeHtml(item.sku)}</strong> — ${escapeHtml(item.name)}<div class="small text-muted">${escapeHtml(item.category)} · ${escapeHtml(item.unit)}</div></div><div class="text-end"><div class="fw-bold">${item.on_hand}</div><div class="small text-danger">Reorder ${item.reorder_level}</div></div>`;
          ra.appendChild(li);
        });
      } else {
        ra.innerHTML = '<li class="list-group-item text-muted">No reorder alerts. All items above reorder levels.</li>';
      }
    }

    const rm = document.getElementById('dash_recent_movements');
    if (rm) {
      rm.innerHTML = '';
      if (data.recent_movements && data.recent_movements.length) {
        data.recent_movements.forEach(m => {
          const li = document.createElement('li');
          li.className = 'list-group-item';
          li.innerHTML = `<div class="d-flex justify-content-between"><div><strong>${escapeHtml(m.sku)}</strong> <span class="small text-muted">(${escapeHtml(m.type)})</span><div class="small text-muted">${escapeHtml(m.user)} · ${escapeHtml(m.ts)}</div></div><div class="fw-bold">${m.qty}</div></div>`;
          rm.appendChild(li);
        });
      } else {
        rm.innerHTML = '<li class="list-group-item text-muted">No recent movements.</li>';
      }
    }
  } catch (e) {
    console.error('refreshDashboardOnce error', e);
  }
}

document.addEventListener('DOMContentLoaded', function () {
  if (document.getElementById('dash_active_skus')) {
    refreshDashboardOnce();
    setInterval(refreshDashboardOnce, 8000);
  }

  // Optional SocketIO push (uncomment if using flask-socketio)
  // if (typeof io === 'function') {
  //   const socket = io();
  //   socket.on('dashboard_update', data => refreshDashboardOnce());
  // }
});


document.getElementById('confirm-receipt').addEventListener('click', function (e) {
  e.preventDefault();
  const receiptId = document.querySelector('#receipt-id').value;
  const payload = {
    receipt_id: receiptId,
    confirmed_by: window.currentUser || 'web'
  };

  fetch('/api/receive/confirm', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content
    },
    body: JSON.stringify(payload),
    credentials: 'same-origin'
  })
  .then(r => r.json())
  .then(j => {
    if (j.status === 'ok') {
      alert('Confirmed. GRN ' + j.grn_no + ' Order ' + j.order_ref);
      window.location.reload();
    } else {
      alert('Error: ' + (j.message || JSON.stringify(j)));
    }
  })
  .catch(err => alert('Network error: ' + err));
});

document.getElementById('confirm-receipt').addEventListener('click', function (e) {
  e.preventDefault();
  const receiptId = document.querySelector('#receipt-id').value;
  const token = document.querySelector('meta[name="csrf-token"]').content;
  const payload = { receipt_id: receiptId, confirmed_by: window.currentUser || 'web' };

  fetch('/api/receive/confirm', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': token
    },
    body: JSON.stringify(payload)
  })
  .then(r => r.json())
  .then(j => {
    if (j.status === 'ok') {
      alert('Confirmed. GRN ' + j.grn_no + ' Order ' + j.order_ref);
      window.location.reload();
    } else {
      alert('Error: ' + (j.message || JSON.stringify(j)));
    }
  })
  .catch(err => alert('Network error: ' + err));
});
