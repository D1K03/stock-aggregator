# web

A concept dashboard for the screener, styled on Copperlane's extracted design
system (Geist, a sand background, one copper accent) with the project's own
data shapes: sector-relative pillar percentiles, pillar agreement, alerts
worded as threshold crossings, and run provenance.

Static and unwired. Every number comes from `lib/data.ts`, invented but shaped
by the schema, so the design can be reviewed without ingest existing. Wiring it
to real snapshots means a JSON endpoint on the status service and same-origin
routing on the box.

```bash
npm install
npm run dev        # http://localhost:3000, /login for the sign-in page
```

`/login` links to the status service's `/auth/login`, which only resolves when
the two are served behind one origin; set `NEXT_PUBLIC_API_BASE` otherwise.
