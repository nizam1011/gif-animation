# Shape to Equations

A single-page browser app that takes a shape and gives you the equations that trace it.

Each outline becomes a pair of Fourier series:

```
x(t) = a0 + Σ (a_k cos kt + b_k sin kt)
y(t) = c0 + Σ (c_k cos kt + d_k sin kt)      k = 1..N,  0 ≤ t ≤ 2π
```

The app computes the coefficients, shows how the fit improves as N grows, and can export an animated GIF of the shape being drawn.

## Features

- **Input:** an image (PNG, JPG, ...), an SVG, or a freehand multi-stroke drawing. Drop a file on the page or paste an image from the clipboard.
- **Tracing:** images are traced along the edge of whatever differs from the background (or along transparency); SVGs follow their actual paths.
- **Multiple outlines:** holes and separate pieces each get their own pair of equations, or can be joined into one continuous curve with a single pair.
- **N slider:** 1 to 800 terms, with a fit-error readout and an Auto button.
- **Rotating circles:** the epicycle animation for x(t) and y(t), with the original shape ghosted behind the curve.
- **Exports:** Python (NumPy), Desmos, LaTeX, plain text, JSON and CSV, plus a coefficient table.
- **Animated GIF:** draws every line in turn, then fades in the original image. Plays once by default, with an option to loop.

The conversion itself runs in the browser, including the GIF encoder. A small Python server adds accounts around it.

## Accounts and limits

- **Sign-up:** email and password, confirmed with a 6-digit code sent to that address. Password reset works the same way.
- **Allowance:** each account starts with 10 conversions. One is used each time a new image, SVG or drawing is converted; changing N, exporting and the built-in samples are free.
- **Requesting more:** when the allowance is used up, the user can send a request with an optional note.
- **Admin page** (`/admin`): approve or deny requests, set any user's limit, and save the mail account used to send codes. Admins are the addresses listed in `ADMIN_EMAILS`; they sign up like anyone else and have no limit.

The allowance is counted on the server, but the conversion happens in the browser, so it limits ordinary use rather than a determined user who edits the page's code.

## Run it

```bash
cp .env.example .env    # then set ADMIN_EMAILS
docker compose up -d --build
```

Then open http://localhost:7777, create an account with an admin address, and save the mail account under **Admin → Email sending**. For Gmail that is the address plus a Google app password.

Until a mail account is saved, no email is sent and the codes appear in the container log:

```bash
docker compose logs -f
```

One container runs everything. Accounts live in a SQLite file on the `shape-data` volume, so they survive rebuilds. Run the same `up` command again after changing the code, and stop it with:

```bash
docker compose down
```

`web/index.html` still works on its own when opened directly as a file, without accounts or limits.

### Behind nginx with https

[deploy/nginx-site.conf](deploy/nginx-site.conf) is a host nginx site that serves the app over https on port 7777 and forwards to the container. With it, set `BIND=127.0.0.1:7780`, `TRUST_PROXY=1` and `COOKIE_SECURE=1` in `.env`, so the container is reachable only through nginx and sign-in cookies are sent over https only.

### Backups

[deploy/backup.sh](deploy/backup.sh) saves a compressed, dated copy of the database to `backups/` and keeps 30 days of them. Schedule it nightly with cron:

```
30 2 * * * /path/to/shape-to-equations/deploy/backup.sh >> /path/to/shape-to-equations/backups/backup.log 2>&1
```

To go back to a backup (anything newer than it is lost):

```bash
deploy/restore.sh backups/app-YYYYMMDD-HHMMSS.db.gz
```

The backups sit on the same disk as the app, so they protect against a bad update or a deleted volume, not a failed disk. They contain password hashes and the mail password; the script keeps them readable by the owner only.

Settings in `.env`:

| Setting | Purpose |
|---|---|
| `ADMIN_EMAILS` | Comma-separated admin addresses |
| `APP_URL` | Address used for links in emails |
| `DEFAULT_QUOTA` | Conversions a new account starts with (default 10) |
| `COOKIE_SECURE` | Set to `1` when served over https |
| `BIND` | Host address and port the container is published on (default `7777`) |
| `TRUST_PROXY` | Set to `1` behind a reverse proxy, to use its forwarded headers |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Mail account, if you prefer the file to the admin page |

## How it works

1. The shape is reduced to one or more closed outlines, each resampled to evenly spaced points along its length.
2. A discrete Fourier transform of the x and y coordinates gives the coefficients a, b, c, d for every harmonic k.
3. Keeping the first N harmonics gives the approximation. Low harmonics carry the overall form; high ones add corners and fine detail.

Coordinates are scaled so the shape fits in −1..1 with y pointing up. Open strokes are traced out and back, so no closing line is added.

## Limits

- Photos with busy backgrounds do not give a clean outline. Silhouettes, logos and line art work best.
- SVG text and `<use>` elements are skipped when following paths; switch to "Rendered silhouette" for those.
- GIFs hold 256 colours, so a photo shows some banding in the final frame.
- A joined curve needs many more terms than separate outlines, because the jumps between pieces are sharp.
