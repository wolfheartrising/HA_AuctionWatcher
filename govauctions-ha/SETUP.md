# Auction alerts + GovAuctions safety bids: Home Assistant OS setup

## What you get

| Piece | What it does | Works right away? |
|---|---|---|
| `govauctions_live.yaml` | Every 10 s, checks **all your live auctions** (your My Bids list, plus any pasted one): price, leader, active bidders in the last minute, booked bids left, your credits. Pushes once when an auction drops to ≤2 bidders under your max price, with a Book button for that auction. Also warns about low credits and a dead feed. | Yes, after the cookie is in `secrets.yaml` |
| `auction_alerts.yaml` | Turns GovAuctions and Mac.bid alert emails into phone pushes. GovAuctions "outbid / running low" pushes carry a **Book safety bids** button. | Yes, after the IMAP step |
| `govauctions.yaml` | The safety button: books N bids (default 10) on the auction you're watching so you stay in while you log in. | After step 4 (capturing the booking request) |
| `dashboard-card.yaml` | One card with every control and button. | Yes |

### Safety limits built into the button
- Does nothing unless **armed**. It auto-disarms after 3 hours.
- At most **25 bids per tap**, whatever the settings say.
- **Daily limit** (default 50). It refuses and tells you why if a tap would go over.
- **Cooldown** (default 3 min) between bookings, and a double-tap only fires once.
- Only books on the one auction you've pasted in.
- Every attempt sends a push saying what happened. A failure usually means your login cookie expired.

---

## Step 1: Install a file editor
Settings → Add-ons → Add-on Store → **File editor** (or **Studio Code Server**) → Install → Start → turn on "Show in sidebar".

## Step 2: Turn on packages and copy the files
1. Open `/config/configuration.yaml` in the file editor. If there's already a `homeassistant:` line, add the `packages:` line under it; otherwise add both:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. Create a folder `/config/packages/`.
3. Copy `govauctions.yaml`, `govauctions_live.yaml` and `auction_alerts.yaml` into it.

## Step 3: Point notifications at your phone
1. Install the Home Assistant app on your phone and log in (this creates the notify service).
2. In HA go to Developer tools → Actions, type `notify.mobile_app`, and note the name, for example `notify.mobile_app_pixel_8`.
3. In `govauctions.yaml`, replace `notify.mobile_app_YOUR_PHONE` with it. There's only one place to change.

## Step 4: Capture the GovAuctions booking request (for the safety button)
1. On your computer, log in to GovAuctions and open a **cheap** auction you don't mind spending 1 bid on.
2. Press **F12** → **Network** tab → click **Fetch/XHR** to filter.
3. On the site, type **1** in "Bid count" and press **Book**.
4. A new request appears in the list (probably under `/api/...`). Right-click it → **Copy** → **Copy as cURL**.
5. Paste it into Notepad and:
   - **Cut out the Cookie line's value** and keep it for step 5. Do the same for anything named `token`, `csrf` or `authorization`.
   - Send me the rest (URL, method, body). I'll fill in the `rest_command` in `govauctions.yaml` and tighten the success check.
6. On the site, click **Cancel** on the booked bid if you want it back.

## Step 5: Add your secret
Open `/config/secrets.yaml` and add the lines from `secrets.example.yaml`, pasting your real cookie value. Never paste it into chat.

## Step 6: Email alerts (both sites)
1. In both GovAuctions and Mac.bid account settings, turn on email notifications (outbid, ending soon, won).
2. HA: Settings → Devices & services → Add integration → **IMAP**.
   - Gmail: server `imap.gmail.com`, port 993. Gmail needs an **App password** (Google Account → Security → 2-Step Verification → App passwords). Use it instead of your normal password.
   - Search: leave it as `UnSeen UnDeleted`.
3. After adding it, open the IMAP integration → **Configure** → enable "**text**" in the event message data. This lets the automation find the auction number in the email.

## Step 7: Restart and add the cards
0. Keep the raw feed out of your history database (it changes every 10 s). Add this to `configuration.yaml`, merging it with any `recorder:` section you already have:
   ```yaml
   recorder:
     exclude:
       entities:
         - sensor.govauctions_feed
         - sensor.govauctions_watch
   ```
1. Developer tools → YAML → **Check configuration**. It should say valid.
2. Restart Home Assistant.
3. Open a dashboard → ✏️ Edit → **+ Add card** → **Manual** → paste `dashboard-card.yaml`.
4. Turn on **Auction email alerts**.

## Step 8: Test safely
1. Set **Safety bids per tap** to `1`.
2. Paste a cheap auction's URL into **GovAuctions auction**.
3. Turn on **armed**, then tap **Book safety bids now**.
4. You should get a push, and the site should show "1 bids booked". Cancel it on the site if you like.
5. Set it back to 10.

---

## Using it
- **Before you step away:** paste the auction URL and arm it.
- **To book, any of these work:**
  - Tap **Book safety bids** on an outbid push, or on the push sent by "Send the Book button to my phone".
  - Say "**book safety bids**" or "**save my auction**" to Assist in the HA app.
  - Tap the dashboard button. On Android you can also add the script as a home-screen widget.
- Then open the site and take over.

10 bids typically lasts 1–3 minutes, depending on how many people are bidding.

## Known limits
- **Email delay:** an alert can arrive seconds to minutes after the event. That's fine for Mac.bid's fixed end times, but on GovAuctions treat it as "go look" rather than precise timing.
- **Cookie expiry:** when you get a "Safety bids FAILED" push, log in on the site again and copy a fresh cookie into `secrets.yaml`. Then Developer tools → YAML → reload REST commands, or restart.
- **Mac.bid live alerts aren't included:** Mac.bid's login token expires 30 minutes after login, so polling would break every half hour. Use email alerts or the Mac.bid app's own push notifications.
- **"Closed to new bidders":** GovAuctions locks auctions to people who haven't bid yet once they've run a while. If you haven't bid before that, you can't join at all, which is why that alert exists.
- **Active bidders** counts distinct names in the last 60 s, from the last ~9 bids the feed keeps, so 9 is the most it can show. People with booked bids count only once their bids actually fire.
