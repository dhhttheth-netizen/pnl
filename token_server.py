# token_server.py — tiny private page: paste today's Upstox token,
# it gets encrypted and pushed into your GitHub repo's Actions secret.
# ─────────────────────────────────────────────────────────────────
#   pip install fastapi uvicorn requests pynacl
#   uvicorn token_server:app --reload --port 8001
#
# This service holds ONE long-lived credential of its own: a GitHub
# Personal Access Token (PAT) with "repo" scope, set as the env var
# GITHUB_PAT below. That PAT is what lets it write the daily Upstox
# token into your repo's encrypted secrets store — it never stores
# the Upstox token itself anywhere except briefly in memory during
# the single request that forwards it to GitHub.
#
# Env vars required:
#   GITHUB_PAT        = a GitHub Personal Access Token with "repo" scope
#   GITHUB_REPO       = "yourusername/shortlist-pnl"   (owner/repo)
#   PAGE_PASSWORD     = a password YOU set, so randos can't overwrite your secret
# ─────────────────────────────────────────────────────────────────
import os
import base64
import requests
from nacl import encoding, public
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Upstox Token Updater")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GITHUB_PAT    = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "")  # e.g. "yourname/shortlist-pnl"
PAGE_PASSWORD = os.environ.get("PAGE_PASSWORD", "")
SECRET_NAME   = "UPSTOX_ACCESS_TOKEN"


def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def encrypt_secret(public_key_b64: str, secret_value: str) -> str:
    """Encrypt a secret using the repo's public key, per GitHub's libsodium spec."""
    public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted  = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


@app.get("/", response_class=HTMLResponse)
def form():
    return """
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Update Upstox Token</title>
      <style>
        body { font-family: monospace; background:#0a0d0f; color:#cdd6db;
               display:flex; align-items:center; justify-content:center;
               min-height:100vh; margin:0; }
        .box { background:#11161a; border:1px solid #1d2429; border-radius:8px;
               padding:32px; width:90%; max-width:420px; }
        h1 { font-size:16px; margin:0 0 18px; color:#2ee6a8; }
        label { display:block; font-size:12px; color:#6b7780; margin-bottom:6px; }
        input { width:100%; padding:10px; margin-bottom:16px; background:#0d1113;
                border:1px solid #1d2429; border-radius:4px; color:#cdd6db; font-family:monospace; }
        button { width:100%; padding:12px; background:#2ee6a8; color:#0a0d0f;
                 border:none; border-radius:4px; font-weight:bold; cursor:pointer; font-family:monospace;}
        button:hover { opacity:0.9; }
        #result { margin-top:14px; font-size:13px; white-space:pre-wrap; }
        .ok { color:#2ee6a8; } .err { color:#ff5d5d; }
      </style>
    </head>
    <body>
      <div class="box">
        <h1>UPDATE TODAY'S UPSTOX TOKEN</h1>
        <form id="f">
          <label>Page password</label>
          <input type="password" id="pw" autocomplete="off" required>
          <label>Fresh Upstox access token</label>
          <input type="password" id="token" autocomplete="off" required>
          <button type="submit">Update &amp; Trigger Scan</button>
        </form>
        <div id="result"></div>
      </div>
      <script>
        document.getElementById('f').addEventListener('submit', async (e) => {
          e.preventDefault();
          const result = document.getElementById('result');
          result.textContent = "Updating...";
          result.className = "";
          try {
            const res = await fetch('/update-token', {
              method: 'POST',
              headers: {'Content-Type':'application/json'},
              body: JSON.stringify({
                password: document.getElementById('pw').value,
                token: document.getElementById('token').value
              })
            });
            const data = await res.json();
            if(res.ok){
              result.textContent = "✓ " + data.message;
              result.className = "ok";
              document.getElementById('token').value = "";
            } else {
              result.textContent = "✗ " + (data.detail || "failed");
              result.className = "err";
            }
          } catch(err) {
            result.textContent = "✗ Network error: " + err;
            result.className = "err";
          }
        });
      </script>
    </body>
    </html>
    """


@app.post("/update-token")
async def update_token(request: Request):
    body = await request.json()
    password = body.get("password", "")
    token    = body.get("token", "").strip()

    if not PAGE_PASSWORD:
        raise HTTPException(500, "Server misconfigured: PAGE_PASSWORD not set")
    if password != PAGE_PASSWORD:
        raise HTTPException(403, "Wrong password")
    if not token:
        raise HTTPException(400, "Token is empty")
    if not GITHUB_PAT or not GITHUB_REPO:
        raise HTTPException(500, "Server misconfigured: GITHUB_PAT / GITHUB_REPO not set")

    # 1. Get the repo's public key (needed to encrypt the secret)
    key_resp = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/public-key",
        headers=_github_headers(), timeout=15,
    )
    if key_resp.status_code != 200:
        raise HTTPException(502, f"GitHub public-key fetch failed: {key_resp.status_code} {key_resp.text[:200]}")
    key_data = key_resp.json()

    # 2. Encrypt the token with that public key
    encrypted_value = encrypt_secret(key_data["key"], token)

    # 3. Push it as the repo secret
    put_resp = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/{SECRET_NAME}",
        headers=_github_headers(), timeout=15,
        json={"encrypted_value": encrypted_value, "key_id": key_data["key_id"]},
    )
    if put_resp.status_code not in (201, 204):
        raise HTTPException(502, f"GitHub secret update failed: {put_resp.status_code} {put_resp.text[:200]}")

    return {"message": "Token updated. Today's scan will use it at the next scheduled run (or trigger manually below)."}


@app.post("/trigger-scan-now")
async def trigger_scan_now(request: Request):
    """Optional: manually fire the workflow right now instead of waiting for 7am."""
    body = await request.json()
    if body.get("password", "") != PAGE_PASSWORD:
        raise HTTPException(403, "Wrong password")

    resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/daily-scan.yml/dispatches",
        headers=_github_headers(), timeout=15,
        json={"ref": "main"},
    )
    if resp.status_code != 204:
        raise HTTPException(502, f"Trigger failed: {resp.status_code} {resp.text[:200]}")
    return {"message": "Scan triggered — check the Actions tab on GitHub in ~30s."}


@app.get("/health")
def health():
    return {"status": "ok", "github_repo": GITHUB_REPO, "configured": bool(GITHUB_PAT and GITHUB_REPO and PAGE_PASSWORD)}
