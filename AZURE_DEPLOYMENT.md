# Hosting Cryptera v3.2.4-correctness on Azure -- deployment plan

This is a plain-language plan for putting **this version** of Cryptera (branch
`v3.2.4-correctness`) on its own small Azure server, so it runs around the clock and does
two things automatically: (1) saves a snapshot of every scan to build this version's own
calibration corpus, and (2) sends you a Telegram alert whenever a real setup fires.

This is a **separate, independent deployment** from any other Cryptera version you may
already be running (e.g. a v4.x monitor on its own VM). It uses its own VM, its own
Telegram bot, and its own corpus -- nothing here reads, writes, or restarts anything on
another machine.

Good news: this version now has the same single-command shape v4.x already validated for
this purpose -- `python monitor.py`. When Telegram and Gemini keys are present, that one
process does both jobs every 15 minutes: it scores every symbol on your watchlist, saves
every snapshot to disk (the corpus), and narrates + alerts only when a setup actually
crosses into an active/conditional state. We just need a small computer that stays on
24/7 to run it.

---

## 1. The architecture, in plain terms

- **One small Linux virtual machine (VM)** in Azure, separate from any other Cryptera VM
  you run -- its own resource group, its own public IP.
- **This branch (`v3.2.4-correctness`) of your repo**, cloned from `Cryptera-private`,
  running inside its own Python virtual environment.
- **A background service** (systemd) that runs `monitor.py` forever, restarting it
  automatically if it ever crashes or the VM reboots.
- **A disk that persists** `snapshots/` and `data/` -- this version's own growing
  calibration corpus, independent of any other version's corpus. It survives restarts.
- **No public website** -- nothing is exposed to the internet except SSH (locked to your
  own IP). The VM only makes *outbound* calls (Binance, Gemini, Telegram).

---

## 2. What it costs

Azure for Students gives you **$100 in credit** (12 months) and, separately, **750 free
hours/month of a small "burstable" VM** for your first 12 months.

**Important if you already run another Cryptera VM on the same subscription:** the 750
free hours/month is a pool shared across your whole subscription, not per-VM. A single VM
running 24/7 already uses roughly the full 750 hours in a 30-day month, so a **second**
concurrent VM's compute time will likely be billed rather than covered by the free tier.

| Item | Cost |
|---|---|
| VM compute (B1s or B2ats_v2, 1-2 vCPU / 1GB RAM), billed (see note above) | ~$7-8/month |
| OS disk (~32GB Standard SSD) | ~$2-3/month |
| Public IP (Basic, so you can SSH in) | ~$3/month |
| **Total from your $100 credit** | **roughly $12-14/month for this second VM** |

Your $100 credit still covers many months of running both VMs concurrently. Gemini API
calls are billed **separately, outside Azure**, on your Gemini/Google AI account -- this
version's own `.env` (`DAILY_BUDGET`/`BUDGET_RESERVE` in `alerts/config.py`) caps how many
narration calls it makes per day, independent of any other version's budget.

---

## 3. An important fix this deployment needs (not a bug in your code)

`pandas-ta` -- the exact package this project's `requirements.txt` pins -- was removed
from PyPI entirely by its maintainer; the whole upstream project was taken down. A fresh
`pip install -r requirements.txt` on a new machine fails today because of this, not
because of anything wrong in Cryptera.

The fix: `pandas-ta-classic` is a community-maintained continuation of that exact same
codebase (same functions, same math -- all eight indicator functions `indicators.py` uses
are present and identical: ema, atr, rsi, macd, supertrend, stochrsi, adx, bbands). The
setup script (`deploy/setup_vm.sh`) installs it and adds a two-line shim so
`import pandas_ta` keeps working exactly as your code already expects -- **no changes to
any engine file.** This only affects fresh installs (your local machine likely already has
the old package cached).

I did **not** modify `requirements.txt` -- that's your call to make later if you want to.
The fix lives entirely in the deploy script.

---

## 4. Before you start: get a Telegram bot and push this branch

**New Telegram bot** (kept separate from any other Cryptera version's bot, so alerts are
never ambiguous about which engine fired them):

1. In Telegram, message **@BotFather** -> `/newbot` -> follow the prompts -> it gives you a
   bot token (`TELEGRAM_BOT_TOKEN`).
2. Message your new bot anything once (so it can see your chat), then visit
   `https://api.telegram.org/bot<token>/getUpdates` in a browser and read off your
   `chat.id` (`TELEGRAM_CHAT_ID`). A private group chat works too -- add the bot to it and
   use the group's (negative) chat id instead.
