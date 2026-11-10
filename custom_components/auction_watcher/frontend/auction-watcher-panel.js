/*
 * Auction Watcher sidebar page.
 *
 * Reads the auction sensors Home Assistant already has (attribute
 * auction_watcher: true) and the account sensors (auction_watcher_account),
 * so it needs no extra API. Countdowns tick every second between polls.
 * Plain web component, no build step.
 */

const SITE_NAMES = { govauctions: "GovAuctions" };
const SHORT_CLOCK = 15; // seconds; same as FINAL_PHASE_MAX_CLOCK in logic.py

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

class AuctionWatcherPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "active";
    this._signature = "";
    this._pending = false;
    this._saved = {};
    this._open = {};
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
    this._maybeRender(true);
  }
  disconnectedCallback() { clearInterval(this._timer); }

  // ---------------------------------------------------------------- data

  _auctions() {
    const out = [];
    for (const s of Object.values(this._hass?.states || {})) {
      const a = s.attributes || {};
      if (a.auction_watcher !== true) continue;
      out.push({ entity_id: s.entity_id, price: s.state, updated: Date.parse(s.last_updated), unavailable: s.state === "unavailable", ...a });
    }
    const rank = (a) => (a.opportunity ? 0 : a.status === "live" ? 1 : a.status === "upcoming" ? 2 : 3);
    return out.sort((a, b) => rank(a) - rank(b) || String(a.title).localeCompare(String(b.title)));
  }

  _accounts() {
    return Object.values(this._hass?.states || {})
      .filter((s) => s.attributes?.auction_watcher_account)
      .map((s) => ({ entity_id: s.entity_id, points: s.state, ...s.attributes }));
  }

  _tabs(auctions) {
    const sites = [...new Set(auctions.map((a) => a.site))].sort();
    return [
      { id: "active", label: "Active", filter: (a) => a.category === "bidding" && (a.status === "live" || a.status === "upcoming") },
      { id: "watching", label: "Watching", filter: (a) => a.category === "watching" && a.status !== "ended" },
      { id: "ended", label: "Ended", filter: (a) => a.status === "ended" },
      ...sites.map((site) => ({ id: "site:" + site, label: SITE_NAMES[site] || site, filter: (a) => a.site === site })),
    ];
  }

  // ---------------------------------------------------------------- rendering

  _maybeRender(force = false) {
    if (!this._hass || !this.isConnected) return;
    // Don't redraw under someone typing a bid limit; redraw once they're done.
    const active = this.shadowRoot.activeElement;
    if (!force && active && active.tagName === "INPUT") { this._pending = true; return; }
    const auctions = this._auctions();
    const accounts = this._accounts();
    const signature = JSON.stringify([this._tab, auctions.map((a) => [a.entity_id, a.price, a.updated, a.bid_limit,
      a.auto_cancel, a.auto_rebook, a.rebook_mode, a.rebook_amount, a.max_price, a.auto_status, a.your_booked_left]), accounts, this._saved]);
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
    const opp = auctions.filter((a) => a.opportunity).length;

    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="toolbar">
        <span class="menu"></span>
        <div class="title">Auctions</div>
        <button class="icon" data-action="refresh" title="Check now">⟳</button>
      </div>
      <div class="summary">
        ${accounts.map((acc) => `
          <div class="pill"><b>${esc(acc.auction_watcher_account)}</b>
            ${num(acc.points)} points · ${esc(acc.polling_mode || "")}${acc.poll_every_seconds ? `, every ${fmtDuration(acc.poll_every_seconds)}` : ""}
          </div>`).join("") || `<div class="pill">No site set up yet: Settings → Devices &amp; services → Add integration → Auction Watcher</div>`}
        ${opp ? `<div class="pill hot">🎯 ${opp} with few bidders left</div>` : ""}
      </div>
      <div class="tabs" role="tablist">
        ${tabs.map((t) => {
          const n = auctions.filter(t.filter).length;
          return `<button role="tab" class="tab ${t.id === this._tab ? "on" : ""}" data-tab="${esc(t.id)}">${esc(t.label)} <span class="count">${n}</span></button>`;
        }).join("")}
      </div>
      <div class="grid">
        ${shown.length ? shown.map((a) => this._card(a)).join("") : `<div class="empty">Nothing here right now.</div>`}
      </div>`;

    // Home Assistant's own sidebar button, for phones / narrow screens
    const slot = this.shadowRoot.querySelector(".menu");
    this._menuButton = document.createElement("ha-menu-button");
    this._menuButton.hass = this._hass;
    this._menuButton.narrow = this._narrow;
    slot.appendChild(this._menuButton);

    this.shadowRoot.querySelectorAll("[data-tab]").forEach((b) =>
      b.addEventListener("click", () => { this._tab = b.dataset.tab; this._maybeRender(true); }));
    this.shadowRoot.querySelector("[data-action=refresh]").addEventListener("click", () =>
      this._hass.callService("auction_watcher", "refresh", {}));
    this.shadowRoot.querySelectorAll("form.limit").forEach((f) => f.addEventListener("submit", (ev) => this._saveLimit(ev)));
    this.shadowRoot.querySelectorAll(".card input").forEach((i) =>
      i.addEventListener("blur", () => { if (this._pending) setTimeout(() => this._maybeRender(), 0); }));
    this.shadowRoot.querySelectorAll("details.booking").forEach((d) =>
      d.addEventListener("toggle", () => { this._open[d.dataset.id] = d.open; }));
    this.shadowRoot.querySelectorAll("[data-act]").forEach((b) =>
      b.addEventListener("click", () => this._bookingAction(b.dataset.act, b.dataset.id, b.closest("details"))));
    this._tick();
  }

  async _bookingAction(act, id, box) {
    const key = "auto:" + id;
    const val = (name) => box.querySelector(`[name="${name}"]`);
    try {
      if (act === "book") {
        const count = parseInt(val("book_count").value, 10) || 0;
        if (count < 1) throw new Error("Enter how many bids to book");
        const maxPrice = parseFloat(val("max_price").value) || undefined;
        if (!confirm(`Book ${count} bids on this auction? The first one is placed right away; the rest wait their turn. This spends points.`)) return;
        const res = await this._hass.callService("auction_watcher", "book_bids",
          { auction_id: id, count, ...(maxPrice ? { max_price: maxPrice } : {}) }, undefined, false, true);
        const booked = res?.response?.booked ?? count;
        this._saved[key] = booked < count ? `Booked ${booked} (capped by your limit or balance)` : `Booked ${booked}`;
      } else if (act === "cancel") {
        if (!confirm("Cancel your booked bids on this auction? Unused points go back to your balance.")) return;
        await this._hass.callService("auction_watcher", "cancel_booked_bids", { auction_id: id });
        this._saved[key] = "Cancelled";
      } else if (act === "auto") {
        const mode = box.querySelector(`input[type=radio]:checked`)?.value || "fixed";
        await this._hass.callService("auction_watcher", "set_auto_booking", {
          auction_id: id,
          auto_cancel: val("auto_cancel").checked,
          auto_rebook: val("auto_rebook").checked,
          rebook_mode: mode,
          rebook_amount: Math.max(1, parseInt(val("rebook_amount").value, 10) || 10),
          max_price: Math.max(0, parseFloat(val("max_price").value) || 0),
        });
        this._saved[key] = "Saved";
      }
    } catch (err) {
      this._saved[key] = "Couldn't do that: " + (err?.message || err);
    }
    this._open[id] = true;
    this._maybeRender(true);
    setTimeout(() => { delete this._saved[key]; this._maybeRender(true); }, 6000);
  }

  _clock(a) {
    // Real deadlines tick down. A penny auction's short clock is reset by every
    // bid, which we only see at the next check, so it's shown "as of" that check
    // instead of counting to a misleading 0.
    if (a.status === "upcoming" && a.starts_at) return { label: "Starts in", deadline: Date.parse(a.starts_at) };
    if (a.ends_at) return { label: "Ends in", deadline: Date.parse(a.ends_at) };
    if (a.status === "live" && a.clock_seconds !== null && a.clock_seconds !== undefined) {
      const clock = Number(a.clock_seconds);
      if (clock > SHORT_CLOCK) return { label: "First countdown", deadline: a.updated + clock * 1000 };
      return { label: "Clock", asOf: a.updated, value: clock };
    }
    return { label: "Clock" };
  }

  _card(a) {
    const c = this._clock(a);
    const img = safeUrl(a.image);
    const url = safeUrl(a.url);
    const labels = (a.labels || []).map((l) => `<span class="tag">${esc(l)}</span>`).join("");
    const bidders = (a.recent_bidders || []).map(esc).join(", ");
    const active = a.active_bidders === null || a.active_bidders === undefined ? "–" : `${a.active_bidders}${a.active_bidders >= 9 ? "+" : ""}`;
    const limitText = a.bid_limit ? ` / ${num(a.bid_limit)}` : "";
    const saved = this._saved[a.auction_id];
    const status = a.unavailable ? "unavailable" : a.status;
    return `
      <div class="card ${a.opportunity ? "opp" : ""} ${a.bid_limit_reached ? "capped" : ""}">
        <div class="top">
          ${img ? `<img src="${img}" alt="" loading="lazy">` : `<div class="noimg">🛒</div>`}
          <div class="head">
            <a class="name" href="${url}" target="_blank" rel="noopener">${esc(a.title)}</a>
            <div class="sub">${esc(SITE_NAMES[a.site] || a.site)} · <span class="status ${esc(status)}">${esc(status)}</span>
              ${a.opportunity ? `<span class="badge hot">🎯 few bidders</span>` : ""}
              ${a.bid_limit_reached ? `<span class="badge cap">⛔ limit reached</span>` : ""}
              ${a.closed_to_new && !a.your_bids ? `<span class="badge">closed to new bidders</span>` : ""}
            </div>
            <div class="tags">${labels}</div>
          </div>
        </div>
        <div class="big">
          <div><div class="k">Price</div><div class="v">${money(a.price)}</div></div>
          <div><div class="k">${c.label}</div>
            ${c.asOf
              ? `<div class="v ${c.value <= 3 ? "urgent" : ""}">${c.value}s</div><div class="k ago" data-since="${c.asOf}"></div>`
              : `<div class="v timer" data-deadline="${c.deadline ?? ""}">–</div>`}</div>
          <div><div class="k">Bidders (last ${esc(a.active_window_minutes || 1)} min)</div><div class="v">${active}</div></div>
        </div>
        ${bidders ? `<div class="line"><span class="k">Bidding:</span> ${bidders}</div>` : ""}
        <div class="rows">
          <div><span class="k">Your bids</span> ${num(a.your_bids)}${limitText} <span class="muted">(${money(a.your_points_cost)})</span></div>
          <div><span class="k">Booked left</span> ${num(a.your_booked_left)}</div>
          <div><span class="k">If you won now</span> ${money(a.cost_if_won_now)} <span class="muted">(pay ${money(a.pay_if_won)})</span></div>
          <div><span class="k">Buy It Now</span> ${money(a.buy_it_now)}</div>
          ${a.no_jumper_limit ? `<div><span class="k">Entry limit</span> ${money(a.no_jumper_limit)}</div>` : ""}
          <div><span class="k">Leader</span> ${esc(a.leader || "–")}</div>
        </div>
        ${a.history_wins ? `
          <div class="hist"><span class="k">Past ${num(a.history_wins)} wins${a.history_period_days ? ` (${a.history_period_days} days)` : ""}:</span>
            median ${money(a.history_median_final_price)}, winners used ~${num(a.history_median_winner_bids)} bids
            ${a.history_avg_winner_cost ? `, avg cost ${money(a.history_avg_winner_cost)}` : ""}</div>` : ""}
        <form class="limit" data-id="${esc(a.auction_id)}">
          <label>Bid limit <input name="limit" type="number" min="0" step="1" inputmode="numeric" value="${esc(a.bid_limit || "")}" placeholder="none"></label>
          <button type="submit">Save</button>
          <span class="saved">${saved ? esc(saved) : ""}</span>
        </form>
        ${a.status === "ended" ? "" : this._bookingControls(a)}
      </div>`;
  }

  _bookingControls(a) {
    const id = esc(a.auction_id);
    const auto = a.auto_cancel || a.auto_rebook;
    const noLimit = !a.bid_limit;
    return `
      <details class="booking" ${auto || this._open[a.auction_id] ? "open" : ""} data-id="${id}">
        <summary>Booking &amp; auto ${auto ? `<span class="badge on">auto on</span>` : ""}</summary>
        <div class="bk-row">
          <input name="book_count" type="number" min="1" step="1" inputmode="numeric" value="${esc(a.rebook_amount || 10)}">
          <button type="button" data-act="book" data-id="${id}">Book bids</button>
          <button type="button" class="secondary" data-act="cancel" data-id="${id}" ${a.your_booked_left ? "" : "disabled"}>
            Cancel booked${a.your_booked_left ? ` (${num(a.your_booked_left)})` : ""}</button>
        </div>
        <label class="bk-line">Max price (site stops bidding above it)
          <input name="max_price" type="number" min="0" step="0.01" inputmode="decimal" value="${esc(a.max_price || "")}" placeholder="none"></label>
        <label class="bk-check"><input name="auto_cancel" type="checkbox" ${a.auto_cancel ? "checked" : ""} ${noLimit ? "disabled" : ""}>
          Auto-cancel booked bids when it gets crowded</label>
        <label class="bk-check"><input name="auto_rebook" type="checkbox" ${a.auto_rebook ? "checked" : ""} ${noLimit ? "disabled" : ""}>
          Auto-rebook when it thins out again</label>
        <div class="bk-line">Rebook
          <label><input type="radio" name="rebook_mode_${id}" value="fixed" ${a.rebook_mode !== "remaining" ? "checked" : ""} ${noLimit ? "disabled" : ""}>
            <input name="rebook_amount" type="number" min="1" step="1" inputmode="numeric" value="${esc(a.rebook_amount || 10)}" ${noLimit ? "disabled" : ""}> bids</label>
          <label><input type="radio" name="rebook_mode_${id}" value="remaining" ${a.rebook_mode === "remaining" ? "checked" : ""} ${noLimit ? "disabled" : ""}>
            what was left when it cancelled</label>
        </div>
        <div class="bk-row">
          <button type="button" data-act="auto" data-id="${id}">Save booking settings</button>
          <span class="saved">${esc(this._saved["auto:" + a.auction_id] || "")}</span>
        </div>
        <div class="note">${noLimit
          ? "Set a bid limit above to allow auto-cancel and auto-rebook."
          : `Crowded means more than your alert number of bidders. Nothing is ever booked past your bid limit (${num(a.bid_limit)}).`}
          The first booked bid is placed right away, so booking 1 is the same as bidding once.
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
    this.shadowRoot.querySelectorAll(".ago").forEach((el) => {
      el.textContent = `as of ${fmtDuration((now - Number(el.dataset.since)) / 1000)} ago`;
    });
  }

  async _saveLimit(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const id = form.dataset.id;
    const raw = form.querySelector("input").value.trim();
    const limit = raw === "" ? 0 : Math.max(0, parseInt(raw, 10) || 0);
    try {
      await this._hass.callService("auction_watcher", "set_bid_limit", { auction_id: id, limit });
      this._saved[id] = limit ? "Saved" : "Limit removed";
    } catch (err) {
      this._saved[id] = "Couldn't save: " + (err?.message || err);
    }
    form.querySelector("input").blur();
    this._maybeRender(true);
    setTimeout(() => { delete this._saved[id]; this._maybeRender(true); }, 4000);
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
  .summary { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px 0; }
  .pill { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 16px; padding: 6px 12px; font-size: 14px; }
  .pill.hot { border-color: var(--warning-color, #ff9800); }
  .tabs { display: flex; gap: 4px; padding: 12px 16px 0; overflow-x: auto; scrollbar-width: none; border-bottom: 1px solid var(--divider-color); }
  .tabs::-webkit-scrollbar { display: none; }
  .tab { background: none; border: 0; border-bottom: 2px solid transparent; color: var(--secondary-text-color); padding: 10px 14px;
         font-size: 15px; cursor: pointer; white-space: nowrap; }
  .tab.on { color: var(--primary-color); border-bottom-color: var(--primary-color); }
  .count { font-size: 12px; background: var(--divider-color); border-radius: 10px; padding: 1px 7px; margin-left: 4px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 16px; padding: 16px; }
  .empty { color: var(--secondary-text-color); padding: 24px 4px; }
  .card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px); padding: 14px;
          box-shadow: var(--ha-card-box-shadow, none); border: 1px solid var(--divider-color); display: flex; flex-direction: column; gap: 10px; }
  .card.opp { border: 2px solid var(--warning-color, #ff9800); }
  .card.capped { opacity: .8; }
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
  .tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
  .tag { font-size: 11px; border: 1px solid var(--divider-color); border-radius: 8px; padding: 0 6px; color: var(--secondary-text-color); }
  .big { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; text-align: center; background: var(--secondary-background-color);
         border-radius: 8px; padding: 8px 4px; }
  .big .v { font-size: 20px; font-weight: 500; font-variant-numeric: tabular-nums; }
  .timer.urgent, .v.urgent { color: var(--error-color, #db4437); }
  .ago { font-size: 11px; }
  .k { color: var(--secondary-text-color); font-size: 12px; }
  .rows { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 12px; font-size: 14px; }
  .rows .k { display: block; }
  .line, .hist { font-size: 13px; }
  .muted { color: var(--secondary-text-color); }
  form.limit { display: flex; align-items: center; gap: 8px; font-size: 14px; border-top: 1px solid var(--divider-color); padding-top: 10px; }
  form.limit input { width: 90px; padding: 6px; border-radius: 6px; border: 1px solid var(--divider-color);
                     background: var(--primary-background-color); color: var(--primary-text-color); }
  form.limit button { padding: 6px 12px; border-radius: 6px; border: 0; background: var(--primary-color); color: var(--text-primary-color, #fff); cursor: pointer; }
  .saved { color: var(--secondary-text-color); font-size: 13px; }
  details.booking { border-top: 1px solid var(--divider-color); padding-top: 8px; font-size: 14px; }
  details.booking summary { cursor: pointer; color: var(--primary-color); font-weight: 500; }
  .badge.on { background: var(--success-color, #43a047); color: #fff; }
  .bk-row, .bk-line { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 8px; }
  .bk-check { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
  details.booking input[type=number] { width: 80px; padding: 6px; border-radius: 6px; border: 1px solid var(--divider-color);
    background: var(--primary-background-color); color: var(--primary-text-color); }
  details.booking button { padding: 6px 12px; border-radius: 6px; border: 0; background: var(--primary-color);
    color: var(--text-primary-color, #fff); cursor: pointer; }
  details.booking button.secondary { background: var(--secondary-background-color); color: var(--primary-text-color);
    border: 1px solid var(--divider-color); }
  details.booking button:disabled, details.booking input:disabled { opacity: .5; cursor: default; }
  .note { color: var(--secondary-text-color); font-size: 12px; margin-top: 8px; }
  @media (max-width: 420px) { .grid { grid-template-columns: 1fr; padding: 12px; } .rows { grid-template-columns: 1fr; } }
`;

customElements.get("auction-watcher-panel") || customElements.define("auction-watcher-panel", AuctionWatcherPanel);
