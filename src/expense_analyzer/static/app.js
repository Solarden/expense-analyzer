/* The dashboard's one page script, loaded with `defer` from base.html. Plain JS, no build step.
 * Every page works without it; listeners are delegated on document, so markup opts in through
 * data-* attributes and nothing here assumes which page it runs on. Chart data arrives in a
 * <script type="application/json" id="<canvas id>-data"> block next to its canvas. */

// The .no-js fallbacks in the stylesheet (keyboard-opened nav menus) stand down.
document.documentElement.classList.remove('no-js');

// A form error arrives with the page, where role="alert" is not reliably announced:
// focus it so a screen reader reads it first.
document.getElementById('form-error')?.focus();

// Filters, month pickers and the lens apply on change; each of those forms keeps a <noscript>
// submit button for when this does not run.
document.addEventListener('change', (e) => {
  if (e.target.matches('[data-autosubmit]')) e.target.form.submit();
});

document.addEventListener('submit', (e) => {
  const message = e.target.dataset.confirm;
  if (message && !confirm(message)) e.preventDefault();
});

// The page's one note modal, opened by .is-open rather than the URL hash: a hash survives a
// reload or Back and would reopen the modal with nothing filled in.
let noteOpener = null;
const closeNote = () => {
  const modal = document.getElementById('note-modal');
  if (!modal || !modal.classList.contains('is-open')) return false;
  modal.classList.remove('is-open');
  noteOpener?.focus();
  noteOpener = null;
  return true;
};

const setMenu = (group, open) => {
  group.classList.toggle('is-open', open);
  group.querySelector('.nav-trigger').setAttribute('aria-expanded', open);
};

// Below md the burger nav shows every menu expanded, so a trigger there opens nothing.
const desktopNav = window.matchMedia('(min-width: 768px)');

document.addEventListener('click', (e) => {
  const toggle = e.target.closest('.nav-toggle');
  if (toggle) {
    const nav = document.getElementById(toggle.getAttribute('aria-controls'));
    toggle.setAttribute('aria-expanded', nav.classList.toggle('is-open'));
    return;
  }

  // A click outside an open menu closes it; Safari never focuses a clicked button, so the
  // focusout handler below would not see it.
  document.querySelectorAll('.nav-group.is-open').forEach((g) => {
    if (!g.contains(e.target)) setMenu(g, false);
  });
  const trigger = e.target.closest('.nav-trigger');
  if (trigger && desktopNav.matches) {
    const group = trigger.closest('.nav-group');
    setMenu(group, !group.classList.contains('is-open'));
    return;
  }

  // [data-note-for]: fill the page's one note modal from the row and open it. The link's own
  // href (the row's edit page) is the fallback without this script.
  const noteLink = e.target.closest('[data-note-for]');
  if (noteLink) {
    const modal = document.getElementById('note-modal');
    if (!modal) return;
    e.preventDefault();
    modal.querySelector('form').action = `/dashboard/transactions/${noteLink.dataset.noteFor}/note`;
    modal.querySelector('[data-note-subject]').textContent = noteLink.dataset.noteSubject;
    const text = modal.querySelector('textarea');
    text.value = noteLink.dataset.note;
    modal.classList.add('is-open');
    noteOpener = noteLink;
    text.focus();
    return;
  }

  if (e.target.closest('[data-modal-close]')) {
    e.preventDefault();
    closeNote();
    return;
  }

  // [data-copy]: copy the attribute's value (a transfer title, amount or account number).
  // An icon button confirms by turning into a check for a moment; a text one says "Copied".
  const copy = e.target.closest('[data-copy]');
  if (!copy || copy.getAttribute('aria-disabled') === 'true') return;
  e.preventDefault();
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(copy.dataset.copy).then(() => {
    // The idle look is read once, so a second click inside the 1.2 s does not keep "Copied".
    const icon = copy.querySelector('use');
    const show = (look) => (icon ? icon.setAttribute('href', look) : (copy.textContent = look));
    const idle = icon ? (icon.dataset.idle ??= icon.getAttribute('href')) : (copy.dataset.idle ??= copy.textContent);
    show(icon ? '#i-check' : 'Copied');
    clearTimeout(copy.copiedTimer);
    copy.copiedTimer = setTimeout(() => show(idle), 1200);
  });
});

// A nav menu closes once focus leaves its group (Tab past the last item, a click elsewhere).
document.addEventListener('focusout', (e) => {
  const group = e.target.closest?.('.nav-group.is-open');
  if (group && !group.contains(e.relatedTarget)) setMenu(group, false);
});

