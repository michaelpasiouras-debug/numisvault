/* Photo candidates require confirmation; photos and results never enter local storage. */
(() => {
  function mount(targetId) {
    const target = document.getElementById(targetId);
    if (!target) return;
    const panel = document.createElement('details');
    panel.className = 'panel';
    const summary = document.createElement('summary');
    summary.textContent = 'Identify from two photos';
    panel.append(summary);
    const help = document.createElement('p');
    help.textContent = 'Upload both sides of the same coin, upright and in focus. Photos are sent to Numista for identification. Confirm the proposed type and enter the year before searching. Photos cannot verify authenticity or metal purity.';
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
    target.closest('.field').after(panel);
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
        note.textContent = data.candidates?.length ? 'Possible matches from Numista — select the correct type, then confirm its year.' : 'No matching type found. Try clearer photos or search by text.';
        results.append(note);
        for (const candidate of data.candidates || []) {
          const row = document.createElement('div');
          const link = document.createElement('a');
          link.href = candidate.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
          link.textContent = `Numista N#${candidate.id}: ${candidate.country} ${candidate.title} (${candidate.min_year ?? '?'}–${candidate.max_year ?? '?'})`;
          const year = document.createElement('input');
          year.type = 'text'; year.inputMode = 'numeric'; year.placeholder = 'Year visible on your coin';
          year.setAttribute('aria-label', 'Year visible on your coin');
          if (candidate.min_year === candidate.max_year && candidate.min_year != null) year.value = candidate.min_year;
          const use = document.createElement('button');
          use.type = 'button'; use.textContent = 'Use this coin';
          use.addEventListener('click', () => {
            if (!/^-?\d{1,4}$/.test(year.value.trim())) {year.setCustomValidity('Enter the year visible on your coin.'); year.reportValidity(); return;}
            year.setCustomValidity('');
            target.value = `${candidate.country} ${candidate.title} ${year.value.trim()}`;
            target.dispatchEvent(new Event('input', {bubbles:true}));
            target.dispatchEvent(new Event('change', {bubbles:true}));
            target.focus();
            note.textContent = 'Coin entered. Review the text, then run your search or auction analysis.';
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
