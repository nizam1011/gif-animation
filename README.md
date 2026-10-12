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

Everything runs in the browser. There is no backend, no build step and no external dependency; the GIF encoder is part of the page.

## Run it

Open `index.html` in a browser, or serve it with Docker:

```bash
docker compose up -d --build
```

Then open http://localhost:7777.

After editing `index.html`, run the same command again to rebuild the image. To stop and remove the container:

```bash
docker compose down
```

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
