# US Macro Observatory — GitHub Pages

The original Korean dashboard, with its tabs, SVG charts, date filters and pointer
tooltips preserved. The page now loads `./data.json` asynchronously and displays a
clear message if loading fails. No build tool or API key is required.

## Files

```text
index.html                         Original interface, async JSON loading
data.json                          Generated dashboard snapshot (15 metrics)
fetch_data.py                      FRED + Yahoo download and calculations
requirements.txt                   Python dependencies
cache/observations.json            Durable source history and retrieval timestamps
.github/workflows/update-data.yml  Daily refresh, commit, and Pages deployment
tests/                            Formula, fallback, and JSON-contract checks
```

The observation cache is committed because GitHub runners are temporary. Only
`index.html` and `data.json` are included in the Pages artifact. A public repository
also makes its source files and observation cache public.

## Connect this local folder to a new GitHub repository

1. Sign in at https://github.com/new.
2. Choose the repository name `us-macro-dashboard` (or another name) and **Public**.
3. Do **not** initialize it with a README, .gitignore, or license; this folder already
   contains files. Click **Create repository**.
4. Open PowerShell in this folder. Replace `YOUR_USERNAME` in the URL below:

```powershell
cd "C:\Users\jihye\OneDrive\Desktop\us-macro-github-pages"
git init -b main
git add .
git commit -m "Add self-updating macro dashboard"
git remote add origin https://github.com/YOUR_USERNAME/us-macro-dashboard.git
git push -u origin main
```

If Git says your identity is missing, set your own identity for this repository,
then repeat the commit and push:

```powershell
git config user.name "YOUR NAME"
git config user.email "YOUR GITHUB EMAIL OR NOREPLY EMAIL"
```

Git Credential Manager may open a browser for GitHub sign-in. Do not put a token in
the repository URL. If you choose a different repository name, change the URL.
If Git says this folder already has `main`, omit `git init`; if `origin` already
exists, inspect `git remote -v` before changing it.

## Enable GitHub Actions and Pages

1. In the repository, open **Settings → Actions → General**. Ensure Actions are
   enabled and GitHub's `actions/*` actions are allowed.
2. The workflow explicitly requests `contents: write`, `pages: write`, and
   `id-token: write`. No repository secrets or FRED API key are needed. If an
   organization policy blocks those permissions, an administrator must allow them.
   A rule requiring pull requests for all changes to `main` also blocks the bot's
   daily commit; use a repository where this workflow can push to `main`.
3. Open **Settings → Pages → Build and deployment → Source** and select
   **GitHub Actions**. Do not select “Deploy from a branch”: this workflow publishes
   an artifact containing the two root-level public files.
4. Open **Actions → Update data and deploy dashboard → Run workflow**. Choose
   `main`, then click **Run workflow**.
5. Wait for the run to finish. The `github-pages` deployment link and **Settings →
   Pages** show the live URL, normally
   `https://YOUR_USERNAME.github.io/us-macro-dashboard/`.

The initial push may start a run before you enable Pages. If that run fails at
Pages configuration, complete step 3 and manually run the workflow again.

The workflow runs daily at **12:00 UTC / 21:00 Korea time**, on manual dispatch,
and on pushes to `main`. Bot commits made with `GITHUB_TOKEN` do not trigger a
second push workflow; deployment happens in the same run. GitHub schedules can
be delayed, so 12:00 is a requested schedule, not an exact delivery guarantee.
Schedules run from the default branch; keep `main` as the default branch. Public
repository schedules may be disabled after 60 days without repository activity.

## Run locally

Install Python 3.12 or newer. On Windows:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe fetch_data.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m http.server 8000 --bind 127.0.0.1
```

Open http://127.0.0.1:8000/. Do not double-click `index.html`; browser restrictions
on `file://` can prevent fetching `data.json`. Stop the server with Ctrl+C.
Use `python fetch_data.py --offline` to recalculate from the committed cache
without downloading data. After the bot has updated the repository, run
`git pull --ff-only` before making local edits.

## Calculation and data rules

- All 15 requested formulas are implemented in `calculate()`; exact formulas are
  also present in every metric's `formula` field.
- Monthly lags mean **calendar months**, not “12 available rows.” Absent months
  become nulls and are never interpolated. Claims require four consecutive weeks.
- Net liquidity uses WALCL dates and only exactly matching WTREGEN and RRP dates.
  No as-of join or forward fill is used. WTREGEN is the requested **weekly average**
  series, whereas WALCL is a Wednesday level; this proxy preserves that requested
  source choice and does not turn WTREGEN into a daily closing balance.
- SOFR/IORB and Treasury curve inputs must have identical observation dates.
- Yahoo uses `Close` with `auto_adjust=False` explicitly. The RSP/SPY ratio is a
  price-relative measure, not dividend-adjusted total return. Rolling windows use
  200 downloaded sessions for the index and 125 common sessions for the ETF ratio.
  Today's Yahoo bar is excluded to avoid publishing an incomplete daily candle.
- Full history is requested; there is no 1998 cutoff. Calculated history begins
  when the necessary inputs and warm-up periods exist. A source can limit its
  available history; a previously cached older prefix is retained when this happens.
  New downloaded values, revisions and explicit nulls replace overlapping cache.
- `delta` retains the original comparison: 3 months for monthly indicators,
  4 weeks for weekly indicators, and 20 observations/trading sessions for daily
  indicators. It is a difference in the metric's displayed unit, not a percentage
  change. For net liquidity, those rows remain weekly anchor dates.
- `generatedAt` is the latest successful source retrieval timestamp in UTC, not
  the observation date. Individual sources show their own `retrieved` timestamp.
  A partial refresh updates the top-level timestamp while failed metrics retain
  their values and old provenance. An entirely failed refresh keeps `generatedAt`.
- A failed dependency preserves the previous whole metric to avoid mixing fresh
  revisions with unavailable inputs. A missing latest calculated value retains
  the last actual value and date while keeping the null in the historical array.
  No point is appended using today's date to pretend an old value is current.
- Status is `ok`, `stale`, or `missing`. Stale/failed retained sources have
  `fallback: true`. Freshness limits are 7 calendar days for daily sources,
  18 for weekly sources, and 75 for monthly sources, measured from observation
  dates. Monthly observation dates commonly precede publication; these thresholds
  are conservative freshness heuristics, not release-calendar guarantees.
- Weekends, holidays, and days between monthly releases do not automatically make
  a value stale. Explicit missing latest observations and failed downloads do.
- Eight isolated source workers run concurrently, each with a hard 40-second
  deadline covering all retries. Progress appears as each source starts and ends.
  Even if all 20 sources hang, downloads finish in approximately two minutes,
  plus process startup and JSON calculation time. The Actions refresh step has
  a separate four-minute emergency timeout.
- Downloads retry up to three times within that deadline. If every download fails, the script writes fallback
  status and exits with code 2. The workflow still publishes valid preserved data,
  then reports a failed run so the outage is visible in Actions.
- Files are replaced atomically and JSON rejects non-finite numbers. The workflow
  tests the formulas and validates the data before committing/deploying it.

FRED historical observations are revised over time. Observation dates are not
publication timestamps. This dashboard is **not a point-in-time backtest**; exact
historical knowledge would require vintage/release data rather than this CSV feed.

## References

- [GitHub Pages custom workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
- [GitHub Actions repository settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)
- [Scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [yfinance download parameters](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)
- [FRED WTREGEN source definition](https://fred.stlouisfed.org/series/WTREGEN)
