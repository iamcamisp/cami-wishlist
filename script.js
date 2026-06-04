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

let DATA = { items: [] };

async function load() {
  const grid = document.getElementById("grid");
  const meta = document.getElementById("meta");
  try {
    const res = await fetch(`items.json?v=${Date.now()}`);
    DATA = await res.json();
    render();
  } catch (err) {
    grid.innerHTML = `<p class="loading">Could not load list.</p>`;
    console.error(err);
  } finally {
    grid.setAttribute("aria-busy", "false");
  }
  if (DATA.last_updated) {
    meta.textContent = `Prices last refreshed ${fmtDate(DATA.last_updated)}.`;
  }
}

function sortItems(items, mode) {
  const arr = items.slice();
  const price = (it) => (it.best_price_chf != null ? it.best_price_chf : Infinity);
  switch (mode) {
    case "price-asc":
      return arr.sort((a, b) => price(a) - price(b));
    case "price-desc":
      return arr.sort((a, b) => price(b) - price(a));
    case "name":
      return arr.sort((a, b) => a.name.localeCompare(b.name, "en"));
    default:
      return arr;
  }
}

function render() {
  const grid = document.getElementById("grid");
  const mode = document.getElementById("sort").value;
  const items = sortItems(DATA.items || [], mode);
  if (!items.length) {
    grid.innerHTML = `<p class="loading">No items yet.</p>`;
    return;
  }
  grid.innerHTML = items
    .map((it) => {
      const best = it.best_price_chf != null ? fmtCHF(it.best_price_chf) : "—";
      const store = it.best_store ? `at ${escape(it.best_store)}` : "";
      // Price-searched (non-manual) items link to a Google search of the product;
      // manual items (vouchers, specific shop pages) keep their direct link.
      const url = it.manual && it.best_url
        ? it.best_url
        : `https://www.google.com/search?q=${encodeURIComponent(it.name)}`;
      const image = it.image_url
        ? `<div class="image"><img src="${escapeAttr(it.image_url)}" alt="${escapeAttr(it.name)}" loading="lazy" onerror="this.parentElement.classList.add('broken')"/></div>`
        : `<div class="image broken"></div>`;
      return `
        <article class="card">
          ${image}
          <div class="name">${escape(it.name)}</div>
          <div class="prices">
            <span class="best">${best}</span>
          </div>
          <div class="store">${store}</div>
          <a class="button" href="${escapeAttr(url)}" target="_blank" rel="noopener">View deal →</a>
        </article>
      `;
    })
    .join("");
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

document.getElementById("sort").addEventListener("change", render);
load();