// Escape closes whatever is open, returning focus to what opened it so keyboard users keep
// their place: the note modal, a nav menu, the mobile burger nav. It also hides a hovered note
// tooltip until the pointer leaves it.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (closeNote()) return;

  const group = document.querySelector('.nav-group.is-open');
  if (group) {
    setMenu(group, false);
    group.querySelector('.nav-trigger').focus();
    return;
  }

  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  document.querySelectorAll('.tip:hover').forEach((tip) => {
    tip.classList.add('tip-off');
    tip.addEventListener('mouseleave', () => tip.classList.remove('tip-off'), { once: true });
  });
  const nav = document.getElementById('primary-nav');
  if (nav && nav.classList.contains('is-open')) {
    nav.classList.remove('is-open');
    const toggle = document.querySelector('.nav-toggle');
    if (toggle) {
      toggle.setAttribute('aria-expanded', 'false');
      toggle.focus();
    }
  }
});

// Loan form (create and edit): the base-rate fields apply only to a variable loan. For a fixed
// one, dim the column and disable its inputs so they don't post.
(() => {
  const rateType = document.getElementById('rate_type');
  const variableOnly = document.getElementById('variable-only');
  if (!rateType || !variableOnly) return;
  const fields = variableOnly.querySelectorAll('input');
  const sync = () => {
    const on = rateType.value === 'variable';
    variableOnly.classList.toggle('is-dimmed', !on);
    fields.forEach((f) => { f.disabled = !on; });
  };
  rateType.addEventListener('change', sync);
  sync();
})();

// Charts. chart.min.js is a plain <script> on the chart pages only, so it has run before this
// deferred file; without it the canvases stay blank.
if (window.Chart) {
  // Out of the box Chart.js assumes a light page. Colours come from the skin's variables, so the
  // charts follow the stylesheet instead of restating it.
  const skin = getComputedStyle(document.documentElement);
  const token = (name) => skin.getPropertyValue(`--${name}`).trim();
  Chart.defaults.color = token('muted');
  Chart.defaults.borderColor = token('line');
  Chart.defaults.font.family = token('sans');

  // Amounts arrive as integer minor units (1/100 PLN); fmt is the one place that divides by 100.
  const fmt = (minor) => (minor / 100).toLocaleString('pl-PL', { style: 'currency', currency: 'PLN' });
  const hbar = (data, colour, extra = {}) => ({
    type: 'bar',
    data: { labels: data.labels, datasets: [{ data: data.data, backgroundColor: colour }] },
    options: {
      indexAxis: 'y',
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => fmt(c.parsed.x) } } },
      scales: { x: { ticks: { callback: fmt } } },
      ...extra,
    },
  });
  // Chart.js v4 ships no default colour cycle. Slices take the skin's categorical palette in
  // the tickers' alphabetical order, so a ticker keeps its colour when its value (and rank) moves.
  const PALETTE = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => token(`chart-${n}`));
  const sliceColours = (labels) => {
    const order = [...new Set(labels)].sort();
    return labels.map((label) => PALETTE[order.indexOf(label) % PALETTE.length]);
  };

  const builders = {
    category: (data) => hbar(data, data.colors, {
      // Click a bar to drill into that category's transactions for the month.
      onClick: (evt, elements) => {
        const link = elements.length && data.links[elements[0].index];
        if (link) window.location = link;
      },
      onHover: (evt, elements) => {
        evt.native.target.style.cursor = elements.length ? 'pointer' : 'default';
      },
    }),
    trend: (data) => ({
      type: 'bar',
      data: {
        labels: data.labels,
        datasets: [
          { label: 'Spending', data: data.spending, backgroundColor: token('neg') },
          { label: 'Income', data: data.income, backgroundColor: token('pos') },
        ],
      },
      options: {
        plugins: { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmt(c.parsed.y)}` } } },
        scales: { y: { ticks: { callback: fmt } } },
      },
    }),
    line: (data) => ({
      type: 'line',
      data: {
        labels: data.labels,
        datasets: [{ label: 'FOR LIVING', data: data.data, borderColor: token('accent'), tension: 0.2 }],
      },
      options: {
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => fmt(c.parsed.y) } } },
        scales: { y: { ticks: { callback: fmt } } },
      },
    }),
    assets: (data) => hbar(data, token('pos')),
    liabilities: (data) => hbar(data, token('neg')),
    allocation: (data) => ({
      type: 'doughnut',
      data: {
        labels: data.labels,
        datasets: [{
          data: data.data,
          backgroundColor: sliceColours(data.labels),
          borderColor: token('panel'),
          borderWidth: 2,
        }],
      },
      options: {
        // Fill the bounded wrapper: a doughnut keeps 1:1, so an unbounded container makes it huge.
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: 'bottom' },
          tooltip: { callbacks: { label: (c) => `${c.label}: ${fmt(c.parsed)}` } },
        },
      },
    }),
  };

  document.querySelectorAll('canvas[data-chart]').forEach((canvas) => {
    const data = JSON.parse(document.getElementById(`${canvas.id}-data`).textContent);
    if (data.labels.length) new Chart(canvas, builders[canvas.dataset.chart](data));
  });
}
