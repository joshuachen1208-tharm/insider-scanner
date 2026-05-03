# SEC Insider Cluster Buy Scanner

Automatically scans SEC EDGAR Form 4 filings to detect clusters of insider buying — when 2 or more C-suite executives or directors at the same company make open-market purchases within a 5-day window.

## Features

- **Scanner** — pulls Form 4 filings from the last 3 days via the SEC EDGAR API, extracts open-market purchases (transaction code `P`), and groups them into clusters
- **Email digest** — sends the top 3 clusters every morning as a formatted HTML email via Gmail SMTP
- **Dashboard** — single-file HTML/CSS/JS UI hosted on GitHub Pages with metrics, filterable cluster cards, a sortable filings table, and a detail panel
- **GitHub Actions** — runs the scanner daily at 7 AM ET, commits updated dashboard data, and sends the email automatically

---

## Quick Start (local)

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/insider-scanner.git
cd insider-scanner
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set environment variables

```bash
export GMAIL_USER="your.email@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"   # 16-char Google App Password
export RECIPIENT_EMAIL="joshuachen1208@gmail.com"  # who gets the digest
```

### 4. Run the scanner

```bash
python scanner.py
```

Results are saved to `data/results.json` and `docs/results.json`.  
Open `docs/index.html` in your browser to view the dashboard.

---

## GitHub Actions Setup

### 1. Push to GitHub

```bash
git remote add origin https://github.com/YOUR_USERNAME/insider-scanner.git
git push -u origin main
```

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Value |
|---|---|
| `GMAIL_USER` | Your Gmail address (e.g. `you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | A [Google App Password](https://myaccount.google.com/apppasswords) (not your real password) |
| `RECIPIENT_EMAIL` | Where to send the digest (e.g. `joshuachen1208@gmail.com`) |

### 3. Enable GitHub Pages

Go to **Settings → Pages** and set:
- **Source**: Deploy from a branch
- **Branch**: `main`, folder `/docs`

Your dashboard will be live at `https://YOUR_USERNAME.github.io/insider-scanner/`.

### 4. Schedule

The workflow runs at **11:00 UTC** (= 7:00 AM EDT / 6:00 AM EST) every day.  
To change the time, edit `.github/workflows/daily-scan.yml` and update the cron expression.

You can also trigger a manual run from the **Actions** tab → **Daily Insider Cluster Scan** → **Run workflow**.

---

## How to Get a Google App Password

1. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Sign in and verify your identity
3. Under "Select app" choose **Mail**; under "Select device" choose **Other** → name it `InsiderScanner`
4. Click **Generate** — copy the 16-character password
5. Add it as the `GMAIL_APP_PASSWORD` secret

> **Note:** 2-Factor Authentication must be enabled on your Google account before App Passwords are available.

---

## Configuration

All tunable constants are at the top of `scanner.py`:

| Variable | Default | Description |
|---|---|---|
| `LOOKBACK_DAYS` | `3` | How many days of filings to pull |
| `CLUSTER_WINDOW_DAYS` | `5` | Max days between buys to count as a cluster |
| `CLUSTER_MIN_INSIDERS` | `2` | Minimum unique insiders to flag a cluster |
| `TOP_N_EMAIL` | `3` | Number of clusters to include in the email digest |

---

## Signal Strength

Clusters are scored based on number of insiders, total shares, and total dollar value:

| Strength | Meaning |
|---|---|
| VERY STRONG | High conviction — multiple insiders, large dollar amount |
| STRONG | Multiple insiders with meaningful buy sizes |
| MODERATE | Cluster present but modest in scale |
| WEAK | Minimum threshold met |

---

## Data Source

All data is sourced directly from [SEC EDGAR](https://www.sec.gov/cgi-bin/browse-edgar) using the public EDGAR EFTS search API. No API key required. Rate limiting follows SEC guidelines (≤10 requests/second).

> **Disclaimer:** This tool is for informational purposes only and does not constitute investment advice.

---

## Project Structure

```
insider-scanner/
├── scanner.py                    # Main scanner script
├── requirements.txt
├── README.md
├── data/
│   └── results.json              # Latest scan output (gitignored or committed)
├── docs/
│   ├── index.html                # Dashboard UI (GitHub Pages)
│   └── results.json              # Dashboard data (committed by CI)
└── .github/
    └── workflows/
        └── daily-scan.yml        # GitHub Actions workflow
```
