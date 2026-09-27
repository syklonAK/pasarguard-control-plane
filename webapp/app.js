const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();
tg?.setHeaderColor?.('#07111f');
tg?.setBackgroundColor?.('#07111f');

const initData = tg?.initData || '', preview = !initData;
const H = { 'X-Telegram-Init-Data': initData, 'Content-Type': 'application/json' };
const $ = id => document.getElementById(id);
const q = s => document.querySelector(s);
const qa = s => [...document.querySelectorAll(s)];
const num = v => new Intl.NumberFormat('fa-IR').format(Number(v || 0));
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[c]));
const money = n => num(n) + ' ریال';
const size = n => { let v = Number(n || 0), i = 0, u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']; while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ } return new Intl.NumberFormat('fa-IR', { maximumFractionDigits: i > 2 ? 2 : 0 }).format(v) + ' ' + u[i] };
const date = v => v ? new Intl.DateTimeFormat('fa-IR', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(v)) : '—';
const notice = message => `<div class="notice">${esc(message)}</div>`;

let state = {
  caps: null, dashboard: null, mode: '', resellers: [], saleable: [], myOrders: [],
  panelId: '', panelName: '', nodeName: '', userOffset: 0, myPanelId: '', myOffset: 0,
  queue: 'pending', contextOrder: null,
};

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...H, ...options.headers } });
  let body = {};
  try { body = await response.json() } catch { }
  if (!response.ok) throw Error(body.detail || `خطای ارتباط (${response.status})`);
  return body;
}

function toast(message, bad = false) {
  const el = $('toast');
  el.textContent = message;
  el.style.borderColor = bad ? '#7d3045' : '#385273';
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 3000);
}

function loading(id, count = 2) { $(id).innerHTML = Array(count).fill('<div class="skeleton"></div>').join('') }

function card(title, subtitle, actions = '') {
  return `<article class="data-card"><div class="main"><h3>${esc(title)}</h3><p>${subtitle}</p></div><div class="card-actions">${actions}</div></article>`;
}

const ROLE_FA = { system_admin: 'مدیر کل سیستم', reseller_admin: 'مدیر نمایندگی', operator: 'اپراتور', finance: 'مدیر مالی', support: 'پشتیبان', viewer: 'مشاهده‌گر', customer: 'مشتری (فروشنده)' };
function roleFa(role) { return ROLE_FA[role] || role || 'کاربر' }

// --------------------------------------------------------------------------------------
// Orders: the customer places one, the seller prices it, payment turns it into an account
// --------------------------------------------------------------------------------------

const STATUS_FA = { pending: 'در انتظار بررسی', quoted: 'قیمت‌گذاری شد', confirmed: 'تأیید شد', payment_declared: 'پرداخت اعلام شد', approved: 'حساب ساخته شد', rejected: 'رد شد', cancelled: 'لغو شد' };
const STATE_STEPS = ['pending', 'quoted', 'confirmed', 'payment_declared', 'approved'];

function orderSteps(order) {
  if (['rejected', 'cancelled'].includes(order.status)) return '';
  const at = STATE_STEPS.indexOf(order.status);
  return `<div class="state-flow">${STATE_STEPS.map((name, i) => `<span class="state ${i <= at ? 'on' : ''}">${STATUS_FA[name]}</span>`).join('')}</div>`;
}

function orderMeta(order) {
  const quote = order.amount_irr ? ` · مبلغ: <b>${money(order.amount_irr)}</b> · هر گیگ: ${money(order.price_per_gib_irr)}` : '';
  const paid = order.status === 'quoted' && order.payment_instructions
    ? `<br><span class="quote-box">${esc(order.payment_instructions)}</span>` : '';
  const refused = order.rejection_reason ? `<br><span class="quote-box bad">${esc(order.rejection_reason)}</span>` : '';
  return `سرور ${esc(order.panel_name || '—')} · ${num(order.user_count)} کاربر · ${num(order.daily_gib)} گیگابایت روزانه${quote}${paid}${refused}<br><small>${date(order.created_at)}</small>`;
}

function orderActions(order) {
  const button = (action, label, cls = 'ghost') => `<button class="${cls}" data-order="${order.id}" data-do="${action}">${label}</button>`;
  if (order.status === 'quoted') return button('confirm', '✅ تأیید قیمت', 'btn primary') + button('cancel', 'لغو');
  if (order.status === 'confirmed') return button('payment', '💳 پرداخت را انجام دادم', 'btn primary') + button('cancel', 'لغو');
  if (order.status === 'pending') return button('cancel', 'لغو درخواست');
  if (order.status === 'approved') return `<button class="ghost" data-nav="mypanel">مشاهدهٔ پنل</button>`;
  return '';
}

function bindOrderButtons(id) {
  qa(`#${id} [data-order]`).forEach(btn => btn.onclick = async () => {
    btn.disabled = true;
    try {
      await api(`/v1/webapp/orders/${btn.dataset.order}/${btn.dataset.do}`, { method: 'POST' });
      toast({ confirm: 'قیمت را تأیید کردید؛ اطلاعات پرداخت را ببینید', payment: 'اعلام پرداخت ثبت شد و برای مدیر ارسال شد', cancel: 'درخواست لغو شد' }[btn.dataset.do] || 'عملیات انجام شد');
      await loadStore();
      await loadDashboard();
      // A seller can order too, so the review queue is refreshed only where it is readable.
      if (perm('decide_orders')) await Promise.all([loadQueue(), loadOrderBadge()]);
    } catch (error) { toast(error.message, true) } finally { btn.disabled = false }
  });
}

