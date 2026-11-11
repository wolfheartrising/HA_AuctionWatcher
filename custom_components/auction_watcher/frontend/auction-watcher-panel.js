/*
 * Auction Watcher sidebar page.
 *
 * Reads the auction sensors Home Assistant already has (attribute
 * auction_watcher: true) and the account sensors (auction_watcher_account),
 * so it needs no extra API. Plain web component, no build step.
 *
 * Home Assistant pushes new state every few seconds. To keep that from
 * wiping what you're typing, every field you touch is kept as a "draft"
 * until you save it, and the page re-draws from those drafts.
 */

const PANEL_VERSION = "1.10.0";  // must match const.py VERSION (a test checks)
const SITE_NAMES = { govauctions: "GovAuctions", macbid: "Mac.bid", ebay: "eBay", ola: "OLA" };
// What the per-auction spending cap is called on each site
const LIMIT_LABEL = { govauctions: "Max bids", macbid: "Max bid ($)", ebay: "Max price ($)", ola: "Max bid ($)" };
// Mac.bid's own "Set your max bid" amounts: $1 to $100, $5 to $500, $10 to $1,000, $50 to $50,000
const MB_STEPS = [[1, 100, 1], [105, 500, 5], [510, 1000, 10], [1050, 50000, 50]];
function mbValidBids(current, count = 60) {
  const out = [];
  for (const [lo, hi, step] of MB_STEPS) for (let a = lo; a <= hi && out.length < count; a += step) if (a > (current || 0)) out.push(a);
  return out;
}
const SITE_COLORS = { govauctions: "#e53935", macbid: "#43a047", ebay: "#1e88e5", ola: "#fb8c00" };
const siteDot = (site, title) =>
  `<span class="dot" style="background:${SITE_COLORS[site] || "#888"}" title="${esc(title || SITE_NAMES[site] || site)}"></span>`;
const SHORT_CLOCK = 15; // seconds; same as FINAL_PHASE_MAX_CLOCK in logic.py

const SORTS = {
  name: { label: "Name", key: (a) => String(a.title || "").toLowerCase() },
  price: { label: "Price (high first)", key: (a) => -Number(a.price || 0) },
  bidders: { label: "Fewest other bidders", key: (a) => (a.active_bidders ?? 999) },
  yours: { label: "Most of your bids", key: (a) => -Number(a.your_bids || 0) },
  clock: { label: "Clock / starts soonest", key: (a) => clockSeconds(a) },
  bestbet: { label: "Best bets first", key: (a) => (Number(a.gov_best_bet_score ?? 0) * -1) + (a.site === "govauctions" ? 0 : 10000) },
  opportunity: { label: "Opportunities first", key: (a) => (a.opportunity ? 0 : a.status === "live" ? 1 : 2) },
};

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? esc(u) : "");
const money = (v) => (v === null || v === undefined || v === "" || isNaN(Number(v)) ? "–" : "$" + Number(v).toFixed(2));
const num = (v) => (v === null || v === undefined || v === "" ? "–" : Number(v).toLocaleString());

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

function clockSeconds(a) {
  if (a.status === "upcoming" && a.starts_at) return (Date.parse(a.starts_at) - Date.now()) / 1000;
  if (a.ends_at) return (Date.parse(a.ends_at) - Date.now()) / 1000;
  if (a.clock_seconds !== null && a.clock_seconds !== undefined) return Number(a.clock_seconds);
  return 1e12;
}

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) { /* private window etc. */ }
  return null;
}

class AuctionWatcherPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "active";
    this._sort = store("auction_watcher_sort") || "name";
    if (!SORTS[this._sort]) this._sort = "name";
    this._signature = "";
    this._saved = {};   // short "Saved" messages, by key
    this._open = {};    // which "Booking & auto" sections are open
    this._drafts = {};  // unsaved field values: {auctionId: {fieldName: value}}
    this._sd = null;    // saved searches + results (fetched from Home Assistant when that tab is open)
    this._sform = { keyword: "", max_price: "", sites: null, save_to_macbid: false, save_to_ola: false };
    this._sfilter = { search: null, site: null, newOnly: false };
    this._sLimit = 60;  // result rows drawn; "Show more" adds 60
  }

  set hass(hass) {
    this._hass = hass;
    if (this._menuButton) this._menuButton.hass = hass;
    this._maybeRender();
  }
  get hass() { return this._hass; }

  set narrow(v) {
    this._narrow = v;
    if (this._menuButton) this._menuButton.narrow = v;
  }

  connectedCallback() {
    this._timer = setInterval(() => this._tick(), 1000);
    // Accounts that can't sign in: show a banner with a way to sign in again
    this._entriesTimer = setInterval(() => this._checkEntries(), 60000);
    setTimeout(() => this._checkEntries(), 1500);
    setTimeout(() => { if (!this._pv && !this._pvLoading && this._hass) this._loadProxy(); }, 1000);
    // While you're looking at the page and a penny auction is in its 5-second phase,
    // ask for a check every 3 s so the clock and bidders keep up with the site.
    this._live = setInterval(() => this._liveRefresh(), 3000);
    this._maybeRender(true);
  }
  disconnectedCallback() { clearInterval(this._timer); clearInterval(this._live); clearInterval(this._entriesTimer); this._stopSearchWatch(); }

  async _checkEntries() {
    if (!this._hass || document.visibilityState !== "visible") return;
    try {
      const entries = await this._hass.callWS({ type: "config_entries/get", domain: "auction_watcher" });
      const bad = entries.filter((e) => !e.disabled_by && e.state !== "loaded" && e.state !== "not_loaded");
      const sig = JSON.stringify(bad.map((e) => [e.entry_id, e.state, e.reason]));
      if (sig !== JSON.stringify((this._badEntries || []).map((e) => [e.entry_id, e.state, e.reason]))) {
        this._badEntries = bad; this._maybeRender(true);
      }
    } catch (e) { /* older Home Assistant: no banner */ }
  }

  _entryBanner() {
    // An app or tab left open across an update still runs the old page code: offer a one-tap reload
    const backend = this._accounts().map((a) => a.integration_version).find(Boolean);
    const stale = backend && backend !== PANEL_VERSION
      ? `<div class="banner update"><b>Auction Watcher was updated</b> <span class="muted">(this page is ${esc(PANEL_VERSION)}, Home Assistant runs ${esc(backend)})</span>
          <button class="btn" data-action="reload">Reload</button></div>` : "";
    return stale + (this._badEntries || []).map((e) => {
      const login = e.state === "setup_error" || /sign in|log ?in|auth|refused the login|token/i.test(e.reason || "");
      return `<div class="banner">
        <b>${esc(e.title)}</b> ${login ? "needs you to sign in again." : "isn't working right now."}
        <span class="muted">${esc(e.reason || e.state)}</span>
        <a class="btn" href="/config/integrations/integration/auction_watcher">${login ? "Sign in again" : "Open the integration"}</a>
        <span class="muted">${login ? "Use Reconfigure there (no need to delete it: deleting loses its saved max bids and settings)." : ""}</span>
      </div>`;
    }).join("");
  }

  _liveRefresh() {
    if (!this._hass || document.visibilityState !== "visible") return;
    const hot = this._auctions().some((a) => a.site === "govauctions" && a.status === "live"
      && a.clock_seconds !== null && a.clock_seconds !== undefined && Number(a.clock_seconds) <= SHORT_CLOCK);
    if (hot) this._hass.callService("auction_watcher", "refresh", { site: "govauctions" }).catch(() => {});
  }

  // ---------------------------------------------------------------- data

  _auctions() {
    const out = [];
    for (const s of Object.values(this._hass?.states || {})) {
      const a = s.attributes || {};
      if (a.auction_watcher !== true) continue;
      out.push({ entity_id: s.entity_id, price: s.state, updated: Date.parse(s.last_updated), unavailable: s.state === "unavailable", ...a });
    }
    const key = SORTS[this._sort].key;
    return out.sort((a, b) => {
      const ka = key(a), kb = key(b);
      return ka < kb ? -1 : ka > kb ? 1 : String(a.title).localeCompare(String(b.title));
    });
  }

  _autoSwitch(site = "govauctions") {
    return Object.values(this._hass?.states || {}).find((x) => x.attributes?.auction_watcher_autoswitch
      && (x.attributes.site || "govauctions") === site);
  }

  // Balance per the site, what's tied up, what could still be spent, and the weekly budget
  _tally(accounts) {
    const cap = accounts.map((a) => a.spending?.cap).find(Boolean);
    return (cap ? this._capLine(cap) : "") + accounts.filter((a) => a.spending).map((acc) =>
      acc.spending.site === "macbid" ? this._macbidTally(acc.spending) : this._govTally(acc.spending)).join("");
  }

  _capLine(c) {
    const pct = c.amount ? Math.min(100, Math.round((c.used / c.amount) * 100)) : 0;
    const per = c.period === "month" ? "this month" : "this week";
    const sites = Object.entries(c.by_site || {}).filter(([, v]) => v).map(([k, v]) => `${siteDot(k)}${esc(SITE_NAMES[k] || k)} ${money(v)}`).join(" · ");
    return `<div class="tally cap">
      <div class="tally-row"><b>Spending cap</b> <span>${money(c.used)} of ${money(c.amount)} ${per}</span>
        <span class="badge ${c.used >= c.amount ? "cap" : pct >= 80 ? "hot" : "on"}">${c.used >= c.amount
          ? (c.mode === "block" ? "reached: bids stopped" : "reached (warning only)") : money(c.left) + " left"}</span>
        <span class="muted">${sites}</span></div>
      <div class="capbar"><div style="width:${pct}%"></div></div>
    </div>`;
  }

  _macbidTally(m) {
    const sw = this._autoSwitch("macbid");
    const on = sw ? sw.state === "on" : !m.auto_paused;
    return `<div class="tally">
      <div class="k"><b>Mac.bid</b></div>
      <div class="tally-grid">
        <div><div class="k">Max bids placed</div><div class="v">${money(m.committed)}</div><div class="k">on items still live</div></div>
        <div><div class="k">Scheduled</div><div class="v">${money(m.scheduled)}</div><div class="k">late bids waiting</div></div>
        <div><div class="k">Budget</div><div class="v">${m.budget ? money(m.budget) : "none"}</div>
          <div class="k">${m.budget_left !== null && m.budget_left !== undefined ? money(Math.max(0, m.budget_left)) + " left" : ""}</div></div>
        <div><div class="k">Won, awaiting pickup</div><div class="v">${money(m.won_awaiting_pickup)}</div><div class="k">bid amounts</div></div>
      </div>
      <div class="tally-row">
        <span class="badge ${on ? "on" : "cap"}">${on ? "Auto-bidding on" : "Auto-bidding paused"}</span>
        ${sw ? `<button type="button" class="${on ? "secondary" : ""}" data-action="toggle-auto" data-site="macbid">${on ? "Pause Mac.bid auto-bidding" : "Resume auto-bidding"}</button>` : ""}
        <span class="muted">Max bids are the most you'd pay before the 15% premium, $3 fee and tax.</span>
      </div>
    </div>`;
  }

  _govTally(m) {
    const sw = this._autoSwitch("govauctions");
    const on = sw ? sw.state === "on" : !m.auto_paused;
    const budget = m.weekly_budget
      ? `<div><div class="k">This week</div><div class="v">${num(m.spent_week)} / ${num(m.weekly_budget)}</div>
           <div class="k">${m.budget_left !== null && m.budget_left !== undefined ? `${num(Math.max(0, m.budget_left))} left` : ""}</div></div>`
      : `<div><div class="k">This week</div><div class="v">${num(m.spent_week)}</div><div class="k">${money(m.spent_week_dollars)} · no budget set</div></div>`;
    return `<div class="tally">
      <div class="tally-grid">
        <div><div class="k">Balance (site)</div><div class="v">${num(m.balance)}</div><div class="k">free to spend</div></div>
        <div><div class="k">Booked</div><div class="v">${num(m.booked)}</div><div class="k">held in booked bids</div></div>
        <div><div class="k">Earmarked</div><div class="v">${num(m.earmarked)}</div><div class="k">auto-rebook could still book</div></div>
        <div><div class="k">At risk</div><div class="v">${num(m.at_risk)}</div><div class="k">${money(m.at_risk_dollars)}</div></div>
        ${budget}
        <div><div class="k">Today</div><div class="v">${num(m.spent_today)}</div><div class="k">bids placed</div></div>
      </div>
      <div class="tally-row">
        <span class="badge ${on ? "on" : "cap"}">${on ? "Auto-booking on" : "Auto-booking paused"}</span>
        ${sw ? `<button type="button" class="${on ? "secondary" : ""}" data-action="toggle-auto" data-site="govauctions">${on ? "Pause all auto-booking" : "Resume auto-booking"}</button>` : ""}
        ${m.at_risk > (m.balance || 0) + m.booked ? `<span class="warn">Your max bids add up to more than you have: auctions will compete for the same points.</span>` : ""}
      </div>
    </div>`;
  }

  _packs() {
    const s = Object.values(this._hass?.states || {}).find((x) => x.attributes?.auction_watcher_packs);
    return s ? { list: s.attributes.packs || [], scanned: s.attributes.scanned_at } : null;
  }

  _accounts() {
    return Object.values(this._hass?.states || {})
      .filter((s) => s.attributes?.auction_watcher_account)
      .map((s) => ({ entity_id: s.entity_id, points: s.state, ...s.attributes }));
  }

  _tabs(auctions) {
    const sites = [...new Set([...auctions.map((a) => a.site), ...this._accounts().map((a) => a.site || "govauctions")]
      .filter(Boolean))].sort();
    return [
      { id: "active", label: "Active", filter: (a) => a.category === "bidding" && (a.status === "live" || a.status === "upcoming") },
      { id: "winning", label: "Winning", filter: (a) => a.status === "live" && !!a.winning },
      { id: "outbid", label: "Outbid", filter: (a) => a.status === "live" && !a.winning
          && (a.site === "govauctions" ? Number(a.your_bids || 0) > 0 : !!a.you_are_bidding) },
      { id: "auto", label: "Auto-bidding", filter: (a) => a.status !== "ended" && a.status !== "awaiting_pickup"
          && (!!a.auto_cancel || !!a.auto_rebook || a.mb_mode === "late") },
      { id: "watching", label: "Watching", filter: (a) => a.category === "watching" && a.status !== "ended" },
      { id: "pickup", label: "Awaiting pickup", filter: (a) => a.status === "awaiting_pickup" },
      { id: "ended", label: "Ended", filter: (a) => a.status === "ended" },
      { id: "searches", label: "Saved searches", filter: () => false, searches: true },
      ...(this._pv?.enabled && this._pv.people.length ? [{ id: "others", label: "For others", others: true,
        filter: (a) => !!a.proxy && ["asked", "confirming", "confirmed", "watching"].includes(a.proxy.state)
          && (!this._subtab || ["all", "following"].includes(this._subtab) || this._subtab === a.proxy.person_id || this._tab !== "others") }] : []),
      { id: "settings", label: "⚙ Settings", filter: () => false, settings: true },
      ...sites.map((site) => ({ id: "site-" + site, label: SITE_NAMES[site] || site, filter: (a) => a.site === site })),
    ];
  }

  // A field's value: your unsaved draft if you've touched it, else the saved value
  _val(id, field, saved) {
    const d = this._drafts[id];
    return d && field in d ? d[field] : saved;
  }

  // ---------------------------------------------------------------- rendering

  _maybeRender(force = false) {
    if (!this._hass || !this.isConnected) return;
    // While a text box has focus, don't redraw under the cursor; redraw on blur.
    const active = this.shadowRoot.activeElement;
    if (!force && active && active.tagName === "INPUT" && active.type !== "checkbox" && active.type !== "radio") {
      this._pending = true;
      return;
    }
    const auctions = this._auctions();
    const accounts = this._accounts();
    const signature = JSON.stringify([this._pv?.people, this._badEntries, this._tab, this._subtab, this._sort, auctions.map((a) => [a.entity_id, a.price, a.updated, a.bid_limit,
      a.auto_cancel, a.auto_rebook, a.rebook_mode, a.rebook_amount, a.topup_at, a.max_price, a.auto_status, a.your_booked_left,
      a.mb_mode, a.mb_placed, a.mb_status, a.proxy, a.winning, a.has_assurance, a.mb_assurance, (a.condition_notes || []).length]),
      accounts, this._saved, this._subtab === "packs" ? [this._packs(), this._showLocked] : null,
      this._tab === "searches" ? [this._sd?.version, !!this._sd, this._sfilter, this._sLimit, this._serr, this._sbusy] : null,
      this._tab === "settings" ? [this._gs, this._gmsg, this._gbusy] : null,
      this._subtab === "settings" ? [this._ssMsg, this._ssBusy, this._ssKey] : null, this._autoSwitch()?.state, this._autoSwitch("macbid")?.state]);
    if (!force && signature === this._signature) return;
    this._signature = signature;
    this._pending = false;
    this._render(auctions, accounts);
  }

  _render(auctions, accounts) {
    const tabs = this._tabs(auctions);
    if (!tabs.find((t) => t.id === this._tab)) this._tab = "active";
    const tab = tabs.find((t) => t.id === this._tab);
    const shown = auctions.filter(tab.filter);
    if (tab.others && this._subtab === "following") this._subtab = "all";
    // Sub-tab of a site tab; Point packs only exists on GovAuctions
    let sub = tab.id.startsWith("site-") ? (this._subtab || "following") : "";
    if (sub === "packs" && !(tab.id === "site-govauctions" && this._packs())) sub = "following";
    const opp = auctions.filter((a) => a.opportunity).length;

    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="toolbar">
        <span class="menu"></span>
        <div class="title">Auctions</div>
        <button class="icon" data-action="refresh" title="Check now">⟳</button>
      </div>
      ${this._entryBanner()}
      <div class="summary">
        ${accounts.map((acc) => `
          <div class="pill"><b>${esc(acc.auction_watcher_account)}</b>
            ${num(acc.points)} points · ${esc(acc.polling_mode || "")}${acc.poll_every_seconds ? `, every ${fmtDuration(acc.poll_every_seconds)}` : ""}
          </div>`).join("") || `<div class="pill">No site set up yet: Settings → Devices &amp; services → Add integration → Auction Watcher</div>`}
        ${opp ? `<div class="pill hot">🎯 ${opp} with few bidders left</div>` : ""}
        <label class="sort">Sort
          <select data-action="sort">
            ${Object.entries(SORTS).map(([k, s]) => `<option value="${k}" ${k === this._sort ? "selected" : ""}>${esc(s.label)}</option>`).join("")}
          </select>
        </label>
      </div>
      ${[tabs.filter((t) => !t.id.startsWith("site-")), tabs.filter((t) => t.id.startsWith("site-"))].filter((row) => row.length).map((row, i) => `
      <div class="tabs ${i ? "site-tabs" : ""}" role="tablist">
        ${i ? `<span class="tabs-label">Sites</span>` : ""}
        ${row.map((t) => {
          const n = t.searches ? (this._sd ? this._sd.results.filter((h) => h.new).length || this._sd.results.length : "…") : auctions.filter(t.filter).length;
          return `<button role="tab" class="tab ${t.id === this._tab ? "on" : ""}" data-tab="${esc(t.id)}">${t.id.startsWith("site-") ? siteDot(t.id.slice(5)) : ""}${esc(t.label)}${t.settings ? "" : ` <span class="count">${n}</span>`}</button>`;
        }).join("")}
      </div>`).join("")}
      ${tab.id === "active" ? this._tally(accounts) : ""}
      ${tab.others ? `<div class="subtabs">
        <button class="subtab ${!this._subtab || this._subtab === "all" || this._subtab === "following" ? "on" : ""}" data-subtab="all">Everyone</button>
        ${this._pv.people.map((p) => {
          const n = auctions.filter((a) => a.proxy?.person_id === p.id && ["asked", "confirming", "confirmed", "watching"].includes(a.proxy.state)).length;
          return `<button class="subtab ${this._subtab === p.id ? "on" : ""}" data-subtab="${esc(p.id)}">💬 ${esc(p.name)} <span class="count">${n}</span></button>`;
        }).join("")}</div>` : ""}
      ${tab.id.startsWith("site-") ? `<div class="subtabs">
        <button class="subtab ${sub === "following" ? "on" : ""}" data-subtab="following">Following <span class="count">${shown.length}</span></button>
        ${tab.id === "site-govauctions" && this._packs() ? `<button class="subtab ${sub === "packs" ? "on" : ""}" data-subtab="packs">Point packs
          <span class="count">${this._packGroups().filter((g) => g.auctions.some((p) => p.open || p.your_bids)).length}</span></button>` : ""}
        <button class="subtab ${sub === "settings" ? "on" : ""}" data-subtab="settings">⚙ Settings</button>
        ${sub === "packs" ? `<span class="muted">Point packs aren't auctions you're in: they're "if you're interested" picks.</span>` : ""}</div>` : ""}
      ${tab.searches ? `<div class="sv-slot"></div>` : tab.settings ? `<div class="gs-slot"></div>`
        : sub === "settings" ? `<div class="ss-slot"></div>` : `<div class="grid">
        ${sub === "packs" ? this._packsView() : shown.length ? shown.map((a) => this._safeCard(a)).join("") : `<div class="empty">Nothing here right now.</div>`}
      </div>`}`;

    const slot = this.shadowRoot.querySelector(".menu");
    this._menuButton = document.createElement("ha-menu-button");
    this._menuButton.hass = this._hass;
    this._menuButton.narrow = this._narrow;
    slot.appendChild(this._menuButton);

    const root = this.shadowRoot;
    root.querySelectorAll("[data-subtab]").forEach((b) =>
      b.addEventListener("click", () => {
        if (this._subtab === "settings" && b.dataset.subtab !== "settings") { this._dropSiteFlow(); this._ssKey = null; }
        this._subtab = b.dataset.subtab; this._maybeRender(true);
      }));
    root.querySelectorAll("[data-tab]").forEach((b) =>
      b.addEventListener("click", () => {
        if (this._tab !== b.dataset.tab) {
          if (this._subtab === "settings") { this._dropSiteFlow(); this._ssKey = null; }
          this._subtab = "following";
        }
        this._tab = b.dataset.tab; if (this._tab !== "searches") this._stopSearchWatch(); this._maybeRender(true);
      }));
    root.querySelector("[data-action=reload]")?.addEventListener("click", () => location.reload());
    root.querySelector("[data-action=refresh]").addEventListener("click", () =>
      this._hass.callService("auction_watcher", "refresh", {}));
    root.querySelector("[data-action=packs-locked]")?.addEventListener("click", () => {
      this._showLocked = !this._showLocked; this._maybeRender(true);
    });
    root.querySelectorAll("[data-action=toggle-auto]").forEach((b) => b.addEventListener("click", () => {
      const sw = this._autoSwitch(b.dataset.site);
      if (sw) this._hass.callService("switch", sw.state === "on" ? "turn_off" : "turn_on", { entity_id: sw.entity_id });
    }));
    root.querySelector("[data-action=sort]").addEventListener("change", (ev) => {
      this._sort = ev.target.value; store("auction_watcher_sort", this._sort); this._maybeRender(true);
    });
    // Remember every edit as a draft so a refresh can't wipe it
    root.querySelectorAll("[data-field]").forEach((el) => {
      const remember = () => {
        const id = el.dataset.id, f = el.dataset.field;
        (this._drafts[id] ||= {})[f] = el.type === "checkbox" ? el.checked : el.type === "radio" ? el.value : el.value;
        if (f === "limit" || f === "max_price") {
          // Update the total as you type, without redrawing the card
          const a = this._auctions().find((x) => String(x.auction_id) === String(id));
          const box = root.querySelector(`[data-total="${CSS.escape(id)}"]`);
          if (a && box) box.innerHTML = this._totalText(a);
        }
      };
      el.addEventListener(el.type === "checkbox" || el.type === "radio" ? "change" : "input", remember);
      el.addEventListener("blur", () => { if (this._pending) setTimeout(() => this._maybeRender(), 0); });
    });
    root.querySelectorAll("details.booking").forEach((d) =>
      d.addEventListener("toggle", () => { this._open[d.dataset.id] = d.open; }));
    root.querySelectorAll("[data-act]").forEach((b) =>
      b.addEventListener("click", () => this._action(b.dataset.act, b.dataset.id, b.dataset.site)));
    root.querySelectorAll("[data-field=proxy_mode]").forEach((el) =>
      el.addEventListener("change", () => { (this._drafts[el.dataset.id] ||= {}).proxy_mode = el.value; this._maybeRender(true); }));
    if (tab.settings) this._mountGlobalSettings(root.querySelector(".gs-slot"));
    if (sub === "settings") this._mountSiteSettings(root.querySelector(".ss-slot"), tab.id.slice(5), accounts);
    if (tab.searches) {
      const slot = root.querySelector(".sv-slot");
      const sig = JSON.stringify([this._sd?.version, this._sd ? 1 : 0, this._sfilter, this._sLimit, this._serr, this._sbusy, this._locsOpen]);
      if (this._svNode && this._svSig === sig) {
        slot.replaceWith(this._svNode);   // same DOM, same pictures: nothing flickers
      } else {
        slot.innerHTML = this._searchesView();
        this._svNode = slot;
        this._svSig = sig;
        this._bindSearches(slot);
      }
    }
    this._tick();
  }

  // ---------------------------------------------------------------- saved searches

  async _loadSearches() {
    if (this._tab !== "searches") { this._stopSearchWatch(); return; }
    if ((this._sd && document.visibilityState !== "visible") || this._sbusy) return;
    try {
      const msg = { type: "auction_watcher/searches" };
      if (this._sd?.version) msg.version = this._sd.version;
      const got = await this._hass.callWS(msg);
      if (!got.unchanged) this._sd = got;
      this._serr = null;
    } catch (e) { this._serr = this._wsError(e); }
    this._maybeRender();
  }

  _stopSearchWatch() {
    clearInterval(this._sTimer); this._sTimer = null;
    const u = this._sUnsub; this._sUnsub = null;
    if (u) Promise.resolve(u).then((fn) => typeof fn === "function" && fn()).catch(() => {});
  }

  async _searchChange(payload) {
    this._sbusy = true; this._maybeRender(true);
    try {
      this._sd = await this._hass.callWS({ type: "auction_watcher/searches/change", ...payload });
      this._serr = null;
    } catch (e) { this._serr = this._wsError(e); }
    this._sbusy = false; this._maybeRender(true);
  }

  // ---------------------------------------------------------------- settings: global (alerts)

  async _loadGlobal() {
    try {
      this._gs = await this._hass.callWS({ type: "auction_watcher/settings" });
      this._gdraft = null;
      try { this._pv = await this._hass.callWS({ type: "auction_watcher/proxy" }); } catch (e) { /* older backend */ }
    } catch (e) { this._gmsg = { warn: true, text: this._wsError(e) }; }
    this._maybeRender(true);
  }

  _mountGlobalSettings(slot) {
    const sig = JSON.stringify([this._gs, this._gmsg, this._gbusy, this._pv]);
    if (this._gsNode && this._gsSig === sig) { slot.replaceWith(this._gsNode); return; }
    if (!this._gs && !this._gLoading) { this._gLoading = true; this._loadGlobal().finally(() => { this._gLoading = false; }); }
    slot.innerHTML = this._globalView();
    this._gsNode = slot; this._gsSig = sig;
    const d = () => (this._gdraft ||= {
      notify_services: [...this._gs.notify_services], notify_entities: [...this._gs.notify_entities], alert_kinds: [...this._gs.alert_kinds],
    });
    slot.querySelectorAll("[data-gs]").forEach((el) => el.addEventListener("change", () => {
      const list = d()[el.dataset.gs], v = el.value;
      if (el.checked && !list.includes(v)) list.push(v);
      if (!el.checked) list.splice(list.indexOf(v), 1);
      const btn = slot.querySelector("[data-gact=save]"); if (btn) btn.disabled = false;
    }));
    slot.querySelectorAll("[data-pact]").forEach((el) => el.addEventListener(el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "click", async () => {
      const act = el.dataset.pact, msg = { type: "auction_watcher/proxy/change", action: "set" };
      if (act === "enable") msg.enabled = el.checked;
      if (act === "bot") msg.bot_entry = el.value;
      if (act === "owner") msg.owner_chat = el.value;
      if (act === "owner_alerts") msg.owner_alerts = el.checked;
      if (act === "edit") {
        const p = this._pv.people.find((x) => x.id === el.dataset.pid);
        if (!p) return;
        const name = prompt("Name:", p.name);
        if (name === null) return;
        const chat = prompt("Telegram chat id (the same number works with every bot):", p.chat_id);
        if (chat === null) return;
        msg.change = { id: p.id, name: name.trim(), chat_id: chat.trim() };
      }
      if (act === "remove") {
        const p = this._pv.people.find((x) => x.id === el.dataset.pid);
        if (!confirm(`Remove ${p?.name || "this person"}? Open requests for them stop.\n\n(To switch bots or fix their chat id, use ✎ instead: their items stay.)`)) return;
        msg.remove = el.dataset.pid;
      }
      if (act === "add") {
        const name = slot.querySelector("[data-pform=name]").value.trim();
        const picked = slot.querySelector("[data-pform=chat]")?.value || "";
        const chat = picked || slot.querySelector("[data-pform=chat_id]").value.trim();
        if (!name || !/^-?\d+$/.test(chat)) { this._gmsg = { warn: true, text: "A person needs a name and their numeric Telegram chat id" }; this._gsSig = null; return this._maybeRender(true); }
        msg.add = { name, chat_id: chat };
      }
      try { this._pv = await this._hass.callWS(msg); this._gmsg = null; }
      catch (e) { this._gmsg = { warn: true, text: e?.message || String(e) }; }
      this._gsSig = null; this._maybeRender(true);
    }));
    slot.querySelector("[data-pform=chat]")?.addEventListener("change", (ev) => {
      const opt = ev.target.selectedOptions[0];
      const typed = slot.querySelector("[data-pform=chat_id]");
      typed.style.display = ev.target.value ? "none" : "";
      if (ev.target.value) slot.querySelector("[data-pform=name]").value = opt.dataset.name || "";
      else typed.focus();
    });
    slot.querySelectorAll("[data-gcap]").forEach((el) => el.addEventListener(el.type === "number" ? "input" : "change", () => {
      if (el.type === "radio" && !el.checked) return;
      d()[el.dataset.gcap] = el.type === "number" ? Number(el.value || 0) : el.value;
      const btn = slot.querySelector("[data-gact=save]"); if (btn) btn.disabled = false;
    }));
    slot.querySelectorAll("[data-gact]").forEach((b) => b.addEventListener("click", async () => {
      this._gbusy = true; this._gmsg = null; this._maybeRender(true);
      try {
        if (b.dataset.gact === "save") {
          this._gs = await this._hass.callWS({ type: "auction_watcher/settings/set", ...d() });
          this._gdraft = null;
          this._gmsg = { text: "Saved. These apply to every site and to saved searches." };
        } else {
          if (this._gdraft) this._gs = await this._hass.callWS({ type: "auction_watcher/settings/set", ...d() });
          this._gdraft = null;
          await this._hass.callWS({ type: "auction_watcher/settings/set", test: true });
          this._gmsg = { text: "Test alert sent. Check your phone, chat or speaker." };
        }
      } catch (e) { this._gmsg = { warn: true, text: this._wsError(e) }; }
      this._gbusy = false; this._maybeRender(true);
    }));
  }

  _proxySettings() {
    const v = this._pv;
    if (!v) return `<section class="gs-sec"><h3>For others</h3><div class="muted">Loading…</div></section>`;
    const chats = v.chats || [];
    return `<section class="gs-sec"><h3>For others</h3>
      <label class="chk"><input type="checkbox" data-pact="enable" ${v.enabled ? "checked" : ""}> Let me bid for people on this list
        (Mac.bid and GovAuctions, from my account) or keep them posted on any item</label>
      ${v.enabled && !v.telegram_ready ? `<div class="warn">Needs Home Assistant's Telegram bot integration (telegram_bot) for the questions and "I agree" buttons.</div>` : ""}
      ${(v.bots || []).length ? `<label class="chk">Telegram bot
        <select data-pact="bot">
          ${(v.bots.length > 1 || v.bot_entry) ? "" : `<option value="">${esc(v.bots[0].title)}</option>`}
          ${v.bots.length > 1 || v.bot_entry ? `<option value="" ${v.bot_entry ? "" : "selected"}>choose…</option>` + v.bots.map((b) => `<option value="${esc(b.entry_id)}" ${b.entry_id === v.bot_entry ? "selected" : ""}>${esc(b.title)}</option>`).join("") : ""}
        </select></label>
        ${v.bots.length > 1 && !v.bot_entry ? `<div class="warn">You have ${v.bots.length} Telegram bots: pick the one these people talk to (it must use Polling or Webhooks so their replies reach Home Assistant).</div>` : ""}
        <div class="k">Your own chat (owner)</div>
        <div class="sadd" style="border:0;padding:0">
          <select data-pact="owner">
            <option value="" ${v.owner_chat ? "" : "selected"}>not set</option>
            ${(v.all_chats || []).map((c) => `<option value="${esc(c.chat_id)}" ${c.chat_id === v.owner_chat ? "selected" : ""}>${esc(c.name)} (${esc(c.source)})</option>`).join("")}
            ${v.owner_chat && !(v.all_chats || []).some((c) => c.chat_id === v.owner_chat) ? `<option value="${esc(v.owner_chat)}" selected>${esc(v.owner_chat)}</option>` : ""}
          </select>
          ${v.owner_chat ? `<label class="chk"><input type="checkbox" data-pact="owner_alerts" ${v.owner_alerts ? "checked" : ""}>
            Send my outbid / few-bidders / good-pack alerts here, with buttons</label>` : ""}
        </div>
        <div class="note">Your chat can use /status, /active (quick bid and Stop per item), /pause, /resume and /help, and gets
          "X wants you to bid" requests with Allow buttons. Anything that spends asks you to confirm; your max, budgets and
          spending cap still apply. If this bot is also under "Where alerts go", untick one or you'll get those alerts twice.
          Not listed? Message the bot once (send /start), then reopen this tab.</div>` : ""}
      ${v.enabled ? `<div class="gs-list">${v.people.map((p) => `<span class="chk gs-item">💬 ${esc(p.name)} <span class="muted">${esc(p.chat_id)}</span>
          <button class="schip-t" data-pact="edit" data-pid="${esc(p.id)}" title="Rename ${esc(p.name)} or fix their chat id (their items stay)">✎</button>
          <button class="schip-x" data-pact="remove" data-pid="${esc(p.id)}" title="Remove ${esc(p.name)}">×</button></span>`).join("")
          || `<span class="muted">Nobody yet.</span>`}</div>
        <div class="sadd" style="border:0;padding:0">
          <select data-pform="chat">
            ${chats.map((c) => `<option value="${esc(c.chat_id)}" data-name="${esc(c.name)}">${esc(c.name)} (${esc(c.source)})</option>`).join("")}
            <option value="">Other: type a chat id…</option>
          </select>
          <input type="text" placeholder="Name" data-pform="name" value="${esc(chats[0]?.name || "")}" style="flex:0 1 160px" autocomplete="off">
          <input type="text" placeholder="Telegram chat id" data-pform="chat_id" style="flex:0 1 180px; ${chats.length ? "display:none" : ""}"
            autocomplete="off" inputmode="numeric" data-lpignore="true" data-1p-ignore>
          <button data-pact="add">Add person</button>
        </div>
        <div class="note">The list shows your Telegram bot's allowed chats, and anyone who messaged the bot since Home Assistant
          started. They must have pressed /start with the bot. Only people you add here are listened to. On an item, open
          "Bid for someone" (Mac.bid, GovAuctions) or "Keep someone posted" (any site). Nothing is bid until they say how much
          and tap "I agree to pay"; your budgets and spending cap still apply.</div>
        ${v.requests.length ? `<div class="k">Recent requests</div><div class="note">${v.requests.slice(0, 8).map((r) =>
          `${esc(r.person)} · ${esc(r.title)} · ${esc(r.state)}${r.agreed_max ? ` · agreed $${num(r.agreed_max)}` : ""}`).join("<br>")}</div>` : ""}` : ""}
    </section>`;
  }

  _globalView() {
    const g = this._gs;
    if (!g) return `<div class="sv"><div class="empty">Loading settings…</div></div>`;
    const dr = this._gdraft || g;
    const label = (svc) => {
      const m = { mobile_app_: "📱 Phone app: ", telegram: "💬 Telegram", alexa_media_: "🔊 Alexa: " };
      for (const [k, v] of Object.entries(m)) if (svc.startsWith(k)) return v + svc.slice(k.length).replace(/_/g, " ");
      return svc.replace(/_/g, " ");
    };
    const box = (group, value, text, on) =>
      `<label class="chk gs-item"><input type="checkbox" data-gs="${group}" value="${esc(value)}" ${on ? "checked" : ""}> ${text}</label>`;
    const services = [...new Set([...g.services_available, ...g.notify_services])];
    const entities = [...new Set([...g.entities_available, ...g.notify_entities])];
    return `<div class="sv gs">
      <div class="note">These apply to every site and to saved searches. Each site's own options (polling, limits, budget, ...)
        are under that site's tab → ⚙ Settings.</div>
      <section class="gs-sec"><h3>Where alerts go</h3>
        <div class="gs-list">${services.map((x) => box("notify_services", x, esc(label(x)), dr.notify_services.includes(x))).join("")
          || `<span class="muted">No notify services found (install the Home Assistant phone app, Telegram, ...).</span>`}</div>
        ${entities.length ? `<div class="k">Notify entities (e.g. Alexa announce, Telegram chats)</div>
        <div class="gs-list">${entities.map((x) => box("notify_entities", x, esc(this._hass.states[x]?.attributes?.friendly_name || x), dr.notify_entities.includes(x))).join("")}</div>` : ""}
      </section>
      <section class="gs-sec"><h3>Which alerts</h3>
        <div class="gs-list kinds">${g.kind_options.map((k) => box("alert_kinds", k.value,
          `${k.sites.map((x) => siteDot(x)).join("")} ${esc(k.label)}`, dr.alert_kinds.includes(k.value))).join("")}</div>
        <div class="note">Each alert is sent once, not on every check.</div>
      </section>
      ${this._proxySettings()}
      <section class="gs-sec"><h3>Spending cap (all sites)</h3>
        <div class="sadd" style="border:0;padding:0">
          <label class="chk">Cap $ <input type="number" min="0" step="1" class="small" data-gcap="cap_amount" value="${esc(dr.cap_amount ?? g.cap.amount)}"></label>
          <label class="chk">per <select data-gcap="cap_period">
            <option value="week" ${(dr.cap_period ?? g.cap.period) === "week" ? "selected" : ""}>week (Mon–Sun)</option>
            <option value="month" ${(dr.cap_period ?? g.cap.period) === "month" ? "selected" : ""}>month</option></select></label>
          <label class="chk"><input type="radio" name="capmode" data-gcap="cap_mode" value="block" ${(dr.cap_mode ?? g.cap.mode) === "block" ? "checked" : ""}> Stop bids at the cap</label>
          <label class="chk"><input type="radio" name="capmode" data-gcap="cap_mode" value="warn" ${(dr.cap_mode ?? g.cap.mode) === "warn" ? "checked" : ""}> Warn only</label>
        </div>
        ${g.cap_summary ? this._capLine(g.cap_summary) : ""}
        <div class="note">0 = no cap. Counts GovAuctions points used plus points held in booked bids (at your point cost),
          Mac.bid max bids on items still live plus scheduled late bids, and Mac.bid items won this period (winning price,
          before fees and tax). "Stop bids" blocks automatic and manual bids and bookings that would go past it; cancelling
          always works. Either way you get one alert when it's reached. Each site's own budget still applies too.</div>
      </section>
      <div class="sbar">
        <button data-gact="save" ${this._gbusy || !this._gdraft ? "disabled" : ""}>Save</button>
        <button class="secondary" data-gact="test" ${this._gbusy ? "disabled" : ""}>Send test alert</button>
        ${this._gmsg ? `<span class="${this._gmsg.warn ? "warn" : "muted"}">${esc(this._gmsg.text)}</span>` : ""}
      </div>
    </div>`;
  }

  // ---------------------------------------------------------------- settings: one site (Home Assistant's own options form)

  async _ensureHaForm() {
    if (customElements.get("ha-form")) return true;
    try {
      // ha-form is loaded with the card editors; ask Home Assistant to load them
      const helpers = await window.loadCardHelpers?.();
      const card = helpers && await helpers.createCardElement({ type: "entities", entities: [] });
      await card?.constructor?.getConfigElement?.();
    } catch (e) { /* fall through */ }
    await Promise.race([customElements.whenDefined("ha-form"), new Promise((r) => setTimeout(r, 4000))]);
    return !!customElements.get("ha-form");
  }

  _mountSiteSettings(slot, site, accounts) {
    const acc = accounts.find((a) => (a.site || "govauctions") === site);
    const key = acc?.entry_id || "";
    if (this._ssNode && this._ssKey === key && this._ssSig === JSON.stringify([this._ssMsg, this._ssBusy])) {
      slot.replaceWith(this._ssNode); return;
    }
    const fresh = this._ssKey !== key;
    this._ssKey = key; this._ssSig = JSON.stringify([this._ssMsg, this._ssBusy]); this._ssNode = slot;
    if (!key) { slot.innerHTML = `<div class="sv"><div class="empty">This site's account sensor doesn't say which entry it is yet: restart Home Assistant after updating.</div></div>`; return; }
    slot.innerHTML = `<div class="sv ss">
      <div class="note">${esc(SITE_NAMES[site] || site)} options. Alerts are on the ⚙ Settings tab and apply to every site.
        Saving reconnects this site with the new settings.</div>
      <div class="ss-form"></div>
      <div class="sbar"><button data-ssact="save" ${this._ssBusy ? "disabled" : ""}>Save</button>
        ${this._ssMsg ? `<span class="${this._ssMsg.warn ? "warn" : "muted"}">${esc(this._ssMsg.text)}</span>` : ""}</div>
    </div>`;
    const host = slot.querySelector(".ss-form");
    if (fresh) this._startSiteFlow(key);
    else if (this._ssForm) host.appendChild(this._ssForm);
    slot.querySelector("[data-ssact=save]").addEventListener("click", () => this._saveSiteFlow());
  }

  async _startSiteFlow(entryId, doneMsg = null) {
    this._ssForm = null; this._ssMsg = doneMsg || { text: "Loading…" }; this._ssData = {};
    await this._dropSiteFlow();
    try {
      if (!(await this._ensureHaForm())) throw new Error("NOFORM");
      const step = await this._hass.callApi("POST", "config/config_entries/options/flow", { handler: entryId, show_advanced_options: false });
      const loc = (await this._hass.loadBackendTranslation?.("options", "auction_watcher")) || this._hass.localize;
      this._ssFlow = step.flow_id;
      this._ssStep = step.step_id;
      const data = {};
      for (const f of step.data_schema || []) {
        if ("default" in f) data[f.name] = f.default;
        else if (f.description?.suggested_value !== undefined) data[f.name] = f.description.suggested_value;
      }
      this._ssData = data;
      const form = document.createElement("ha-form");
      form.hass = this._hass;
      form.schema = step.data_schema;
      form.data = data;
      const k = (part, name) => `component.auction_watcher.options.step.${step.step_id}.${part}.${name}`;
      form.computeLabel = (f) => loc(k("data", f.name)) || f.name;
      form.computeHelper = (f) => loc(k("data_description", f.name)) || "";
      form.addEventListener("value-changed", (ev) => { this._ssData = ev.detail.value; form.data = this._ssData; });
      this._ssForm = form;
      this._ssMsg = doneMsg;
    } catch (e) {
      this._ssMsg = { warn: true, text: e?.message === "NOFORM"
        ? "Couldn't load Home Assistant's form here. Use Settings → Devices & services → Auction Watcher → Configure."
        : (e?.body?.message || e?.message || String(e)) };
    }
    this._ssSig = null; this._maybeRender(true);
  }

  async _saveSiteFlow() {
    if (!this._ssFlow) return;
    this._ssBusy = true; this._maybeRender(true);
    try {
      const res = await this._hass.callApi("POST", `config/config_entries/options/flow/${this._ssFlow}`, this._ssData);
      if (res.type === "create_entry") {
        this._ssFlow = null;
        const saved = { text: "Saved. This site reconnected with the new settings." };
        this._ssMsg = { text: "Saved. Reconnecting…" };
        this._ssForm = null;
        const key = this._ssKey;
        // Reload the form with the saved values once the site has reconnected
        setTimeout(() => { if (this._ssKey === key && this._subtab === "settings") this._startSiteFlow(key, saved); }, 3000);
      } else if (res.type === "form") {
        this._ssFlow = res.flow_id;
        if (this._ssForm) this._ssForm.error = res.errors || {};
        this._ssMsg = { warn: true, text: "Please fix the highlighted fields." };
      }
    } catch (e) { this._ssMsg = { warn: true, text: e?.body?.message || e?.message || String(e) }; }
    this._ssBusy = false; this._maybeRender(true);
  }

  async _dropSiteFlow() {
    const id = this._ssFlow; this._ssFlow = null;
    if (id) { try { await this._hass.callApi("DELETE", `config/config_entries/options/flow/${id}`); } catch (e) { /* already gone */ } }
  }

  _wsError(e) {
    const msg = e?.message || String(e);
    return /unknown command/i.test(msg) || e?.code === "unknown_command"
      ? "Home Assistant is still running the previous version of Auction Watcher. Restart Home Assistant to finish the update."
      : msg;
  }

  _searchesView() {
    if (!this._sTimer) {
      // Home Assistant tells the page when saved searches change, so there's no polling.
      // A slow check every 5 minutes is only a safety net (e.g. after the connection drops).
      this._sTimer = setInterval(() => this._loadSearches(), 300000);
      setTimeout(() => this._loadSearches(), 0);
      if (!this._sUnsub && this._hass?.connection) {
        this._sUnsub = this._hass.connection.subscribeEvents(() => this._loadSearches(), "auction_watcher_searches_updated")
          .catch(() => { this._sUnsub = null; });
      }
    }
    const d = this._sd;
    if (!d) return `<div class="sv"><div class="empty">${this._serr ? `<span class="warn">${esc(this._serr)}</span>` : "Loading saved searches…"}</div></div>`;
    const f = this._sform, flt = this._sfilter;
    const formSites = f.sites || d.sites;
    const byId = Object.fromEntries(d.searches.map((x) => [x.id, x]));
    const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : "never");
    let results = d.results;
    if (flt.search) results = results.filter((h) => h.searches.includes(flt.search));
    if (flt.site) results = results.filter((h) => h.site === flt.site);
    if (flt.newOnly) results = results.filter((h) => h.new);
    const cutoff = Date.now() - 5 * 60000;
    results = results.filter((h) => !h.ends_at || Date.parse(h.ends_at) > cutoff);
    const total = results.length;
    results = results.slice(0, this._sLimit);
    const resultSites = [...new Set(d.results.map((h) => h.site))];
    const onCount = d.macbid_buildings.filter((b) => b.on).length;
    const excl = flt.search ? (byId[flt.search]?.excluded || 0) : d.excluded_total;

    return `<div class="sv">
      <div class="sbar">
        <label>Search every day at <input type="time" data-sform="run_time" value="${esc(d.run_time)}"></label>
        <label>and again <select data-sform="refresh_hours">
          ${[[0, "no (once a day)"], [1, "every hour"], [2, "every 2 hours"], [3, "every 3 hours"], [6, "every 6 hours"], [12, "every 12 hours"]]
            .map(([v, t]) => `<option value="${v}" ${Number(d.refresh_hours || 0) === v ? "selected" : ""}>${t}</option>`).join("")}
        </select></label>
        <button data-sact="run" ${d.running || this._sbusy ? "disabled" : ""}>${d.running ? "Searching…" : "Search now"}</button>
        <span class="muted">Last run ${esc(when(d.last_run))} · next ${esc(new Date(d.next_run).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }))}</span>
      </div>
      ${this._serr ? `<div class="warn">${esc(this._serr)}</div>` : ""}
      ${Object.entries(d.errors || {}).map(([site, msg]) => `<div class="warn">${siteDot(site)} ${esc(SITE_NAMES[site] || site)}: ${esc(msg)}</div>`).join("")}

      <div class="sadd">
        <input type="text" placeholder="Keyword, e.g. greenhouse" data-sform="keyword" value="${esc(f.keyword)}">
        ${d.sites.map((site) => `<label class="chk">${siteDot(site)}<input type="checkbox" data-sform-site="${site}" ${formSites.includes(site) ? "checked" : ""}> ${esc(SITE_NAMES[site])}</label>`).join("")}
        <label class="chk">Max $ <input type="number" min="0" step="1" class="small" data-sform="max_price" value="${esc(f.max_price)}" placeholder="any"></label>
        <label class="chk" title="The sites' own searches are loose (they match descriptions and similar words)"><input type="checkbox" data-sform="title_only" ${f.title_only !== false ? "checked" : ""}> Title must contain the words</label>
        ${d.sites.includes("macbid") ? `<label class="chk"><input type="checkbox" data-sform="save_to_macbid" ${f.save_to_macbid ? "checked" : ""}> Also save to my Mac.bid alerts</label>` : ""}
        ${d.sites.includes("ola") ? `<label class="chk"><input type="checkbox" data-sform="save_to_ola" ${f.save_to_ola ? "checked" : ""}> Also save to my OLA searches</label>` : ""}
        <button data-sact="add" ${this._sbusy ? "disabled" : ""}>Add</button>
      </div>

      <div class="schips">
        <button class="schip ${!flt.search ? "on" : ""}" data-sfilter-search="">All searches <span class="count">${d.results.length}</span></button>
        ${d.searches.map((x) => `<span class="schip ${flt.search === x.id ? "on" : ""}">
          <button class="schip-main" data-sfilter-search="${esc(x.id)}" title="Searches ${esc(x.sites.map((k) => SITE_NAMES[k] || k).join(", "))}${x.max_price ? ` · max $${x.max_price}` : ""}${x.excluded ? ` · ${x.excluded} Mac.bid results outside your locations` : ""}">
            ${Object.keys(x.sources || {}).map((k) => siteDot(k, "Saved on " + (SITE_NAMES[k] || k))).join("")}
            ${esc(x.keyword)} <span class="count">${x.count}</span>${x.new ? `<span class="badge on">${x.new} new</span>` : ""}
          </button>
          <button class="schip-t ${x.title_only !== false ? "on" : ""}" data-sact="title" data-sid="${esc(x.id)}"
            title="${x.title_only !== false ? "Only items with these words in the title. Tap to also allow matches in descriptions." : "Includes the site's loose matches. Tap for title matches only."}">T</button>
          <button class="schip-x" data-sact="remove" data-sid="${esc(x.id)}" title="Delete this search${x.sources?.macbid ? " (also from your Mac.bid alerts)" : ""}">×</button>
        </span>`).join("")}
      </div>
      <div class="note">Colored dots show where a search is also saved: ${siteDot("govauctions")} GovAuctions ${siteDot("macbid")} Mac.bid ${siteDot("ebay")} eBay ${siteDot("ola")} OLA. Same keyword from two sites is one search (both dots); deleting it here deletes it on each.</div>

      ${d.sites.includes("macbid") ? `<details class="locs" ${this._locsOpen ? "open" : ""}>
        <summary>Mac.bid pickup locations: ${onCount ? `${onCount} on` : "all (no filter)"} ${d.macbid_from_account ? `<span class="muted">· from your Mac.bid account</span>` : `<span class="muted">· your own choice</span>`}</summary>
        <div class="note">Results show items at these buildings, or ones that can be transferred to them (marked "transfer needed").</div>
        <div class="locgrid">
          ${d.macbid_buildings.map((b) => `<label class="chk loc"><input type="checkbox" data-sloc="${esc(b.name)}" ${b.on ? "checked" : ""}> ${esc(b.name)}
            ${b.account ? `<span class="badge">your account</span>` : ""}${b.outpost ? `<span class="badge">outpost</span>` : ""}</label>`).join("") || `<span class="muted">Loading locations…</span>`}
        </div>
        ${d.macbid_from_account ? "" : `<button class="secondary" data-sact="use-account">Use my Mac.bid account's locations</button>`}
      </details>` : ""}

      <div class="sfilters">
        <button class="subtab ${!flt.site ? "on" : ""}" data-sfilter-site="">All sites</button>
        ${resultSites.map((site) => `<button class="subtab ${flt.site === site ? "on" : ""}" data-sfilter-site="${site}">${siteDot(site)} ${esc(SITE_NAMES[site] || site)}</button>`).join("")}
        <label class="chk"><input type="checkbox" data-sfilter-new ${flt.newOnly ? "checked" : ""}> New only</label>
        <span class="muted">${total} result${total === 1 ? "" : "s"}</span>
        ${excl && (!flt.site || flt.site === "macbid") ? `<span class="muted">Excluded ${num(excl)} Mac.bid result${excl === 1 ? "" : "s"} by location filters</span>` : ""}
      </div>

      <div class="hits">
        ${results.length ? results.map((h) => this._hitRow(h, byId)).join("") + (total > results.length
          ? `<button class="secondary more" data-sact="more">Show ${Math.min(60, total - results.length)} more (${total - results.length} left)</button>` : "") : `<div class="empty">${d.searches.length ? (d.last_run ? "No matches." : "Searches run at the time above, or tap Search now.") : "Add a keyword to start. Your Mac.bid keyword alerts are brought in automatically."}</div>`}
      </div>
    </div>`;
  }

  _hitRow(h, byId) {
    const left = h.ends_at ? (Date.parse(h.ends_at) - Date.now()) / 1000 : null;
    const where = h.transfer ? `at ${h.transfer} · transfer needed` : h.location || "";
    return `<div class="hit">
      ${safeUrl(h.image) ? `<img src="${safeUrl(h.image)}" alt="" width="64" height="64" loading="lazy" decoding="async">` : `<div class="noimg"></div>`}
      <div class="hbody">
        <div class="htitle"><button class="star ${h.following ? "on" : ""}" data-sact="star" data-site="${esc(h.site)}" data-item="${esc(h.item_id)}"
            data-on="${h.following ? "0" : "1"}" title="${h.following ? "You're following this (watching or bidding). Tap to remove it from your " + (h.site === "govauctions" ? "bookmarks" : "watchlist") : "Add to your " + (h.site === "govauctions" ? "GovAuctions bookmarks" : SITE_NAMES[h.site] + " watchlist") + ": it then shows on the Watching tab"}">${h.following ? "★" : "☆"}</button>
          ${siteDot(h.site)} ${h.new ? `<span class="badge on">NEW</span>` : ""}
          <a href="${safeUrl(h.url)}" target="_blank" rel="noreferrer">${esc(h.title)}</a></div>
        <div class="hmeta">
          <b>${money(h.price)}</b>
          ${left !== null && left > 0 ? `<span>ends in ${fmtDuration(left)}</span>` : ""}
          ${h.bids !== null && h.bids !== undefined ? `<span>${num(h.bids)} bid${Number(h.bids) === 1 ? "" : "s"}</span>` : ""}
          ${where ? `<span>${esc(where)}</span>` : ""}
          ${h.condition ? `<span class="muted">${esc(h.condition)}</span>` : ""}
        </div>
        <div class="hkw">${h.searches.map((id) => `<span class="kw">${esc(byId[id]?.keyword || "")}</span>`).join("")}</div>
      </div>
    </div>`;
  }

  _bindSearches(root) {
    const f = this._sform;
    root.querySelectorAll("[data-sform]").forEach((el) => {
      const k = el.dataset.sform;
      if (k === "run_time") {
        el.addEventListener("change", () => this._searchChange({ action: "settings", run_time: el.value }));
        return;
      }
      if (k === "refresh_hours") {
        el.addEventListener("change", () => this._searchChange({ action: "settings", refresh_hours: Number(el.value) }));
        return;
      }
      el.addEventListener(el.type === "checkbox" ? "change" : "input", () => { f[k] = el.type === "checkbox" ? el.checked : el.value; });
      el.addEventListener("blur", () => { if (this._pending) setTimeout(() => this._maybeRender(), 0); });
      if (k === "keyword") el.addEventListener("keydown", (ev) => { if (ev.key === "Enter") root.querySelector("[data-sact=add]")?.click(); });
    });
    root.querySelectorAll("[data-sform-site]").forEach((el) => el.addEventListener("change", () => {
      const cur = new Set(f.sites || this._sd.sites);
      el.checked ? cur.add(el.dataset.sformSite) : cur.delete(el.dataset.sformSite);
      f.sites = [...cur];
    }));
    root.querySelectorAll("[data-sact]").forEach((b) => b.addEventListener("click", async () => {
      const act = b.dataset.sact;
      if (act === "run") return this._searchChange({ action: "run" });
      if (act === "more") { this._sLimit += 60; return this._maybeRender(true); }
      if (act === "star") {
        b.disabled = true; b.textContent = "…";
        return this._searchChange({ action: "watch", site: b.dataset.site, item_id: b.dataset.item, on: b.dataset.on === "1" });
      }
      if (act === "title") {
        const x = this._sd.searches.find((y) => y.id === b.dataset.sid);
        if (x) return this._searchChange({ action: "update", search_id: x.id, title_only: x.title_only === false });
      }
      if (act === "use-account") return this._searchChange({ action: "settings", use_account: true });
      if (act === "remove") {
        const x = this._sd.searches.find((y) => y.id === b.dataset.sid);
        if (!x || !confirm(`Delete the saved search "${x.keyword}"?` + (x.sources?.macbid ? "\nIt's also removed from your Mac.bid alerts." : ""))) return;
        if (this._sfilter.search === x.id) this._sfilter.search = null;
        return this._searchChange({ action: "remove", search_id: x.id });
      }
      if (act === "add") {
        const keyword = (f.keyword || "").trim();
        if (!keyword) {
          this._serr = "Type a keyword first";
          setTimeout(() => { if (this._serr === "Type a keyword first") { this._serr = null; this._maybeRender(true); } }, 4000);
          return this._maybeRender(true);
        }
        const payload = { action: "add", keyword, sites: f.sites || this._sd.sites, save_to_macbid: !!f.save_to_macbid, save_to_ola: !!f.save_to_ola,
          title_only: f.title_only !== false };
        if (f.max_price !== "" && !isNaN(Number(f.max_price))) payload.max_price = Number(f.max_price);
        this._sform = { keyword: "", max_price: "", sites: null, save_to_macbid: false, save_to_ola: false, title_only: true };
        await this._searchChange(payload);
        setTimeout(() => this._loadSearches(), 4000);
      }
    }));
    root.querySelectorAll("[data-sloc]").forEach((el) => el.addEventListener("change", () => {
      this._locsOpen = true;
      this._searchChange({ action: "settings", building: el.dataset.sloc, on: el.checked });
    }));
    root.querySelector("details.locs")?.addEventListener("toggle", (ev) => { this._locsOpen = ev.target.open; });
    root.querySelectorAll("[data-sfilter-search]").forEach((b) => b.addEventListener("click", () => {
      this._sfilter.search = b.dataset.sfilterSearch || null; this._sLimit = 60; this._maybeRender(true);
    }));
    root.querySelectorAll("[data-sfilter-site]").forEach((b) => b.addEventListener("click", () => {
      this._sfilter.site = b.dataset.sfilterSite || null; this._sLimit = 60; this._maybeRender(true);
    }));
    root.querySelector("[data-sfilter-new]")?.addEventListener("change", (ev) => {
      this._sfilter.newOnly = ev.target.checked; this._sLimit = 60; this._maybeRender(true);
    });
  }

  _safeCard(a) {
    try {
      return this._card(a);
    } catch (err) {
      return `<div class="card"><b>${esc(a.title || a.entity_id)}</b><div class="note">Couldn't show this auction: ${esc(err?.message || err)}</div></div>`;
    }
  }

  _clock(a) {
    // Real deadlines tick down. A penny auction's short clock is reset by every
    // bid, which we only see at the next check, so it's shown "as of" that check.
    if (a.status === "ended" && a.remove_after) return { label: "Leaves page in", deadline: Number(a.remove_after) * 1000 };
    if (a.status === "awaiting_pickup") return { label: "Pick up within", deadline: a.ends_at ? Date.parse(a.ends_at) : null };
    if (a.status === "upcoming" && a.starts_at) return { label: "Starts in", deadline: Date.parse(a.starts_at) };
    if (a.ends_at) return { label: a.status === "ended" ? "Ended" : "Ends in", deadline: Date.parse(a.ends_at) };
    if (a.status === "live" && a.clock_seconds !== null && a.clock_seconds !== undefined) {
      const clock = Number(a.clock_seconds);
      if (clock > SHORT_CLOCK) return { label: "First countdown", deadline: a.updated + clock * 1000 };
      return { label: "Clock", asOf: a.updated, value: clock };
    }
    return { label: "Clock" };
  }

  _card(a) {
    if (a.site === "macbid" || a.site === "ebay" || a.site === "ola") return this._macbidCard(a);
    const id = esc(a.auction_id);
    const c = this._clock(a);
    const img = safeUrl(a.image);
    const url = safeUrl(a.url);
    const notes = a.label_notes || {};
    const labels = (a.labels || []).map((l) =>
      `<div class="label"><span class="tag">${esc(l)}</span>${notes[l] ? ` <span class="muted">${esc(notes[l])}</span>` : ""}</div>`).join("");
    const bidders = (a.recent_bidders || []).map(esc).join(", ");
    const active = a.active_bidders === null || a.active_bidders === undefined ? "–" : `${a.active_bidders}${a.active_bidders >= 9 ? "+" : ""}`;
    const status = a.unavailable ? "unavailable" : a.status;
    const limitLabel = LIMIT_LABEL[a.site] || "Bid limit";
    return `
      <div class="card ${a.opportunity ? "opp" : ""} ${a.bid_limit_reached ? "capped" : ""}">
        <div class="top">
          ${img ? `<img src="${img}" alt="" loading="lazy">` : `<div class="noimg">🛒</div>`}
          <div class="head">
            <a class="name" href="${url}" target="_blank" rel="noopener">${esc(a.title)}</a>
            ${this._stopButton(a)}
            ${this._watchButton(a)}
            <div class="sub">${esc(SITE_NAMES[a.site] || a.site)} · <span class="status ${esc(status)}">${esc(status)}</span>
              ${a.opportunity ? `<span class="badge hot">🎯 few bidders</span>` : ""}
                ${a.gov_phase_label ? `<span class="badge ${a.gov_war ? "hot" : "on"}">${esc(a.gov_phase_label)}</span>` : ""}
                ${a.gov_best_bet ? `<span class="badge on">${esc(a.gov_best_bet)}</span>` : ""}
                ${a.gov_collapse ? `<span class="badge cap">Collapse</span>` : ""}
                ${a.bid_limit_reached ? `<span class="badge cap">⛔ max bids reached</span>` : ""}
                ${a.closed_to_new && !a.your_bids ? `<span class="badge">closed to new bidders</span>` : ""}
                ${a.dropped ? `<span class="badge">no longer followed</span>` : ""}
              </div>
          </div>
        </div>
        ${labels ? `<div class="labels">${labels}</div>` : ""}
        ${a.gov_collapse_detail ? `<div class="note">${esc(a.gov_collapse_detail)}</div>` : ""}
        <div class="big">
          <div><div class="k">Price</div><div class="v">${money(a.price)}</div></div>
          <div><div class="k">${c.label}</div>
            ${c.asOf
              ? `<div class="v penny" data-from="${c.value}" data-since="${c.asOf}">${c.value}s</div><div class="k ago" data-since="${c.asOf}"></div>`
              : `<div class="v timer" data-deadline="${c.deadline ?? ""}">–</div>`}</div>
          <div><div class="k">Other bidders (${esc(a.active_window_minutes || 1)} min)</div><div class="v">${active}</div>
            ${a.you_are_bidding ? `<div class="k">+ you</div>` : ""}</div>
        </div>
        ${this._overtimeLine(a)}
        ${(a.status === "live" || a.status === "upcoming") && (!a.closed_to_new || a.your_bids) && !a.bid_limit_reached
          ? `<div class="quick"><button type="button" class="secondary" data-act="quick" data-id="${id}"
              title="Place one bid now (1 point). Staking a claim before the entry limit lets you come back later${(a.labels || []).some((l) => /re-?entry/i.test(l)) ? "; this one is No Re-Entry" : ""}.">+1 bid</button>
              <span class="muted">${a.your_bids ? `you've placed ${num(a.your_bids)}` : "stake a claim before the entry limit"}</span></div>` : ""}
        ${bidders ? `<div class="line"><span class="k">Bidding:</span> ${bidders}</div>` : ""}
        <div class="rows">
          <div><span class="k">Your bids</span> ${num(a.your_bids)}${a.bid_limit ? ` / ${num(a.bid_limit)}` : ""} <span class="muted">(${money(a.your_points_cost)})</span></div>
          <div><span class="k">Booked left</span> ${num(a.your_booked_left)}</div>
          <div><span class="k">If you won now</span> ${money(a.cost_if_won_now)} <span class="muted">(pay ${money(a.pay_if_won)})</span>
            ${a.bundle_points ? `<br><span class="muted">minus ${num(a.bundle_points)} bonus pts (${money(a.bundle_value)}) = <b>${money(a.net_cost_if_won_now)}</b></span>` : ""}</div>
          <div><span class="k">Buy It Now</span> ${money(a.buy_it_now)}</div>
          ${a.no_jumper_limit ? `<div><span class="k">Entry limit</span> ${money(a.no_jumper_limit)}</div>` : ""}
          <div><span class="k">Leader</span> ${esc(a.leader || "–")}</div>
        </div>
        ${a.history_wins ? `
          <div class="hist"><span class="k">Past ${num(a.history_wins)} wins${a.history_period_days ? ` (${a.history_period_days} days)` : ""}:</span>
            median ${money(a.history_median_final_price)}, winners used ~${num(a.history_median_winner_bids)} bids${a.history_avg_winner_cost ? `, avg cost ${money(a.history_avg_winner_cost)}` : ""}</div>` : ""}
        ${a.gov_chase_summary ? `
          <div class="hist"><span class="k">Chase verdict</span>
            <span class="badge ${a.gov_chase_verdict === "green" ? "on" : a.gov_chase_verdict === "yellow" ? "hot" : a.gov_chase_verdict === "red" ? "cap" : ""}">${esc(a.gov_chase_label || a.gov_chase_verdict || "Review")}</span>
            ${esc(a.gov_chase_summary)}${a.suggested_cap ? ` · suggested cap ${money(a.suggested_cap)}${a.suggested_cap_cover !== undefined && a.suggested_cap_cover !== null ? ` (${Math.round(a.suggested_cap_cover * 100)}% of wins under it)` : ""}` : ""}</div>` : ""}
        <div class="limits">
          <div class="lim-row">
            <label>${esc(limitLabel)}
              <input data-id="${id}" data-field="limit" type="number" min="0" step="1" inputmode="numeric"
                value="${esc(this._val(a.auction_id, "limit", a.bid_limit || ""))}" placeholder="none"></label>
            <button type="button" data-act="limit" data-id="${id}">Save</button>
            <span class="saved">${esc(this._saved["limit:" + a.auction_id] || "")}</span>
          </div>
          <div class="note">The most bids you'll spend here in total, including the ${num(a.your_bids)} already placed.</div>
          <div class="lim-row">
            <label>Max price
              <input data-id="${id}" data-field="max_price" type="number" min="0" step="0.01" inputmode="decimal"
                value="${esc(this._val(a.auction_id, "max_price", a.max_price || ""))}" placeholder="none"></label>
            <button type="button" data-act="max_price" data-id="${id}">Save</button>
            <span class="saved">${esc(this._saved["max_price:" + a.auction_id] || "")}</span>
          </div>
          <div class="note">Your booked bids stop once the auction price passes this.${this._isOneCent(a) ? " On a 1¢ auction the price doesn't change what you pay." : ""}</div>
          <div class="total" data-total="${id}">${this._totalText(a)}</div>
        </div>
        ${a.status === "ended" ? "" : this._bookingControls(a)}
        ${"proxy" in a && (a.status === "live" || a.status === "upcoming" || a.proxy) ? this._proxyBlock(a, id, String(a.auction_id)) : ""}
      </div>`;
  }

  // ---------------------------------------------------------------- point packs

  _packGroups() {
    const data = this._packs();
    if (!data) return [];
    // One card per pack size (product); its live/upcoming auctions listed inside.
    // Rows arrive best-first, so a group's first row is its best auction.
    const groups = new Map();
    for (const p of data.list) {
      const k = String(p.product_id || p.title);
      if (!groups.has(k)) groups.set(k, { head: p, auctions: [] });
      groups.get(k).auctions.push(p);
    }
    return [...groups.values()];
  }

  _packsView() {
    const data = this._packs();
    if (!data) return `<div class="empty">Turn on the point-pack check in the GovAuctions settings.</div>`;
    const blockLabel = { night: "midnight–6 AM", morning: "6 AM–noon", afternoon: "noon–6 PM", evening: "6 PM–midnight" };
    const gradeText = { green: "🟢 Good", yellow: "🟡 Fair", red: "🔴 Poor", unknown: "⚪ Not enough history" };
    const scanned = data.scanned ? new Date(data.scanned).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "not yet";
    const intro = `<details class="packs-intro note"><summary>The site's Packs list, one card per pack size, checked hourly (last ${esc(scanned)}). How to read this</summary>
      Break-even = the pack size in bids (1¢ auctions). <b>Good</b> means past winners of that pack usually needed
      ≤35% of its size around this time of day; <b>Poor</b>, more than 60%.
      <b>Suggested max</b> covers most past wins but never more than half the pack, so a win at it always pays.
      <b>Stars</b> = how cheaply it usually goes at that auction's time of day (★★★ winners needed ≤35% of the pack, ★★ ≤45%, ★ ≤60%).
      <b>Entry</b>: past the entry limit only people already bidding can continue, so those auctions are hidden unless you're in them.
      Past results are an upper bound on your chances: you still have to outlast the others. When you join a pack, its Max bids is set to the suggestion unless you set one.</details>`;
    if (!data.list.length) return intro + `<div class="empty">No point packs listed right now.</div>`;
    const visible = this._packGroups()
      .map((g) => ({ ...g, auctions: g.auctions.filter((p) => this._showLocked || p.open || p.your_bids) }))
      .filter((g) => g.auctions.length);
    const lockedN = data.list.filter((p) => !p.open && !p.your_bids).length;
    const toggle = lockedN ? `<div class="packs-more"><button class="secondary" data-action="packs-locked">${this._showLocked ? "Hide" : "Show"} ${lockedN} auction${lockedN === 1 ? "" : "s"} you can't join anymore</button></div>` : "";
    return intro + toggle + (visible.length ? "" : `<div class="empty">No packs open to new bidders right now.</div>`) + visible.map(({ head: p, auctions }) => {
      const blocks = Object.entries(p.blocks || {}).map(([k, v]) => `${esc(blockLabel[k] || k)}: ~${num(v.median)} bids (${v.wins})`).join(" · ");
      const rows = auctions.map((a) => {
        const start = a.starts_at ? Date.parse(a.starts_at) : null;
        // GovAuctions: a live auction first runs a long countdown (bidding already open), then the short
        // per-bid clock where booked bids take turns. "unixStarts" is when that first countdown ends.
        const bidding = a.bidders !== null && a.bidders !== undefined ? ` · ${num(a.bidders)} bidding` : "";
        const when = a.live && start && start > Date.now()
          ? `<b>live</b> · first countdown <span class="timer" data-deadline="${start}">–</span>${bidding}`
          : a.live
            ? `<b>live</b>${a.clock !== null && a.clock !== undefined ? ` · clock ${a.clock}s` : ""}${bidding}`
            : start ? `starts in <span class="timer" data-deadline="${start}">–</span>` : "";
        const following = this._auctions().some((x) => x.site === "govauctions" && String(x.auction_id) === String(a.id));
        const follow = following ? `<span class="badge on">following</span>`
          : (a.open || a.your_bids) ? `<button type="button" class="secondary small-btn" data-act="pack_follow" data-id="${esc(a.id)}"
              data-limit="${esc(p.suggested_max || "")}" title="Bookmark it on GovAuctions so it gets a full card (Book, auto-booking, limits)${p.suggested_max ? ` and set its Max bids to ${p.suggested_max}` : ""}">Follow</button>` : "";
        const entry = a.open ? (a.bids_to_lock !== null && a.bids_to_lock !== undefined ? `entry open ~${num(a.bids_to_lock)} more bids` : "entry open") : `<span class="warn">entry closed</span>`;
        const stars = a.stars !== null && a.stars !== undefined ? `<span class="stars" title="How cheaply it usually goes at this auction's time of day">${a.stars ? "★".repeat(a.stars) + "☆".repeat(3 - a.stars) : "skip"}</span>` : "";
        return `<div class="pack-auction"><a href="${safeUrl(a.url)}" target="_blank" rel="noopener">${a.price !== null && a.price !== undefined ? money(a.price) : "–"}</a>
          <span>${when}</span><span class="muted">${entry}</span>${a.your_bids ? `<span class="badge on">you: ${num(a.your_bids)} bids</span>` : ""}${follow}${stars}</div>`;
      }).join("");
      return `<div class="card pack ${esc(p.grade)}">
        <div class="pack-head">
          <a class="name" href="${safeUrl(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a>
          <span class="badge grade-${esc(p.grade)}">${gradeText[p.grade] || p.grade}</span>
        </div>
        <div class="big">
          <div><div class="k">Worth</div><div class="v">${money(p.value)}</div><div class="k">${num(p.points)} pts</div></div>
          <div><div class="k">Winners usually used</div><div class="v">${p.expected_bids && p.grade !== "unknown" ? "~" + num(p.expected_bids) : "–"}</div>
            <div class="k">${p.grade !== "unknown" && p.ratio !== null && p.ratio !== undefined ? Math.round(p.ratio * 100) + "% of break-even" : p.past_wins ? `only ${num(p.past_wins)} past wins` : "no past wins on record"}</div></div>
          <div><div class="k">Entry limit</div><div class="v small">${money(p.entry_limit)}</div><div class="k">${auctions.length} auction${auctions.length === 1 ? "" : "s"}</div></div>
        </div>
        <div class="pack-auctions">${rows}</div>
        ${p.suggested_max ? `<div class="suggest"><b>Suggested max: ${num(p.suggested_max)} bids (${money(p.max_cost)})</b>
          <span class="muted">· ${Math.round((p.cover || 0) * 100)}% of past winners needed no more ·
          a win at the max nets +${num(p.net_win)} points${p.bid_limit ? ` · your max: ${num(p.bid_limit)}` : ""}</span>
          ${!p.affordable && p.balance !== null && p.balance !== undefined ? `<br><b class="warn">Needs ${num(p.suggested_max)} bids, you have ${num(p.balance)}: you'd likely run out before winning.</b>` : ""}
          ${p.stars === 0 ? `<br><span class="muted">At this time of day winners of this pack usually need more than 60% of it. Skip it.</span>` : ""}</div>` : ""}
        ${p.past_wins ? `<details class="hist"><summary><span class="k">Past ${num(p.past_wins)} wins:</span> median ~${num(p.median_bids)} bids,
          ${Math.round((p.lost_money || 0) * 100)}% of winners still lost points. Cheapest: ${esc(blockLabel[p.cheapest_block] || "–")}.</summary>
          <span class="muted">${blocks}</span></details>` : ""}
      </div>`;
    }).join("");
  }

  // ---------------------------------------------------------------- Mac.bid cards

  _macbidCard(a) {
    const id = esc(a.auction_id);
    const c = this._clock(a);
    const img = safeUrl(a.image);
    const url = safeUrl(a.url);
    const pickup = a.status === "awaiting_pickup";
    const status = a.unavailable ? "unavailable" : pickup ? "awaiting pickup" : a.status;
    let badge = "";
    if (pickup) badge = a.overdue ? `<span class="badge cap">⛔ pickup overdue</span>` : `<span class="badge">${a.paid ? "paid" : "not paid yet"}</span>`;
    else if (a.winning) badge = `<span class="badge on">✅ you're winning</span>`;
    else if (a.you_are_bidding && a.status === "live") badge = `<span class="badge hot">⚠️ outbid</span>`;
    if (a.in_extension) badge += ` <span class="badge">⏱ extended</span>`;
    if (a.dropped) badge += ` <span class="badge">no longer followed</span>`;
    if (a.status === "ended" && !a.dropped) badge += a.winning ? ` <span class="badge on">🏆 you won</span>` : "";
    if (a.bid_limit && Number(a.price) > Number(a.bid_limit) && !a.winning && !pickup) badge += ` <span class="badge cap">past your max</span>`;
    const head = `
        <div class="top">
          ${img ? `<img src="${img}" alt="" loading="lazy">` : `<div class="noimg">🛒</div>`}
          <div class="head">
            <a class="name" href="${url}" target="_blank" rel="noopener">${esc(a.title)}</a>
            ${this._stopButton(a)}
            ${this._watchButton(a)}
            <div class="sub">${esc(SITE_NAMES[a.site] || a.site)} · <span class="status ${esc(a.status)}">${esc(status)}</span> ${badge}</div>
            ${(a.labels || []).length ? `<div class="labels">${a.labels.map((l) => `<span class="tag cond-label ${esc(String(l).toLowerCase().replace(/\s+/g, "-"))}">${esc(l)}</span>`).join(" ")}</div>` : ""}
          </div>
        </div>
        ${this._conditionBlock(a)}`;
    const clock = `<div><div class="k">${c.label}</div><div class="v timer" data-deadline="${c.deadline ?? ""}">–</div></div>`;
    if (pickup) {
      return `<div class="card ${a.overdue ? "capped" : ""}">${head}
        <div class="big">
          <div><div class="k">Won for</div><div class="v">${money(a.price)}</div></div>
          ${clock}
          <div><div class="k">Lot</div><div class="v small">${esc(a.lot_number || "–")}</div></div>
        </div>
        <div class="note">Pickup deadline: ${a.ends_at ? esc(new Date(a.ends_at).toLocaleString()) : "–"}.
          ${a.paid ? "" : "Pay the invoice before picking up."} <a href="${url}" target="_blank" rel="noopener">Open awaiting pickup</a></div>
      </div>`;
    }
    const limit = this._val(a.auction_id, "limit", a.bid_limit || "");
    return `<div class="card ${a.winning ? "opp" : ""}">${head}
        <div class="big">
          <div><div class="k">Current bid</div><div class="v">${money(a.price)}</div></div>
          ${clock}
          <div><div class="k">Other bidders</div><div class="v">${a.active_bidders ?? "–"}</div>
            ${a.you_are_bidding ? `<div class="k">+ you</div>` : ""}</div>
        </div>
        ${this._overtimeLine(a)}
        <div class="rows">
          ${a.site === "ebay" || a.site === "ola"
            ? `<div><span class="k">Your max bid</span> ${a.your_max_bid ? money(a.your_max_bid) : "–"}</div>
               <div><span class="k">Shipping</span> ${a.shipping === 0 ? "free" : money(a.shipping)}</div>`
            : `<div><span class="k">If you win now</span> ${money(a.cost_if_won_now)} <span class="muted">(+15% + $3, before tax)</span></div>
               <div><span class="k">Retail</span> ${money(a.retail_price)}</div>`}
          <div><span class="k">Buy now</span> ${money(a.buy_it_now)}</div>
          <div><span class="k">Bids · watchers</span> ${num(a.total_bids)} · ${num(a.watchers)}</div>
        </div>
        <div class="limits">
          <div class="lim-row">
            <label>${esc(LIMIT_LABEL[a.site] || "Max bid ($)")}
              ${a.site === "macbid" && a.status === "live" ? (() => {
                const opts = mbValidBids(a.price);
                const cur = Number(limit) || 0;
                if (cur && !opts.includes(cur)) opts.unshift(cur);
                return `<select data-id="${id}" data-field="limit">
                  <option value="" ${cur ? "" : "selected"}>none</option>
                  ${opts.map((v) => `<option value="${v}" ${v === cur ? "selected" : ""}>$${num(v)}${v <= (a.price || 0) ? " (below the current bid)" : ""}</option>`).join("")}
                </select>`;
              })() : `<input data-id="${id}" data-field="limit" type="number" min="0" step="1" inputmode="numeric"
                value="${esc(limit)}" placeholder="none">`}</label>
            ${a.site === "macbid" && a.status === "live"
              ? `<button type="button" class="bid-now" data-act="mb_place" data-id="${id}">Bid now</button>
                 ${!a.winning && a.min_next_bid ? `<button type="button" class="secondary" data-act="quick" data-id="${id}"
                    title="Place the smallest bid Mac.bid accepts right now (checked live when you tap)">Bid ${money(a.min_next_bid)} (next step)</button>` : ""}
                 ${a.winning && a.price && Number(a.mb_placed || a.bid_limit || 0) > Number(a.price)
                   ? `<button type="button" class="secondary" data-act="mb_lower" data-id="${id}"
                        title="You're winning: pull your max back to the current price (Mac.bid allows this while you lead)">Lower my max to ${money(a.price)}</button>` : ""}
                 <button type="button" class="secondary" data-act="limit" data-id="${id}" title="Save the max without bidding (for alerts and late auto-bids)">Save</button>`
              : a.site === "ola" && a.status === "live"
              ? `<button type="button" class="bid-now" data-act="ola_place" data-id="${id}">Bid now</button>
                 ${!a.winning && a.min_next_bid ? `<button type="button" class="secondary" data-act="quick" data-id="${id}"
                    title="Place the smallest bid OLA accepts right now (checked live when you tap)">Bid ${money(a.min_next_bid)} (next)</button>` : ""}
                 <button type="button" class="secondary" data-act="limit" data-id="${id}" title="Save the max without bidding (for alerts)">Save</button>`
              : `<button type="button" data-act="limit" data-id="${id}">Save</button>`}
            <span class="saved">${esc(this._saved["limit:" + a.auction_id] || this._saved["mb:" + a.auction_id] || "")}</span>
          </div>
          ${a.site === "macbid" && a.status === "live" ? `<div class="note">Minimum next bid: <b>${money(a.min_next_bid)}</b>. The list shows the same amounts Mac.bid's website offers
              ($1 steps to $100, $5 to $500, $10 to $1,000, then $50).
            Your max is the most you'll pay; Mac.bid raises your bid for you up to it.
            ${a.assurance_cost ? (a.has_assurance ? ` · ✅ Buyer's Assurance added` : ` · Buyer's Assurance ${money(a.assurance_cost)}: you'll be asked when you bid.`) : ""}</div>` : ""}
          ${a.site === "ola" && a.status === "live" && a.min_next_bid ? `<div class="note">Minimum next bid: <b>${money(a.min_next_bid)}</b> (OLA bids in whole dollars). Your max is the most you'll pay; OLA raises your bid for you up to it. Bids can't be cancelled, only raised.</div>` : ""}
          <div class="total" data-total="${id}">${this._totalText(a)}</div>
          ${a.site === "macbid" || a.site === "ola" ? "" : `<div class="note">You get an alert when the price passes this. Bid on ${esc(SITE_NAMES[a.site] || a.site)} itself; this doesn't place bids.</div>`}
        </div>
        ${a.site === "macbid" ? this._macbidExtras(a) : ""}
      </div>`;
  }

  // Colored TESTED / DAMAGED / PARTIAL boxes (as on the item page) + what the condition means
  _conditionBlock(a) {
    if (a.site !== "macbid") return "";
    const notes = (a.condition_notes || []).map((n) => `<div class="cond ${esc(n.kind)}">
        <span class="cond-tag">${esc(n.label)}</span>${n.note ? `<div>${esc(n.note)}</div>` : ""}</div>`).join("");
    const explained = a.condition_explained
      ? `<div class="note"><b>${esc((a.labels || [])[0] || "Condition")}:</b> ${esc(a.condition_explained)}</div>` : "";
    return notes || explained ? `<div class="cond-block">${notes}${explained}</div>` : "";
  }

  _macbidExtras(a) {
    const id = esc(a.auction_id);
    const aid = a.auction_id;
    const desc = a.description ? `<details class="sub-menu"><summary>Description</summary><div class="pre">${esc(a.description)}</div></details>` : "";
    const loc = a.location ? `<div class="line">📍 <b>${esc(a.location)}</b>${a.location_city ? ` · ${esc(a.location_city)}` : ""}
        ${a.location_address ? `<span class="muted"> · ${esc(a.location_address)}</span>` : ""}</div>` : "";
    const hours = a.location_hours ? `<details class="sub-menu"><summary>Pickup hours</summary><div class="pre">${esc(a.location_hours)}</div></details>` : "";
    const transfers = a.location ? `<details class="sub-menu"><summary>Transfers: ${a.transferable && (a.transfer_to || []).length
        ? `can move to ${a.transfer_to.length} location${a.transfer_to.length === 1 ? "" : "s"}` : "not available"}</summary>
        ${a.transferable && (a.transfer_to || []).length
          ? `<ul>${a.transfer_to.map((t) => `<li>${esc(t)}</li>`).join("")}</ul><div class="note">Set up a transfer on Mac.bid after you win.
              $10 each (free to pickup outposts and for Buyers Club members); it can take up to 7 days to arrive,
              then you have 7 days to pick it up.</div>`
          : `<div class="note">Pick up at ${esc(a.location)}. You can also pay Mac.bid to hold it 7 more days.</div>`}</details>` : "";
    const pickupRule = a.status === "live" && a.location
      ? `<div class="note">If you win: pick up within <b>3 business days</b> at ${esc(a.location)}, or transfer / pay to hold it.</div>` : "";
    if (a.status === "ended" || a.status === "awaiting_pickup") return desc + loc + hours + transfers;
    const late = String(this._val(aid, "mb_late", a.mb_mode === "late")) === "true";
    const lateMin = this._val(aid, "mb_late_min", Math.round((a.mb_late_seconds || 180) / 60));
    const assure = String(this._val(aid, "mb_assure", !!a.mb_assurance)) === "true";
    return `${desc}${loc}${pickupRule}${hours}${transfers}
      <details class="booking" ${late || this._open[aid] ? "open" : ""} data-id="${id}">
        <summary>Auto-bid ${late ? `<span class="badge on">late bid set</span>` : ""}${a.mb_placed ? ` <span class="badge">your max $${num(a.mb_placed)}</span>` : ""}</summary>
        <label class="bk-check"><input data-id="${id}" data-field="mb_late" type="checkbox" ${late ? "checked" : ""}>
          Place my max automatically
          <input data-id="${id}" data-field="mb_late_min" type="number" min="1" max="60" step="1" value="${esc(lateMin)}"> min before the end</label>
        ${a.assurance_cost ? `<label class="bk-check"><input data-id="${id}" data-field="mb_assure" type="checkbox" ${assure ? "checked" : ""}>
          Add Buyer's Assurance (${money(a.assurance_cost)}) when it bids</label>` : ""}
        <div class="bk-row">
          <button type="button" data-act="mb_auto" data-id="${id}">Save auto-bid</button>
        </div>
        <div class="note">Keeps your max hidden until near the end. Mac.bid's 2-minute extension still gives others a chance to answer.
          ${a.mb_status ? `<br><b>Status:</b> ${esc(a.mb_status)}` : ""}</div>
      </details>
      ${"proxy" in a && (a.status === "live" || a.status === "upcoming" || a.proxy) ? this._proxyBlock(a, id, aid) : ""}`;
  }

  _proxyBlock(a, id, aid) {
    const p = a.proxy;
    const people = this._pv?.people || [];
    if (!this._pv && !this._pvLoading) this._loadProxy();
    const canBid = a.site === "macbid" || a.site === "govauctions";
    const gov = a.site === "govauctions";
    const amt = (n) => (gov ? `${num(n)} bids` : `$${num(n)}`);
    const wish = p && p.mode === "watch" && p.state === "watching" && p.wants ? p.wants : 0;
    const mode = this._val(aid, "proxy_mode", wish && canBid ? "bid" : p?.mode || (canBid ? "bid" : "watch"));
    const stateText = {
      watching: "gets updates on this item (nothing is bid)",
      asked: "asked; waiting for their max",
      confirming: "they named a max; waiting for them to tap \"I agree to pay\"",
      confirmed: "agreed",
      declined: "said no thanks",
      cancelled: "cancelled",
    };
    const open = p && !p.stopped && ["asked", "confirming", "confirmed", "watching"].includes(p.state);
    const wishLine = wish ? `<div class="line warn">🙋 ${esc(p.person)} wants you to bid up to ${amt(wish)}${p.wants_price ? `, max price ${money(p.wants_price)}` : ""}.
      ${canBid ? "Set your max below and tap \"Ask them\": they'll go straight to \"I agree to pay\"." : "eBay can't be bid automatically: place it on eBay yourself."}</div>` : "";
    const head = p ? `<span class="badge ${p.state === "confirmed" || p.state === "watching" ? "on" : open ? "hot" : ""}">${p.mode === "watch" ? "👀 " : "for "}${esc(p.person)}${p.bid ? ` · ${amt(p.bid)} max` : ""}</span>` : "";
    return `<details class="booking proxy" ${open || wish || this._open["proxy:" + aid] ? "open" : ""} data-id="proxy:${id}">
      <summary>${canBid ? "Bid for someone" : "Keep someone posted"} ${head}</summary>
      ${wishLine}
      ${p ? (() => {
        const known = people.some((x) => x.id === p.person_id);
        return `<div class="bk-row">
          ${known ? "" : `<span class="warn">This was for someone no longer on your list.</span>
            ${people.length ? `<select data-id="${id}" data-field="proxy_reassign">${people.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("")}</select>
            <button type="button" class="secondary" data-act="proxy_reassign" data-id="${id}" data-site="${esc(a.site)}">Reassign</button>` : ""}`}
          ${!open || !known ? `<button type="button" class="secondary" data-act="proxy_forget" data-id="${id}" data-site="${esc(a.site)}"
            title="Remove this request record (no message is sent)">Forget</button>` : ""}</div>`;
      })() : ""}
      ${p ? `<div class="line"><b>${esc(p.person)}</b>: ${esc(p.stopped ? "stopped; the max bid already placed stays (Mac.bid bids can't be withdrawn)" : stateText[p.state] || p.state)}</div>
        ${p.mode === "watch" ? "" : `<div class="line"><span class="k">Your max</span> ${amt(p.hard_max)}
          ${p.their_max ? ` · <span class="k">they offered</span> ${amt(p.their_max)}` : ""}
          ${p.agreed_max ? ` · <span class="k">agreed to pay</span> ${gov
            ? `for up to ${num(p.agreed_max)} bids (${money(p.agreed_max * (p.point_cost || 0.13))}, win or lose)${p.agreed_price ? `, max price ${money(p.agreed_price)}` : ""}`
            : `up to $${num(p.agreed_max)}${p.estimate ? ` (≈ ${money(p.estimate)} with fees)` : ""}`}` : ""}
          ${p.bid && !gov ? ` · <b>bid $${num(p.bid)}</b>` : ""}
          ${gov && p.agreed_max ? ` · <b>${num(Math.max(0, Math.min(p.agreed_max, (a.your_bids || 0) - (p.start_bids || 0))))} of theirs used</b>` : ""}</div>`}
        <div class="note">${(p.log || []).map(([t, txt]) => `${esc(new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }))}: ${esc(txt)}`).join("<br>")}</div>` : ""}
      ${a.status === "live" || a.status === "upcoming" ? `<div class="bk-row">
        ${canBid ? `<select data-id="${id}" data-field="proxy_mode">
          <option value="bid" ${mode === "bid" ? "selected" : ""}>Bid for them (they confirm)</option>
          <option value="watch" ${mode === "watch" ? "selected" : ""}>Just keep them posted</option></select>` : ""}
        <select data-id="${id}" data-field="proxy_person">${(() => {
          const known = people.some((x) => x.id === p?.person_id) ? p.person_id : people[0]?.id;
          return people.map((x) => `<option value="${esc(x.id)}" ${this._val(aid, "proxy_person", known) === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("");
        })()}</select>
        ${mode === "bid" && !gov ? `<label>my max <select data-id="${id}" data-field="proxy_max">
          ${(() => { const cur = Number(this._val(aid, "proxy_max", p?.hard_max || wish || "")) || 0; const o = mbValidBids(a.price, 80);
            if (cur && !o.includes(cur)) o.unshift(cur);
            return `<option value="">choose</option>` + o.map((v) => `<option value="${v}" ${v === cur ? "selected" : ""}>$${num(v)}</option>`).join(""); })()}
          </select></label>` : ""}
        ${mode === "bid" && gov ? `<label>my max bids<input data-id="${id}" data-field="proxy_max" type="number" min="1" step="1" class="small"
          value="${esc(this._val(aid, "proxy_max", p?.hard_max || wish || ""))}"></label>` : ""}
        <button type="button" data-act="proxy_ask" data-id="${id}" data-site="${esc(a.site)}" ${people.length ? "" : "disabled"}>${
          mode === "watch" ? (p && p.mode === "watch" && open ? "Watching" : "Keep them posted") : p && open && p.mode === "bid" ? "Update" : wish ? "Ask them to confirm" : "Ask them"}</button>
        ${open ? `<button type="button" class="secondary" data-act="proxy_cancel" data-id="${id}" data-site="${esc(a.site)}">${p.mode === "watch" ? "Stop updates" : "Cancel request"}</button>` : ""}
      </div>` : ""}
      <div class="note">${!people.length ? "Add people on the ⚙ Settings tab (Bid for others) first."
        : mode === "watch" ? "They get a Telegram message now, another when it's about to end, and one when it ends. Nothing is bid."
        : gov ? `They get a Telegram message and reply with how many bids they'll pay for (up to yours; e.g. "40" or "40 $25" with a
          max price), then tap "I agree to pay": the bids used (${money(0.13)} each) win or lose, plus the final price if they win.
          Then their bids are booked from your account (or auto-booking spends them if it's on here). They hear when their bids
          run out and how it ends, with what they owe.`
        : `They get a Telegram message, reply with their max (up to yours), then tap "I agree to pay". Only then is a max bid
          placed from your account: the lower of theirs and yours. They also hear when they're outbid and how it ends.`}</div>
      ${this._saved["proxy:" + aid] ? `<div class="note"><b>${esc(this._saved["proxy:" + aid])}</b></div>` : ""}
    </details>`;
  }

  async _loadProxy() {
    this._pvLoading = true;
    try { this._pv = await this._hass.callWS({ type: "auction_watcher/proxy" }); } catch (e) { this._pv = null; }
    this._pvLoading = false;
    this._maybeRender(true);
  }

  async _proxyAction(act, id, site) {
    const key = "proxy:" + id;
    try {
      if (act === "proxy_ask") {
        const a = this._auctions().find((x) => String(x.auction_id) === String(id)) || {};
        const mode = this._field(id, "proxy_mode", a.site === "macbid" || a.site === "govauctions" ? "bid" : "watch");
        const person = this._field(id, "proxy_person", this._pv?.people?.[0]?.id);
        const max = parseInt(this._field(id, "proxy_max", 0), 10) || 0;
        if (mode === "bid" && !max) throw new Error("set your max first");
        this._pv = await this._hass.callWS({ type: "auction_watcher/proxy/change", action: "ask", site: site || a.site,
          mode, lot_id: id, person_id: person, hard_max: mode === "bid" ? max : 0 });
        const who = this._pv.people.find((x) => x.id === person)?.name || "them";
        this._saved[key] = mode === "watch" ? `${who} will get updates on Telegram` : `Asked ${who} on Telegram`;
        this._clearDrafts(id, ["proxy_max", "proxy_mode"]);
      } else {
        this._pv = await this._hass.callWS({ type: "auction_watcher/proxy/change", action: "cancel", site, lot_id: id });
        this._saved[key] = "Stopped";
      }
    } catch (e) {
      this._saved[key] = "Couldn't: " + (e?.message || e);
    }
    this._maybeRender(true);
  }

  _running(a) {
    if (a.status !== "live" && a.status !== "upcoming") return false;
    return a.site === "macbid" ? a.mb_mode === "late"
      : !!(a.auto_cancel || a.auto_rebook || a.topup_at || a.your_booked_left);
  }

  _hasBids(a) {
    if (a.site === "govauctions") return Number(a.your_bids || 0) > 0 || Number(a.your_booked_left || 0) > 0;
    return !!(a.you_are_bidding || a.winning || a.your_max_bid);
  }

  _watchButton(a) {
    const followed = !!a.on_watchlist || a.category === "watching";
    if (!followed) return "";
    if (this._hasBids(a)) return "";
    if (a.site === "ola") return "";
    const key = `watch:${a.site}:${a.auction_id}`;
    return `<button type="button" class="unfollow-btn" data-act="watch_off" data-id="${esc(a.auction_id)}" data-site="${esc(a.site)}"
      title="Remove this item from the site's watch/follow list">Unfollow</button>
      ${this._saved[key] ? `<div class="note"><b>${esc(this._saved[key])}</b></div>` : ""}`;
  }

  _stopButton(a) {
    if (!this._running(a)) return this._saved["stop:" + a.auction_id] ? `<div class="note"><b>${esc(this._saved["stop:" + a.auction_id])}</b></div>` : "";
    return `<button type="button" class="stop-btn" data-act="stop" data-id="${esc(a.auction_id)}"
      title="Stop everything automatic on this item">■ Stop</button>`;
  }

  _overtimeLine(a) {
    if (!a.original_end) return "";
    const orig = Date.parse(a.original_end);
    const when = new Date(orig).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
    if (a.status === "live" && Date.now() > orig) {
      return `<div class="line"><span class="k">Original end</span> ${esc(when)} · <b>overtime <span class="overtime" data-since="${orig}">${fmtDuration((Date.now() - orig) / 1000)}</span></b></div>`;
    }
    if (a.ends_at && Math.abs(Date.parse(a.ends_at) - orig) > 1000) {
      return `<div class="line"><span class="k">Original end</span> ${esc(when)} · extended ${fmtDuration((Date.parse(a.ends_at) - orig) / 1000)}</div>`;
    }
    return a.status === "live" || a.status === "upcoming" ? `<div class="line"><span class="k">Scheduled end</span> ${esc(when)}</div>` : "";
  }

  _isOneCent(a) {
    return (a.labels || []).some((l) => /1¢/.test(l));
  }

  // Total max cost = max bids × point cost + what you'd pay if you won at your max price
  _totalText(a) {
    const aid = a.auction_id;
    if (a.site === "ebay") {
      const max = parseFloat(this._val(aid, "limit", a.bid_limit || 0)) || 0;
      if (!max) return `<span class="muted">Set a max price to get an alert when it's passed.</span>`;
      const ship = Number(a.shipping || 0);
      return `<b>At your max: ${money(max + ship)}</b> <span class="muted">= ${money(max)}${ship ? ` + ${money(ship)} shipping` : ""}, before tax.</span>`;
    }
    if (a.site === "macbid") {
      const max = parseFloat(this._val(aid, "limit", a.bid_limit || 0)) || 0;
      if (!max) return `<span class="muted">Set a max bid to see what winning at it would cost.</span>`;
      return `<b>If you win at your max: ${money(max * 1.15 + 3)}</b> <span class="muted">= ${money(max)} + 15% buyer's premium + $3 lot fee, before tax.</span>`;
    }
    const limit = parseInt(this._val(aid, "limit", a.bid_limit || 0), 10) || 0;
    const maxPrice = parseFloat(this._val(aid, "max_price", a.max_price || 0)) || 0;
    if (!limit) return `<span class="muted">Set ${esc(LIMIT_LABEL[a.site] || "a bid limit")} to see your total max cost.</span>`;
    const pointCost = Number(a.point_cost ?? 0.13);
    const bidsCost = limit * pointCost;
    let pay, payText;
    if (this._isOneCent(a)) { pay = 0.01; payText = "1¢ to win (1¢ auction)"; }
    else if (maxPrice) {
      const m = (a.labels || []).join(" ").match(/(\d{1,3})\s*%\s*off/i);
      pay = m ? maxPrice * (1 - Number(m[1]) / 100) : maxPrice;
      payText = `${money(pay)} if you win at your max price`;
    } else { pay = null; payText = "plus the final price (no max price set)"; }
    const total = pay === null ? null : bidsCost + pay;
    return `<b>Total max cost: ${total === null ? money(bidsCost) + "+" : money(total)}</b>
      <span class="muted">= ${num(limit)} bids × ${money(pointCost)} (${money(bidsCost)}) + ${esc(payText)}.
      Spent so far: ${money(a.your_points_cost)}.</span>`;
  }

  _bookingControls(a) {
    const id = esc(a.auction_id);
    const aid = a.auction_id;
    const auto = a.auto_cancel || a.auto_rebook;
    const mode = this._val(aid, "rebook_mode", a.rebook_mode || "fixed");
    const limitLabel = LIMIT_LABEL[a.site] || "bid limit";
    return `
      <details class="booking" ${auto || this._open[aid] ? "open" : ""} data-id="${id}">
        <summary>Booking &amp; auto ${auto ? `<span class="badge on">auto on</span>` : ""}</summary>
        <div class="bk-row">
          <input data-id="${id}" data-field="book_count" type="number" min="1" step="1" inputmode="numeric"
            value="${esc(this._val(aid, "book_count", a.rebook_amount || 10))}">
          <button type="button" data-act="book" data-id="${id}">Book bids</button>
          <button type="button" class="secondary" data-act="cancel" data-id="${id}" ${a.your_booked_left ? "" : "disabled"}>
            Cancel booked${a.your_booked_left ? ` (${num(a.your_booked_left)})` : ""}</button>
        </div>
        <label class="bk-check"><input data-id="${id}" data-field="auto_cancel" type="checkbox"
          ${this._val(aid, "auto_cancel", a.auto_cancel) ? "checked" : ""}>
          Auto-cancel booked bids when it gets crowded</label>
        <label class="bk-check"><input data-id="${id}" data-field="auto_rebook" type="checkbox"
          ${this._val(aid, "auto_rebook", a.auto_rebook) ? "checked" : ""}>
          Auto-rebook when it thins out again</label>
        <div class="bk-line">Rebook
          <label><input data-id="${id}" data-field="rebook_mode" type="radio" name="rebook_mode_${id}" value="fixed" ${mode !== "remaining" ? "checked" : ""}>
            <input data-id="${id}" data-field="rebook_amount" type="number" min="1" step="1" inputmode="numeric"
              value="${esc(this._val(aid, "rebook_amount", a.rebook_amount || 10))}"> bids</label>
          <label><input data-id="${id}" data-field="rebook_mode" type="radio" name="rebook_mode_${id}" value="remaining" ${mode === "remaining" ? "checked" : ""}>
            what was left when it cancelled</label>
        </div>
        <div class="bk-line">Top up when
          <input data-id="${id}" data-field="topup_at" type="number" min="0" step="1" inputmode="numeric" class="small"
            value="${esc(this._val(aid, "topup_at", a.topup_at || 0))}"> or fewer booked bids are left
          <span class="muted">(0 = only once they run out)</span></div>
        <div class="bk-row">
          <button type="button" data-act="auto" data-id="${id}">Save booking settings</button>
          <span class="saved">${esc(this._saved["auto:" + aid] || "")}</span>
        </div>
        <div class="note">
          Crowded = more other bidders than your alert number (you aren't counted).
          Auto needs a ${esc(limitLabel)} value above, and never books past it.
          The first booked bid is placed right away.
          ${a.auto_status ? `<br><b>Auto:</b> ${esc(a.auto_status)}` : ""}</div>
      </details>`;
  }

  _tick() {
    const now = Date.now();
    this.shadowRoot.querySelectorAll(".timer").forEach((el) => {
      const dl = Number(el.dataset.deadline);
      if (!dl) { el.textContent = "–"; return; }
      const left = (dl - now) / 1000;
      el.textContent = left <= 0 ? "now" : fmtDuration(left);
      el.classList.toggle("urgent", left > 0 && left <= 60);
    });
    this.shadowRoot.querySelectorAll(".penny").forEach((el) => {
      const left = Number(el.dataset.from) - (now - Number(el.dataset.since)) / 1000;
      el.textContent = left > 0 ? `${Math.ceil(left)}s` : "0s";
      el.classList.toggle("urgent", left <= 3);
    });
    this.shadowRoot.querySelectorAll(".overtime").forEach((el) => {
      el.textContent = fmtDuration((now - Number(el.dataset.since)) / 1000);
    });
    this.shadowRoot.querySelectorAll(".ago").forEach((el) => {
      el.textContent = `as of ${fmtDuration((now - Number(el.dataset.since)) / 1000)} ago`;
    });
  }

  // ---------------------------------------------------------------- actions

  _field(id, field, fallback) {
    const a = this._auctions().find((x) => String(x.auction_id) === String(id)) || {};
    const saved = { limit: a.bid_limit, max_price: a.max_price, book_count: a.rebook_amount || 10, auto_cancel: a.auto_cancel,
      auto_rebook: a.auto_rebook, rebook_mode: a.rebook_mode || "fixed", rebook_amount: a.rebook_amount || 10, topup_at: a.topup_at || 0,
      mb_late: a.mb_mode === "late", mb_assure: !!a.mb_assurance, mb_late_min: Math.round((a.mb_late_seconds || 180) / 60),
      proxy_person: (this._pv?.people || []).some((x) => x.id === a.proxy?.person_id) ? a.proxy.person_id : undefined,
      proxy_max: a.proxy?.hard_max, proxy_mode: a.proxy?.mode }[field];
    const v = this._val(id, field, saved);
    return v === undefined || v === null || v === "" ? fallback : v;
  }

  async _macbidAction(act, id) {
    const key = "mb:" + id;
    const a = this._auctions().find((x) => String(x.auction_id) === String(id)) || {};
    const max = parseInt(this._field(id, "limit", a.bid_limit || 0), 10) || 0;
    try {
      if (act === "mb_lower") {
        const price = Math.round(Number(a.price || 0));
        if (!a.winning || !price) throw new Error("you can only lower your max while you're winning");
        if (!confirm(`Lower your max on "${a.title}" to the current price, $${price}?\n\nYou stay in the lead at $${price}, but anyone who bids more wins it. Your saved max here becomes $${price} too, so auto-bid won't raise it again.`)) return;
        await this._hass.callService("auction_watcher", "set_bid_limit", { auction_id: id, limit: price });
        this._clearDrafts(id, ["limit"]);
        await this._hass.callService("auction_watcher", "place_bid", { auction_id: id, amount: price, buyers_assurance: false }, undefined, false, true);
        this._saved[key] = `Lowering your max to $${price}: checking Mac.bid recorded it`;
      } else if (act === "mb_place") {
        if (!max) throw new Error("enter your max bid first");
        if (a.min_next_bid && max < a.min_next_bid) throw new Error(`your max must be at least ${money(a.min_next_bid)}`);
        const total = (max * 1.15 + 3) * (1 + (a.sales_tax || 0));
        if (!confirm(`Bid on "${a.title}" with a max of $${max}?\n\nMac.bid raises your bid for you up to $${max}. If you win at your max you pay about ${money(total)} with the 15% premium, $3 fee and tax.\n\nBids can't be cancelled. (You can only lower your max to the current bid later.)`)) return;
        let assurance = false;
        if (a.assurance_cost && !a.has_assurance) {
          assurance = confirm(`Add Buyer's Assurance for ${money(a.assurance_cost)}?\n\nGet a 5-business-day return guarantee on this item. Without it, items are sold as-is.\n\nOK = Add protection for ${money(a.assurance_cost)}\nCancel = I'll take the risk`);
        }
        // the max also becomes this item's max (alerts, auto-bid)
        await this._hass.callService("auction_watcher", "set_bid_limit", { auction_id: id, limit: max });
        this._clearDrafts(id, ["limit"]);
        await this._hass.callService("auction_watcher", "place_bid", { auction_id: id, amount: max, buyers_assurance: assurance }, undefined, false, true);
        this._saved[key] = `Sent a $${max} max${assurance ? " + Buyer's Assurance" : ""}: checking Mac.bid recorded it (see Auto-bid status)`;
      } else {
        const late = String(this._field(id, "mb_late", a.mb_mode === "late")) === "true";
        const lateMin = Math.max(1, parseInt(this._field(id, "mb_late_min", Math.round((a.mb_late_seconds || 180) / 60)), 10) || 3);
        const assure = String(this._field(id, "mb_assure", !!a.mb_assurance)) === "true";
        if (late && !max) throw new Error("enter your max bid above and Save it first");
        if (late && max) await this._hass.callService("auction_watcher", "set_bid_limit", { auction_id: id, limit: max });
        await this._hass.callService("auction_watcher", "set_macbid_bidding",
          { auction_id: id, mode: late ? "late" : "off", late_seconds: lateMin * 60, assurance: assure });
        this._clearDrafts(id, ["mb_late", "mb_late_min", "mb_assure", "limit"]);
        this._saved[key] = late ? `Saved: bids $${max} about ${lateMin} min before the end` : "Auto-bid off";
      }
    } catch (err) {
      this._saved[key] = "Couldn't do that: " + (err?.message || err);
    }
    this._open[id] = true;
    this._maybeRender(true);
    setTimeout(() => { delete this._saved[key]; this._maybeRender(true); }, 8000);
  }

  async _olaPlace(id) {
    const key = "mb:" + id;
    const a = this._auctions().find((x) => String(x.auction_id) === String(id)) || {};
    const max = parseInt(this._field(id, "limit", a.bid_limit || 0), 10) || 0;
    try {
      if (!max) throw new Error("enter your max bid first");
      if (a.min_next_bid && max < a.min_next_bid) throw new Error(`your max must be at least ${money(a.min_next_bid)}`);
      if (!confirm(`Send a $${max} max on "${a.title}"?\n\nOLA raises your bid for you up to $${max}. Bids can't be cancelled, only raised. If you win you pay the final price plus any OLA fees and shipping.`)) return;
      await this._hass.callService("auction_watcher", "set_bid_limit", { auction_id: id, limit: max });
      this._clearDrafts(id, ["limit"]);
      await this._hass.callService("auction_watcher", "place_bid", { auction_id: id, amount: max, buyers_assurance: false }, undefined, false, true);
      this._saved[key] = `Sent a $${max} max: checking OLA recorded it`;
    } catch (err) {
      this._saved[key] = "Couldn't bid: " + (err?.message || err);
    }
    this._open[id] = true;
    this._maybeRender(true);
    setTimeout(() => { delete this._saved[key]; this._maybeRender(true); }, 8000);
  }

  async _action(act, id, site) {
    if (act === "mb_place" || act === "mb_auto" || act === "mb_lower") return this._macbidAction(act, id);
    if (act === "ola_place") return this._olaPlace(id);
    if (act === "quick") {
      const a = this._auctions().find((x) => String(x.auction_id) === String(id)) || {};
      const msg = a.site === "macbid"
        ? `Bid the next step on "${a.title}"?\n\nThat's the smallest bid Mac.bid accepts right now (about ${money(a.min_next_bid)}; checked live when you tap). If you win at it you pay about ${money(((a.min_next_bid || 0) * 1.15 + 3) * (1 + (a.sales_tax || 0)))} with the 15% premium, $3 fee and tax. Bids can't be cancelled.`
        : a.site === "ola"
        ? `Bid the next step on "${a.title}"?\n\nThat's the smallest bid OLA accepts right now (about ${money(a.min_next_bid)}; checked live when you tap). It's a max — OLA raises for you up to it. Bids can't be cancelled, only raised. If you win you pay the final price plus any OLA fees and shipping.`
        : `Place 1 bid on "${a.title}"?\n\nIt costs 1 point (${money(a.point_cost || 0.13)}) and is placed right away.`;
      if (!confirm(msg)) return;
      try {
        const res = await this._hass.callService("auction_watcher", "quick_bid", { auction_id: id }, undefined, false, true);
        this._saved["stop:" + id] = (res?.response?.done || "Done") + (a.site === "macbid" ? ": checking Mac.bid recorded it" : a.site === "ola" ? ": checking OLA recorded it" : "");
      } catch (err) {
        this._saved["stop:" + id] = "Couldn't bid: " + (err?.message || err);
      }
      this._maybeRender(true);
      setTimeout(() => { delete this._saved["stop:" + id]; this._maybeRender(true); }, 10000);
      return;
    }
    if (act === "stop") {
      const a = this._auctions().find((x) => String(x.auction_id) === String(id)) || {};
      const msg = a.site === "macbid"
        ? `Stop the scheduled auto-bid on "${a.title}"?${a.mb_placed ? `\n\nYour $${a.mb_placed} max already placed stays: Mac.bid bids can't be withdrawn.${a.winning ? " While you're winning you can lower it to the current price." : ""}` : ""}`
        : `Stop everything automatic on "${a.title}"?\n\nAuto-cancel, auto-rebook and top-up go off${a.your_booked_left ? `, and your ${a.your_booked_left} booked bids are cancelled (unused bids come back)` : ""}. Bids already placed are spent.`;
      if (!confirm(msg)) return;
      try {
        const res = await this._hass.callService("auction_watcher", "stop", { auction_id: id }, undefined, false, true);
        this._saved["stop:" + id] = "Stopped: " + (res?.response?.done || "done");
      } catch (err) {
        this._saved["stop:" + id] = "Couldn't stop: " + (err?.message || err);
      }
      this._maybeRender(true);
      setTimeout(() => { delete this._saved["stop:" + id]; this._maybeRender(true); }, 10000);
      return;
    }
    if (act === "pack_follow") {
      const btn = this.shadowRoot.querySelector(`[data-act=pack_follow][data-id="${CSS.escape(id)}"]`);
      const limit = parseInt(btn?.dataset.limit || "0", 10) || 0;
      if (btn) { btn.disabled = true; btn.textContent = "…"; }
      try {
        await this._hass.callService("auction_watcher", "follow", { auction_id: id, site: "govauctions", ...(limit ? { limit } : {}) });
        if (btn) btn.textContent = "Followed: see Following";
      } catch (err) {
        if (btn) { btn.disabled = false; btn.textContent = "Follow"; btn.title = "Couldn't: " + (err?.message || err); }
      }
      return;
    }
    if (act === "watch_off") {
      const a = this._auctions().find((x) => String(x.auction_id) === String(id) && (!site || x.site === site)) || {};
      if (!confirm(`Unfollow "${a.title || id}" on ${SITE_NAMES[site || a.site] || (site || a.site)}?\n\nThis only removes it from the watch/follow list. It won't cancel bids.`)) return;
      const key = `watch:${site || a.site}:${id}`;
      try {
        await this._hass.callService("auction_watcher", "watch", { auction_id: id, site: site || a.site, on: false });
        this._saved[key] = "Unfollowed";
      } catch (err) {
        this._saved[key] = "Couldn't unfollow: " + (err?.message || err);
      }
      this._maybeRender(true);
      setTimeout(() => { delete this._saved[key]; this._maybeRender(true); }, 8000);
      return;
    }
    if (act === "proxy_ask" || act === "proxy_cancel") return this._proxyAction(act, id, site);
    if (act === "proxy_forget" || act === "proxy_reassign") {
      const key = "proxy:" + id;
      try {
        if (act === "proxy_forget") {
          if (!confirm("Forget this request? It's removed from the list; no message is sent. (A Mac.bid max already placed stays on Mac.bid.)")) return;
          this._pv = await this._hass.callWS({ type: "auction_watcher/proxy/change", action: "forget", site, lot_id: id });
          this._saved[key] = "Forgotten";
        } else {
          const person = this._val(id, "proxy_reassign", this._pv?.people?.[0]?.id);
          this._pv = await this._hass.callWS({ type: "auction_watcher/proxy/change", action: "reassign", site, lot_id: id, person_id: person });
          this._saved[key] = "Reassigned to " + (this._pv.people.find((x) => x.id === person)?.name || "them");
        }
      } catch (err) { this._saved[key] = "Couldn't: " + (err?.message || err); }
      this._maybeRender(true);
      return;
    }
    const key = (act === "limit" || act === "max_price" ? act + ":" : "auto:") + id;
    const int = (v) => Math.max(0, parseInt(v, 10) || 0);
    const dec = (v) => Math.max(0, parseFloat(v) || 0);
    try {
      if (act === "limit") {
        const limit = int(this._field(id, "limit", 0));
        await this._hass.callService("auction_watcher", "set_bid_limit", { auction_id: id, limit });
        this._clearDrafts(id, ["limit"]);
        this._saved[key] = limit ? "Saved" : "Removed";
      } else if (act === "max_price") {
        const maxPrice = dec(this._field(id, "max_price", 0));
        await this._hass.callService("auction_watcher", "set_auto_booking", { auction_id: id, max_price: maxPrice });
        this._clearDrafts(id, ["max_price"]);
        this._saved[key] = maxPrice ? "Saved" : "Removed";
      } else if (act === "book") {
        const count = int(this._field(id, "book_count", 0));
        if (count < 1) throw new Error("enter how many bids to book");
        const maxPrice = dec(this._field(id, "max_price", 0));
        if (!confirm(`Book ${count} bids on this auction? The first one is placed right away; the rest wait their turn. This spends points.`)) return;
        const res = await this._hass.callService("auction_watcher", "book_bids",
          { auction_id: id, count, ...(maxPrice ? { max_price: maxPrice } : {}) }, undefined, false, true);
        const booked = res?.response?.booked ?? count;
        this._saved[key] = booked < count ? `Booked ${booked} (capped by max bids or balance)` : `Booked ${booked}`;
      } else if (act === "cancel") {
        if (!confirm("Cancel your booked bids on this auction? Unused points go back to your balance.")) return;
        await this._hass.callService("auction_watcher", "cancel_booked_bids", { auction_id: id });
        this._saved[key] = "Cancelled";
      } else if (act === "auto") {
        const wantsAuto = !!this._field(id, "auto_cancel", false) || !!this._field(id, "auto_rebook", false);
        const limit = int(this._field(id, "limit", 0));
        if (wantsAuto && !limit) throw new Error("set Max bids for this auction first (box above)");
        // Save Max bids / Max price too if you typed them, so one button does it all
        await this._hass.callService("auction_watcher", "set_bid_limit", { auction_id: id, limit });
        await this._hass.callService("auction_watcher", "set_auto_booking", {
          auction_id: id,
          auto_cancel: !!this._field(id, "auto_cancel", false),
          auto_rebook: !!this._field(id, "auto_rebook", false),
          rebook_mode: this._field(id, "rebook_mode", "fixed"),
          rebook_amount: Math.max(1, int(this._field(id, "rebook_amount", 10))),
          topup_at: Math.max(0, int(this._field(id, "topup_at", 0))),
          max_price: dec(this._field(id, "max_price", 0)),
        });
        this._clearDrafts(id, ["limit", "max_price", "auto_cancel", "auto_rebook", "rebook_mode", "rebook_amount", "topup_at"]);
        this._saved[key] = "Saved";
      }
    } catch (err) {
      this._saved[key] = "Couldn't do that: " + (err?.message || err);
    }
    if (act === "book" || act === "cancel" || act === "auto") this._open[id] = true;
    this._maybeRender(true);
    setTimeout(() => { delete this._saved[key]; this._maybeRender(true); }, 6000);
  }

  _clearDrafts(id, fields) {
    const d = this._drafts[id];
    if (!d) return;
    fields.forEach((f) => delete d[f]);
    if (!Object.keys(d).length) delete this._drafts[id];
  }
}

