/* Loan form progressive disclosure (Phase 17). Shared by the create form
 * (loans.html) and the edit form (loan_edit.html) — extracted instead of
 * duplicating the IIFE in both, mirroring the vendored chart-theme.js.
 *
 * The base-rate fields ("Variable only") apply only to a variable loan. For a
 * fixed loan, dim the column and disable its inputs so they don't post (their
 * server defaults are empty anyway). Runs immediately (the <script> sits after
 * the form markup) and inits from the current value so a pre-selected rate type
 * is reflected at once. Null-guarded so it's inert if the form isn't present. */
(function () {
  const rateType = document.getElementById('rate_type');
  const variableOnly = document.getElementById('variable-only');
  if (!rateType || !variableOnly) return;
  const fields = variableOnly.querySelectorAll('input');
  function sync() {
    const on = rateType.value === 'variable';
    variableOnly.classList.toggle('is-dimmed', !on);
    fields.forEach((f) => { f.disabled = !on; });
  }
  rateType.addEventListener('change', sync);
  sync();
})();
