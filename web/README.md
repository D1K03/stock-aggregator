# web

A dashboard for the screener, styled on Copperlane's extracted design system
(Geist, a sand background, one copper accent) with the project's own data
shapes: sector-relative pillar percentiles, pillar agreement, alerts worded as
threshold crossings, and run provenance.

The Overview reads the real scored screen. `lib/screen.ts` calls the status
service's `/api/screen` and `/api/screen/security`, behind the session that
`/login` establishes, and the page draws only what those endpoints return —
nothing here invents a figure.

```bash
npm install
npm run dev        # http://localhost:3000, /login for the sign-in page
```

`/login` links to the status service's `/auth/login`, which only resolves when
the two are served behind one origin; set `NEXT_PUBLIC_API_BASE` otherwise.
