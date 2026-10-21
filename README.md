# jobboardmonitor

Daily scan of company careers boards (Greenhouse, Ashby, Lever). No AI in the scan.

- `config.json`: boards, title rules, location rules
- `scraper.py`: fetches, filters, writes the reports
- `alerts/latest.md`: new, still open, closed since last run
- `dropped/latest.md`: sales titles rejected on location, for near miss review
- `state/seen.json`: ledger with first seen, last seen, closed date
- `.github/workflows/scrape.yml`: runs daily at 05:17 UTC
