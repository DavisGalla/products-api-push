# push_all.py reference

`push_all.py` links the pushers together. It runs every enabled pusher, one
after another, and then emails a single report from a Gmail account:

```
Subject: [Product push] PROBLEMS - 8 pushed, 1 rejected

Vinted                OK              5 pushed, 1 skipped, 40 already listed
Amazon EU             PROBLEMS        3 pushed, 1 rejected/errors, 2 skipped
                        DE: ! 400 INVALID: price too high
Fnac                  FAILED          0 pushed - exit code 1: Fnac login failed (ERROR): bad key
Casa del Libro        NOT CONFIGURED  missing CASADELLIBRO_API_URL, CASADELLIBRO_API_KEY
```

The email has a plain-text version and an HTML table, and the full output of
every pusher is attached as `push_log.txt`.

- [How it works](#how-it-works)
- [Setup](#setup)
- [Where credentials are stored](#where-credentials-are-stored) (Bitwarden Secrets Manager)
- [Usage](#usage)
- [Running it on a server](#running-it-on-a-server)
- [Result statuses](#result-statuses)
- [Files](#files)
- [Limitations](#limitations)
- [Testing](#testing)

## How it works

```
products JSON (scraper output)
        │
        ▼
bws run (Bitwarden Secrets Manager) ── credentials as environment variables
        │
        ▼
push_all.py ── push_all_config.json (what to run)
        │
        ├─► vinted_api_push.py push …   ─┐
        ├─► ebay_push.py …               │  each runs as its own process,
        ├─► amazon_push.py push …        │  one after another, with --yes
        ├─► casadellibro_push.py push …  │
        ├─► fnac_push.py push …          │
        ├─► abebooks_push.py push …      │
        ├─► elcorteingles_push.py push … │
        ├─► carrefour_push.py push …     │
        └─► kaufland_push.py push …      ─┘
        │
        ├─► logs/<date_time>/<pusher>.log + report.txt
        └─► Gmail (SMTP) ─► report email
```

The pushers are not changed, so each one still works on its own. The runner
works out each pusher's result from three things:

| Source | Gives |
|---|---|
| The pusher's state file, before and after (`posted_api.json`, `amazon_posted.json`, …) | **pushed**: new or changed entries |
| Its output | **skipped**: `SKIP` lines. **rejected/errors**: `!` lines and batch failures. **already listed**: `already pushed` lines. |
| Its exit code | **failed**: non-zero, e.g. bad credentials or a missing file |

One pusher failing doesn't stop the others.

## Setup

**1. The config file.** Copy `push_all_config.example.json` to
`push_all_config.json`, then edit it:

```json
{
  "json_file": "C:/programming/scraper/books.json",
  "timeout_minutes": 60,
  "email": {"to": ["you@example.com", "boss@example.com"], "when": "always"},
  "pushers": {
    "vinted":        {"enabled": true,  "args": ["--catalog-id", "1234", "--package-size-id", "1"]},
    "ebay":          {"enabled": false, "args": ["--market", "EBAY_ES"]},
    "amazon":        {"enabled": true,  "args": ["--markets", "all"]},
    "casadellibro":  {"enabled": false, "args": []},
    "fnac":          {"enabled": false, "args": []},
    "abebooks":      {"enabled": true,  "args": []},
    "elcorteingles": {"enabled": false, "args": []},
    "carrefour":     {"enabled": false, "args": []},
    "kaufland":      {"enabled": false, "args": ["--storefront", "es"]}
  }
}
```

| Key | Meaning |
|---|---|
| `json_file` | Products file passed to every pusher. A relative path is relative to this folder. |
| `timeout_minutes` | A pusher that runs longer than this is killed and reported as `FAILED`. You can also set it per pusher. |
| `email.to` | Report recipients. The `REPORT_TO` variable overrides it. The default is `GMAIL_ADDRESS`. |
| `email.when` | `always` sends a report after every run. `problems` sends one only when something was rejected or failed. |
| `pushers.<name>.enabled` | Whether this pusher is part of a normal run. |
| `pushers.<name>.args` | Extra options for that pusher, exactly as on its command line (see its doc). Don't add `--yes`; the runner adds it. |

**2. The credentials.** Store them in **Bitwarden Secrets Manager**, as set out
in [Where credentials are stored](#where-credentials-are-stored). A `.env` file
also works, as a simpler alternative. A pusher whose credentials are missing is
reported as `NOT CONFIGURED` and isn't run.

**3. Gmail.** Use a Gmail account just for sending reports, not a personal one.
Gmail doesn't accept the normal account password from scripts, so create an
**App Password**:

1. Google Account → **Security** → turn on **2-Step Verification** (required).
2. Google Account → **Security** → **App passwords** (or search "App passwords"),
   then create one, for example named "product pushers".
3. Copy the 16-character password straight away, because Google shows it only
   once. Store it as the secret `GMAIL_APP_PASSWORD`, and the address as
   `GMAIL_ADDRESS`. The spaces Google shows don't matter.

If the App Passwords page says the setting isn't available, 2-Step Verification
is off, or the account is a work/school account or uses Advanced Protection.

Then check it:

```powershell
bws run -- python push_all.py --test-email
```

Mail is sent through `smtp.gmail.com` on port 465 (TLS). An App Password can
be revoked at any time without changing the account password. Work or school
accounts and accounts with Advanced Protection may not offer App Passwords.

## Where credentials are stored

**Rules that apply whatever you choose:**
- Never put credentials in git, in `push_all_config.json`, or in the scripts.
  `.env` is in `.gitignore`.
- Give every marketplace and Gmail its own key, so one can be revoked without
  touching the others.
- If a key leaks (for example in a screenshot, a chat or a commit), revoke it
  in that marketplace's back office and create a new one.

### Recommended: Bitwarden Secrets Manager

All credentials (Gmail and every marketplace) live in a Bitwarden
**Secrets Manager** project. Bitwarden's command-line tool `bws` fetches them
when a run starts, and passes them to the scripts as environment variables:

```
bws run -- python push_all.py
```

The scripts already read every credential from environment variables, so they
need no Bitwarden-specific code. The only secret stored on the machine is one
Bitwarden **access token**. This setup was tested end to end on 2026-11-05: the
Gmail credentials came from Bitwarden and the report email was delivered.

**Why Bitwarden instead of a `.env` file:**
- **No plain-text credentials on the server.** The only secret stored there is
  the access token, and it can be revoked on its own.
- **One place to change a key.** Update the secret in Bitwarden, and the next
  run uses the new value. Nothing on the server needs editing.
- **Team access.** Several people can manage the secrets, with an access log,
  without sharing files.
- **Cost.** The free plan allows 2 users, 3 projects and 3 machine accounts,
  which is enough for this setup.

Secrets Manager is a separate product from the normal Bitwarden password
manager. You reach it from the bottom-left menu of the Bitwarden web app, next
to *Password Manager* and *Admin Console*.

#### One-time setup in Bitwarden

1. **Organization.** Create (or use) a Bitwarden organization, then open
   **Secrets Manager** from the bottom-left menu.
   - If Secrets Manager isn't enabled, go to the Admin Console →
     **Billing → Subscription** → **Add Secrets Manager**. The free plan is enough.
   - If it says you have no access, go to the Admin Console → **Members** →
     your user, and turn on **Secrets Manager** access.
2. **Project.** In Secrets Manager, choose **New → Project** and name it
   `product-pushers`.
3. **Secrets.** For each credential, choose **New → Secret** and put it in the
   `product-pushers` project. The secret's **Name** becomes the environment
   variable name, so it must exactly match the names in `.env.example`:

   | Secret name | Value |
   |---|---|
   | `GMAIL_ADDRESS` | sender Gmail address |
   | `GMAIL_APP_PASSWORD` | Gmail App Password (see [Setup](#setup)) |
   | `REPORT_TO` | optional, comma-separated recipients |
   | `VINTED_PRO_TOKEN`, `EBAY_…`, `AMAZON_…`, `CASADELLIBRO_…`, `FNAC_…`, `ABEBOOKS_…`, `ELCORTEINGLES_…`, `CARREFOUR_…`, `KAUFLAND_…` | marketplace credentials, added when you have them |

4. **Machine account.** Choose **Machine accounts → New** and name it, for
   example, `product-pushers-server`.
   - Open it, then on the **Projects** tab add `product-pushers` with **Can read**.
   - On the **Access tokens** tab, choose **Create access token**. Copy the
     token right away, because Bitwarden shows it only once.

   Use one machine account (and token) per machine, so each machine can be
   revoked on its own.

#### Install `bws`

Download `bws` from Bitwarden's official GitHub releases,
<https://github.com/bitwarden/sdk-sm/releases>, using the latest `bws-v…`
release (2.1.0 at the time of writing). Pick the file for your system:

- **Windows:** `bws-x86_64-pc-windows-msvc-<version>.zip`
- **Linux:** `bws-x86_64-unknown-linux-gnu-<version>.zip`

Compare the file's SHA-256 with `bws-sha256-checksums-<version>.txt` from the
same release, then unzip it.

- **Windows:** this setup puts `bws.exe` in `%LOCALAPPDATA%\Programs\bws`, adds
  that folder to the user `PATH`, and opens a new terminal.
- **Linux:** run `sudo install bws /usr/local/bin/`.

Check it with `bws --version`.

#### Give `bws` the access token

`bws` reads the token from the `BWS_ACCESS_TOKEN` environment variable. Never
type it in a command, chat or file that might be shared or committed.

**Windows (your own PC)**: copy the token, then run this in PowerShell. It
reads the token from the clipboard, so the token doesn't end up in the command
history:

```powershell
[Environment]::SetEnvironmentVariable("BWS_ACCESS_TOKEN", (Get-Clipboard).Trim(), "User"); "saved, length: " + ([Environment]::GetEnvironmentVariable("BWS_ACCESS_TOKEN","User")).Length
```

The length shown should be roughly 90 characters, not 1. Open a new terminal
afterwards, and copy something else so the token leaves the clipboard. Pasting
into a masked `Read-Host -AsSecureString` prompt can save only the first
character, which is why the clipboard method is used.

**Linux server**: put the token in a file that only the user running the
pushers can read, and load it in the job:

```bash
sudo install -m 600 -o pusher /dev/null /etc/product-pushers.token   # "pusher" = the user that runs the job; then put the token in the file
# in the job:  BWS_ACCESS_TOKEN="$(cat /etc/product-pushers.token)" bws run -- python3 push_all.py
```

**EU accounts:** if your Bitwarden account is on `vault.bitwarden.eu`, also set
`BWS_SERVER_URL=https://vault.bitwarden.eu`. Accounts on `vault.bitwarden.com`
need nothing extra.

#### Check it

```powershell
bws project list                               # should show product-pushers
bws run -- python push_all.py --test-email     # sends a test email using the Bitwarden secrets
```

`bws secret list` prints the secret **values** in plain text. Don't run it
where others can see the screen or the output gets saved.

#### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `bws project list` / `secret list` come back empty | The machine account has no access to the project, or the secrets aren't in the project | Machine account → **Projects** → add `product-pushers` with **Can read**. Check that each secret's **Project** is `product-pushers`. |
| Authentication error from `bws` | Wrong or truncated token (check its length), or an EU account without `BWS_SERVER_URL` | Set the token again, or create a new one. |
| `Test email failed: … Username and Password not accepted` | Wrong App Password, or 2-Step Verification was turned off (which revokes App Passwords) | Create a new App Password and update the secret. |
| A pusher shows `NOT CONFIGURED` | Its secrets aren't in the project, or a name is misspelled | Add them with the exact names from `.env.example`. |
| `bws` is not recognised | The terminal was opened before `PATH` was changed | Open a new terminal. |

#### Rotating a credential

1. Create a new key in that marketplace (or Gmail).
2. Paste it into the secret in Bitwarden.
3. Revoke the old key.

Nothing on the server changes. To cut a machine off, revoke its access token in
the machine account.

### Alternatives

| Option | Good for | How |
|---|---|---|
| **`.env` file** | Quick local testing, or no Bitwarden | Copy `.env.example` to `.env` next to the scripts and fill it in. Run `python push_all.py` without `bws`. On Linux, run `chmod 600 .env`. Real environment variables take precedence over `.env`. |
| **Server environment variables** | One server, set up by an admin | The same variable names in a systemd `EnvironmentFile=` (mode 600) or in the scheduler's environment. |
| **Other secrets managers** (Doppler, Infisical, AWS / Google Cloud Secret Manager, HashiCorp Vault) | Companies already using one | Use their "run with secrets" command, e.g. `doppler run -- python push_all.py`. No code change is needed. |

## Usage

With Bitwarden, put `bws run --` in front of every command. With a `.env`
file, leave it out.

```powershell
bws run -- python push_all.py --test-email            # Gmail works?
bws run -- python push_all.py --dry-run --no-email    # every pusher in its safe mode, report printed only
bws run -- python push_all.py --dry-run               # same, and email the report
bws run -- python push_all.py                         # real run of all enabled pushers + email
bws run -- python push_all.py --only vinted,amazon    # only these (also runs disabled ones you name)
bws run -- python push_all.py --no-email              # real run, report printed only
```

`--dry-run` runs each pusher in its safe mode: `--validate-only` for Vinted
and Amazon, `--dry-run` for the rest. Casa del Libro, El Corte Inglés and Carrefour still
need credentials in dry-run mode, because they check the catalog.

Exit codes are `0` when everything ran, `1` when a pusher failed, and `2` when
the report email couldn't be sent. A scheduler can alert on non-zero.

## Running it on a server

1. Copy the repository to the server, and add `push_all_config.json`.
2. Install `bws`, and give the server its own machine-account token (see
   [Bitwarden](#recommended-bitwarden-secrets-manager)).
3. Make sure the products JSON is on the same machine: either run the scraper
   there too, or copy its output across.
4. Schedule the run.

**Linux (cron)**, daily at 06:00, after the scraper:

```
0 6 * * * cd /opt/product-pushers && BWS_ACCESS_TOKEN="$(cat /etc/product-pushers.token)" /usr/local/bin/bws run -- /usr/bin/python3 push_all.py >> logs/cron.log 2>&1
```

**Windows (Task Scheduler)**: the task runs as your user, so it sees the
`BWS_ACCESS_TOKEN` user variable:

```powershell
schtasks /Create /TN "Product push" /SC DAILY /ST 06:00 /TR "cmd /c cd /d \"C:\programming\product pushers\" && bws run -- python push_all.py"
```

If Bitwarden can't be reached, or the token is wrong, the run stops before
any pusher starts. No report email can be sent in that case, because the Gmail
password comes from Bitwarden too, so watch the scheduler's exit code or
`logs/cron.log`.

`push_all.py` uses a lock file, so two runs can't overlap and push twice. A
lock older than 6 hours is treated as left over from a crash.

## Result statuses

| Status | Meaning |
|---|---|
| `OK` | The pusher finished and nothing was rejected. |
| `PROBLEMS` | It finished, but some items were rejected. Each rejection is listed with the marketplace's reason. |
| `FAILED` | It stopped with an error (credentials, missing file, crash) or timed out. Its last output lines are shown. |
| `NOT CONFIGURED` | Its credentials are missing, so it wasn't run. |

## Files

| File | In git? | Contents |
|---|---|---|
| `push_all.py` | yes | The runner. |
| `push_all_config.example.json`, `.env.example` | yes | Templates. |
| `push_all_config.json` | no | Your configuration. |
| `.env` | no | Optional: credentials, if you don't use Bitwarden. |
| `logs/<date_time>/` | no | One `<pusher>.log` per pusher, plus `report.txt`. Delete old folders when you like. |
| `push_all.lock` | no | Exists only while a run is in progress. |

## Limitations

- **Results are read from the pushers' output.** If a pusher's messages change,
  the counts in the report can be off. The attached log always shows the full
  output.
- **"Pushed" means accepted for processing.** Vinted, Amazon and Mirakl process
  listings asynchronously, so an item can still be rejected later. Each
  pusher's `status` command shows the final state.
- **Pushers run one after another, not in parallel.** That keeps within each
  marketplace's rate limits and keeps the logs readable.
- **Gmail sending is verified; the marketplaces aren't yet.** On 2026-11-05 a
  test email and a dry-run report were sent through real Gmail, with the
  credentials taken from Bitwarden. No real marketplace push has been made yet.

## Testing

```powershell
python tests\test_push_all.py
```

[tests/test_push_all.py](../tests/test_push_all.py) runs the runner in a
temporary folder with fake pushers (one succeeds, one has a rejection, one
fails on credentials, one hangs, one has no credentials) and a fake mail
server on `127.0.0.1:8766`. It checks the following:

- the Gmail login, with spaces stripped from the app password
- the recipients
- the subject and body counts
- the rejection reasons
- the timeout kill
- `NOT CONFIGURED` handling
- the HTML part and the log attachment
- the saved logs and the lock file
- re-runs, `--only`, `--dry-run` and `--no-email`
- exit codes
- a failed email

Nothing is pushed or emailed.