async function loadStore() {
  loading('my-orders', 2);
  try {
    if (preview) {
      state.saleable = [{ id: 'demo', name: 'سرور نمایشی پاسارگارد' }];
      state.myOrders = [{ id: 'demo', status: 'quoted', business_name: 'کسب‌وکار نمایشی', panel_name: 'سرور نمایشی', user_count: 50, daily_gib: 3, amount_irr: 5000000, price_per_gib_irr: 12000, payment_instructions: 'کارت نمایشی', created_at: new Date() }];
    } else {
      const [options, rows] = await Promise.all([api('/v1/webapp/order-options'), api('/v1/webapp/orders')]);
      state.saleable = options.panels || [];
      state.myOrders = rows;
    }
    const list = state.myOrders.map(o => card(`${o.business_name} — ${STATUS_FA[o.status] || o.status}`, orderMeta(o) + orderSteps(o), orderActions(o))).join('');
    $('my-orders').innerHTML = list || '<div class="notice"><span>🛒</span><div><b>سفارشی ثبت نکرده‌اید</b><p>با دکمهٔ «ثبت سفارش جدید» تعداد کاربر و مصرف روزانهٔ خود را اعلام کنید.</p></div></div>';
    bindOrderButtons('my-orders');
  } catch (error) { $('my-orders').innerHTML = notice(error.message) }
}

async function loadQueue() {
  qa('#order-tabs .chip').forEach(chip => chip.classList.toggle('active', chip.dataset.queue === state.queue));
  loading('order-list', 2);
  try {
    const rows = preview
      ? [{ id: 'demo', status: state.queue, business_name: 'نماینده شمال', panel_name: 'سرور نمایشی', user_count: 40, daily_gib: 5, note: 'توضیح مشتری', created_at: new Date(), amount_irr: state.queue === 'pending' ? null : 6000000, price_per_gib_irr: 15000 }]
      : await api(`/v1/webapp/orders/queue?status=${state.queue}`);
    const actions = order => order.status === 'payment_declared'
      ? `<button class="btn primary" data-review="approve" data-order="${order.id}">تأیید و ساخت حساب</button><button class="ghost danger" data-review="reject" data-order="${order.id}">رد</button>`
      : order.status === 'pending'
        ? `<button class="btn primary" data-review="quote" data-order="${order.id}">قیمت‌گذاری</button><button class="ghost danger" data-review="reject" data-order="${order.id}">رد</button>`
        : `<span class="badge">در انتظار مشتری</span><button class="ghost danger" data-review="reject" data-order="${order.id}">رد</button>`;
    $('order-list').innerHTML = rows.length
      ? rows.map(o => card(`${o.business_name} · ${num(o.user_count)} کاربر`, `${esc(o.panel_name || '')} — ${num(o.daily_gib)} گیگابایت روزانه${o.amount_irr ? ` · قیمت: <b>${money(o.amount_irr)}</b>` : ''}<br><i>${esc(o.note || 'بدون توضیح')}</i><br><small>${date(o.created_at)}</small>`, actions(o))).join('')
      : '<div class="notice">سفارشی در این وضعیت وجود ندارد.</div>';
    qa('#order-list [data-review]').forEach(btn => btn.onclick = async () => {
      const order = rows.find(o => o.id === btn.dataset.order);
      if (btn.dataset.review === 'quote') return openModal('quote', order);
      if (btn.dataset.review === 'reject') return openModal('rejectOrder', order);
      btn.disabled = true;
      try {
        const result = await api(`/v1/webapp/orders/${btn.dataset.order}/approve`, { method: 'POST' });
        toast(result.bound ? `حساب ساخته شد و روی ${result.panel_name} متصل است` : 'سازمان ساخته شد؛ برای اتصال پنل، Admin ID را ثبت کنید');
        await Promise.all([loadQueue(), loadDashboard()]);
      } catch (error) { toast(error.message, true) } finally { btn.disabled = false }
    });
  } catch (error) { $('order-list').innerHTML = notice(error.message) }
}

async function loadOrderBadge() {
  if (preview || !perm('decide_orders')) return;
  try {
    const rows = await api('/v1/webapp/orders/queue?status=pending');
    $('order-badge').textContent = rows.length;
    $('order-badge').classList.toggle('hidden', !rows.length);
  } catch { /* the badge is decoration; the queue page still reports the real error */ }
}

// --------------------------------------------------------------------------------------
// The customer area: the bound server and its subscribers
// --------------------------------------------------------------------------------------

async function loadMyPanel() {
  loading('my-panel-card', 1);
  try {
    const p = preview ? { panel_id: 'demo', name: 'سرور نمایشی پاسارگارد', status: 'active', admin_username: 'admin-demo', lifetime_bytes: 3221225472 }
      : await api('/v1/webapp/my-panel');
    state.myPanelId = p.panel_id;
    $('my-panel-note').classList.add('hidden');
    $('my-users').classList.remove('hidden');
    const meta = `حساب مدیر پنل: <code>${esc(p.admin_username || '—')}</code> · مصرف کل: ${size(p.lifetime_bytes)}`
      + (p.observed_at ? ` · آخرین همگام‌سازی: ${date(p.observed_at)}` : '');
    $('my-panel-card').innerHTML = card(p.name, meta,
      `<span class="badge ${p.status === 'active' ? '' : 'off'}">${p.status === 'active' ? 'فعال' : 'غیرفعال'}</span><button class="ghost" id="my-reload">مشترکان را دوباره بخوان</button>`);
    $('my-reload').onclick = loadMyUsers;
    await loadMyUsers();
  } catch (error) {
    state.myPanelId = '';
    $('my-panel-card').innerHTML = '';
    $('my-users').classList.add('hidden');
    $('my-panel-note').classList.remove('hidden');
    toast(error.message, true);
  }
}

const NODE_ON = n => n.enable === true || ['online', 'active', 'connected'].includes(String(n.status || ''));

