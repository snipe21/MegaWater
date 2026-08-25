# WaterFlow - Standalone Deployment (no Home Assistant required)

This branch drops the Home Assistant Supervisor addon packaging entirely.
Same codebase, same features (Phases 1-3) - just runs as plain Docker
containers, with its own independent Cloudflare Tunnel. No HA installation,
no addon store, no config.yaml/options.json, no exposure to HA's own release
cycle or reverse-proxy config changes.

## What changed from the HA-addon version

- **Config no longer lives in HA addon options.** Every setting (branding,
  ChirpStack connection, domain, notifications) now lives in the database,
  editable from **Super Admin > Settings** in the PWA itself - including
  **Export** (JSON backup) and **Import** buttons, and a **Restart WaterFlow**
  button for the handful of settings that need a restart to take effect
  (flagged clearly in the UI).
- **First login is `admin` / `admin`** (Super Admin, at `/super`). Change
  this immediately via Settings > Change My Password - there's no config
  field to edit anymore, it's entirely self-service.
- **Cloudflared runs as its own container**, completely independent of
  Home Assistant's own tunnel (if you still run HA separately for other
  things, its tunnel is untouched by any of this).

## Custom-range PDF reports

Every "Download Report" section (Admin's per-unit Manage panel, Tenant's own
usage report) has a **Custom** button alongside Today/Week/Month - reveals
start/end date pickers, then downloads the same PDF the presets use, just
for that exact range.

## ChirpStack device naming stays in sync

Reassigning a meter to a different unit renames its ChirpStack device to
match (`Unit-{new_unit_number}`), the same way it's named when first
assigned. If the ChirpStack rename call fails (network hiccup etc.), the
local reassignment still succeeds - only the ChirpStack-side label goes
stale until the next successful rename; check Audit Log for
`chirpstack_rename_failed` if a device's name in ChirpStack looks wrong.

## Daily digest (Telegram)

Set **Telegram Bot Token**, **Telegram Digest Chat ID**, and **Digest Send
Time** in Super Admin > Settings > Notifications. Runs automatically once
configured - checks every 5 minutes and fires within the configured
send-time window, once per day. **Digest Time Zone Offset** defaults to 2
(SAST); adjust if the server's assumed local time is wrong.

Sends a text summary (per-unit usage, building total, any meters that went
quiet that day) plus a CSV with full detail, to the configured chat. Use
**Send Test Digest Now** in that same Settings section to confirm delivery
immediately rather than waiting for the scheduled time.

## Biometric login (fingerprint / Face ID)

Every portal (Super Admin, Admin, Tenant) supports passwordless login via
the device's own fingerprint/Face ID sensor, using WebAuthn platform
credentials. It's opt-in per device: log in with a password once, then use
**🔐 Biometric Login** in the sidebar to enable it on that device. From then
on, the login screen shows an "Unlock with Fingerprint / Face ID" button.

Requires **Domain Name** to be set in Super Admin > Settings (a real HTTPS
domain - it won't work over a bare LAN IP or plain http://, since WebAuthn
credentials are bound to the origin that created them).

## First deploy

```bash
git clone <this-repo> waterflow
cd waterflow
cp .env.example .env
nano .env   # paste your Cloudflare Tunnel token (see .env.example for how to get one)
docker compose up -d --build
```

Then:
1. Browse to `http://<server-ip>:8000/super`
2. Log in: `admin` / `admin`
3. **Settings tab** - fill in ChirpStack connection details, branding, domain,
   notifications. Save.
4. If any field showed "(restart required)", click **Restart WaterFlow Now**
   right there in the Settings tab - no server access needed.
5. **Settings > Change My Password** - replace the default immediately.
6. **Settings > Export Backup** - good habit to do this now, and before every
   future update, so you always have a fallback if something goes wrong.

## Day-to-day update workflow

```bash
cd /path/to/waterflow
git pull
docker compose up -d --build
```

That's the whole cycle. Your `.env` (tunnel token) is gitignored and never
touched by a pull. Your database (`./data/waterflow.db` - meters, tenants,
readings, billing history, and now all your settings too) lives outside the
container and survives rebuilds untouched.

**Good habit before pulling an update:** Super Admin > Settings > Export
Backup, just in case. Takes ten seconds, gives you a clean rollback point.

## Cloudflare Tunnel setup (if starting fresh)

1. Cloudflare Zero Trust dashboard > Networking > Tunnels > Create a tunnel
2. Name it (e.g. "waterflow"), choose **Docker** as the connector type
3. Copy the token from the generated command - just the token, not the whole
   `cloudflared tunnel run --token ...` line
4. Paste it into `.env` as `CLOUDFLARE_TUNNEL_TOKEN`
5. Back in the tunnel's **Public Hostname** tab, add a route:
   - Subdomain: whatever you want (e.g. `water`)
   - Domain: your domain
   - Service: **HTTP**, URL: `waterflow:8000` (container name, not localhost/IP -
     Docker Compose's internal network resolves this automatically)
6. `docker compose up -d`, then set that same hostname as `domain_name` in
   Settings once WaterFlow is up

## What's NOT in this branch

The `config.yaml` HA addon manifest was removed - this branch never installs
into Home Assistant's Add-on Store. If you ever want that path again, the
original HA-addon version of this project remains untouched in its own
branch/repo, exactly as it was.
