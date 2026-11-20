# Running the pushers on a server

This guide puts the whole project on a small Linux server. Once it's set up,
two jobs run on their own:

- **Every day at 06:00:** `push_all.py` runs every enabled pusher and emails
  the report.
- **Every 15 minutes:** `stock_sync.py` reads new orders, takes sold copies
  down on every marketplace and accepts orders.

Nothing has to stay on on your own PC. The setup is one script.
[GitHub runs it](../.github/workflows/tests.yml) on a fresh Ubuntu machine on
every push, so it's known to work.

- [What you need](#what-you-need)
- [1. Rent a server](#1-rent-a-server)
- [2. Log in](#2-log-in)
- [3. Install](#3-install)
- [4. Give it the Bitwarden token](#4-give-it-the-bitwarden-token)
- [5. Configure](#5-configure)
- [6. The products file (books.json)](#6-the-products-file-booksjson)
- [7. Test](#7-test)
- [Everyday use](#everyday-use)
- [Updating](#updating)
- [Security and backups](#security-and-backups)
- [Troubleshooting](#troubleshooting)

## What you need

- **A Linux server:** Ubuntu 24.04 or 22.04, or Debian 12. The smallest size
  is enough (1–2 CPUs, 2 GB RAM).
- **The Bitwarden machine account access token** of the project
  `product-pushers`, the one your PC uses.
- **About 30 minutes.**

## 1. Rent a server

Any provider works. Typical choices, about €4–6 a month:

| Provider | Plan | Where |
|---|---|---|
| Hetzner Cloud | CX22 | Germany / Finland |
| OVHcloud | VPS-1 | France / Germany |
| DigitalOcean | Basic Droplet 2 GB | Frankfurt / Amsterdam |

Choose **Ubuntu 24.04**. Add your SSH key when the provider asks; it's safer
than a password. Note the server's **IPv4 address**.

The server's address stays the same. That matters because **eMAG and OnBuy
only accept API calls from IP addresses you register with them**. The setup
script prints the address at the end.

## 2. Log in

From PowerShell on your PC:

```powershell
ssh root@<server-ip>
```

## 3. Install

On the server:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/DavisGalla/products-api-push.git /tmp/pushers-setup
TIMEZONE=Europe/Madrid bash /tmp/pushers-setup/deploy/setup_server.sh
```

The script:

1. installs Python, git and fonts
2. creates a user `pushers`, which runs the jobs and has no login
3. puts the code in `/opt/product-pushers`, with its own Python environment
4. installs the Bitwarden CLI `bws` 2.1.0, after checking its official checksum
5. creates the empty token file `/etc/product-pushers.token`, readable only by
   root and `pushers`
6. creates `push_all_config.json` from the example
7. installs and starts the two timers
8. runs a quick self-test and prints the server's IP address

**Options**, set as `NAME=value` in front of the command:

| Option | Default | Meaning |
|---|---|---|
| `TIMEZONE` | server's own | e.g. `Europe/Madrid` or `Europe/Riga`. The daily time is in this zone. |
| `DAILY_TIME` | `06:00` | When `push_all` runs. Set it after the scraper's run. |
| `SYNC_EVERY` | `15` | Minutes between stock syncs (5–60) |

Running the script again is safe. It updates the code, packages and timers,
and keeps your token and config.

## 4. Give it the Bitwarden token

On the server:

```bash
nano /etc/product-pushers.token
```

Paste the **access token** (one line), then save with Ctrl+O, Enter and
Ctrl+X. The token never appears on screen or in the shell history.

- **Don't** put it in the config file, in git, or in an email or chat.
- **Prefer a separate token for the server.** In Bitwarden you can create a
  second access token on the same machine account just for the server, so
  you can revoke it alone if needed.
- **The machine account must read only the real project.** The jobs run
  `bws run --`, which injects every secret the account can read. If the same
  account can also read a sandbox project (for example
  `product-pusher-sandbox`, with its own `EBAY_*` keys), the sandbox keys end up
  in the real runs. Give the sandbox project its own machine account, and run
  sandbox tests with `bws run --project-id <sandbox project id> -- ...` using
  that account's token.

## 5. Configure

```bash
nano /opt/product-pushers/push_all_config.json
```

| Setting | Set it to |
|---|---|
| `json_file` | The path of `books.json` **on the server** (see step 6) |
| `email.to` | Who gets the daily report, e.g. `["you@example.com", "boss@example.com"]` |
| `email.when` | `"always"`, or `"problems"` to only email when something failed or orders need you |
| `stock_sync.enabled` | `true` |
| `pushers` | `"enabled": true` for every marketplace you have an account for, with its options |

You can copy the `pushers` part from your PC's `push_all_config.json`.

## 6. The products file (books.json)

The pushers read the scraper's `books.json`. Pick one way to get it onto the
server:

- **Run the scraper on the server too:** best. Clone the scraper project, run
  it a little before `DAILY_TIME` (its own timer or cron), and point
  `json_file` at its output.
- **Copy it from your PC after each scrape.** In PowerShell:
  ```powershell
  scp C:\programming\scraper\books.json root@<server-ip>:/opt/product-pushers/books.json
  ```
  Then set `json_file` to `/opt/product-pushers/books.json`.

The stock sync uses the file's modification time as the time of the scrape,
so copy the file only when it's new.

## 7. Test

**Safe test.** Every pusher runs in its safe mode, nothing is sent to any
marketplace, and the report is printed on screen instead of emailed:

```bash
cd /opt/product-pushers
sudo -u pushers ./deploy/run.sh push_all --dry-run --no-email
```

**Email test.** Sends one email to the addresses in `email.to`:

```bash
sudo -u pushers ./deploy/run.sh push_all --test-email
```

**The timers:**

```bash
systemctl list-timers 'product-pushers-*'
```

You should see the next run time of both. Done: from now on it runs by
itself.

## Everyday use

| What | Command |
|---|---|
| When do the jobs run next? | `systemctl list-timers 'product-pushers-*'` |
| Output of the last daily run | `journalctl -u product-pushers-daily -n 200` |
| Output of the stock syncs today | `journalctl -u product-pushers-stock-sync --since today` |
| Full logs per run (also attached to the email) | `ls /opt/product-pushers/logs/` |
| Run the daily job now | `systemctl start product-pushers-daily` |
| Stock sync status (sales, stock sent) | `sudo -u pushers ./deploy/run.sh stock_sync status` |
| Pause everything | `systemctl stop product-pushers-daily.timer product-pushers-stock-sync.timer` |
| Resume | `systemctl start product-pushers-daily.timer product-pushers-stock-sync.timer` |

Run the `./deploy/run.sh` commands from `/opt/product-pushers`.

The daily email is also your **health check**. If it doesn't arrive one
morning, look at `journalctl -u product-pushers-daily`.

**Overlaps are safe.** The daily run and the stock sync never run at the same
time; they share one lock. A stock sync that starts during the daily run
stops with "in progress" (shown as failed in the journal), and the next one
15 minutes later runs normally.

## Updating

After new code is pushed to GitHub:

```bash
bash /opt/product-pushers/deploy/setup_server.sh
```

This is the same script. It pulls the new code and updates packages and
timers. Your token, config, state files (`*_posted.json`), stock ledger and
logs stay. None of them are in git.

## Security and backups

- **Firewall:** only SSH needs to be open. Nothing listens for connections.
  ```bash
  ufw allow OpenSSH && ufw --force enable
  ```
- **Automatic security updates:**
  ```bash
  apt-get install -y unattended-upgrades && dpkg-reconfigure -f noninteractive unattended-upgrades
  ```
- **Limited user:** the jobs run as `pushers`, with no login, and systemd
  doesn't let them change system files.
- **Secrets** live only in Bitwarden, plus the one token file on the server.
- **What to back up:** the state files are what keep the pushers from listing
  books twice, and the ledger keeps the stock sync's sales. Copy these
  occasionally, or use your provider's snapshots:
  - `/opt/product-pushers/*_posted.json`
  - `/opt/product-pushers/emag_ids.json`
  - `/opt/product-pushers/stock_ledger.json`
  - `/opt/product-pushers/push_all_config.json`

  For example, from your PC:
  ```powershell
  scp "root@<server-ip>:/opt/product-pushers/*.json" C:\backup\pushers\
  ```

## Troubleshooting

| Message | Cause | What to do |
|---|---|---|
| `No Bitwarden token in /etc/product-pushers.token and no .env file` | Step 4 not done | Put the token in the file |
| `bws: ... unauthorized` / `invalid access token` | Wrong or revoked token | Create a new access token in Bitwarden and replace the file's content |
| `Products file not found` | `json_file` points to a missing file | Step 6 |
| `Another push_all run is in progress` | The other job was running | Nothing; it runs next time. If it persists after a crash, delete `/opt/product-pushers/push_all.lock`. |
| eMAG / OnBuy refuse the login | Server IP not whitelisted | Send them the IP the setup printed (or `curl https://api.ipify.org`) |
| No email arrives | Gmail secrets or recipients | `sudo -u pushers ./deploy/run.sh push_all --test-email` shows the error |