function subscriberRow(prefix, panelId, u) {
  const on = u.status !== false;
  const base = `/v1/webapp/panels/${panelId}/users/${u.id}`;
  const controls = perm('subscriber_control')
    ? `<button class="ghost" data-user="${base}/${on ? 'disable' : 'enable'}">${on ? 'غیرفعال کردن' : 'فعال کردن'}</button><button class="ghost danger" data-user="${base}/reset-data">پاک کردن مصرف</button>` : '';
  return card(u.username || `کاربر ${u.id}`, `${size(u.used_traffic || u.lifetime_used_traffic || 0)} · شناسه ${esc(u.id)}`,
    `<span class="badge ${on ? '' : 'off'}">${on ? 'فعال' : 'غیرفعال'}</span>${controls}`);
}

async function fetchSubscribers(prefix, panelId, offset) {
  if (!panelId) return;
  loading(`${prefix}-list`, 2);
  try {
    const result = preview ? { users: [{ id: 11, username: 'subscriber-a', status: true, used_traffic: 858993459 }], total: 1 }
      : await api(`/v1/webapp/panels/${panelId}/users?offset=${offset}&limit=20`);
    const users = result.users || [];
    $(`${prefix}-count`).textContent = num(result.total ?? users.length);
    $(`${prefix}-list`).innerHTML = users.length ? users.map(u => subscriberRow(prefix, panelId, u)).join('') : '<div class="notice">مشترکی برای این پنل ثبت نشده است.</div>';
    qa(`#${prefix}-list [data-user]`).forEach(btn => btn.onclick = async () => {
      btn.disabled = true;
      try { await api(btn.dataset.user, { method: 'POST' }); toast('دستور به پنل ارسال شد'); await fetchSubscribers(prefix, panelId, offset) }
      catch (error) { toast(error.message, true) } finally { btn.disabled = false }
    });
  } catch (error) { $(`${prefix}-list`).innerHTML = notice(error.message) }
}

const loadMyUsers = () => { $('my-users-page').textContent = `صفحهٔ ${num(Math.floor(state.myOffset / 20) + 1)}`; return fetchSubscribers('my-user', state.myPanelId, state.myOffset) };

// --------------------------------------------------------------------------------------
// Staff surface
// --------------------------------------------------------------------------------------

function setPage(id) {
  qa('.page,.side-nav button,.bottom-nav button').forEach(el => el.classList.remove('active'));
  $(id).classList.add('active');
  qa(`[data-page="${id}"]`).forEach(el => el.classList.add('active'));
  const titles = { dashboard: 'داشبورد', mypanel: 'پنل من', store: 'سفارش پنل', orders: 'سفارش‌های پنل', network: state.caps?.is_system_admin ? 'مدیریت نمایندگان' : 'شبکه فروش', servers: 'سرورها و نودها', finance: 'امور مالی', account: 'حساب و پشتیبانی' };
  $('page-title').textContent = titles[id];
  if (id === 'network') loadResellers();
  if (id === 'servers') loadServers();
  if (id === 'finance') loadFinance();
  if (id === 'account') renderProfile();
  if (id === 'store') loadStore();
  if (id === 'orders') loadQueue();
  if (id === 'mypanel') loadMyPanel();
  tg?.HapticFeedback?.selectionChanged();
}

const perm = name => !!state.caps?.permissions?.[name];

function applyCapabilities() {
  const admin = !!state.caps?.is_system_admin;
  const area = state.caps?.area || 'staff';
  qa('.admin-only').forEach(el => el.classList.toggle('hidden', !admin));
  qa('[data-permission]').forEach(el => el.classList.toggle('hidden', !perm(el.dataset.permission)));
  // Two areas, one build: a buyer never renders a staff page and a seller never renders the store.
  qa('[data-area]').forEach(el => el.classList.toggle('hidden', el.dataset.area !== area));
  $('role-label').textContent = admin ? 'مدیر کل سیستم' : roleFa(state.caps?.role);
  $('network-title').textContent = admin ? 'مدیریت نمایندگان' : 'زیرمجموعه‌های من';
  $('pending-section').classList.toggle('hidden', !perm('decide_funding'));
  $('approvals-section').classList.toggle('hidden', !perm('decide_adjustment'));
  $('eyebrow').textContent = area === 'customer' ? 'بخش مشتری' : 'مرکز کنترل';
  const active = q('.page.active');
  if (active?.dataset.area && active.dataset.area !== area) setPage('dashboard');
}

async function boot() {
  if (preview) { demo(); return }
  try {
    const session = await api('/v1/webapp/session');
    if (session.needs_setup) { $('setup').classList.remove('hidden'); return }
    state.caps = await api('/v1/webapp/capabilities');
    applyCapabilities();
    await loadDashboard();
    await loadOrderBadge();
    $('status-pill').textContent = 'آنلاین';
    $('connection-label').textContent = 'اتصال امن برقرار است';
  } catch (error) {
    $('status-pill').textContent = 'خطا';
    $('status-pill').classList.add('error');
    toast(error.message, true);
  }
}

function demo() {
  const granted = ['view_dashboard', 'view_finance', 'view_audit', 'export_reports', 'create_support_ticket', 'answer_support', 'request_funding', 'decide_funding', 'request_adjustment', 'decide_adjustment', 'manage_servers', 'node_control', 'subscriber_control', 'admin_limit', 'change_billing_coefficient', 'manage_resellers', 'order_panel', 'decide_orders'];
  state.caps = { is_system_admin: true, area: 'staff', role: 'system_admin', permissions: Object.fromEntries(granted.map(name => [name, true])) };
  state.dashboard = { organization: { name: 'نمایندگی مرکزی' }, actor: { name: 'مدیر سیستم', role: 'system_admin' }, wallet: { available_irr: 185000000, balance_irr: 135000000, credit_limit_irr: 50000000 }, usage: { lifetime_bytes: 3092376453120 }, children_count: 12 };
  applyCapabilities();
  renderDashboard();
  $('status-pill').textContent = 'پیش‌نمایش';
}

