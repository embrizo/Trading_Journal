# Deploy on a free-tier GCP e2-micro

This runs the same `docker compose` stack the Pi uses, on Google Cloud's
**Always Free** e2-micro VM. It is the right shape for this app: an always-on
process holding a live WebSocket, with SQLite on a persistent disk — not a
serverless/Cloud Run workload.

## What "free" covers (and the limits)

- **1 × e2-micro per month**, and **only** in `us-west1`, `us-central1`, or
  `us-east1`. Any other region is billed.
- 30 GB of **standard** persistent disk (not SSD) free.
- 1 GB/month egress to most of the world free. This app's egress is tiny
  (Telegram/Discord sends, exchange WS), so you will not get near it.
- **The catch: e2-micro has ~1 GB RAM.** numpy + pandas + matplotlib on 1 GB is
  borderline. The fix is a swapfile (step 4). If the watcher still gets
  OOM-killed on a chart render, step up to **e2-small** (2 GB, ~$13–15/mo —
  your ฿329 credit covers most of it).

## 1. Create the VM

Console → Compute Engine → Create instance, or gcloud:

```bash
gcloud compute instances create break-signal \
  --zone=us-central1-a \
  --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-standard
```

Region/zone **must** be one of the three free ones above. The 30 GB boot disk
is where `./data` (both SQLite DBs, screenshots, backups) lives — no extra
volume needed at this scale.

> **Firewall:** the compose file is **outbound-only** by default (ports are
> commented out), so you need **no** ingress rule. Only if you later enable the
> TradingView webhook receiver / dashboard (port 8787) do you open a rule — and
> prefer locking its source to your own IP rather than `0.0.0.0/0`.

## 2. SSH in and install Docker

```bash
gcloud compute ssh break-signal --zone=us-central1-a
```

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker   # run docker without sudo
```

## 3. Get the code

```bash
git clone https://github.com/embrizo/Trading_Journal.git
cd Trading_Journal
```

## 4. Add a 2 GB swapfile (do this before the first build)

The image build and matplotlib both spike memory; swap keeps a 1 GB box alive.

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab   # survive reboot
free -h   # confirm Swap: 2.0Gi
```

## 5. Secrets: config.yaml + .env

```bash
cp config.example.yaml config.yaml
nano config.yaml     # fill Telegram token + chat id, Discord webhook, exchange, params
```

Put the Gemini key for the AI coach in a `.env` next to the compose file (it is
read by `${GEMINI_API_KEY}` in `docker-compose.yml`, and `.env` is gitignored):

```bash
echo "GEMINI_API_KEY=your-key-here" > .env
```

> **Note on the exchange:** OKX was DNS/403-blocked on your home ISPs. A GCP VM
> egresses from Google's network, so OKX may now be reachable — try
> `exchange: okx` first; keep `exchange: binance` as the known-good fallback.

## 6. Build and run

```bash
# AI_ENABLED=1 installs the Gemini + Anthropic SDKs so /ask, /review and the
# weekly narrative work. Leave it off if you only want alerts + the journal.
AI_ENABLED=1 docker compose build
docker compose up -d
docker compose logs -f           # watch it backfill, stream, and arm
```

> **Dockerfile note:** it adds `--extra-index-url .../piwheels.org` to get
> prebuilt ARM wheels on the Pi. On the x86 e2-micro that index simply has no
> matching wheels and pip falls back to PyPI's x86 manylinux wheels — nothing to
> change, no source compiles.

`restart: unless-stopped` + the `/etc/fstab` swap line mean it comes back on its
own after a VM reboot.

## 7. Verify it's actually working

A connection log is **not** proof data arrives (a lesson already learned in the
handoff). Confirm a real closed candle and, ideally, one live alert:

```bash
docker compose logs --since 30m | grep -iE "closed candle|signal|alert|backfill"
```

Then watch for the next 1D close to produce an alert in Telegram/Discord.

## Keeping the ฿329 credit honest

- The credit's highest-value use is the **Gemini coach API calls** (the "Gen AI"
  part). The e2-micro hosting is ~free on its own, so the credit mostly
  subsidizes the LLM usage.
- Watch billing the first week: Console → Billing → Reports, filter to this
  project. Free-tier e2-micro should show ~$0 for compute; if a line item
  appears, you likely picked a non-free region or disk type.

## If 1 GB proves too tight

Symptoms: the container is `OOMKilled` (`docker inspect break-signal | grep OOM`)
or disappears mid chart-render. Then:

```bash
gcloud compute instances stop break-signal --zone=us-central1-a
gcloud compute instances set-machine-type break-signal \
  --zone=us-central1-a --machine-type=e2-small
gcloud compute instances start break-signal --zone=us-central1-a
```

e2-small (2 GB) is no longer free, but your credit covers most of the ~$13–15/mo.
