/* Desk theme system: four skins as data, one tiny swatch switcher.
   Used by the portal (b4rruf3t.com) and the desk app — single source. */

const THEMES = {
  shadcn: {
    label: "shadcn",
    vars: {
      "--bg": "#09090b", "--panel": "#101012", "--panel2": "#18181b",
      "--line": "#232326", "--text": "#fafafa", "--dim": "#a1a1aa",
      "--accent": "#fafafa", "--accent-contrast": "#09090b",
      "--green": "#4ade80", "--red": "#f87171", "--amber": "#fbbf24",
      "--user": "#1c1c1f", "--radius": "10px",
      "--font": '"Geist","Inter",system-ui,sans-serif',
    },
  },
  gallery: {
    label: "gallery",
    vars: {
      "--bg": "#13151b", "--panel": "rgba(255,255,255,.04)", "--panel2": "rgba(255,255,255,.07)",
      "--line": "rgba(255,255,255,.09)", "--text": "#eceef4", "--dim": "#9ba3b2",
      "--accent": "#7c8aff", "--accent-contrast": "#0d0f16",
      "--green": "#3ecf8e", "--red": "#f4657f", "--amber": "#ffb454",
      "--user": "#232c4a", "--radius": "8px",
      "--font": '"Inter",system-ui,sans-serif',
    },
  },
  daisy: {
    label: "daisy",
    vars: {
      "--bg": "#1d232a", "--panel": "#191e24", "--panel2": "#15191e",
      "--line": "rgba(255,255,255,.06)", "--text": "#eceff4", "--dim": "#9fa9b8",
      "--accent": "#605dff", "--accent-contrast": "#ffffff",
      "--green": "#00d390", "--red": "#ff6f70", "--amber": "#eab308",
      "--user": "#262e38", "--radius": "14px",
      "--font": '"Inter",system-ui,sans-serif',
    },
  },
  saas: {
    label: "slate",
    vars: {
      "--bg": "#0f172a", "--panel": "#16213b", "--panel2": "#1c2947",
      "--line": "#26334f", "--text": "#e6ecf7", "--dim": "#8fa0bd",
      "--accent": "#3b82f6", "--accent-contrast": "#ffffff",
      "--green": "#34d399", "--red": "#fb7185", "--amber": "#fbbf24",
      "--user": "#1e2b4d", "--radius": "10px",
      "--font": '"Inter",system-ui,sans-serif',
    },
  },
};

const THEME_KEY = "desk-theme";
// ONE theme, chosen on the MAIN page. Framed surfaces (the Marcus panel, the
// desk inside a tab) render no selector and adopt the parent's theme via a
// request/reply handshake — their own localStorage never wins over the top.
const FRAMED = (() => { try { return window.self !== window.top; } catch { return true; } })();
let currentTheme = "shadcn";

function applyTheme(name, { broadcast = true } = {}) {
  const t = THEMES[name];
  if (!t) return;
  currentTheme = name;
  for (const [k, v] of Object.entries(t.vars)) {
    document.documentElement.style.setProperty(k, v);
  }
  try { localStorage.setItem(THEME_KEY, name); } catch {}
  document.querySelectorAll("#theme-dots button").forEach((b) =>
    b.classList.toggle("on", b.dataset.theme === name));
  window.dispatchEvent(new CustomEvent("themechange", { detail: name }));
  if (broadcast) {
    document.querySelectorAll("iframe").forEach((f) => {
      try { f.contentWindow.postMessage({ deskTheme: name }, "*"); } catch {}
    });
  }
}

function renderThemeDots() {
  const host = document.getElementById("theme-dots");
  if (!host) return;
  host.innerHTML = "";
  Object.assign(host.style, { display: "flex", gap: "8px", alignItems: "center",
                              margin: "14px 0 10px" });
  for (const [name, t] of Object.entries(THEMES)) {
    const b = document.createElement("button");
    b.dataset.theme = name;
    b.title = t.label;
    b.setAttribute("aria-label", "theme: " + t.label);
    Object.assign(b.style, {
      width: "16px", height: "16px", borderRadius: "50%", cursor: "pointer",
      background: t.vars["--bg"], border: "2px solid " + t.vars["--accent"],
      padding: "0", outlineOffset: "2px",
    });
    b.addEventListener("click", () => applyTheme(name));
    host.appendChild(b);
  }
  const style = document.createElement("style");
  style.textContent = "#theme-dots button{opacity:.45;transition:opacity .12s,transform .12s}" +
    "#theme-dots button:hover{opacity:.85}" +
    "#theme-dots button.on{opacity:1;transform:scale(1.22)}";
  document.head.appendChild(style);
}

window.addEventListener("message", (e) => {
  if (e.data && e.data.deskTheme && THEMES[e.data.deskTheme]) {
    applyTheme(e.data.deskTheme, { broadcast: e.data.rebroadcast === true });
  }
  if (e.data && e.data.deskThemeRequest && e.source) {
    // a child frame just booted: hand it the current theme
    try { e.source.postMessage({ deskTheme: currentTheme }, "*"); } catch {}
  }
});

(function initTheme() {
  const boot = () => {
    let saved = "shadcn";
    try { saved = localStorage.getItem(THEME_KEY) || saved; } catch {}
    if (FRAMED) {
      // no selector inside frames — paint the local guess to avoid a flash,
      // then ask the top page for the real theme
      applyTheme(saved, { broadcast: false });
      try { window.top.postMessage({ deskThemeRequest: true }, "*"); } catch {}
      return;
    }
    renderThemeDots();
    applyTheme(saved, { broadcast: false });
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