async function loadDashboard() {
  state.dashboard = await api('/v1/webapp/dashboard');
  renderDashboard();
  if (state.caps.is_system_admin && !preview) {
    const overview = await api('/v1/webapp/admin/overview');
    renderAdminKpis(overview);
  }
}

function renderDashboard() {
  const d = state.dashboard, org = d.organization.name;
  $('org-name').textContent = org;
  $('profile-org').textContent = org;
  $('avatar').textContent = org.trim()[0] || 'P';
  $('profile-avatar').textContent = org.trim()[0] || 'P';
  $('welcome').textContent = `${d.actor.name || 'کاربر'} · ${roleFa(d.actor.role)}`;
  $('available').textContent = money(d.wallet.available_irr);
  $('balance').textContent = money(d.wallet.balance_irr);
  $('credit').textContent = money(d.wallet.credit_limit_irr);
  $('usage').textContent = size(d.usage.lifetime_bytes);
  $('usage-time').textContent = d.usage.observed_at ? date(d.usage.observed_at) : 'هنوز همگام نشده';
  $('children-count').textContent = num(d.children_count);
  $('network-badge').textContent = d.children_count;
}

function renderAdminKpis(o) {
  const el = $('admin-kpis');
  el.classList.remove('hidden');
  el.innerHTML = [['کل سازمان‌ها', o.organizations], ['سرور فعال', o.panels], ['کاربر متصل', o.actors], ['درخواست باز', o.pending_funding]]
    .map(([x, v]) => `<article><b>${num(v)}</b><span>${x}</span></article>`).join('');
  $('fund-badge').textContent = o.pending_funding;
  $('fund-badge').classList.toggle('hidden', !o.pending_funding);
}

async function loadResellers() {
  loading('reseller-list', 3);
  try {
    const rows = preview ? [{ name: 'نماینده شمال', slug: 'north', status: 'active', telegram_id: 123456789, balance_irr: 42000000, credit_limit_irr: 10000000, price_per_gib_irr: 18500 }]
      : await api(state.caps.is_system_admin ? '/v1/webapp/admin/resellers' : '/v1/webapp/children');
    state.resellers = rows;
    renderResellers(rows);
  } catch (error) { $('reseller-list').innerHTML = notice(error.message) }
}

function renderResellers(rows) {
  if (!rows.length) {
    $('reseller-list').innerHTML = '<div class="notice"><span>👥</span><div><b>هنوز نماینده‌ای ثبت نشده</b><p>اولین نماینده یا زیرمجموعه فروش خود را ایجاد کنید.</p></div></div>';
    return;
  }
  $('reseller-list').innerHTML = rows.map(x => {
    const total = Number(x.balance_irr || 0) + Number(x.credit_limit_irr || 0);
    const meta = state.caps.is_system_admin
      ? `${esc(x.slug || '')} · تلگرام: ${esc(x.telegram_id || 'متصل نشده')} · قیمت گیگ: ${money(x.price_per_gib_irr || 0)}`
      : `اعتبار قابل استفاده: ${money(total)}`;
    return card(x.name, meta, `<span class="badge ${x.status === 'active' ? '' : 'off'}">${x.status === 'active' ? 'فعال' : 'غیرفعال'}</span><b>${money(total)}</b>`);
  }).join('');
}

async function loadServers() {
  loading('server-list', 2);
  try {
    const rows = preview ? [{ id: 'demo', name: 'سرور اصلی پاسارگارد', base_url: 'https://panel.example.com', status: 'active', saleable: true }]
      : await api('/v1/webapp/panels');
    const actions = x => [
      `<span class="badge ${x.status === 'active' ? '' : 'off'}">${x.status === 'active' ? 'متصل' : 'قطع'}</span>`,
      perm('manage_servers') ? `<span class="badge ${x.saleable ? 'on' : 'off'}">${x.saleable ? '🛒 آماده فروش' : '⛔ خارج از فروش'}</span>` : '',
      perm('manage_servers') ? `<button class="ghost" data-sale="${x.id}" data-saleable="${x.saleable ? 'false' : 'true'}">${x.saleable ? 'خروج از فروش' : 'آمادهٔ فروش'}</button>` : '',
      perm('manage_servers') ? `<button class="ghost" data-panel="${x.id}" data-name="${esc(x.name)}">مشاهده نودها</button>` : '',
      perm('admin_limit') ? `<button class="ghost" data-tool="limit" data-id="${x.id}" data-name="${esc(x.name)}">سقف پنل</button>` : '',
      perm('change_billing_coefficient') ? `<button class="ghost" data-tool="coefficient" data-id="${x.id}" data-name="${esc(x.name)}">ضریب صورتحساب</button>` : '',
    ].join('');
    $('server-list').innerHTML = rows.length ? rows.map(x => card(x.name, esc(x.base_url), actions(x))).join('')
      : '<div class="notice"><span>🖥</span><div><b>سروری ثبت نشده</b><p>برای اتصال به PasarGuard اولین سرور را ثبت کنید.</p></div></div>';
    const select = btn => { state.panelId = btn.dataset.panel || btn.dataset.id; state.panelName = btn.dataset.name };
    qa('[data-sale]').forEach(btn => btn.onclick = async () => {
      btn.disabled = true;
      try { await api(`/v1/webapp/panels/${btn.dataset.sale}/saleable?saleable=${btn.dataset.saleable}`, { method: 'POST' }); toast('لیست فروش به‌روز شد'); await loadServers() }
      catch (error) { toast(error.message, true) } finally { btn.disabled = false }
    });
    qa('[data-panel]').forEach(btn => btn.onclick = () => {
      select(btn);
      state.userOffset = 0;
      $('user-panel').classList.remove('hidden');
      $('users-title').textContent = `مشترکان ${btn.dataset.name}`;
      loadNodes(btn.dataset.panel, btn.dataset.name);
      loadUsers();
    });
    qa('[data-tool]').forEach(btn => btn.onclick = () => { select(btn); openModal(btn.dataset.tool) });
  } catch (error) { $('server-list').innerHTML = notice(error.message) }
}

