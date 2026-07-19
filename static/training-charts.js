// Shared SVG chart-rendering helpers for the Training Journal dashboard
// (templates/training_dashboard.html) and single-metric chart page
// (templates/training_chart.html) -- kept in one file so the two pages can't
// drift into rendering the same data two different ways.
//
// Categorical palette: the dataviz skill's validated dark-mode categorical set
// (references/palette.md), not eyeballed. This app is dark-only (no light-mode
// variant anywhere in this codebase), so only the dark column is used,
// validated against this app's --surface (#1a1a1a). Callers must define the
// --chart-1..--chart-8 custom properties on :root (see the <style> block in
// each page that includes this file) before TrainingCharts.colors() is called.

const TrainingCharts = (() => {
  const tooltip = document.getElementById('chart-tooltip');

  function showTooltip(evt, text) {
    if (!tooltip) return;
    tooltip.textContent = text;
    tooltip.style.display = 'block';
    tooltip.style.left = (evt.clientX + 12) + 'px';
    tooltip.style.top = (evt.clientY + 12) + 'px';
  }
  function hideTooltip() { if (tooltip) tooltip.style.display = 'none'; }

  const NS = 'http://www.w3.org/2000/svg';
  function svgEl(tag, attrs) {
    const el = document.createElementNS(NS, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  function colors() {
    return ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5', '--chart-6', '--chart-7', '--chart-8']
      .map(v => getComputedStyle(document.documentElement).getPropertyValue(v).trim());
  }

  function appendTableToggle(container, rows, cols) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'table-toggle';
    btn.textContent = 'View as table';
    const table = document.createElement('table');
    table.className = 'chart-table';
    table.hidden = true;
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    cols.forEach(c => { const th = document.createElement('th'); th.textContent = c; headRow.appendChild(th); });
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    rows.forEach(r => {
      const tr = document.createElement('tr');
      cols.forEach(c => { const td = document.createElement('td'); td.textContent = r[c]; tr.appendChild(td); });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    btn.addEventListener('click', () => {
      table.hidden = !table.hidden;
      btn.textContent = table.hidden ? 'View as table' : 'Hide table';
    });
    container.appendChild(btn);
    container.appendChild(table);
  }

  // Single-series line chart with hover points, 2px line, ≥8px hit targets.
  function renderLineChart(containerId, points, xKey, yKey, color, yLabel, tableCols) {
    const container = document.getElementById(containerId);
    if (!points.length) {
      container.innerHTML = '<div class="chart-empty">No data yet.</div>';
      return;
    }
    const W = 640, H = 220, PAD = 32;
    const ys = points.map(p => p[yKey]);
    const yMin = Math.min(...ys), yMax = Math.max(...ys);
    const yRange = (yMax - yMin) || 1;
    const xStep = points.length > 1 ? (W - PAD * 2) / (points.length - 1) : 0;

    const svg = svgEl('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}` });
    for (let i = 0; i <= 2; i++) {
      const y = PAD + (H - PAD * 2) * (i / 2);
      svg.appendChild(svgEl('line', { class: 'gridline', x1: PAD, x2: W - PAD, y1: y, y2: y }));
    }

    const coords = points.map((p, i) => ({
      x: PAD + i * xStep,
      y: PAD + (H - PAD * 2) * (1 - (p[yKey] - yMin) / yRange),
      p,
    }));

    const pathD = coords.map((c, i) => (i === 0 ? 'M' : 'L') + c.x + ',' + c.y).join(' ');
    svg.appendChild(svgEl('path', { d: pathD, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linecap': 'round' }));

    coords.forEach(c => {
      const dot = svgEl('circle', { class: 'mark-point', cx: c.x, cy: c.y, r: 4, fill: color });
      dot.addEventListener('mouseenter', e => showTooltip(e, `${c.p[xKey]}: ${c.p[yKey]} ${yLabel}`));
      dot.addEventListener('mousemove', e => showTooltip(e, `${c.p[xKey]}: ${c.p[yKey]} ${yLabel}`));
      dot.addEventListener('mouseleave', hideTooltip);
      svg.appendChild(dot);
    });

    [0, Math.floor(coords.length / 2), coords.length - 1].forEach(i => {
      if (i < 0 || i >= coords.length) return;
      svg.appendChild(svgEl('text', { x: coords[i].x, y: H - 6, 'text-anchor': 'middle' })).textContent = coords[i].p[xKey];
    });

    container.innerHTML = '';
    container.appendChild(svg);
    appendTableToggle(container, points, tableCols);
  }

  function renderBarChart(containerId, points, xKey, yKey, color, yLabel, tableCols) {
    const container = document.getElementById(containerId);
    if (!points.length) {
      container.innerHTML = '<div class="chart-empty">No data yet.</div>';
      return;
    }
    const W = 640, H = 220, PAD = 32;
    const yMax = Math.max(...points.map(p => p[yKey])) || 1;
    const slot = (W - PAD * 2) / points.length;
    const barW = Math.max(6, slot * 0.6);

    const svg = svgEl('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}` });
    for (let i = 0; i <= 2; i++) {
      const y = PAD + (H - PAD * 2) * (i / 2);
      svg.appendChild(svgEl('line', { class: 'gridline', x1: PAD, x2: W - PAD, y1: y, y2: y }));
    }

    points.forEach((p, i) => {
      const barH = (H - PAD * 2) * (p[yKey] / yMax);
      const x = PAD + i * slot + (slot - barW) / 2;
      const y = H - PAD - barH;
      const rect = svgEl('rect', { class: 'mark-point', x, y, width: barW, height: barH, rx: 3, fill: color });
      rect.addEventListener('mouseenter', e => showTooltip(e, `${p[xKey]}: ${p[yKey]} ${yLabel}`));
      rect.addEventListener('mousemove', e => showTooltip(e, `${p[xKey]}: ${p[yKey]} ${yLabel}`));
      rect.addEventListener('mouseleave', hideTooltip);
      svg.appendChild(rect);
    });

    [0, Math.floor(points.length / 2), points.length - 1].forEach(i => {
      if (i < 0 || i >= points.length) return;
      const x = PAD + i * slot + slot / 2;
      svg.appendChild(svgEl('text', { x, y: H - 6, 'text-anchor': 'middle' })).textContent = points[i][xKey];
    });

    container.innerHTML = '';
    container.appendChild(svg);
    appendTableToggle(container, points, tableCols);
  }

  // Multi-series line chart — one line per exercise, fixed categorical color
  // order (never cycled/reassigned by filtering), legend always present.
  function renderMultiLineChart(containerId, seriesByName) {
    const container = document.getElementById(containerId);
    const names = Object.keys(seriesByName);
    if (!names.length) {
      container.innerHTML = '<div class="chart-empty">No lifts logged yet.</div>';
      return;
    }
    const CHART_COLORS = colors();
    const W = 640, H = 240, PAD = 36;
    const allVals = names.flatMap(n => seriesByName[n].map(p => p.one_rm));
    const yMin = Math.min(...allVals), yMax = Math.max(...allVals);
    const yRange = (yMax - yMin) || 1;
    const allDates = [...new Set(names.flatMap(n => seriesByName[n].map(p => p.date)))].sort();
    const xStep = allDates.length > 1 ? (W - PAD * 2) / (allDates.length - 1) : 0;
    const xIndex = Object.fromEntries(allDates.map((d, i) => [d, i]));

    const svg = svgEl('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}` });
    for (let i = 0; i <= 2; i++) {
      const y = PAD + (H - PAD * 2) * (i / 2);
      svg.appendChild(svgEl('line', { class: 'gridline', x1: PAD, x2: W - PAD, y1: y, y2: y }));
    }

    names.forEach((name, si) => {
      const color = CHART_COLORS[si % CHART_COLORS.length];
      const pts = seriesByName[name];
      const coords = pts.map(p => ({
        x: PAD + xIndex[p.date] * xStep,
        y: PAD + (H - PAD * 2) * (1 - (p.one_rm - yMin) / yRange),
        p,
      }));
      const pathD = coords.map((c, i) => (i === 0 ? 'M' : 'L') + c.x + ',' + c.y).join(' ');
      svg.appendChild(svgEl('path', { d: pathD, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linecap': 'round' }));
      coords.forEach(c => {
        const dot = svgEl('circle', { class: 'mark-point', cx: c.x, cy: c.y, r: 4, fill: color });
        dot.addEventListener('mouseenter', e => showTooltip(e, `${name} — ${c.p.date}: ${c.p.one_rm} lbs`));
        dot.addEventListener('mousemove', e => showTooltip(e, `${name} — ${c.p.date}: ${c.p.one_rm} lbs`));
        dot.addEventListener('mouseleave', hideTooltip);
        svg.appendChild(dot);
      });
    });

    container.innerHTML = '';
    container.appendChild(svg);

    const legend = document.createElement('div');
    legend.className = 'chart-legend';
    names.forEach((name, si) => {
      const item = document.createElement('span');
      item.className = 'item';
      const sw = document.createElement('span');
      sw.className = 'swatch';
      sw.style.background = CHART_COLORS[si % CHART_COLORS.length];
      item.appendChild(sw);
      item.appendChild(document.createTextNode(name));
      legend.appendChild(item);
    });
    container.insertBefore(legend, container.firstChild);

    const flatRows = names.flatMap(name => seriesByName[name].map(p => ({ exercise: name, date: p.date, one_rm: p.one_rm })));
    appendTableToggle(container, flatRows, ['exercise', 'date', 'one_rm']);
  }

  return { colors, renderLineChart, renderBarChart, renderMultiLineChart };
})();
