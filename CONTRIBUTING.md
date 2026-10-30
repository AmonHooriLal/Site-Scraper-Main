# Contributing

Thanks for helping improve Site Scraper Main!

1. Fork the repository and create a branch.
2. Set it up: `pip install -e ".[full,dev]"`.
3. Make your change. Keep the scraper polite: it must keep honouring robots.txt and rate limits, and must never work around logins, captchas or bot protection.
4. Add or update tests in `tests/`. They run against the offline site in `tests/fixtures/site/`, so please don't make them depend on real websites.
5. Check that `ruff check .` and `pytest` both pass.
6. Open a pull request that describes what changed and why.

For bugs, please open an issue with the command you ran, what you expected and what happened. Leave out any personal data, cookies or private URLs.
