// Copy-to-clipboard for the plan's "how to pay" card (Phase 19b).
//
// Delegated click handler on any [data-copy] element: copies its attribute value
// (the transfer title, amount, or payee IBAN) and flashes brief feedback. Offline,
// no libraries — navigator.clipboard only (the app is LAN/local-only, so there's no
// CDN to pull). Null-/feature-guarded so it's harmless where clipboard is absent.
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-copy]');
  if (!btn) return;
  e.preventDefault();
  const text = btn.getAttribute('data-copy');
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(text).then(() => {
    const original = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = original; }, 1200);
  });
});