const STYLE = `
  :host { display: block; min-height: 100vh; background: var(--primary-background-color); color: var(--primary-text-color);
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
  .toolbar { display: flex; align-items: center; gap: 8px; height: 56px; padding: 0 12px; background: var(--app-header-background-color, var(--primary-color));
             color: var(--app-header-text-color, #fff); }
  .toolbar .title { flex: 1; font-size: 20px; }
  .icon { background: none; border: 0; color: inherit; font-size: 22px; cursor: pointer; padding: 6px 10px; border-radius: 50%; }
  .icon:hover { background: rgba(255,255,255,.15); }
  .summary { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px 0; align-items: center; }
  .pill { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 16px; padding: 6px 12px; font-size: 14px; }
  .pill.hot { border-color: var(--warning-color, #ff9800); }
  .sort { margin-left: auto; font-size: 14px; color: var(--secondary-text-color); display: flex; gap: 6px; align-items: center; }
  .sort select { padding: 5px 8px; border-radius: 6px; border: 1px solid var(--divider-color); background: var(--card-background-color);
                 color: var(--primary-text-color); }
  .tabs { display: flex; gap: 4px; padding: 12px 16px 0; overflow-x: auto; scrollbar-width: none; border-bottom: 1px solid var(--divider-color); }
  .tabs::-webkit-scrollbar { display: none; }
  .tabs.site-tabs { padding-top: 4px; align-items: center; }
  .tabs.site-tabs .tab { font-size: 14px; padding: 8px 12px; }
  .tabs-label { font-size: 12px; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: .05em; padding-right: 4px; }
  .pack-auctions { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
  .pack-auction { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: center; padding: 4px 8px; border-radius: 6px;
                  background: var(--secondary-background-color); }
  .pack-auction a { font-weight: 600; color: var(--primary-color); text-decoration: none; min-width: 52px; }
  .pack-auction .stars { margin-left: auto; }
  .small-btn { padding: 2px 10px; font-size: 12px; }
  .quick { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 13px; }
  .stop-btn { float: right; margin-left: 8px; background: var(--error-color, #db4437); color: #fff; border: 0; border-radius: 6px;
              padding: 4px 10px; font-size: 13px; font-weight: 600; cursor: pointer; }
  .unfollow-btn { float: right; margin-left: 8px; background: var(--card-background-color); color: var(--primary-text-color); border: 1px solid var(--divider-color); border-radius: 6px;
                  padding: 4px 10px; font-size: 12px; font-weight: 600; cursor: pointer; }
  details.packs-intro, .packs-more { grid-column: 1 / -1; }
  details.packs-intro summary, details.hist summary { cursor: pointer; }
  .banner { margin: 12px 16px 0; padding: 10px 14px; border-radius: 12px; border: 1px solid var(--error-color, #db4437);
            background: rgba(219, 68, 55, .12); display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; }
  .banner.update { border-color: var(--primary-color); background: rgba(3, 169, 244, .12); }
  .banner button.btn { border: 0; cursor: pointer; }
  .banner .btn { background: var(--primary-color); color: var(--text-primary-color, #fff); border-radius: 6px; padding: 5px 12px; text-decoration: none; }
  .packs-more { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 3px; vertical-align: middle; }
  .sv { padding: 16px; display: flex; flex-direction: column; gap: 12px; max-width: 1100px; }
  .sbar, .sadd, .sfilters { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; }
  .sadd { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px; padding: 12px; }
  .sv input[type=text], .sv input[type=number], .sv input[type=time] { padding: 6px 8px; border-radius: 6px; border: 1px solid var(--divider-color);
     background: var(--card-background-color); color: var(--primary-text-color); }
  .sv input[type=text] { flex: 1 1 200px; min-width: 0; }
  .sv select { padding: 5px 8px; border-radius: 6px; border: 1px solid var(--divider-color); background: var(--card-background-color);
               color: var(--primary-text-color); }
  .sv input.small { width: 70px; }
  .chk { display: inline-flex; align-items: center; gap: 4px; font-size: 14px; }
  .schips { display: flex; flex-wrap: wrap; gap: 8px; }
  .schip { display: inline-flex; align-items: center; border: 1px solid var(--divider-color); border-radius: 16px;
           background: var(--card-background-color); color: var(--primary-text-color); padding: 0; overflow: hidden; }
  button.schip { padding: 5px 12px; }
  .schip.on { border-color: var(--primary-color); box-shadow: 0 0 0 1px var(--primary-color); }
  .schip-main, .schip-x { background: none; color: inherit; border-radius: 0; padding: 5px 8px 5px 12px; display: inline-flex; align-items: center; gap: 4px; }
  .schip-t { background: none; color: var(--secondary-text-color); border-radius: 0; padding: 5px 8px; border-left: 1px solid var(--divider-color);
             font-size: 12px; font-weight: 700; }
  .schip-t.on { color: var(--primary-color); }
  .hits .more { align-self: center; }
  .star { background: none; border: 0; padding: 0 4px 0 0; font-size: 20px; line-height: 1; color: var(--secondary-text-color); cursor: pointer; vertical-align: middle; }
  .star.on { color: #f5b301; }
  .star:hover { color: #f5b301; }
  .gs-sec { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px; padding: 12px 14px; }
  .gs-sec h3 { margin: 0 0 8px; font-size: 16px; font-weight: 500; }
  .gs-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 6px 12px; margin-bottom: 8px; }
  .gs-list.kinds { grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
  .gs .k { font-size: 12px; color: var(--secondary-text-color); margin: 8px 0 4px; }
  .tally.cap .capbar { height: 6px; border-radius: 3px; background: var(--divider-color); overflow: hidden; margin-top: 6px; }
  .tally.cap .capbar div { height: 100%; background: var(--primary-color); }
  .ss-form { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px; padding: 8px 14px; }
  .schip-x { padding: 5px 10px; border-left: 1px solid var(--divider-color); font-size: 16px; line-height: 1; }
  .schip-x:hover { background: var(--error-color, #db4437); color: #fff; }
  details.locs { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px; padding: 10px 12px; }
  details.locs summary { cursor: pointer; font-weight: 500; }
  .locgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 6px; margin: 10px 0; }
  .hits { display: flex; flex-direction: column; gap: 8px; }
  .hit { display: flex; gap: 12px; background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px; padding: 10px; }
  .hit img, .hit .noimg { width: 64px; height: 64px; object-fit: cover; border-radius: 8px; flex: none; background: var(--secondary-background-color); }
  .hbody { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
  .htitle a { color: var(--primary-text-color); text-decoration: none; font-weight: 500; }
  .htitle a:hover { text-decoration: underline; }
  .hmeta { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 13px; }
  .kw { font-size: 11px; background: var(--secondary-background-color); border-radius: 8px; padding: 1px 7px; margin-right: 4px; }
  @media (max-width: 600px) { .sv { padding: 12px; } .hit img, .hit .noimg { width: 48px; height: 48px; } }
  .subtabs { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 12px 16px 0; font-size: 13px; }
  .subtab { background: var(--secondary-background-color); color: var(--primary-text-color); border: 1px solid var(--divider-color);
            border-radius: 16px; padding: 5px 12px; }
  .subtab.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  .tab { background: none; border: 0; border-bottom: 2px solid transparent; color: var(--secondary-text-color); padding: 10px 14px;
         font-size: 15px; cursor: pointer; white-space: nowrap; }
  .tab.on { color: var(--primary-color); border-bottom-color: var(--primary-color); }
  .count { font-size: 12px; background: var(--divider-color); border-radius: 10px; padding: 1px 7px; margin-left: 4px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 16px; padding: 16px; }
  .empty { color: var(--secondary-text-color); padding: 24px 4px; }
  .card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px); padding: 14px;
          box-shadow: var(--ha-card-box-shadow, none); border: 1px solid var(--divider-color); display: flex; flex-direction: column; gap: 10px; }
  .card.opp { border: 2px solid var(--warning-color, #ff9800); }
  .card.capped { opacity: .85; }
  .top { display: flex; gap: 12px; }
  .top img, .noimg { width: 72px; height: 72px; object-fit: contain; border-radius: 8px; background: #fff; flex: none; }
  .noimg { display: grid; place-items: center; font-size: 28px; background: var(--secondary-background-color); }
  .head { min-width: 0; }
  .name { color: var(--primary-text-color); font-weight: 500; text-decoration: none; display: -webkit-box; -webkit-line-clamp: 2;
          -webkit-box-orient: vertical; overflow: hidden; }
  .name:hover { color: var(--primary-color); }
  .sub { color: var(--secondary-text-color); font-size: 13px; margin-top: 2px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .status.live { color: var(--success-color, #4caf50); font-weight: 500; }
  .status.upcoming { color: var(--info-color, #039be5); }
  .badge { font-size: 12px; border-radius: 10px; padding: 1px 8px; background: var(--secondary-background-color); }
  .badge.hot { background: var(--warning-color, #ff9800); color: #000; }
  .badge.cap { background: var(--error-color, #db4437); color: #fff; }
  .badge.on { background: var(--success-color, #43a047); color: #fff; }
  .labels { display: flex; flex-direction: column; gap: 3px; font-size: 12px; }
  .tag { font-size: 11px; border: 1px solid var(--divider-color); border-radius: 8px; padding: 0 6px; color: var(--primary-text-color);
         white-space: nowrap; }
  .big { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; text-align: center; background: var(--secondary-background-color);
         border-radius: 8px; padding: 8px 4px; }
  .big .v { font-size: 20px; font-weight: 500; font-variant-numeric: tabular-nums; }
  .timer.urgent, .v.urgent { color: var(--error-color, #db4437); }
  .ago { font-size: 11px; }
  .big .v.small { font-size: 15px; }
  .k { color: var(--secondary-text-color); font-size: 12px; }
  .rows { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 12px; font-size: 14px; }
  .rows .k { display: block; }
  .line, .hist { font-size: 13px; }
  .muted { color: var(--secondary-text-color); }
  .limits { display: flex; flex-direction: column; gap: 4px; font-size: 14px; border-top: 1px solid var(--divider-color); padding-top: 10px; }
  .lim-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 4px; }
  .limits label { display: flex; align-items: center; gap: 6px; min-width: 150px; justify-content: space-between; }
  .total { margin-top: 6px; padding: 8px; border-radius: 8px; background: var(--secondary-background-color); font-size: 13px; }
  input[type=number] { width: 84px; padding: 6px; border-radius: 6px; border: 1px solid var(--divider-color);
                       background: var(--primary-background-color); color: var(--primary-text-color); }
  button { padding: 6px 12px; border-radius: 6px; border: 0; background: var(--primary-color); color: var(--text-primary-color, #fff); cursor: pointer; }
  button.secondary { background: var(--secondary-background-color); color: var(--primary-text-color); border: 1px solid var(--divider-color); }
  button:disabled { opacity: .5; cursor: default; }
  .toolbar .icon { background: none; color: inherit; }
  .tab { border-radius: 0; }
  .saved { color: var(--secondary-text-color); font-size: 13px; }
  details.booking { border-top: 1px solid var(--divider-color); padding-top: 8px; font-size: 14px; }
  details.booking summary { cursor: pointer; color: var(--primary-color); font-weight: 500; }
  .bk-row, .bk-line { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 8px; }
  .bk-check { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
  .note { color: var(--secondary-text-color); font-size: 12px; }
  .packs-intro { grid-column: 1 / -1; font-size: 13px; }
  .stars { color: var(--warning-color, #ff9800); font-size: 15px; letter-spacing: 1px; white-space: nowrap; }
  .warn { color: var(--error-color, #db4437); }
  .sub-menu { font-size: 13px; }
  button.bid-now { background: #7cb02d; font-weight: 600; }
  .cond-block { display: flex; flex-direction: column; gap: 6px; }
  .tag.cond-label.damaged { background: #ff455b; color: #fff; border-color: #ff455b; }
  .tag.cond-label.like-new { background: #7cb02d; color: #fff; border-color: #7cb02d; }
  .tag.cond-label.open-box { background: var(--secondary-background-color); }
  .cond { border-radius: 10px; padding: 8px 10px; font-size: 13px; border: 1px solid; }
  .cond.tested { background: rgba(124, 176, 45, 0.12); border-color: #7cb02d; }
  .cond.damaged { background: rgba(255, 69, 91, 0.10); border-color: #ff455b; }
  .cond.partial { background: rgba(255, 193, 7, 0.12); border-color: #ffc107; }
  .cond-tag { display: inline-block; border-radius: 12px; padding: 1px 10px; margin-bottom: 4px; font-size: 12px; color: #fff; }
  .cond.tested .cond-tag { background: #7cb02d; }
  .cond.damaged .cond-tag { background: #ff455b; }
  .cond.partial .cond-tag { background: #e0a800; color: #000; }
  .sub-menu summary { cursor: pointer; color: var(--primary-color); }
  .sub-menu ul { margin: 6px 0 0 18px; padding: 0; }
  .pre { white-space: pre-line; color: var(--secondary-text-color); margin-top: 4px; }
  .tally { margin: 12px 16px 0; padding: 12px; border-radius: 12px; background: var(--card-background-color); border: 1px solid var(--divider-color); }
  .tally-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; text-align: center; }
  .tally-grid .v { font-size: 20px; font-weight: 500; font-variant-numeric: tabular-nums; }
  .tally-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 10px; font-size: 13px; }
  .suggest { font-size: 13px; padding: 8px; border-radius: 8px; background: var(--secondary-background-color); }
  .pack-head { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
  .card.pack.green { border: 2px solid var(--success-color, #43a047); }
  .badge.grade-green { background: var(--success-color, #43a047); color: #fff; }
  .badge.grade-yellow { background: var(--warning-color, #ff9800); color: #000; }
  .badge.grade-red { background: var(--error-color, #db4437); color: #fff; }
  details .note { margin-top: 8px; }
  @media (max-width: 420px) { .grid { grid-template-columns: 1fr; padding: 12px; } .rows { grid-template-columns: 1fr; } .sort { margin-left: 0; } }
`;

customElements.get("auction-watcher-panel") || customElements.define("auction-watcher-panel", AuctionWatcherPanel);
