# Landing-page motion and interaction coverage (V43)

The landing page adapts the 12 guide patterns to the employee management system. Effects are illustrative UI only; database totals are fetched from `/public-stats`, and forms only show progress while the real server request is pending.

1. **Hero reveal** — staggered headline reveal (`heroWordReveal`).
2. **Word rotator** — rotates “Made Simple / Made Secure / Made Smarter / Made Clear”.
3. **Activity ping** — status indicator uses aggregate database totals only.
4. **Card shuffle** — capability cards rotate; no fake reviews/testimonials.
5. **Count-up stats** — animated count-up from `/public-stats`; no sample social-proof numbers.
6. **Bento build** — responsive feature-card grid with reveal/hover transitions.
7. **Cursor tour** — user-triggered illustrative dashboard tour; does not mutate real data.
8. **Process draw** — SVG connector draws between onboarding steps, with resize-aware geometry.
9. **Before/after** — keyboard/touch-friendly native range comparison control.
10. **Price-roll adaptation** — 7-day / 30-day reporting range switch animates real registration totals and chart bars.
11. **Us vs. them** — responsive workflow comparison table; copy describes system capabilities rather than invented metrics.
12. **Click → success / loading** — login, registration OTP and other form submissions show loading only while the real POST is submitted; successful state comes from the server response, never from a client-side fake success.

Accessibility and safety notes:
- `prefers-reduced-motion: reduce` is respected in CSS and JavaScript.
- Mobile layouts and touch-friendly controls are included.
- Illustrative previews are labelled as illustrations; public stats do not invent user counts or reviews.
- Existing Flask routes and database logic are retained.
