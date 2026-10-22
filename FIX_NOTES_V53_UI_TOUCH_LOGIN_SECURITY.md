# Management System V53 — UI, Touch, Login Process & Security Notes

## Changes in this build
- Fixed malformed `ui_advanced.js` cache-busting script tags on the admin dashboard and profile page (`?v=menu4`), which could prevent the shared mobile menu handler from loading.
- Increased the mobile hamburger control to a 48×48 CSS-pixel touch target, raised its stacking order, and added a dismissible dimmed backdrop for the mobile sidebar.
- Fixed the login password eye control alignment and touch target; added keyboard focus styles and standard `autocomplete="username"` / `autocomplete="current-password"` semantics.
- Login process Steps 03 and 04 now both have visible active animation while waiting for the real server response. They transition to done/failed only after the actual response.
- Fixed a login-success detection bug: an HTTP 200 response on a path other than `/login` is no longer enough if the final path still contains `login`. This prevents invalid credentials rendered at `/login` from being treated as a successful login.
- Added mobile and laptop breakpoints for login layout and made the loader fit within short phone screens while allowing internal scroll.
- No database tables, user records, or credentials are modified by these UI changes.

## Security warning shown by Chrome
The Chrome “Check your passwords / deceptive site” warning is controlled by Google Safe Browsing and the browser password manager, not by page CSS/JavaScript. This build does not hide or bypass that warning. Before entering any credentials, check the exact domain using Google Safe Browsing Transparency Report and inspect the Vercel deployment/repository for unexpected redirects, injected scripts, and unknown dependencies. If the warning persists, the site owner must resolve the cause and request review in Google Search Console/Safe Browsing. If a real password was entered after that warning appeared, change it from a trusted, known-safe device/site and rotate any reused password.

## Automated checks
- Python syntax compile: run during packaging.
- Jinja template compilation: run during packaging.
- Node syntax checks for shared JavaScript and inline scripts: run during packaging.
- CSS parser checks: run during packaging.
- ZIP integrity: run during packaging.

## Not verified
- Live Vercel deployment, real mobile browser interaction, live Neon database, and actual email delivery are not testable without access to the deployed project and its configured services.
