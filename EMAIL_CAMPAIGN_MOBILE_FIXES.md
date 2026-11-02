# Email Campaign Center - Mobile and workflow improvements

This update keeps the existing Flask campaign routes and database models while refining the email campaign page.

## Included
- Test email remains a separate direct-to-address form and does not use campaign audience selection.
- Test form copies the current subject, message, CTA label, and CTA URL from the live editor.
- Branded confirmation dialog is used for send, schedule, test, retry, and scheduled-campaign cancellation actions.
- Audience validation is shown in the branded dialog instead of browser alert popups.
- Campaign history and recipient report tables render as labeled cards on narrow screens.
- Recipient emails and provider error text wrap instead of causing horizontal page overflow.
- Schedule controls stack on mobile; created/sent timestamps are displayed in India Standard Time (IST).
- Desktop table layout remains available at wider screen sizes.

## Important
- Test email delivery still requires working Brevo or SMTP environment variables in the deployment environment.
- A provider acceptance is not proof of inbox delivery; provider logs remain the source for final delivery/bounce status.
- Scheduled campaigns still require the configured cron endpoint and CRON_SECRET.
- No database schema migration is included in this UI-focused update.


Project credit: **Developed by Aman Kumar.**
