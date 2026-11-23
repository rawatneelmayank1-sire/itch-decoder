# `web/` — the static order-book viewer

A dependency-free page that replays the reconstructed Nasdaq order book in the browser:
a canvas depth ladder you can scrub through the session, a price-against-time depth
heatmap, the spread curve, and the correctness and throughput measurements behind them.

It is **static**. No backend, no Python at view time — publishable to GitHub Pages by
serving this directory. Everything it draws comes from `data/*.js`, a downsampled export
of the real decode.

```
web/
  index.html        markup and copy
  app.css           tokens, layout, light/dark
  app.js            decoding, canvas rendering, playback (no libraries)
  data/
    summary.js      session facts, oracle counts, message mix, benchmark rows
    book-SPY.js     one file per symbol: the sampled depth ladders
    book-QQQ.js
    book-TSLA.js
    book-MSFT.js
    _manifest.json  what the last export produced (not read by the page)
  screenshot.png
```

Total `data/` size is under 1 MB, small enough to commit. The 409 MB ITCH source it was
built from is gitignored and must not be.

## Run it locally

```bash
cd web
python -m http.server 8000
# then open http://127.0.0.1:8000/
```

Opening `index.html` straight off disk also works: the export writes `.js` files that
assign to a global rather than `.json` files fetched over XHR, precisely so `file://`
does not break it. A local server is still the better test, since that is how Pages will
serve it.

## Regenerate the data

From the project root, with the real ITCH file present at
`data/01302019.NASDAQ_ITCH50.prefix.itch` (fetch it with `python tools_download.py`):

```bash
python scripts/export_web_data.py
```

The full run reads 409 MB, decodes 12,554,295 messages, and replays each exported symbol
through the same `itch.book.MultiBook` the test suite covers. It takes about a minute.

```bash
python scripts/export_web_data.py --force              # rebuild even if output is fresh
python scripts/export_web_data.py --symbols SPY QQQ    # a subset
python scripts/export_web_data.py --grid 2             # finer time grid (larger files)
```

It is **resumable**: a symbol whose `book-<SYM>.js` is newer than both the ITCH file and
the export script is skipped, so a crash costs one symbol rather than the run. With
nothing to do it exits immediately.

The script also asserts that its ladder-capturing replay ends on the same touch as a real
`itch.replay.replay()` run of the same events. If the two ever disagree, the export fails
loudly instead of shipping a wrong book.

## How the snapshot stays small

The source is 409 MB; `data/` is under 1 MB. Three things get it there, none of which
change a number:

1. **A uniform 4-second grid, sampled sparsely.** Book state is sampled onto a fixed grid
   across 04:00:00–09:30:30 ET (4,958 slots). A frame is written only when the visible
   ladder actually differs from the previous one, and the browser forward-fills. SPY needs
   2,797 frames for the whole session; TSLA, quiet in pre-market, needs 538.
2. **Relative integer prices.** Prices stay fixed-point integers with four implied
   decimals, exactly as on the wire and in SQLite. Each frame stores its best price, then
   the rest of the ladder as offsets from it divided by `punit` — the largest exact common
   divisor observed for that symbol, measured rather than assumed because sub-penny prices
   are legal below $1.00. Division by 10,000 happens only when a price is drawn.
3. **Columnar flat arrays.** Fixed-stride integer columns, delta-encoded where they are
   monotonic. Nested `[[a,b],[c,d]]` spends a third of its bytes on brackets.

## Where the figures come from

Nothing on the page is typed in by hand. The exporter copies the oracle counts, message
mix and timings verbatim out of `results/pipeline_summary.json`, the benchmark matrix out
of `results/benchmarks.json`, and the spread statistics out of
`results/analysis_summary.json` — all three written by `python run_all.py`. Re-running the
pipeline and re-running the exporter is the only way to change what the page says.

## Rendering notes

- **Three canvases, one cursor.** The ladder is the vertical slice through the session
  heatmap at the playhead; the spread strip shares the heatmap's time axis. A violet
  caliper marks the ladder's price span on the heatmap.
- **Precomputed backdrops.** The heatmap and spread strip are painted once per symbol into
  offscreen canvases, so scrubbing only blits them and redraws the overlay. Dragging stays
  at one frame per input event.
- **Pixel accumulation, not sub-pixel rects.** ~5,000 grid slots land on ~1,000 pixels, so
  the heatmap accumulates the largest resting quantity per device pixel and paints the
  buffer as one `ImageData`. Drawing a rectangle per level per frame antialiases itself
  into invisibility.
- **Time-based playback.** Playback advances by elapsed wall-clock time, not per animation
  frame, so the speed control means the same thing on a 60 Hz and a 144 Hz display.
- **Colour is semantic.** Blue is bid, amber is ask, violet is the reconstruction's own
  reading — touch, spread, playhead, focus. Blue against amber is the safest pair for
  colour-blind readers, and neither reads as profit or loss.

## Accessibility

Light and dark follow `prefers-color-scheme`; both palettes clear 4.5:1 for body and label
text. The scrubber is a native `<input type="range">` — arrows step, Home/End jump, and
`aria-valuetext` announces exchange time rather than a slot index. Space toggles playback.
Focus is always visible. `prefers-reduced-motion` suppresses the page-load sweep; the page
renders complete without it.

## Deploying to GitHub Pages

Serve this directory. `.github/workflows/pages.yml` publishes `web/` on every push to
`main` that touches it; enable it once under Settings > Pages > Source: GitHub Actions. Everything is relative — no build step, no CDN, no external font.