const NODE_RESULT = { reconnect: 'دستور اتصال مجدد ارسال شد', enable: 'نود روشن شد', disable: 'نود خاموش شد', reset: 'دستور بازنشانی نود ارسال شد' };

function nodeRow(panelId, n) {
  const on = NODE_ON(n), base = `/v1/webapp/panels/${panelId}/nodes/${n.id}`;
  const controls = perm('node_control')
    ? `<button class="ghost" data-node="${base}/reconnect">اتصال مجدد</button><button class="ghost" data-node="${base}/${on ? 'disable' : 'enable'}">${on ? 'خاموش کردن' : 'روشن کردن'}</button><button class="ghost danger" data-node="${base}/reset">بازنشانی</button>` : '';
  return card(n.name || `Node ${n.id}`, esc(n.address || n.message || n.status || 'نود پاسارگارد'),
    `<span class="badge ${on ? '' : 'off'}">${on ? 'فعال' : 'خاموش'}</span>${controls}<button class="ghost" data-node="${base}/status">وضعیت</button>`);
}

async function runNodeOp(panelId, nodeId, kind, button) {
  button.disabled = true;
  try {
    if (kind === 'status') {
      const result = await api(`/v1/webapp/panels/${panelId}/nodes/${nodeId}/status`);
      toast(`وضعیت: ${String(result && result.message || JSON.stringify(result)).slice(0, 120)}`);
    } else {
      await api(`/v1/webapp/panels/${panelId}/nodes/${nodeId}/${kind}`, { method: 'POST' });
      toast(NODE_RESULT[kind]);
      await loadNodes(panelId, state.nodeName);
    }
  } catch (error) { toast(error.message, true) } finally { button.disabled = false }
}

async function loadNodes(panelId, name) {
  state.nodeName = name;
  $('node-panel').classList.remove('hidden');
  $('node-title').textContent = `نودهای ${name}`;
  loading('node-list', 2);
  try {
    const result = preview ? { nodes: [{ id: 1, name: 'Germany-01', status: 'connected' }, { id: 2, name: 'Finland-02', enable: false }] }
      : await api(`/v1/webapp/panels/${panelId}/nodes`);
    const nodes = result.nodes || [];
    $('node-list').innerHTML = nodes.length ? nodes.map(n => nodeRow(panelId, n)).join('') : '<div class="notice">نودی از پنل دریافت نشد.</div>';
    qa('[data-node]').forEach(btn => btn.onclick = () => runNodeOp(panelId, btn.dataset.node.split('/').at(-2), btn.dataset.node.split('/').pop(), btn));
  } catch (error) { $('node-list').innerHTML = notice(error.message) }
}

const loadUsers = () => fetchSubscribers('user', state.panelId, state.userOffset);

async function loadFinance() {
  loading('transaction-list', 2);
  try {
    const txs = preview ? [{ kind: 'usage', created_at: new Date(), side: 'debit', amount_irr: 125000 }] : await api('/v1/webapp/transactions');
    $('transaction-list').innerHTML = txs.length ? txs.map(x => card(kindFa(x.kind), date(x.created_at), `<b class="amount ${x.side}">${x.side === 'credit' ? '+' : '−'} ${money(x.amount_irr)}</b>`)).join('') : '<div class="notice">تراکنشی ثبت نشده است.</div>';
    if (perm('decide_funding')) await loadFunding();
    if (perm('decide_adjustment')) await loadApprovals();
  } catch (error) { $('transaction-list').innerHTML = notice(error.message) }
}

function kindFa(k) {
  return ({ usage: 'تسویه مصرف', fund: 'شارژ کیف پول', adjustment: 'اصلاح حساب', 'order-funded': 'شارژ سفارش پنل' }[k]) || k;
}

const FUNDING_PATH = () => state.caps.is_system_admin ? '/v1/webapp/admin/funding-requests' : '/v1/webapp/funding-requests';

async function loadFunding() {
  const rows = preview ? [{ id: '1', organization: 'نماینده شمال', telegram_id: 123, amount_irr: 10000000, created_at: new Date() }] : await api(FUNDING_PATH());
  $('pending-count').textContent = `${num(rows.length)} درخواست`;
  $('funding-list').innerHTML = rows.length ? rows.map(x => card(x.organization, `${date(x.created_at)} · ${esc(x.telegram_id)}`,
    `<b>${money(x.amount_irr)}</b><button class="ghost" data-fund="${x.id}" data-decision="approve">تأیید</button><button class="ghost danger" data-fund="${x.id}" data-decision="reject">رد</button>`)).join('')
    : '<div class="notice">درخواست بازی وجود ندارد.</div>';
  qa('[data-fund]').forEach(btn => btn.onclick = () => decideFunding(btn));
}

async function decideFunding(btn) {
  btn.disabled = true;
  try {
    await api(`${FUNDING_PATH()}/${btn.dataset.fund}/${btn.dataset.decision}`, { method: 'POST' });
    toast(btn.dataset.decision === 'approve' ? 'کیف پول شارژ شد' : 'درخواست رد شد');
    await Promise.all([loadFunding(), loadDashboard()]);
  } catch (error) { toast(error.message, true) } finally { btn.disabled = false }
}

const APPROVAL_ACTION = { ['wallet.adjust']: 'اصلاح کیف پول' };

