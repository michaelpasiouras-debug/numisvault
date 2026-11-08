/* Photos come first (optional). The best identity fills the search box but never
   starts a search: the user reviews it, then searches. Photos and results never
   enter local storage. */
(() => {
  function mount(targetId) {
    const target = document.getElementById(targetId);
    if (!target) return;
    const panel = document.createElement('details');
    panel.open = false; // optional path: many users have no photo at hand
    panel.className = 'panel';
    const summary = document.createElement('summary');
    summary.textContent = 'Have photos? Start by identifying the coin from both sides (optional)';
    panel.append(summary);
    const help = document.createElement('p');
    help.textContent = 'Upload both sides of the same coin, upright and in focus. Claude AI fills the search box below with its best identification — check it, especially the year, before searching. Photos cannot verify authenticity or metal purity.';
    panel.append(help);
    const grid = document.createElement('div');
    grid.className = 'grid2';
    panel.append(grid);
    const inputs = [];
    const previews = [];
    let revision = 0;
    let controller;
    for (const side of ['Side 1', 'Side 2']) {
      const label = document.createElement('label');
      label.textContent = side;
      const input = document.createElement('input');
      input.type = 'file'; input.accept = 'image/jpeg,image/png';
      const preview = document.createElement('img');
      preview.alt = `${side} preview`; preview.hidden = true;
      preview.style.cssText = 'max-width:100%;height:160px;object-fit:contain';
      input.addEventListener('change', () => {
        revision++; controller?.abort(); results.replaceChildren();
        if (preview.dataset.url) URL.revokeObjectURL(preview.dataset.url);
        preview.hidden = !input.files[0];
        if (input.files[0]) {
          preview.dataset.url = URL.createObjectURL(input.files[0]);
          preview.src = preview.dataset.url;
        }
      });
      label.append(input, preview); grid.append(label); inputs.push(input); previews.push(preview);
    }
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'primary'; button.textContent = 'Identify coin';
    const results = document.createElement('div');
    results.setAttribute('aria-live', 'polite');
    panel.append(button, results);
    target.closest('.field').before(panel);
    button.addEventListener('click', async () => {
      const current = ++revision;
      controller?.abort(); controller = new AbortController();
      const activeController = controller;
      const signal = activeController.signal;
      button.disabled = true; results.textContent = 'Reading photos…';
      const timer = setTimeout(() => activeController.abort(), 50000);
      try {
        const images = await Promise.all(inputs.map(input => prepare(input.files[0])));
        if (current !== revision) return;
        const endpoint = new URL(getApiEndpoint(), location.href);
        endpoint.pathname = endpoint.pathname.replace(/\/api\/coin-search\/?$/, '/api/coin-identify-images');
        endpoint.search = ''; endpoint.hash = '';
        const response = await fetch(endpoint, {method:'POST', signal,
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({images})});
        const data = await response.json();
        if (current !== revision) return;
        if (!response.ok) throw new Error(data.error || 'Photo recognition is unavailable.');
        results.replaceChildren();
        const note = document.createElement('p');
        const candidates = data.candidates || [];
        note.textContent = candidates.length ? '' : 'No matching type found. Try clearer photos or write the coin in the box below.';
        results.append(note);
        const fill = (candidate, yearText) => {
          target.value = [candidate.country, candidate.title, yearText].filter(Boolean).join(' ');
          target.dispatchEvent(new Event('input', {bubbles:true}));
          target.dispatchEvent(new Event('change', {bubbles:true}));
          target.focus();
        };
        // Pre-fill the search box with the best identification. Searching still
        // requires the user's own click, so nothing runs on an unchecked guess.
        if (candidates.length) {
          const best = candidates[0];
          const bestYear = best.year != null && /^-?\d{1,4}$/.test(String(best.year).trim()) ? String(best.year).trim() : '';
          fill(best, bestYear);
          note.textContent = bestYear
            ? `Search box filled with Claude AI's best identification: "${target.value}". Check it, then search.`
            : `Search box filled with Claude AI's best identification, but the year could not be read — add the year visible on your coin before searching.`;
          if (candidates.length > 1) {
            const other = document.createElement('p');
            other.textContent = 'Not this coin? Other possible identities:';
            results.append(other);
          }
        }
        for (const [index, candidate] of candidates.entries()) {
          if (index === 0) continue;
          const row = document.createElement('div');
          const link = document.createElement('p');
          link.textContent = `${candidate.country} ${candidate.title} — ${candidate.evidence || 'Please verify the identity.'}`;
          const year = document.createElement('input');
          year.type = 'text'; year.inputMode = 'numeric'; year.placeholder = 'Year visible on your coin';
          year.setAttribute('aria-label', 'Year visible on your coin');
          if (candidate.year != null) year.value = candidate.year;
          const use = document.createElement('button');
          use.type = 'button'; use.textContent = 'Use this instead';
          use.addEventListener('click', () => {
            if (!/^-?\d{1,4}$/.test(year.value.trim())) {year.setCustomValidity('Enter the year visible on your coin.'); year.reportValidity(); return;}
            year.setCustomValidity('');
            fill(candidate, year.value.trim());
            note.textContent = `Search box changed to "${target.value}". Check it, then search.`;
          });
          year.addEventListener('input', () => year.setCustomValidity(''));
          row.append(link, year, use); results.append(row);
        }
      } catch (error) {
        if (current === revision) results.textContent = error.name === 'AbortError' ? 'Recognition timed out. Please try again.' : error.message;
      } finally {clearTimeout(timer); button.disabled = false;}
    });
  }
  async function prepare(file) {
    if (!file) throw new Error('Please upload both sides of the coin.');
    if (!['image/jpeg', 'image/png'].includes(file.type) || file.size > 10 * 1024 * 1024)
      throw new Error('Use JPEG or PNG photos up to 10 MB each.');
    const bitmap = await createImageBitmap(file);
    try {
      const scale = Math.min(1, 1000 / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      return {mime_type:'image/jpeg', image_data:canvas.toDataURL('image/jpeg', .88).split(',')[1]};
    } finally {bitmap.close();}
  }
  mount('rFreeText'); mount('aCoin');
})();
