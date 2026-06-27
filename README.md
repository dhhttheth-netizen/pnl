# Shortlist PnL — fully automated, free, public link

**What this gives you:** open one link, on any device, anytime the market's
open (or closed), and see live PnL on today's shortlisted short candidates.
The only thing YOU do each morning is paste a fresh Upstox token into a
small private page — everything else (scanning, shortlisting, dashboard)
runs on its own, even if your laptop is off.

---

## ⚠️ Before anything else: rotate your Upstox token

The access token shared earlier in this conversation is no longer private.
**Go to your Upstox developer dashboard and regenerate/revoke it now.**
None of the setup below needs that old one — you'll generate a fresh token
each morning anyway.

---

## How it fits together

```
 You, each morning           Automatic, every day, forever
 ──────────────────          ─────────────────────────────────────────
 1. Generate Upstox    →     2. Paste it into the token webpage
    token (~30 sec)             │
                                 ▼
                          3. Webpage encrypts it, saves as a
                             GitHub Actions secret
                                 │
                                 ▼
                          4. ~7:00 AM IST: GitHub Actions runs
                             scanner.py automatically (your laptop
                             can be off — this runs on GitHub's servers)
                                 │
                                 ▼
                          5. scanner.py writes shortlist_latest.csv
                             and commits it to the repo
                                 │
                                 ▼
                          6. Render sees the new commit, auto-redeploys
                             the backend with the fresh shortlist
                                 │
                                 ▼
                          7. Anyone opens your dashboard link, anytime →
                             sees today's live PnL, zero further action
```

---

## Part 1 — Create the GitHub repo

1. Go to https://github.com/new
2. Name it `shortlist-pnl` (or anything)
3. **Public** repo (Render's free tier needs this)
4. Don't initialize with anything. Click **Create repository**.
5. Click **uploading an existing file** and drag in every file from this
   folder, preserving the folder structure:
   - `main.py`
   - `index.html`
   - `requirements-backend.txt`
   - `scanner.py`
   - `shortlist_latest.csv` (placeholder — gets overwritten automatically each morning)
   - `render.yaml`
   - `token-webpage/token_server.py`
   - `token-webpage/requirements.txt`
   - `.github/workflows/daily-scan.yml`

   GitHub's drag-and-drop preserves folder paths if you drag whole folders,
   or you may need to type the path (e.g. `token-webpage/token_server.py`)
   in the upload box for nested files.
6. Commit changes.

---

## Part 2 — Create a GitHub Personal Access Token (PAT)

This is a **separate, one-time** credential — not the daily Upstox token.
It lets the token-webpage update your repo's secret on your behalf.

1. Go to https://github.com/settings/tokens?type=beta (Fine-grained tokens)
2. **Generate new token**
3. Give it a name like `shortlist-secret-updater`
4. **Repository access** → Only select repositories → choose `shortlist-pnl`
5. Under **Permissions** → **Repository permissions** → find **Secrets** →
   set to **Read and write**
6. Also set **Actions** → **Read and write** (needed for the "trigger scan now" button)
7. Generate the token, **copy it somewhere safe** — you'll paste it into
   Render in the next step (you won't see it again after leaving the page)

---

## Part 3 — Deploy to Render (free)

1. Go to https://render.com, sign up with GitHub (no credit card)
2. **New** → **Blueprint** → connect your `shortlist-pnl` repo
3. Render reads `render.yaml` and shows **three services**:
   - `shortlist-pnl-backend`
   - `shortlist-pnl-dashboard` ← **this is the link you share**
   - `shortlist-token-updater` ← **this is your private morning page**
4. Click **Apply**

### Fill in the token-updater's secret environment variables
After deploy, go to the `shortlist-token-updater` service → **Environment**:
| Key | Value |
|---|---|
| `GITHUB_PAT` | the PAT you created in Part 2 |
| `GITHUB_REPO` | `yourusername/shortlist-pnl` |
| `PAGE_PASSWORD` | any password you choose — protects your private page |

Save — Render redeploys that one service automatically.

### Point the dashboard at your real backend URL
Same as before: open `index.html` on GitHub, edit the `API_BASE` /
`WS_URL` lines near the top of the `<script>` block to match your actual
`shortlist-pnl-backend` URL from Render, commit.

---

## Part 4 — Your daily routine (the only manual step, ever)

1. Open the Upstox app/website, generate a fresh access token (~30 sec)
2. Open your `shortlist-token-updater` link (bookmark it on your phone)
3. Enter your `PAGE_PASSWORD` and paste the token → **Update**
4. Done. The 7am job picks it up automatically tomorrow — or if you want
   today's shortlist refreshed immediately, there's a manual trigger
   available by calling `/trigger-scan-now` on the same service (same
   password), or from your repo's **Actions** tab → "Daily Shortlist Scan" →
   **Run workflow**.

If you forget to paste a token before 7am, that morning's automated run
will fail cleanly (logged in the repo's **Actions** tab) rather than
silently using stale or wrong data — yesterday's `shortlist_latest.csv`
just stays in place until you paste a new token and re-trigger.

---

## Things worth knowing

- **Free tier sleeps.** Both the dashboard backend and the token-updater
  spin down after 15 idle minutes, waking in ~30-60s on first use.
  Totally fine for this use case.
- **The scan itself runs on GitHub's servers**, not Render and not your
  laptop — that's what makes "even if I don't have my laptop" true.
- **Token security:** your daily Upstox token is encrypted before it
  ever leaves the token-webpage (libsodium, the same method GitHub uses
  internally) and is stored only as an encrypted GitHub Actions secret —
  never in plain text, never committed to the repo.
- **The GitHub PAT** (Part 2) is the one truly long-lived credential in
  this system. Keep it private — anyone with it could read/write secrets
  on this one repo. If it's ever exposed, revoke and regenerate it the
  same way you'd rotate the Upstox token.
- **Updating shortlisting logic later:** edit `scanner.py` directly on
  GitHub and commit — next morning's run uses the new logic automatically.