async function loadApprovals() {
  const rows = preview ? [{ id: 'a1', action: 'wallet.adjust', target_id: 'org', payload: { amount_irr: -500000, note: 'تجاوز ثبت‌شده در صورتحساب' }, created_at: new Date() }] : await api('/v1/webapp/admin/approvals');
  $('approval-count').textContent = `${num(rows.length)} در انتظار`;
  $('approval-list').innerHTML = rows.length ? rows.map(a => card(`${APPROVAL_ACTION[a.action] || a.action} · ${esc(a.target_id)}`,
    `${date(a.created_at)} · ${esc(String(a.payload?.note || '').slice(0, 120))}`,
    `<b class="amount ${Number(a.payload?.amount_irr) > 0 ? 'credit' : 'debit'}">${money(a.payload?.amount_irr)}</b><button class="ghost" data-approval="${a.id}" data-decision="approve">تأیید</button><button class="ghost danger" data-approval="${a.id}" data-decision="reject">رد</button>`)).join('')
    : '<div class="notice">درخواست تأییدی برای بررسی نمانده است.</div>';
  qa('[data-approval]').forEach(btn => btn.onclick = async () => {
    btn.disabled = true;
    try {
      await api(`/v1/webapp/admin/approvals/${btn.dataset.approval}/${btn.dataset.decision}`, { method: 'POST' });
      toast('تصمیم ثبت شد');
      await Promise.all([loadApprovals(), loadDashboard()]);
    } catch (error) { toast(error.message, true) } finally { btn.disabled = false }
  });
}

async function loadAudit() {
  loading('audit-list', 2);
  try {
    const rows = preview ? [{ action: 'panel.create', organization: 'نمایندگی مرکزی', actor: 'مدیر سیستم', created_at: new Date() }] : await api('/v1/webapp/audit?limit=50');
    $('audit-panel').classList.remove('hidden');
    $('audit-list').innerHTML = rows.length ? rows.map(x => card(`${esc(x.action)} · ${esc(x.organization || '—')}`,
      `${date(x.created_at)} · ${esc(x.actor || 'سیستم')} · ${esc(x.target_type)} ${esc(x.target_id || '')}`)).join('') : '<div class="notice">گزارشی ثبت نشده است.</div>';
  } catch (error) { $('audit-list').innerHTML = notice(error.message) }
}

function renderProfile() {
  if (!state.dashboard) return;
  const d = state.dashboard;
  $('profile-org').textContent = d.organization.name;
  $('profile-role').textContent = state.caps.is_system_admin ? 'مدیر کل سیستم' : roleFa(state.caps.role);
  $('profile-id').textContent = state.caps.area === 'customer' ? 'حساب مشتری' : 'دسترسی سازمانی';
}

function organizationOptions() {
  const rows = [{ id: state.caps.organization_id, name: 'کیف پول خودتان' }].concat(state.resellers.map(x => ({ id: x.id, name: x.name })));
  return `<label>سازمان هدف<select name="organization_id" required>${rows.map(x => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('')}</select></label>`;
}

function saleableOptions() {
  if (!state.saleable.length) return '<p class="error">هنوز سروری برای فروش باز نشده است؛ از مدیر مجموعه بخواهید سروری را «آمادهٔ فروش» کند.</p>';
  return `<label>سرور<select name="panel_id" required>${state.saleable.map(x => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('')}</select></label>`;
}

