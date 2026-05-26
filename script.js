const fmtCHF = (n) =>
  new Intl.NumberFormat("de-CH", {
    style: "currency",
    currency: "CHF",
    maximumFractionDigits: 2,
  }).format(n);

const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
};

async function load() {
  const grid = document.getElementById("grid");
  const meta = document.getElementById("meta");
  try {
    const res = await fetch(`items.json?v=${Date.now()}`);
    const data = await res.json();
    render(data, grid, meta);
  } catch (err) {
    grid.innerHTML = `<p class="loading">Could not load list.</p>`;
    console.error(err);
  } finally {
    grid.setAttribute("aria-busy", "false");
  }
}

function render(data, grid, meta) {
  const items = data.items || [];
  if (!items.length) {
    grid.innerHTML = `<p class="loading">No items yet.</p>`;
  } else {
    grid.innerHTML = items
      .map((it) => {
        const best = it.best_price_chf != null ? fmtCHF(it.best_price_chf) : "—";
        const avg = it.avg_price_chf != null ? `avg ${fmtCHF(it.avg_price_chf)}` : "";
        const store = it.best_store ? `at ${escape(it.best_store)}` : "";
        const url = it.best_url || "#";
        const image = it.image_url
          ? `<div class="image"><img src="${escapeAttr(it.image_url)}" alt="${escapeAttr(it.name)}" loading="lazy" onerror="this.parentElement.classList.add('broken')"/></div>`
          : `<div class="image broken"></div>`;
        return `
          <article class="card">
            ${image}
            <div class="name">${escape(it.name)}</div>
            <div class="prices">
              <span class="best">${best}</span>
              <span class="avg">${avg}</span>
            </div>
            <div class="store">${store}</div>
            <a class="button" href="${escapeAttr(url)}" target="_blank" rel="noopener">View deal →</a>
          </article>
        `;
      })
      .join("");
  }
  if (data.last_updated) {
    meta.textContent = `Prices last refreshed ${fmtDate(data.last_updated)}.`;
  }
}

function escape(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function escapeAttr(s) {
  return escape(s);
}

load();
