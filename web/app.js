/* -------------------------------------------------------------------------------------
   itch — order book reconstruction viewer

   Everything here is presentation. The numbers arrive already computed in data/*.js,
   written by scripts/export_web_data.py out of the real decode, so nothing on this page
   is derived from an assumption about the market.

   Three canvases share one time cursor: the depth ladder is the vertical slice through
   the session heatmap at the playhead, and the spread strip sits on the heatmap's x axis.
   The heatmap and the strip are painted once per symbol into offscreen canvases; scrubbing
   only blits them and redraws the overlay, so a drag stays at one frame per input event.
   ------------------------------------------------------------------------------------- */
'use strict';

(function () {
  const S = window.ITCH_SUMMARY;
  const $ = (id) => document.getElementById(id);

  if (!S) {
    document.querySelector('main').insertAdjacentHTML(
      'afterbegin',
      '<p class="nodata"><strong>Snapshot data not loaded.</strong> Run ' +
        '<code>python scripts/export_web_data.py</code> from the project root to write ' +
        '<code>web/data/</code>, then reload.</p>'
    );
    return;
  }

  /* ------------------------------------------------------------------ formatting */

  const NS = 1e9;
  const fmtInt = (n) => Math.round(n).toLocaleString('en-US');
  const pad = (n, w) => String(n).padStart(w, '0');

  function clockOf(ns) {
    const s = ns / NS;
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = Math.floor(s % 60);
    return `${pad(h, 2)}:${pad(m, 2)}:${pad(sec, 2)}`;
  }

  // Prices are fixed-point integers with four implied decimals, exactly as on the wire.
  function px(v, dp) {
    return (v / 10000).toFixed(dp === undefined ? 2 : dp);
  }

  /* ------------------------------------------------------------------ canvas plumbing */

  const CSS = getComputedStyle(document.documentElement);
  let C = {};
  function readTheme() {
    const g = (n) => CSS.getPropertyValue(n).trim();
    C = {
      fg: g('--fg'),
      dim: g('--fg-dim'),
      faint: g('--fg-faint'),
      rule: g('--rule'),
      ruleSoft: g('--rule-soft'),
      panel: g('--panel'),
      panel2: g('--panel-2'),
      bid: g('--bid'),
      ask: g('--ask'),
      mid: g('--mid'),
      heat: g('--heat-ground'),
      grid: g('--grid-line'),
      mono: g('--mono'),
      display: g('--display'),
    };
    C.bidRGB = toRGB(C.bid);
    C.askRGB = toRGB(C.ask);
    C.midRGB = toRGB(C.mid);
  }
  function toRGB(hex) {
    const h = hex.replace('#', '');
    const n = parseInt(h.length === 3 ? h.replace(/./g, '$&$&') : h, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  readTheme();

  /** Size a canvas to its CSS box at device pixel ratio; returns a scaled 2D context. */
  function surface(cv) {
    const r = cv.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(r.width));
    const h = Math.max(1, Math.round(r.height));
    if (cv.width !== w * dpr || cv.height !== h * dpr) {
      cv.width = w * dpr;
      cv.height = h * dpr;
    }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function offscreen(w, h) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cv = document.createElement('canvas');
    cv.width = Math.max(1, Math.round(w * dpr));
    cv.height = Math.max(1, Math.round(h * dpr));
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { cv, ctx, w, h };
  }

  const label = (ctx, size, weight) => {
    ctx.font = `${weight || 600} ${size}px ${C.display}`;
  };
  const mono = (ctx, size, weight) => {
    ctx.font = `${weight || 400} ${size}px ${C.mono}`;
  };

  /* ------------------------------------------------------------------ symbol decoding */

  const cache = new Map();

  function decode(raw) {
    const c = raw.cols;
    const n = raw.frames;
    const L = raw.levels;
    const u = raw.punit;

    const slot = new Int32Array(n);
    const bid = new Float64Array(n);
    const ask = new Float64Array(n);
    let acc = 0;
    let accBid = 0;
    for (let i = 0; i < n; i++) {
      acc += c.slot_d[i];
      slot[i] = acc;
      accBid += c.bid_d[i];
      const bb = accBid;
      bid[i] = bb;
      const sp = c.spread[i];
      // Mirrors the encoder exactly: -1 means one side had no live level at all.
      ask[i] = bb !== 0 ? (sp !== -1 ? bb + sp : 0) : c.ask_only[i];
    }

    // Every grid slot maps to the last frame at or before it, so the scrubber is O(1)
    // and forward-fills the stretches where the book did not move.
    const map = new Int32Array(raw.slots).fill(-1);
    for (let i = 0; i < n; i++) {
      const end = i + 1 < n ? slot[i + 1] : raw.slots;
      for (let s = slot[i]; s < end && s < raw.slots; s++) map[s] = i;
    }

    // Price envelope for the heatmap. Framed on the *touch* rather than on every level:
    // pre-market books occasionally carry a resting order dollars away from the market
    // (MSFT has one at 118 against a 104 touch), and letting that set the axis squashes
    // the entire session's price track into a few pixels. Levels outside the frame clip
    // to the edge; the range actually shown is printed above the chart.
    const touch = [];
    const extent = [];
    const allQty = [];
    for (let i = 0; i < n; i++) {
      if (bid[i]) touch.push(bid[i]);
      if (ask[i]) touch.push(ask[i]);
      let deepBid = 0;
      let deepAsk = 0;
      for (let k = 0; k < L; k++) {
        const bq = c.bid_qty[i * L + k];
        if (bq > 0) {
          allQty.push(bq);
          deepBid = Math.max(deepBid, c.bid_off[i * L + k] * u);
        }
        const aq = c.ask_qty[i * L + k];
        if (aq > 0) {
          allQty.push(aq);
          deepAsk = Math.max(deepAsk, c.ask_off[i * L + k] * u);
        }
      }
      if (deepBid) extent.push(deepBid);
      if (deepAsk) extent.push(deepAsk);
    }
    touch.sort((a, b) => a - b);
    extent.sort((a, b) => a - b);
    allQty.sort((a, b) => a - b);
    const q = (arr, p) => (arr.length ? arr[Math.min(arr.length - 1, Math.floor(p * arr.length))] : 0);
    // The typical distance from the touch to the deepest shown level, so the frame has
    // room for the ladder — the median, not a high percentile: in a thin pre-market book
    // the eighth level can sit dollars away and would drag the axis open on its own.
    const reach = q(extent, 0.5);
    let lo = q(touch, 0.005) - reach;
    let hi = q(touch, 0.995) + reach;
    if (!(hi > lo)) {
      lo -= u * 10;
      hi += u * 10;
    }
    const padPx = Math.max((hi - lo) * 0.08, u * 4);

    // Spread ceiling, likewise clipped: pre-market spreads occasionally blow out to
    // dollars and would otherwise squash the whole strip flat.
    const spreads = [];
    for (let i = 0; i < n; i++) if (bid[i] && ask[i]) spreads.push(ask[i] - bid[i]);
    spreads.sort((a, b) => a - b);

    return {
      raw,
      L,
      u,
      n,
      slots: raw.slots,
      gridS: raw.grid_s,
      t0: raw.t0_ns,
      slot,
      bid,
      ask,
      map,
      pxLo: lo - padPx,
      pxHi: hi + padPx,
      qtyMax: Math.max(1, q(allQty, 0.97)),
      spreadMax: Math.max(u, q(spreads, 0.95)),
      heat: null,
      strip: null,
    };
  }

  /** Read one frame into a plain object the renderers can use. */
  function frameAt(D, slotIdx) {
    const i = D.map[Math.max(0, Math.min(D.slots - 1, slotIdx))];
    const out = {
      empty: i < 0,
      bid: 0,
      ask: 0,
      bids: [],
      asks: [],
      nBid: 0,
      nAsk: 0,
      depthBid: 0,
      depthAsk: 0,
    };
    if (i < 0) return out;
    const c = D.raw.cols;
    const L = D.L;
    out.bid = D.bid[i];
    out.ask = D.ask[i];
    out.nBid = c.n_levels[i * 2];
    out.nAsk = c.n_levels[i * 2 + 1];
    for (let k = 0; k < L; k++) {
      const bq = c.bid_qty[i * L + k];
      if (bq > 0) {
        out.bids.push([out.bid - c.bid_off[i * L + k] * D.u, bq]);
        out.depthBid += bq;
      }
      const aq = c.ask_qty[i * L + k];
      if (aq > 0) {
        out.asks.push([out.ask + c.ask_off[i * L + k] * D.u, aq]);
        out.depthAsk += aq;
      }
    }
    return out;
  }

  /* ------------------------------------------------------------------ heatmap */

  const HEAT_PAD = { l: 0, r: 54, t: 6, b: 16 };

  const xOfSlot = (D, s, w) => ((w - HEAT_PAD.r) * s) / D.slots;
  const yOfPx = (D, p, h) =>
    HEAT_PAD.t + (1 - (p - D.pxLo) / (D.pxHi - D.pxLo)) * (h - HEAT_PAD.t - HEAT_PAD.b);

  /**
   * Paint resting depth into the heat canvas at device-pixel resolution.
   *
   * Two Float32 grids accumulate the largest quantity seen at each (column, row); a
   * pixel therefore shows the deepest level that passed through it rather than whichever
   * frame happened to be drawn last. Bid and ask are kept apart so the two hues never
   * average into mud where they meet at the touch — the deeper side simply wins.
   */
  function paintDepth(ctx, D, w, h, dpr) {
    const PW = Math.max(1, Math.round((w - HEAT_PAD.r) * dpr));
    const PH = Math.max(1, Math.round((h - HEAT_PAD.t - HEAT_PAD.b) * dpr));
    const top = Math.round(HEAT_PAD.t * dpr);
    const bidG = new Float32Array(PW * PH);
    const askG = new Float32Array(PW * PH);

    const span = D.pxHi - D.pxLo;
    const rowOf = (p) => Math.round((1 - (p - D.pxLo) / span) * (PH - 1));
    const colOf = (s) => Math.round((PW * s) / D.slots);
    const tickRows = Math.max(1, Math.round((PH * D.u) / span));

    const c = D.raw.cols;
    const L = D.L;

    const stamp = (grid, col0, col1, p, q) => {
      const r0 = rowOf(p) - ((tickRows / 2) | 0);
      for (let r = r0; r < r0 + tickRows; r++) {
        if (r < 0 || r >= PH) continue;
        const base = r * PW;
        for (let x = col0; x < col1; x++) if (grid[base + x] < q) grid[base + x] = q;
      }
    };

    for (let i = 0; i < D.n; i++) {
      const c0 = colOf(D.slot[i]);
      const c1 = Math.max(c0 + 1, Math.min(PW, colOf(i + 1 < D.n ? D.slot[i + 1] : D.slots)));
      for (let k = 0; k < L; k++) {
        const bq = c.bid_qty[i * L + k];
        if (bq > 0) stamp(bidG, c0, c1, D.bid[i] - c.bid_off[i * L + k] * D.u, bq);
        const aq = c.ask_qty[i * L + k];
        if (aq > 0) stamp(askG, c0, c1, D.ask[i] + c.ask_off[i * L + k] * D.u, aq);
      }
    }

    const img = ctx.createImageData(PW, PH);
    const px8 = img.data;
    const bg = toRGB(C.heat);
    const logMax = Math.log1p(D.qtyMax);
    for (let i = 0, n = PW * PH; i < n; i++) {
      const bq = bidG[i];
      const aq = askG[i];
      const o = i * 4;
      px8[o + 3] = 255;
      if (!bq && !aq) {
        px8[o] = bg[0];
        px8[o + 1] = bg[1];
        px8[o + 2] = bg[2];
        continue;
      }
      const useBid = bq >= aq;
      const q = useBid ? bq : aq;
      const col = useBid ? C.bidRGB : C.askRGB;
      const a = 0.22 + 0.78 * Math.min(1, Math.log1p(q) / logMax);
      px8[o] = bg[0] + (col[0] - bg[0]) * a;
      px8[o + 1] = bg[1] + (col[1] - bg[1]) * a;
      px8[o + 2] = bg[2] + (col[2] - bg[2]) * a;
    }
    ctx.putImageData(img, 0, top);
  }

  function buildHeat(D, w, h) {
    const { cv, ctx } = offscreen(w, h);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const plotW = w - HEAT_PAD.r;
    const plotH = h - HEAT_PAD.t - HEAT_PAD.b;
    ctx.fillStyle = C.heat;
    ctx.fillRect(0, 0, w, h);

    // Nearly 5,000 grid slots land on ~1,000 pixels, so a rectangle per level per frame
    // is sub-pixel and antialiases itself into invisibility. Accumulate the largest
    // resting quantity per device pixel instead, then paint the buffer once.
    paintDepth(ctx, D, w, h, dpr);

    // hour gridlines, in exchange time
    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    label(ctx, 10);
    ctx.fillStyle = C.faint;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    for (let hh = 5; hh <= 9; hh++) {
      const s = (hh * 3600 * NS - D.t0) / (D.gridS * NS);
      if (s < 0 || s > D.slots) continue;
      const x = Math.round(xOfSlot(D, s, w)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, HEAT_PAD.t);
      ctx.lineTo(x, HEAT_PAD.t + plotH);
      ctx.stroke();
      ctx.fillText(`${pad(hh, 2)}:00`, x + 4, h - HEAT_PAD.b + 3);
    }

    // the opening cross
    const openSlot = (S.session.open_ns - D.t0) / (D.gridS * NS);
    if (openSlot > 0 && openSlot < D.slots) {
      const x = Math.round(xOfSlot(D, openSlot, w)) + 0.5;
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = C.fg;
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      ctx.moveTo(x, HEAT_PAD.t);
      ctx.lineTo(x, HEAT_PAD.t + plotH);
      ctx.stroke();
      ctx.restore();
      label(ctx, 10);
      ctx.fillStyle = C.fg;
      ctx.textAlign = 'right';
      ctx.textBaseline = 'top';
      ctx.fillText('09:30 OPEN', x - 5, HEAT_PAD.t + 2);
    }

    // price axis on the right
    ctx.fillStyle = C.panel;
    ctx.fillRect(plotW, 0, HEAT_PAD.r, h);
    ctx.strokeStyle = C.ruleSoft;
    ctx.beginPath();
    ctx.moveTo(plotW + 0.5, 0);
    ctx.lineTo(plotW + 0.5, h);
    ctx.stroke();
    mono(ctx, 10);
    ctx.fillStyle = C.faint;
    ctx.textAlign = 'right';
    for (let j = 0; j <= 4; j++) {
      const p = D.pxLo + ((D.pxHi - D.pxLo) * j) / 4;
      const y = yOfPx(D, p, h);
      ctx.textBaseline = j === 0 ? 'bottom' : j === 4 ? 'top' : 'middle';
      ctx.fillText(px(p), w - 6, y);
    }
    return cv;
  }

  function buildStrip(D, w, h) {
    const { cv, ctx } = offscreen(w, h);
    const plotW = w - HEAT_PAD.r;
    const top = 8;
    const plotH = h - top - 12;
    ctx.fillStyle = C.heat;
    ctx.fillRect(0, 0, w, h);

    const yOf = (sp) => top + plotH * (1 - Math.min(1, sp / D.spreadMax));

    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    for (let hh = 5; hh <= 9; hh++) {
      const s = (hh * 3600 * NS - D.t0) / (D.gridS * NS);
      if (s < 0 || s > D.slots) continue;
      const x = Math.round(xOfSlot(D, s, w)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, top + plotH);
      ctx.stroke();
    }

    // Thousands of samples over ~1,000 pixels: drawn as a step line this reads as a
    // picket fence. Reduce each pixel column to the range of spreads observed in it and
    // draw the extent as a band with the typical value as a line on top — the standard
    // way to show a dense series without pretending it is smooth. Columns where one side
    // of the book was empty stay blank: there is no spread when there is nothing to cross.
    const cols = Math.max(1, Math.round(plotW));
    const lo = new Float64Array(cols).fill(Infinity);
    const hi = new Float64Array(cols).fill(-Infinity);
    const sum = new Float64Array(cols);
    const cnt = new Float64Array(cols);
    for (let i = 0; i < D.n; i++) {
      if (!D.bid[i] || !D.ask[i]) continue;
      const sp = D.ask[i] - D.bid[i];
      const c0 = Math.min(cols - 1, Math.max(0, Math.round((cols * D.slot[i]) / D.slots)));
      const c1 = Math.min(
        cols,
        Math.max(c0 + 1, Math.round((cols * (i + 1 < D.n ? D.slot[i + 1] : D.slots)) / D.slots))
      );
      for (let x = c0; x < c1; x++) {
        if (sp < lo[x]) lo[x] = sp;
        if (sp > hi[x]) hi[x] = sp;
        sum[x] += sp;
        cnt[x] += 1;
      }
    }

    ctx.fillStyle = rgba(C.midRGB, 0.3);
    for (let x = 0; x < cols; x++) {
      if (!cnt[x]) continue;
      const yTop = yOf(hi[x]);
      const yBot = yOf(lo[x]);
      ctx.fillRect(x, yTop, 1, Math.max(1, yBot - yTop));
    }
    ctx.fillStyle = C.mid;
    for (let x = 0; x < cols; x++) {
      if (!cnt[x]) continue;
      ctx.fillRect(x, yOf(sum[x] / cnt[x]) - 0.5, 1, 1.4);
    }

    ctx.fillStyle = C.panel;
    ctx.fillRect(plotW, 0, HEAT_PAD.r, h);
    ctx.strokeStyle = C.ruleSoft;
    ctx.beginPath();
    ctx.moveTo(plotW + 0.5, 0);
    ctx.lineTo(plotW + 0.5, h);
    ctx.stroke();
    mono(ctx, 10);
    ctx.fillStyle = C.faint;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'top';
    ctx.fillText(px(D.spreadMax), w - 6, top - 2);
    ctx.textBaseline = 'bottom';
    ctx.fillText('0.00', w - 6, top + plotH + 2);
    return cv;
  }

  /* ------------------------------------------------------------------ ladder */

  function drawLadder(D, f) {
    const { ctx, w, h } = surface($('ladder'));
    const L = D.L;
    const rows = L * 2 + 1;
    const rh = h / rows;
    const padL = 12;
    const priceW = 62;
    const qtyW = 54;
    const barX = padL + priceW + 10;
    const barW = Math.max(20, w - barX - qtyW - padL);

    ctx.fillStyle = C.panel;
    ctx.fillRect(0, 0, w, h);

    let maxQ = 1;
    for (const [, q] of f.bids) maxQ = Math.max(maxQ, q);
    for (const [, q] of f.asks) maxQ = Math.max(maxQ, q);

    const row = (idx, side, lvl) => {
      const y = idx * rh;
      const cRGB = side === 'b' ? C.bidRGB : C.askRGB;
      const col = side === 'b' ? C.bid : C.ask;
      if (!lvl) {
        if (idx === 0 || idx === rows - 1) {
          label(ctx, 10);
          ctx.fillStyle = C.faint;
          ctx.textAlign = 'left';
          ctx.textBaseline = 'middle';
          ctx.fillText(side === 'b' ? 'NO RESTING BIDS' : 'NO RESTING OFFERS', padL, y + rh / 2);
        }
        return;
      }
      const [p, q] = lvl;
      const bw = Math.max(2, (barW * q) / maxQ);

      ctx.fillStyle = rgba(cRGB, 0.22);
      ctx.fillRect(barX, y + rh * 0.16, bw, rh * 0.68);
      ctx.fillStyle = col;
      ctx.fillRect(barX, y + rh * 0.16, Math.min(bw, 2), rh * 0.68);

      mono(ctx, Math.min(13, rh * 0.62));
      ctx.fillStyle = col;
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(px(p), padL + priceW, y + rh / 2);

      mono(ctx, Math.min(12, rh * 0.58));
      ctx.fillStyle = C.dim;
      ctx.fillText(fmtInt(q), w - padL, y + rh / 2);
    };

    // asks: deepest at the top, best offer immediately above the touch band
    for (let k = 0; k < L; k++) row(k, 'a', f.asks[L - 1 - k]);

    // the touch band
    const ty = L * rh;
    ctx.fillStyle = rgba(C.midRGB, 0.1);
    ctx.fillRect(0, ty, w, rh);
    ctx.strokeStyle = rgba(C.midRGB, 0.55);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, Math.round(ty) + 0.5);
    ctx.lineTo(w, Math.round(ty) + 0.5);
    ctx.moveTo(0, Math.round(ty + rh) + 0.5);
    ctx.lineTo(w, Math.round(ty + rh) + 0.5);
    ctx.stroke();

    ctx.textBaseline = 'middle';
    if (f.bid && f.ask) {
      label(ctx, 10);
      ctx.fillStyle = C.faint;
      ctx.textAlign = 'left';
      ctx.fillText('SPREAD', padL, ty + rh / 2);
      mono(ctx, Math.min(13, rh * 0.62), 600);
      ctx.fillStyle = C.mid;
      ctx.textAlign = 'right';
      ctx.fillText(`${px(f.ask - f.bid)}   mid ${px((f.ask + f.bid) / 2, 4)}`, w - padL, ty + rh / 2);
    } else {
      label(ctx, 10);
      ctx.fillStyle = C.faint;
      ctx.textAlign = 'left';
      ctx.fillText(f.empty ? 'BOOK EMPTY AT THIS INSTANT' : 'ONE-SIDED BOOK — NO SPREAD', padL, ty + rh / 2);
    }

    for (let k = 0; k < L; k++) row(L + 1 + k, 'b', f.bids[k]);

    // A dimension bracket down the left edge, matching the one drawn on the heatmap at
    // the playhead: the ladder is that slice of the session, enlarged. Same violet, same
    // shape, so the two panes read as one instrument and its detail callout.
    if (f.bids.length || f.asks.length) {
      const top = Math.max(0, (L - f.asks.length) * rh) + 1;
      const bot = Math.min(h, (L + 1 + f.bids.length) * rh) - 1;
      ctx.strokeStyle = C.mid;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(2.5, top);
      ctx.lineTo(2.5, bot);
      ctx.moveTo(2.5, top);
      ctx.lineTo(9, top);
      ctx.moveTo(2.5, bot);
      ctx.lineTo(9, bot);
      ctx.stroke();
    }
  }

  /* ------------------------------------------------------------------ overlays */

  function drawHeat(D, slotIdx, f, sweep) {
    const cv = $('heat');
    const { ctx, w, h } = surface(cv);
    if (!D.heat || D.heatW !== w || D.heatH !== h) {
      D.heat = buildHeat(D, w, h);
      D.heatW = w;
      D.heatH = h;
    }
    const cut = sweep === undefined ? 1 : sweep;
    ctx.drawImage(D.heat, 0, 0, D.heat.width * cut, D.heat.height, 0, 0, w * cut, h);

    if (sweep !== undefined && sweep < 1) return;

    const x = xOfSlot(D, slotIdx, w);

    // the price span currently in the ladder, bracketed at the playhead
    const prices = f.bids.map((v) => v[0]).concat(f.asks.map((v) => v[0]));
    if (prices.length) {
      const y0 = yOfPx(D, Math.max(...prices), h);
      const y1 = yOfPx(D, Math.min(...prices), h);
      ctx.strokeStyle = C.mid;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x - 6, y0);
      ctx.lineTo(x, y0);
      ctx.lineTo(x, y1);
      ctx.lineTo(x - 6, y1);
      ctx.stroke();
    }

    ctx.strokeStyle = rgba(C.midRGB, 0.85);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, HEAT_PAD.t);
    ctx.lineTo(Math.round(x) + 0.5, h - HEAT_PAD.b);
    ctx.stroke();
  }

  function drawStrip(D, slotIdx) {
    const { ctx, w, h } = surface($('spreadchart'));
    if (!D.strip || D.stripW !== w || D.stripH !== h) {
      D.strip = buildStrip(D, w, h);
      D.stripW = w;
      D.stripH = h;
    }
    ctx.drawImage(D.strip, 0, 0, w, h);
    const x = Math.round(xOfSlot(D, slotIdx, w)) + 0.5;
    ctx.strokeStyle = rgba(C.midRGB, 0.85);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
  }

  /* ------------------------------------------------------------------ state */

  const symbols = S.symbols.map((s) => s.symbol);
  let sym = symbols[0];
  let D = null;
  let cursor = 0;
  let playing = false;
  let sweepStart = 0;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function render(sweep) {
    if (!D) return;
    const f = frameAt(D, cursor);
    drawLadder(D, f);
    drawHeat(D, cursor, f, sweep);
    drawStrip(D, cursor);

    const ts = D.t0 + cursor * D.gridS * NS;
    $('clock').textContent = clockOf(ts);
    const scrub = $('scrub');
    scrub.setAttribute('aria-valuetext', `${clockOf(ts)} Eastern`);
    scrub.style.setProperty('--pct', `${(cursor / Math.max(1, D.slots - 1)) * 100}%`);

    $('r-bid').textContent = f.bid ? px(f.bid) : '—';
    $('r-ask').textContent = f.ask ? px(f.ask) : '—';
    $('r-spread').textContent = f.bid && f.ask ? px(f.ask - f.bid) : '—';
    $('r-mid').textContent = f.bid && f.ask ? px((f.bid + f.ask) / 2, 4) : '—';
    $('r-bdepth').textContent = f.depthBid ? fmtInt(f.depthBid) : '—';
    $('r-adepth').textContent = f.depthAsk ? fmtInt(f.depthAsk) : '—';
    const tot = f.depthBid + f.depthAsk;
    $('r-imb').textContent = tot
      ? ((f.depthBid - f.depthAsk) / tot >= 0 ? '+' : '') +
        ((f.depthBid - f.depthAsk) / tot).toFixed(4)
      : '—';
    $('r-levels').textContent = `${fmtInt(f.nBid)} / ${fmtInt(f.nAsk)}`;
    $('ladder-levels').textContent = `${f.bids.length} bid · ${f.asks.length} ask shown`;
  }

  function setCursor(v, fromInput) {
    cursor = Math.max(0, Math.min(D.slots - 1, Math.round(v)));
    if (!fromInput) $('scrub').value = String(cursor);
    render();
  }

  function loadSymbol(name, then) {
    if (cache.has(name)) {
      D = cache.get(name);
      then();
      return;
    }
    const done = () => {
      const raw = (window.ITCH_BOOK || {})[name];
      if (!raw) return;
      D = decode(raw);
      cache.set(name, D);
      then();
    };
    if ((window.ITCH_BOOK || {})[name]) return done();
    const s = document.createElement('script');
    s.src = `data/book-${name}.js`;
    s.onload = done;
    s.onerror = () => {
      document.querySelector('main').insertAdjacentHTML(
        'afterbegin',
        `<p class="nodata"><strong>Could not load <code>data/book-${name}.js</code>.</strong> ` +
          'Run <code>python scripts/export_web_data.py</code> from the project root.</p>'
      );
    };
    document.head.appendChild(s);
  }

  function selectSymbol(name) {
    sym = name;
    for (const b of $('tabs').children) b.setAttribute('aria-pressed', String(b.dataset.sym === name));
    loadSymbol(name, () => {
      $('scrub').max = String(D.slots - 1);
      $('heat-range').textContent = `${px(D.pxLo)} – ${px(D.pxHi)} · ${fmtInt(D.n)} distinct book states`;
      $('spread-scale').textContent = `0.00 – ${px(D.spreadMax)} shown`;
      // Land on the opening cross: the book is at its deepest and the ladder is full.
      const openSlot = Math.round((S.session.open_ns - D.t0) / (D.gridS * NS));
      setCursor(Math.min(D.slots - 1, openSlot));
      if (reduced) render();
      else runSweep();
      updateFooterFrames();
    });
  }

  function runSweep() {
    sweepStart = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - sweepStart) / 750);
      render(k * k * (3 - 2 * k));
      if (k < 1) requestAnimationFrame(step);
      else render();
    };
    requestAnimationFrame(step);
  }

  /* ------------------------------------------------------------------ controls */

  $('tabs').innerHTML = S.symbols
    .map(
      (s, i) =>
        `<button class="tab" type="button" data-sym="${s.symbol}" ` +
        `aria-pressed="${i === 0}" title="${fmtInt(s.events)} book events, ${fmtInt(
          s.frames
        )} distinct states">${s.symbol}</button>`
    )
    .join('');

  $('tabs').addEventListener('click', (e) => {
    const b = e.target.closest('.tab');
    if (b) selectSymbol(b.dataset.sym);
  });
  $('tabs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const kids = [...$('tabs').children];
    const i = kids.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    const next = kids[(i + (e.key === 'ArrowRight' ? 1 : kids.length - 1)) % kids.length];
    next.focus();
    selectSymbol(next.dataset.sym);
  });

  $('scrub').addEventListener('input', (e) => {
    stop();
    setCursor(Number(e.target.value), true);
  });

  function scrubFrom(cv, ev) {
    const r = cv.getBoundingClientRect();
    const plotW = r.width - HEAT_PAD.r;
    const frac = Math.max(0, Math.min(1, (ev.clientX - r.left) / plotW));
    setCursor(frac * (D.slots - 1));
  }
  for (const id of ['heat', 'spreadchart']) {
    const cv = $(id);
    cv.addEventListener('pointerdown', (e) => {
      stop();
      cv.setPointerCapture(e.pointerId);
      scrubFrom(cv, e);
    });
    cv.addEventListener('pointermove', (e) => {
      if (cv.hasPointerCapture(e.pointerId)) scrubFrom(cv, e);
    });
  }

  let timer = 0;
  function stop() {
    playing = false;
    $('play').textContent = 'Play';
    $('play').setAttribute('aria-pressed', 'false');
    if (timer) cancelAnimationFrame(timer);
    timer = 0;
  }
  function start() {
    if (playing) return;
    playing = true;
    $('play').textContent = 'Pause';
    $('play').setAttribute('aria-pressed', 'true');
    // Advance by elapsed wall-clock time, not by frames: stepping a fixed number of
    // slots per animation frame would run 2.4x faster on a 144 Hz display than on a
    // 60 Hz one, and the speed control would mean nothing.
    let last = performance.now();
    let pos = cursor;
    const tick = (now) => {
      if (!playing) return;
      const dt = Math.min(0.25, (now - last) / 1000); // clamp: a backgrounded tab resumes
      last = now;
      const slotsPerSecond = (Number($('rate').value) * 60) / D.gridS;
      pos += slotsPerSecond * dt;
      if (pos >= D.slots - 1) pos = 0; // wrap, so the loop keeps running
      setCursor(pos);
      timer = requestAnimationFrame(tick);
    };
    timer = requestAnimationFrame(tick);
  }
  $('play').addEventListener('click', () => (playing ? stop() : start()));

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, select, textarea, button')) return;
    if (e.code === 'Space') {
      e.preventDefault();
      playing ? stop() : start();
    }
  });

  let rt = 0;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => {
      if (D) {
        D.heat = null;
        D.strip = null;
      }
      render();
      drawThroughput();
      drawMix();
    }, 140);
  });

  const dark = window.matchMedia('(prefers-color-scheme: dark)');
  dark.addEventListener('change', () => {
    readTheme();
    if (D) {
      D.heat = null;
      D.strip = null;
    }
    render();
    drawThroughput();
    drawMix();
  });

  /* ------------------------------------------------------------------ static charts */

  function drawThroughput() {
    const rows = S.throughput;
    const { ctx, w, h } = surface($('throughput'));
    const padL = Math.min(128, w * 0.34);
    const padR = 8;
    const barMax = w - padL - padR;
    const gh = h / rows.length;
    const max = Math.max(...rows.map((r) => r.vector));

    ctx.textBaseline = 'middle';
    rows.forEach((r, i) => {
      const y = i * gh;
      label(ctx, 11, r.real ? 700 : 500);
      ctx.fillStyle = r.real ? C.fg : C.faint;
      ctx.textAlign = 'left';
      ctx.fillText(r.label.toUpperCase(), 0, y + gh * 0.34);
      mono(ctx, 10);
      ctx.fillStyle = C.faint;
      ctx.fillText(`${fmtInt(r.decoded)} records`, 0, y + gh * 0.63);

      const bh = Math.min(12, gh * 0.24);
      ctx.fillStyle = C.faint;
      ctx.globalAlpha = r.real ? 0.7 : 0.42;
      ctx.fillRect(padL, y + gh * 0.24, (barMax * r.naive) / max, bh);
      ctx.globalAlpha = 1;
      ctx.fillStyle = C.mid;
      ctx.globalAlpha = r.real ? 1 : 0.6;
      ctx.fillRect(padL, y + gh * 0.24 + bh + 3, (barMax * r.vector) / max, bh);
      ctx.globalAlpha = 1;

      mono(ctx, 11, 600);
      ctx.fillStyle = r.real ? C.fg : C.faint;
      ctx.textAlign = 'left';
      const bx = padL + (barMax * r.vector) / max;
      ctx.textAlign = bx > w - 46 ? 'right' : 'left';
      ctx.fillText(
        `${r.speedup.toFixed(2)}×`,
        bx > w - 46 ? bx - 6 : bx + 6,
        y + gh * 0.24 + bh + 3 + bh / 2
      );
    });
  }

  const TYPE_NAMES = {
    S: 'System event',
    R: 'Stock directory',
    H: 'Stock trading action',
    Y: 'Reg SHO restriction',
    L: 'Market participant position',
    V: 'MWCB decline level',
    A: 'Add order',
    F: 'Add order, attributed',
    E: 'Order executed',
    C: 'Order executed with price',
    X: 'Order cancel',
    D: 'Order delete',
    U: 'Order replace',
    P: 'Trade, non-cross',
    Q: 'Cross trade',
    I: 'Net order imbalance',
  };
  const BOOK_TYPES = new Set(['A', 'F', 'E', 'C', 'X', 'D', 'U', 'P']);

  function drawMix() {
    const { ctx, w, h } = surface($('msgmix'));
    const entries = Object.entries(S.type_counts).sort((a, b) => b[1] - a[1]);
    const padL = Math.min(210, w * 0.42);
    const padR = 70;
    const top = 6;
    const bottom = 18;
    const barMax = w - padL - padR;
    const rh = (h - top - bottom) / entries.length;
    const logMax = Math.log10(Math.max(...entries.map((e) => e[1])) * 1.6);
    const xOf = (n) => (barMax * Math.log10(Math.max(1, n) * 1.6)) / logMax;

    // log decade gridlines
    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    label(ctx, 9);
    ctx.fillStyle = C.faint;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (let d = 0; d <= 6; d++) {
      const n = Math.pow(10, d);
      const x = Math.round(padL + xOf(n)) + 0.5;
      if (x > w - padR) break;
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, h - bottom);
      ctx.stroke();
      ctx.fillText(d === 0 ? '1' : `10${'⁰¹²³⁴⁵⁶'[d]}`, x, h - bottom + 4);
    }

    ctx.textBaseline = 'middle';
    entries.forEach(([t, n], i) => {
      const y = top + i * rh;
      const isBook = BOOK_TYPES.has(t);
      const col = isBook ? C.bid : C.faint;

      label(ctx, Math.min(15, rh * 0.8), 700);
      ctx.fillStyle = isBook ? C.mid : C.faint;
      ctx.textAlign = 'left';
      ctx.fillText(t, 0, y + rh / 2);

      label(ctx, Math.min(11.5, rh * 0.62), 500);
      ctx.fillStyle = isBook ? C.dim : C.faint;
      ctx.fillText((TYPE_NAMES[t] || 'other').toUpperCase(), 16, y + rh / 2);

      const bh = Math.max(4, rh * 0.5);
      ctx.fillStyle = col;
      ctx.globalAlpha = isBook ? 0.9 : 0.62;
      ctx.fillRect(padL, y + (rh - bh) / 2, Math.max(1, xOf(n)), bh);
      ctx.globalAlpha = 1;

      mono(ctx, Math.min(11.5, rh * 0.6));
      ctx.fillStyle = isBook ? C.fg : C.faint;
      ctx.textAlign = 'left';
      ctx.fillText(fmtInt(n), padL + Math.max(1, xOf(n)) + 7, y + rh / 2);
    });
  }

  /* ------------------------------------------------------------------ static text */

  function fillStatic() {
    const o = S.oracle;
    $('m-bytes').textContent = fmtInt(S.source.bytes_read);
    $('m-msgs').textContent = fmtInt(S.source.messages);
    $('m-events').textContent = `${fmtInt(S.source.book_events)}`;
    $('m-exec').textContent = `${fmtInt(o.executions_checked)}`;

    $('c-agree').firstChild.textContent = `${(o.agreement_rate * 100).toFixed(4)}%`;
    $('c-exec').textContent = fmtInt(o.executions_checked);
    $('c-refs').textContent = `${fmtInt(o.refs_resolved)} / ${fmtInt(o.executions_checked)}`;
    $('c-away').textContent = fmtInt(o.away_from_touch);
    $('c-empty').textContent = fmtInt(o.side_empty);
    $('c-qty').textContent = fmtInt(o.executions_checked - o.quantity_feasible);
    $('c-snaps').textContent = fmtInt(o.snapshots_taken);
    $('c-cross').textContent = fmtInt(o.crossed_snapshots);

    const hero = S.throughput.find((r) => r.label === 'real 100 MB') || S.throughput[0];
    $('c-speedup').firstChild.textContent = `${hero.speedup.toFixed(2)}×`;
    $('c-rates').innerHTML =
      `naive <b>${fmtInt(hero.naive)}</b> → vectorised <b>${fmtInt(hero.vector)}</b> ` +
      `decoded records / sec<br>${hero.label}, ${fmtInt(hero.decoded)} records, ` +
      `median of 5 trials`;
    $('c-profile').textContent =
      `Vectorising field extraction moved the bottleneck rather than removing it: ` +
      `${(S.profile.scan_share * 100).toFixed(1)}% of the remaining ` +
      `${S.profile.total_s.toFixed(2)} s is the sequential framing scan, which cannot be ` +
      `vectorised — a record's length is only knowable once you are standing at its start, ` +
      `so offsets form a chain, not an arithmetic sequence.`;

    $('f-source').textContent =
      `${S.source.file} — the first ${fmtInt(S.source.bytes_read)} bytes of Nasdaq's free ` +
      `historical ${S.source.trading_day} sample, ${fmtInt(S.source.messages)} messages, ` +
      `04:00:00 to 09:30:30 Eastern. ${fmtInt(S.source.symbols_in_directory)} symbols in the ` +
      `stock directory, ${fmtInt(S.source.symbols_with_activity)} with book activity. The ` +
      `file itself is 409 MB and is not in the repository.`;
    $('f-env').textContent =
      `Python ${S.environment.python} on ${S.environment.platform}, ${S.environment.processor}. ` +
      `Decode ${S.timing.decode_seconds.toFixed(2)} s ` +
      `(${fmtInt(S.timing.decode_msgs_per_sec)} msg/s); replay of ` +
      `${fmtInt(S.source.book_events)} events with oracle checks on ` +
      `${S.timing.replay_seconds.toFixed(2)} s (${fmtInt(S.timing.replay_msgs_per_sec)} events/s).`;

    // scrubber axis: five even marks across the exported window
    const t0 = S.session.t0_ns;
    const t1 = S.session.t1_ns;
    for (let i = 0; i <= 4; i++) $(`ax-${i}`).textContent = clockOf(t0 + ((t1 - t0) * i) / 4);
  }

  function updateFooterFrames() {
    const m = S.symbols.find((s) => s.symbol === sym);
    $('f-frames').textContent =
      `${sym}: ${fmtInt(m.events)} book-affecting messages replayed into ` +
      `${fmtInt(m.frames)} distinct book states, sampled on a ${S.session.grid_s}-second grid ` +
      `and forward-filled. ${S.symbols.length} symbols exported; ` +
      `every displayed price is the fixed-point integer from the wire, divided by 10,000 ` +
      `only at the moment it is drawn.`;
  }

  /* ------------------------------------------------------------------ boot */

  fillStatic();
  drawThroughput();
  drawMix();
  selectSymbol(sym);
})();