function openModal(type, order) {
  state.mode = type;
  state.contextOrder = order || null;
  $('modal').classList.remove('hidden');
  $('modal-error').textContent = '';
  let title = '', kicker = '', fields = '';
  if (type === 'fund') {
    title = 'درخواست افزایش اعتبار'; kicker = 'کیف پول';
    fields = '<label>مبلغ درخواستی به ریال<input name="amount_irr" type="number" min="1" required placeholder="10000000"></label>';
  } else if (type === 'adjust') {
    title = 'درخواست اصلاح حساب'; kicker = 'حسابداری';
    fields = organizationOptions() + '<label>مبلغ به ریال (منفی برای بستانکاری)<input name="amount_irr" type="number" required placeholder="-500000"><small>مبلغ منفی فقط تا سقف اعتبار توافق‌شده پذیرفته می‌شود.</small></label><label>توضیح عملیات<textarea name="note" minlength="10" maxlength="2000" required rows="3" placeholder="دلیل اصلاح، شماره فاکتور یا مرجع حسابداری"></textarea></label>';
  } else if (type === 'limit') {
    title = 'سقف مصرف پنل'; kicker = 'زیرساخت';
    fields = `<label>سقف به بایت<input name="limit_use_in_bytes" type="number" min="0" required placeholder="1099511627776"><small>برای سرور ${esc(state.panelName || 'انتخاب‌شده')} و Admin صورتحساب آن اعمال می‌شود.</small></label>`;
  } else if (type === 'coefficient') {
    title = 'ضریب صورتحساب'; kicker = 'قیمت‌گذاری';
    fields = '<label>ضریب مصرف<input name="usage_coefficient" type="number" step="0.01" min="0.01" max="1000" required value="1"><small>مصرف ثبت‌شده پنل در این ضریب ضرب می‌شود؛ فقط مدیر کل سیستم می‌تواند آن را تغییر دهد.</small></label>';
  } else if (type === 'reseller') {
    title = state.caps.is_system_admin ? 'ساخت نماینده جدید' : 'ساخت زیرنماینده'; kicker = 'شبکه فروش';
    fields = '<label>نام کسب‌وکار<input name="name" required></label><label>شناسه انگلیسی<input name="slug" required pattern="[a-z0-9][a-z0-9-]{1,78}[a-z0-9]"></label>' + (state.caps.is_system_admin ? '<label>شناسه عددی تلگرام<input name="telegram_id" type="number" min="1" required><small>کاربر می‌تواند با دستور /id شناسه خود را دریافت کند.</small></label>' : '') + '<label>قیمت هر گیگابایت (ریال)<input name="price_per_gib_irr" type="number" min="1" required></label><label>سقف اعتبار اولیه<input name="credit_limit_irr" type="number" min="0" value="0"></label>';
  } else if (type === 'order') {
    title = 'سفارش پنل تازه'; kicker = 'بخش مشتری';
    fields = saleableOptions() + '<label>نام کسب‌وکار<input name="business_name" required maxlength="160" placeholder="مثلاً فروش شمال"></label><label>تعداد کاربران<input name="user_count" type="number" min="1" required placeholder="50"></label><label>مصرف روزانه (گیگابایت)<input name="daily_gib" type="number" min="1" required placeholder="3"></label><label>توضیح برای مدیر<textarea name="note" maxlength="500" rows="3" placeholder="تعداد سرور، موقعیت مکانی یا هر نیاز ویژه"></textarea></label>';
  } else if (type === 'quote') {
    title = `قیمت‌گذاری سفارش ${state.contextOrder?.business_name || ''}`; kicker = 'سفارش‌های پنل';
    fields = `<p class="quote-box">${num(state.contextOrder?.user_count)} کاربر · ${num(state.contextOrder?.daily_gib)} گیگابایت روزانه روی ${esc(state.contextOrder?.panel_name || '')}</p>`
      + '<label>مبلغ کل به ریال<input name="amount_irr" type="number" min="1" required placeholder="50000000"><small>همین مبلغ پس از تأیید پرداخت به کیف پول مشتری واریز می‌شود.</small></label>'
      + '<label>سقف اعتبار (ریال)<input name="credit_limit_irr" type="number" min="0" value="0"></label>'
      + '<label>قیمت هر گیگابایت (ریال)<input name="price_per_gib_irr" type="number" min="1" required placeholder="12000"></label>'
      + '<label>اطلاعات پرداخت<textarea name="payment_instructions" minlength="3" maxlength="2000" required rows="3" placeholder="شماره کارت، نام گیرنده و توضیح واریز"></textarea></label>'
      + '<label>Admin ID روی سرور مادر (اختیاری)<input name="pg_admin_id" type="number" min="1"><small>پس از ساخت دستی Admin در PasarGuard وارد کنید تا صورتحساب و مشترکان به این حساب متصل شود.</small></label>';
  } else if (type === 'rejectOrder') {
    title = 'رد سفارش'; kicker = 'سفارش‌های پنل';
    fields = `<p class="quote-box">${esc(state.contextOrder?.business_name || '')}</p><label>دلیل رد (برای مشتری نمایش داده می‌شود)<textarea name="reason" maxlength="500" rows="3" required placeholder="مثلاً ظرفیت سرور با مصرف اعلام‌شده نمی‌خواند"></textarea></label>`;
  } else {
    title = 'ثبت سرور پاسارگارد'; kicker = 'زیرساخت';
    fields = '<label>نام سرور<input name="name" required></label><label>آدرس HTTPS پنل<input name="base_url" type="url" required placeholder="https://panel.example.com"></label><label>API Key<input name="api_key" type="password" minlength="8" required></label><label>نام کاربری مالک (اختیاری)<input name="owner_username"></label><label>رمز مالک (اختیاری)<input name="owner_password" type="password"></label><label>Admin ID برای صورتحساب (اختیاری)<input name="pg_admin_id" type="number" min="1"></label><label>نام Admin (اختیاری)<input name="admin_username"></label>' + (state.caps.is_system_admin ? '<label class="check"><input type="checkbox" id="verify-tls" name="verify_tls" checked> اعتبارسنجی گواهی TLS</label><p id="tls-warning" class="error hidden">با خاموش‌کردن اعتبارسنجی، ارتباط با این سرور در برابر شنود و جعل محافظت نمی‌شود؛ آن را فقط برای گواهی self-signed آزمایشگاهی بردارید.</p>' : '');
  }
  $('modal-title').textContent = title;
  $('modal-kicker').textContent = kicker;
  $('modal-fields').innerHTML = fields;
  const tls = $('verify-tls');
  if (tls) tls.onchange = () => $('tls-warning').classList.toggle('hidden', tls.checked);
}