3. Fill both into your local `.env` (copy `.env.example` -> `.env` first if you haven't).

**Push this branch to your private repo** (from your own machine -- my sandbox doesn't
have your GitHub credentials, and shouldn't be the one committing your real files):

```bash
git add -A
git commit -m "Pre-deploy snapshot"
git push private v3.2.4-correctness
```

---

## 5. Step-by-step setup

Azure Cloud Shell gives you a terminal right inside the Azure Portal, already signed in --
go to [portal.azure.com](https://portal.azure.com), sign in, click the `>_` icon (Cloud
Shell), choose **Bash**.

### Step 1 -- Create the VM

Deliberately different resource-group/VM names from any other Cryptera deployment, so
this never collides with or accidentally touches another VM:

```bash
REGION="centralindia"   # matches your existing cryptera-vm (CRYPTERA-RG) -- confirmed
                         # working for VM creation on this account/subscription
RG="cryptera-v3-rg"
VM="cryptera-v3-vm"

az group create --name $RG --location $REGION

az vm create \
  --resource-group $RG \
  --name $VM \
  --image Ubuntu2404 \
  --size Standard_B2ats_v2 \
  --admin-username azureuser \
  --generate-ssh-keys \
  --public-ip-sku Basic

# If Standard_B2ats_v2 isn't available in centralindia for this subscription, use
# Standard_B1s instead -- or match whatever size cryptera-vm itself uses:
#   az vm show -g CRYPTERA-RG -n cryptera-vm --query hardwareProfile.vmSize -o tsv
```

This prints the VM's public IP -- save it.

**Lock SSH down to just you:**

```bash
MYIP=$(curl -s ifconfig.me)
az vm nsg-rule update --resource-group $RG --nsg-name "${VM}NSG" \
  --name default-allow-ssh --source-address-prefixes "$MYIP/32"
```

**Leave auto-shutdown off** -- the whole point is 24/7 monitoring.

### Step 2 -- Set a budget alert (safety net)

Azure Portal -> **Cost Management + Billing** -> **Budgets** -> **Add**. Scope it to your
subscription (it'll cover both VMs if you run more than one), amount under your remaining
credit, email alerts at 50/80/100%.

### Step 3 -- Get the code and secrets onto the VM

```bash
ssh azureuser@<the-public-ip-from-step-1>
```

On the VM, clone your private repo at this branch (a fine-grained GitHub PAT scoped
read-only to `Cryptera-private` works well):

```bash
git clone -b v3.2.4-correctness https://<your-token>@github.com/fornkb/Cryptera-private.git Cryptera-v3
cd Cryptera-v3
```

Copy your `.env` over from **your own machine's terminal** (not the VM, not Cloud Shell --
so the secrets never touch GitHub):

```powershell
scp "C:\Users\NKB\Desktop\cryptera\cryptera-v3.2.3-2026-06-08\.env" azureuser@<public-ip>:~/Cryptera-v3/.env
```

(Run that from PowerShell on Windows. No `scp`? Cloud Shell's upload icon works too, then
`mv` it into place on the VM.)

### Step 4 -- Run the setup script

```bash
cd ~/Cryptera-v3
bash deploy/setup_vm.sh
```

This installs Python, creates a virtual environment, installs dependencies (including the
pandas-ta fix), compiles every module, and registers (but doesn't yet start) the
background service.

### Step 5 -- Smoke-test before going 24/7

```bash
source venv/bin/activate
python monitor.py --once
```

This runs one cycle immediately -- check your Telegram (a "monitor online" heartbeat plus
any active setup) and `ls snapshots/` for new files. Fix any `.env` typo now, before it's
running unattended.

### Step 6 -- Turn on the 24/7 service

```bash
sudo systemctl enable --now cryptera-monitor
sudo systemctl enable --now cryptera-label.timer
sudo journalctl -u cryptera-monitor -f
```

Ctrl+C stops watching the log (the service keeps running). It now runs forever, through
crashes and reboots, scoring every 15 minutes, saving every snapshot, alerting on
Telegram.

The `cryptera-label.timer` (IMP-3) fires `tools.label_snapshots --horizon 24` once daily at
03:10 UTC, so realised TP/SL outcomes accrue automatically -- no operator discipline needed
to keep the corpus calibration-ready. Confirm it is scheduled with
`systemctl list-timers cryptera-label.timer`. Calibration itself stays manual on purpose
(see below).

---

## 6. Living with it

**Checking it's alive:** `sudo systemctl status cryptera-monitor` or
`sudo journalctl -u cryptera-monitor -n 50`.

**Updating the code:**

```bash
cd ~/Cryptera-v3 && git pull
sudo systemctl restart cryptera-monitor
```

**Running calibration tools** against the corpus accumulating in `snapshots/`. Labelling
already runs daily via `cryptera-label.timer`, so you normally only need eval + calibrate;
the manual `label_snapshots` call is there to catch up on demand:

```bash
cd ~/Cryptera-v3 && source venv/bin/activate
python -m tools.label_snapshots --horizon 24    # optional -- the timer does this daily
python -m tools.eval_engine
python -m tools.calibrate
```

Run these on the VM, or `scp`/rsync `snapshots/` and `data/` back to your own machine to
calibrate locally instead.

**After refreshing `data/calibration.json` (FA-16): restart the service.** `monitor.py`
loads calibration once, at first use, and caches it for the life of the process (it runs
24/7) -- a `tools.calibrate` run that overwrites `calibration.json` has no effect until you
`sudo systemctl restart cryptera-monitor`.

**Stopping everything** (pause spending): `az vm deallocate --resource-group cryptera-v3-rg
--name cryptera-v3-vm` stops compute billing (disk/IP still cost their small amount). To
remove it completely: `az group delete --name cryptera-v3-rg`.

---

## FAQ

**Will this affect my other Cryptera deployment or my local machine?** No. Separate VM,
separate resource group, separate Telegram bot, separate corpus. Nothing here reads from
or writes to another version's `snapshots/`/`data/`, and nothing on your local machine
changes.

**What if the VM's 1GB of RAM isn't enough?** The setup script adds a 2GB swap file. If
the service restarts a lot in the logs, resize: `az vm resize --resource-group
cryptera-v3-rg --name cryptera-v3-vm --size Standard_B2s` (no longer free, ~$30/month).

**What happens to the snapshot corpus over time?** Roughly 340KB/cycle across a 3-symbol
watchlist (~1GB/month). A 32GB disk gives two-plus years of runway; nothing gets silently
deleted (`--retention 0`, as the service runs, keeps everything).

**Do I need to expose the Flask web dashboard too?** Not for this (calibration + Telegram
alerts) -- this plan opens no web port. That's a separate step if you want it later.

**Can I delete the VM and start over?** Yes -- `az group delete --name cryptera-v3-rg`
removes everything cleanly and stops all billing for it. Nothing here is destructive to
your local project, your GitHub repo, or your other Cryptera deployment.
