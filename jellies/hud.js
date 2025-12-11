
// hud.js — Transparent HUD with palette-aware bars, overlay labels sized to bar height
// Bottom randomized words now use the SAME font-size as the bar labels.
// Inputs: ?temp=0..100&money=0..100&w1=..&w2=..&w3=..

(function () {
  if (window.__JELLY_HUD__) return;
  window.__JELLY_HUD__ = true;

  // ---------- Utils ----------
  function clamp(n) {
    n = parseInt(n ?? "0", 10);
    if (!Number.isFinite(n)) n = 0;
    return Math.max(0, Math.min(100, n));
  }
  function getParams() {
    const raw = location.search || location.hash || "";
    const qs = raw.startsWith("?") ? raw : raw.replace(/^#/, "?");
    const p = new URLSearchParams(qs);
    return {
      temp: clamp(p.get("temp")),
      money: clamp(p.get("money")),
      w1: (p.get("w1") || "").trim(),
      w2: (p.get("w2") || "").trim(),
      w3: (p.get("w3") || "").trim(),
    };
  }

  // Color parsing/mixing
  function parseColor(str, fallback="#66ccff") {
    const s = (str || "").trim();
    if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(s)) {
      let hex = s.slice(1);
      if (hex.length === 3) hex = hex.split("").map(c => c + c).join("");
      const num = parseInt(hex, 16);
      return { r: (num >> 16) & 255, g: (num >> 8) & 255, b: num & 255 };
    }
    const m = s.match(/rgba?\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*[\d.]+\s*)?\)/i);
    if (m) return { r: +m[1], g: +m[2], b: +m[3] };
    return parseColor(fallback, "#66ccff");
  }
  function mix(a, b, t) {
    return { r: Math.round(a.r + (b.r - a.r) * t),
             g: Math.round(a.g + (b.g - a.g) * t),
             b: Math.round(a.b + (b.b - a.b) * t) };
  }
  function rgbStr(c) { return `rgb(${c.r}, ${c.g}, ${c.b})`; }

  function getSpritePalette() {
    const cs = getComputedStyle(document.documentElement);
    const bodyCol = parseColor(cs.getPropertyValue("--body") || cs.getPropertyValue("--tent") || "#66ccff");
    const tentCol = parseColor(cs.getPropertyValue("--tent") || cs.getPropertyValue("--body") || "#3399ff");
    const cool = mix(bodyCol, tentCol, 0.35);
    const hot  = { r: 255, g: 60, b: 48 };
    const okGreen = { r: 50, g: 220, b: 120 };
    return { bodyCol, tentCol, cool, hot, okGreen };
  }

  // ---------- DOM ----------
  function ensureHUD() {
    if (document.getElementById("jelly-hud")) return;

    const style = document.createElement("style");
    style.textContent = `

      @font-face {
        font-family: "JellyHUDFont";
        src:
          url("./Diskus.woff2") format("woff2"),
        font-weight: 400;
        font-style: normal;
        font-display: swap;
      }

      :root {
        --hud-bar-height: 32px;            /* bar height */
        --hud-top-font-scale: 0.9;         /* overlay label font = scale * bar height */
        --hud-text: #fff;
        --hud-glow: 0 0 6px rgba(0,0,0,.65), 0 0 2px rgba(0,0,0,.8);
      }

      #jelly-hud {
        position: fixed; left: 12px; right: 12px; top: 10px;
        display: grid; gap: 14px; z-index: 2147483647;
        font-family: system-ui, ui-sans-serif, Segoe UI, Roboto, Helvetica, Arial, sans-#serif;
        pointer-events: none;
      }

      .meter { display: flex; flex-direction: column; gap: 0; }
      .bar {
        position: relative;
        width: 100%;
        height: var(--hud-bar-height);
        background: transparent;
        overflow: hidden;
      }
      .fill {
        height: 100%;
        width: 0%;
        transition: width .25s ease-out, background-color .15s linear;
      }
      .label-overlay {
        position: absolute; inset: 0;
        display: flex; align-items: center; justify-content: center;
        color: var(--hud-text);
        font-size: calc(var(--hud-bar-height) * var(--hud-top-font-scale)); /* match bar labels */
        line-height: var(--hud-bar-height);
        font-weight: 900;
        letter-spacing: .02em;
        text-transform: uppercase;
        text-shadow: var(--hud-glow);
        pointer-events: none;
        mix-blend-mode: normal;
      }

      #jelly-words {
        position: fixed; left: 12px; right: 12px; bottom: 10px;
        display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; z-index: 2147483647;
        font-family: system-ui, ui-sans-serif, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
        pointer-events: none;
      }
      /* Bottom words use the SAME size as overlay labels */
      #jelly-words .slot {
        min-height: var(--hud-bar-height);
        display: flex; align-items: center; justify-content: center;
        color: var(--hud-text);
        font-size: calc(var(--hud-bar-height) * var(--hud-top-font-scale));
        font-weight: 800;
        letter-spacing: .02em;
        text-shadow: var(--hud-glow);
      }
      #jelly-words .slot.empty { color: transparent; text-shadow: none; }

      @media (max-width: 500px){
        :root { --hud-bar-height: 28px; }
        #jelly-hud { left: 8px; right: 8px; top: 6px; gap: 12px; }
        #jelly-words { left: 8px; right: 8px; bottom: 6px; gap: 10px; }
      }
    `;
    document.head.appendChild(style);

    const top = document.createElement("div");
    top.id = "jelly-hud";
    top.innerHTML = `
      <div class="meter">
        <div class="bar">
          <div class="fill" id="hud-temp-fill"></div>
          <div class="label-overlay">Temperature</div>
        </div>
      </div>
      <div class="meter">
        <div class="bar">
          <div class="fill" id="hud-money-fill"></div>
          <div class="label-overlay">Money</div>
        </div>
      </div>
    `;
    document.body.appendChild(top);

    const bottom = document.createElement("div");
    bottom.id = "jelly-words";
    bottom.innerHTML = `
      <div class="slot" id="word-slot-1"></div>
      <div class="slot" id="word-slot-2"></div>
      <div class="slot" id="word-slot-3"></div>
    `;
    document.body.appendChild(bottom);
  }

  // ---------- Palette & mapping ----------
  const PALETTE = getSpritePalette();
  const COOL = PALETTE.cool;
  const HOT  = PALETTE.hot;
  const OK   = PALETTE.bodyCol;
  const OK_FALLBACK = PALETTE.okGreen;

  function lerpColor(a, b, t) { return rgbStr(mix(a, b, Math.max(0, Math.min(1, t)))); }
  function colorForMoney(v) { const good = OK || OK_FALLBACK; return lerpColor(HOT, good, v / 100); }
  function colorForTemp(v)  { return lerpColor(COOL, HOT, v / 100); }

  // ---------- Setters ----------
  function setMeter(prefix, v) {
    const fill = document.getElementById(`hud-${prefix}-fill`);
    if (!fill) return;
    const val = Math.max(0, Math.min(100, v|0));
    fill.style.width = `${val}%`;
    fill.style.backgroundColor = (prefix === "money") ? colorForMoney(val) : colorForTemp(val);
  }

  function setWordSlot(i, text) {
    const el = document.getElementById(`word-slot-${i}`);
    if (!el) return;
    const t = (text || "").trim();
    if (t) { el.textContent = t; el.classList.remove("empty"); }
    else   { el.textContent = ""; el.classList.add("empty"); }
  }

  function applyFromURL() {
    const { temp, money, w1, w2, w3 } = getParams();
    setMeter("temp", temp);
    setMeter("money", money);
    setWordSlot(1, w1);
    setWordSlot(2, w2);
    setWordSlot(3, w3);
  }

  // ---------- Public API ----------
  window.JellyHUD = {
    set(temp, money) { setMeter("temp", clamp(temp)); setMeter("money", clamp(money)); },
    setTemp(v) { setMeter("temp", clamp(v)); },
    setMoney(v) { setMeter("money", clamp(v)); },
    setWords(arr) {
      setWordSlot(1, arr?.[0] ?? "");
      setWordSlot(2, arr?.[1] ?? "");
      setWordSlot(3, arr?.[2] ?? "");
    },
    refresh() { applyFromURL(); },
  };

  // ---------- Init ----------
  function init() {
    try { ensureHUD(); applyFromURL(); }
    catch (e) { console.error("hud.js init error:", e); }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