$('modal-form').onsubmit = async event => {
  event.preventDefault();
  const button = $('modal-submit'), data = Object.fromEntries(new FormData(event.currentTarget)), mode = state.mode;
  if (mode === 'order' && !data.panel_id) { $('modal-error').textContent = 'سروری برای انتخاب وجود ندارد'; button.disabled = false; return }
  button.disabled = true;
  try {
    if (mode === 'fund') await api('/v1/webapp/funding-requests', { method: 'POST', body: JSON.stringify({ amount_irr: +data.amount_irr }) });
    else if (mode === 'adjust') await api('/v1/webapp/admin/adjustments', { method: 'POST', body: JSON.stringify({ organization_id: data.organization_id, amount_irr: +data.amount_irr, note: data.note }) });
    else if (mode === 'limit') await api(`/v1/webapp/panels/${state.panelId}/limit`, { method: 'POST', body: JSON.stringify({ limit_use_in_bytes: +data.limit_use_in_bytes }) });
    else if (mode === 'coefficient') await api(`/v1/webapp/panels/${state.panelId}/coefficient`, { method: 'POST', body: JSON.stringify({ usage_coefficient: data.usage_coefficient }) });
    else if (mode === 'reseller') {
      await api(state.caps.is_system_admin ? '/v1/webapp/admin/resellers' : '/v1/webapp/children', {
        method: 'POST', body: JSON.stringify({ ...data, telegram_id: data.telegram_id ? +data.telegram_id : undefined, price_per_gib_irr: +data.price_per_gib_irr, credit_limit_irr: +data.credit_limit_irr }),
      });
    } else if (mode === 'order') {
      await api('/v1/webapp/orders', { method: 'POST', body: JSON.stringify({ ...data, user_count: +data.user_count, daily_gib: +data.daily_gib, note: data.note || '' }) });
    } else if (mode === 'quote') {
      await api(`/v1/webapp/orders/${state.contextOrder.id}/quote`, {
        method: 'POST', body: JSON.stringify({ amount_irr: +data.amount_irr, credit_limit_irr: +data.credit_limit_irr, price_per_gib_irr: +data.price_per_gib_irr, payment_instructions: data.payment_instructions, pg_admin_id: data.pg_admin_id ? +data.pg_admin_id : null }),
      });
    } else if (mode === 'rejectOrder') {
      await api(`/v1/webapp/orders/${state.contextOrder.id}/reject`, { method: 'POST', body: JSON.stringify({ reason: data.reason }) });
    } else await api('/v1/webapp/panels', { method: 'POST', body: JSON.stringify({ ...data, pg_admin_id: data.pg_admin_id ? +data.pg_admin_id : null, verify_tls: 'verify_tls' in data }) });
    $('modal').classList.add('hidden');
    event.currentTarget.reset();
    state.contextOrder = null;
    toast({ adjust: 'درخواست اصلاح ثبت و در انتظار تأیید است', order: 'سفارش شما ثبت و برای مدیر ارسال شد', quote: 'قیمت برای مشتری ارسال شد', rejectOrder: 'سفارش رد شد' }[mode] || 'عملیات با موفقیت انجام شد');
    await loadDashboard();
    if (mode === 'reseller') await loadResellers();
    if (mode === 'server') await loadServers();
    if (mode === 'fund' || mode === 'adjust') await loadFinance();
    if (['order', 'quote', 'rejectOrder'].includes(mode)) await loadStore();
    if (mode === 'order' && state.caps.area === 'customer') setPage('store');
    if (perm('decide_orders')) { await loadQueue(); await loadOrderBadge() }
  } catch (error) {
    $('modal-error').textContent = error.message;
    tg?.HapticFeedback?.notificationOccurred('error');
  } finally { button.disabled = false }
};

$('setup-form').onsubmit = async event => {
  event.preventDefault();
  try {
    await api('/v1/onboarding/bootstrap', { method: 'POST', body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))) });
    $('setup').classList.add('hidden');
    toast('فضای کاری ساخته شد');
    await boot();
  } catch (error) { $('setup-error').textContent = error.message }
};

$('cancel').onclick = () => { $('modal').classList.add('hidden'); state.contextOrder = null };
$('close-nodes').onclick = () => $('node-panel').classList.add('hidden');
$('close-users').onclick = () => $('user-panel').classList.add('hidden');
$('users-prev').onclick = () => { if (state.userOffset > 0) { state.userOffset = Math.max(0, state.userOffset - 20); loadUsers() } };
$('users-next').onclick = () => { state.userOffset += 20; loadUsers() };
$('my-users-prev').onclick = () => { if (state.myOffset > 0) { state.myOffset = Math.max(0, state.myOffset - 20); loadMyUsers() } };
$('my-users-next').onclick = () => { state.myOffset += 20; loadMyUsers() };
$('reload-orders').onclick = loadQueue;
qa('#order-tabs .chip').forEach(chip => chip.onclick = () => { state.queue = chip.dataset.queue; loadQueue() });
$('refresh').onclick = async () => {
  try {
    await loadDashboard();
    const active = q('.page.active')?.id;
    if (active === 'network') await loadResellers();
    if (active === 'servers') await loadServers();
    if (active === 'finance') await loadFinance();
    if (active === 'store') await loadStore();
    if (active === 'orders') await loadQueue();
    if (active === 'mypanel') await loadMyPanel();
    toast('اطلاعات به‌روز شد');
  } catch (error) { toast(error.message, true) }
};
$('reload-finance').onclick = loadFinance;
$('load-audit').onclick = loadAudit;
$('close-audit').onclick = () => $('audit-panel').classList.add('hidden');
// Navigation is delegated so buttons rendered inside lists work without rebinding.
document.addEventListener('click', event => {
  const target = event.target.closest('[data-page],[data-nav]');
  if (target) setPage(target.dataset.page || target.dataset.nav);
});
qa('[data-action]').forEach(btn => btn.onclick = () => openModal(btn.dataset.action));
$('reseller-search').oninput = event => {
  const term = event.target.value.trim().toLowerCase();
  renderResellers(state.resellers.filter(x => [x.name, x.slug, x.telegram_id].some(v => String(v || '').toLowerCase().includes(term))));
};

boot();

function initMotion() {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.addEventListener('pointerdown', event => {
    if (reduce) return;
    const button = event.target.closest('button');
    if (!button) return;
    const rect = button.getBoundingClientRect(), ripple = document.createElement('span');
    ripple.className = 'ripple';
    ripple.style.left = `${event.clientX - rect.left}px`;
    ripple.style.top = `${event.clientY - rect.top}px`;
    button.appendChild(ripple);
    setTimeout(() => ripple.remove(), 700);
  });
  if (reduce || !window.matchMedia('(hover:hover) and (pointer:fine)').matches) return;
  qa('.hero-card,.quick-grid button').forEach(card => {
    card.classList.add('tilt');
    card.addEventListener('pointermove', event => {
      const r = card.getBoundingClientRect(), x = (event.clientX - r.left) / r.width - .5, y = (event.clientY - r.top) / r.height - .5;
      card.style.transform = `perspective(900px) rotateX(${-y * 3}deg) rotateY(${x * 4}deg) translateY(-2px)`;
    });
    card.addEventListener('pointerleave', () => card.style.transform = '');
  });
}
initMotion();
