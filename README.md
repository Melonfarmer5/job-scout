# Job Scout

Checks about 290 companies' own hiring systems three times a day and lists the
learning, talent development and AI enablement roles that fit Eddie's search.

## What is in here

| File | What it does |
|---|---|
| `companies.json` | The companies to check and where each one posts jobs |
| `scout.py` | The scanner. Pulls each feed, filters, writes the results |
| `docs/index.html` | The website |
| `docs/jobs.json`, `docs/feed.txt` | Results (written by the scanner) |
| `data/state.json` | Memory of jobs already seen (written by the scanner) |
| `.github/workflows/scout.yml` | The schedule that runs it on GitHub for free |
| `not_covered.json` | Big employers on systems this scanner cannot read yet |
| `test_scout.py` | Offline tests |

## Rules it applies

- Title mentions learning, L&D, instructional, enablement, talent development, training, onboarding, AI adoption and similar
- Location is New York City, or remote in the US
- Keeps a job when the top of the pay range is $120k or more, or when pay is not listed
- Skips jobs asking for 8+ years, and Director/VP/Head titles
- Anything skipped for pay or seniority is listed under "Filtered out" on the site

Change the numbers at the top of `scout.py` to adjust.

## Set up (about 10 minutes, one time)

1. Make a free account at github.com and create a new **public** repository called `job-scout`.
2. Upload everything in this folder to it. The `.github` folder is hidden on a Mac
   (press Cmd+Shift+. in Finder to show it) and must be included.
3. In the repository: **Settings → Pages → Source: Deploy from a branch → Branch: main, folder /docs → Save**.
4. **Settings → Actions → General → Workflow permissions → Read and write → Save**.
5. **Actions tab → Job Scout → Run workflow.** After a few minutes the site is live at
   `https://YOUR-USERNAME.github.io/job-scout/`.

### Phone notifications (optional)

1. Install the free **ntfy** app and subscribe to a topic with a hard-to-guess name, like `eddie-jobs-7f3k9x`.
2. In the repository: **Settings → Secrets and variables → Actions → New repository secret**,
   name `NTFY_TOPIC`, value the topic name.
3. Under the **Variables** tab add `SITE_URL` with the site address, so tapping the alert opens it.

## Adding a company

Add a line to `companies.json`:

- Greenhouse, Lever, Ashby: `{"name": "Acme", "ats": "greenhouse", "slug": "acme"}`
  (the slug is the company part of its job link, e.g. `job-boards.greenhouse.io/acme/...`)
- Workday: `{"name": "Acme", "ats": "workday", "tenant": "acme", "wd": "wd5", "site": "AcmeCareers"}`
  (from a link like `acme.wd5.myworkdayjobs.com/AcmeCareers/job/...`)

Feeds that fail show up under "Problems" on the site.
