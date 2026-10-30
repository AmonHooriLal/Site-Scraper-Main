# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Instead, use GitHub's private reporting: go to the **Security** tab of this repository and choose **Report a vulnerability**.

Please include what you found, how to reproduce it, and what impact you expect. You'll get a reply within a few days, and a fix or a plan as soon as possible.

## Supported versions

Only the latest release receives security fixes.

## Scope

Site Scraper Main treats all scraped content as untrusted. These areas are especially relevant:
- formula injection into the Excel and CSV output;
- requests to private or local network addresses (SSRF);
- oversized or compressed "bomb" downloads;
- file names built from URLs.
