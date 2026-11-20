# PS5 Stock Bot (Canada)

Checks **Best Buy, Amazon.ca, Walmart.ca, Costco.ca and EB Games** for the
**PS5 Pro, PS5 Slim and PS5 Slim Digital** about once a minute, and emails you
the moment one is in stock **sold by the store itself**. Marketplace and
third-party sellers (the scalper listings on Amazon and Walmart) are ignored.

It runs free on GitHub Actions, so your computer doesn't need to be on.

---

## Setup (about 10 minutes)

### 1. Get a Gmail app password
The bot sends email through a Gmail account (yours, or a new one just for this).

1. Turn on 2-Step Verification: https://myaccount.google.com/security
2. Create an app password: https://myaccount.google.com/apppasswords
   (name it "PS5 bot"). Copy the 16-character password.

### 2. Put the code on GitHub
1. Create a free account at https://github.com if you don't have one.
2. Click **New repository** → name it `ps5-stock-bot` → choose **Public** → Create.
   - Public is recommended: GitHub Actions minutes are unlimited for public repos.
     Your email password is **not** in the code (it goes in Secrets, below), so this is safe.
   - If you choose Private, change the cron in `.github/workflows/check-stock.yml`
     to `*/30 * * * *` and `--loop-minutes 4` to `--loop-minutes 0`, or you'll run out of free minutes.
3. On the new repo page click **uploading an existing file**, drag in **everything
   in this folder** (including the `.github` folder), and click **Commit changes**.
   - Tip: if the `.github` folder doesn't upload via drag-and-drop on your browser, click
     **Add file → Create new file**, type `.github/workflows/check-stock.yml` as the name,
     and paste in the file's contents.

### 3. Add your email settings as Secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Name | Value |
|---|---|
| `SMTP_USER` | the Gmail address sending alerts |
| `SMTP_PASS` | the 16-character app password from step 1 |
| `EMAIL_TO`  | where alerts go (can be the same address; comma-separate for several) |

(Using Outlook or another provider instead? Also add `SMTP_HOST` and `SMTP_PORT`, e.g. `smtp.office365.com` / `587`.)

### 4. Turn it on and test it
1. Go to the **Actions** tab → enable workflows if asked.
2. Click **PS5 stock check → Run workflow**, tick **Just send a test email**, Run.
   You should get a test email within a minute.
3. Run it again **without** the box ticked. Open the run → the **Summary** shows a
   table of every store/console and what the bot saw.

After that it runs on its own every 5 minutes. Done!

---

## What the statuses mean (in the run Summary)

| Status | Meaning |
|---|---|
| 🟢 IN_STOCK | Sold by the store itself — you got an email |
| ⚪ OUT_OF_STOCK | Not available |
| 🟡 THIRD_PARTY | Only a marketplace seller has it — ignored on purpose |
| 🟠 UNKNOWN | Page loaded but the bot couldn't tell (e.g. Walmart in stock but seller unclear — no email, to be safe) |
| 🔴 BLOCKED / ERROR | The store blocked the check or the page changed |

You only get **one email per restock** (not one every minute). If a store blocks
the bot for ~5 hours straight, you'll get one heads-up email about it.

## Things to know

- **Some stores fight bots.** Amazon, Walmart and Costco sometimes block requests
  coming from GitHub's servers. The bot uses a browser-like connection to reduce this,
  but if a store shows 🔴 constantly, see "Running on your own computer" below — a home
  internet connection gets blocked much less.
- **GitHub schedules can lag** a few minutes when GitHub is busy. Each run checks
  about 4 times to make up for it.
- **GitHub pauses scheduled workflows after 60 days with no repo activity.** If that
  happens you'll get an email from GitHub — just click to re-enable.
- **Costco** needs a membership to buy online.
- **PlayStation Direct** doesn't sell to Canada, and **The Source** is now Best Buy, so
  neither is included.
- This bot only **alerts** you — it doesn't buy anything. Be ready to check out fast
  (stay logged in to each store, payment saved).

## Add or remove consoles

Edit `products.yaml` on GitHub (click the file → pencil icon). Each entry is just a
store, a name and a link. Best Buy needs the "Web Code" from the product page, Amazon
the ASIN (the `B0…` code in the URL). Delete an entry, or add `enabled: false`, to stop watching it.

## Optional settings

Add these under **Settings → Secrets and variables → Actions → Variables tab**:

- `REMIND_HOURS` — re-email you every N hours while something stays in stock (default 0 = off)
- `HEALTH_ALERT_AFTER` — how many failed rounds before the "store is blocking" email (default 60)

## Running on your own computer (optional)

```bash
pip install -r requirements.txt
export SMTP_USER=you@gmail.com SMTP_PASS=yourapppassword EMAIL_TO=you@gmail.com
python bot.py --test-email          # check email works
python bot.py --dry-run             # one check, print results, no email
python bot.py --loop-minutes 600    # keep checking every minute for 10 hours
```

Run the tests with `pip install pytest && pytest -q`.
