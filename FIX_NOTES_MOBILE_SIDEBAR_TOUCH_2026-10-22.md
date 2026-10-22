# Mobile sidebar touch fix

- Removed inline hamburger handlers so only one JavaScript handler toggles the drawer.
- Replaced duplicate drawer listeners with one delegated click/escape handler.
- Corrected mobile CSS so the sidebar is hidden when closed and receives touch input when open.
- Raised sidebar above the backdrop and added 48px navigation touch targets.
- Preserved the Email Campaigns confirmation dialog update from the prior combined project.

Please deploy and test on the target Vercel deployment; live browser/device testing has not been performed here.
