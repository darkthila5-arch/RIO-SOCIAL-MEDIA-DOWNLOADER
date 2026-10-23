(() => {
  'use strict';
  const root = document.documentElement;
  root.classList.add('js-ready'); // scroll-reveal only hides sections once this script is running

  const $ = (id) => document.getElementById(id);
  const form = $('form'), input = $('url'), go = $('go'), goLabel = $('goLabel');
  const statusEl = $('status'), result = $('result'), qlist = $('qlist'), noteEl = $('note');
  const dl = $('dl'), dlLabel = $('dlLabel'), dlBar = $('dlBar'), dlStatus = $('dlStatus');
  const tabs = $('tabs'), pasteBtn = $('paste'), wmBlock = $('wmBlock'), wmBox = $('wm'), posRow = $('posRow');
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
  const whole = new Intl.NumberFormat('en');
  const KINDS = ['video', 'audio', 'document'];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const DOC_LABEL = { file: 'Download original file', cover: 'Download cover image', txt: 'Download details (TXT)' };
  const NOTES = {
    video: 'MP4 video for your gallery.',
    audio: 'Converted to a real MP3 with the title and channel in its tags.',
    document: {
      file: 'The best quality, without watermark, saved as a plain file instead of into the gallery.',
      cover: 'The video cover as an image.',
      txt: 'A text file with the title, channel, tags, stats and link.'
    }
  };

  const SITES = {
    youtube: { name: 'YouTube', re: /(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$/i },
    instagram: { name: 'Instagram', re: /(^|\.)(instagram\.com|instagr\.am)$/i },
    facebook: { name: 'Facebook', re: /(^|\.)(facebook\.com|fb\.watch|fb\.com|fb\.me)$/i },
    tiktok: { name: 'TikTok', re: /(^|\.)tiktok\.com$/i },
    pinterest: { name: 'Pinterest', re: /(^|\.)(pin\.it|pinterest\.(com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2}))$/i }
  };
  const platEls = document.querySelectorAll('#plats .plat');
  const LINK_RE = /https?:\/\/[^\s<>"'`]+/i;
  const BARE_RE = /(?:^|[\s(])((?:[a-z0-9-]+\.)+[a-z]{2,}(?:\/[^\s<>"'`]*)?)/gi;
  function hostSite(host) { return Object.keys(SITES).find((k) => SITES[k].re.test(host)) || ''; }
  // Pull the link out of whatever was pasted. A link without https:// (fb.watch/abc, www.facebook.com/...) works too.
  function findLink(text) {
    const m = LINK_RE.exec(text || '');
    if (m) return m[0].replace(/[.,;:!?)\]}>*_]+$/, '');
    BARE_RE.lastIndex = 0;
    let b;
    while ((b = BARE_RE.exec(text || ''))) {
      const candidate = 'https://' + b[1].replace(/[.,;:!?)\]}>*_]+$/, '');
      try { if (hostSite(new URL(candidate).hostname)) return candidate; } catch { /* next */ }
    }
    return '';
  }
  function detectSite(text) {
    const link = findLink(text);
    if (!link) return '';
    try { return hostSite(new URL(link).hostname); } catch { return ''; }
  }
  // black and white until a link is recognised; then the whole page (see :root[data-site] in style.css) and the
  // browser bar take that platform's colours, and the matching pill lights up
  const THEME_COLOR = { '': '#050505', youtube: '#0c0404', instagram: '#0b0409', facebook: '#040811', tiktok: '#03090a', pinterest: '#0c0405' };
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  function markSite(site) {
    platEls.forEach((el) => el.classList.toggle('on', el.dataset.p === site));
    if (site) root.dataset.site = site; else delete root.dataset.site;
    if (themeMeta) themeMeta.content = THEME_COLOR[site] || THEME_COLOR[''];
  }

  let current = null;
  let kind = 'video';
  let lastUrl = '';
  let dlBusy = false;

  /* ---------- storage (may be blocked in private mode) ---------- */
  const store = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ } }
  };

  $('year').textContent = new Date().getFullYear();

  /* ---------- helpers ---------- */
  function fmtDuration(s) {
    if (!s) return '';
    return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
  }
  function fmtSize(bytes) {
    if (!bytes) return '';
    return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
  }
  function setStatus(el, text, isError = false) {
    el.textContent = text;
    el.className = 'status' + (isError ? ' error' : '');
  }
  let toastTimer;
  function toast(text) {
    const t = $('toast');
    t.textContent = text;
    t.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('on'), 2800);
  }
  function setBusy(busy) {
    document.body.classList.toggle('loading', busy);
    go.disabled = busy;
    goLabel.textContent = busy ? 'Getting video...' : 'Get video';
  }

  /* Talk to the server. Always ends in either parsed JSON or an Error with a readable message. */
  function friendly(status) {
    if (status === 429) return 'Too many requests. Wait a minute and try again.';
    if (status === 502 || status === 503 || status === 504) return 'The server is waking up. Wait a few seconds and try again.';
    if (status === 404) return 'This link has expired. Start again.';
    return 'Something went wrong. Try again.';
  }
  async function api(path, body, timeout = 90000) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeout);
    let res;
    try {
      res = await fetch(path, {
        method: body ? 'POST' : 'GET',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        cache: 'no-store',
        signal: ctl.signal
      });
    } catch (e) {
      throw new Error(e && e.name === 'AbortError'
        ? 'The server is taking too long. Try again.'
        : 'No connection. Check your internet and try again.');
    } finally {
      clearTimeout(timer);
    }
    let data = null;
    try { data = await res.json(); } catch { /* not JSON (proxy error page) */ }
    if (!res.ok) {
      const msg = data && data.error ? data.error + (data.detail ? ` (${data.detail})` : '') : friendly(res.status);
      throw new Error(msg);
    }
    if (!data) throw new Error('Unexpected reply from the server. Try again.');
    return data;
  }

  async function copyText(text, done) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;opacity:0;top:0;left:0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch { /* ignore */ }
      ta.remove();
    }
    toast(done);
  }

  /* ---------- live download counter ---------- */
  // Three places show the number: the header pill, the pill in the hero (phones) and the big counter band.
  // The pills count up whenever the number changes; the band counts up from 0 when it scrolls into view.
  const pills = [...document.querySelectorAll('.live')];
  const pillNums = [...document.querySelectorAll('.live .js-count')];
  const bandNum = document.querySelector('.js-count-band');
  const labels = document.querySelectorAll('.js-count-label');
  let shown = 0, target = 0, tween = 0, haveCount = false;
  let bandShown = 0, bandTween = 0, bandSeen = false;

  function tweenNumber(els, from, to, apply) {
    const start = performance.now(), dur = Math.min(1600, 450 + Math.abs(to - from) * 8);
    let raf = 0;
    const step = (now) => {
      const p = Math.min(1, (now - start) / dur);
      const v = Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3)));
      apply(v);
      if (p < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }
  let stopPills = () => {}, stopBand = () => {};
  function paintPills(v) { shown = v; pillNums.forEach((el) => { el.textContent = whole.format(v); }); }
  function paintBand(v) { bandShown = v; if (bandNum) bandNum.textContent = whole.format(v); }

  function showCount(n) {
    const grew = haveCount && n > target;
    target = n;
    haveCount = true;
    labels.forEach((el) => { el.textContent = n === 1 ? 'download' : 'downloads'; });
    pills.forEach((el) => { el.hidden = false; });
    stopPills();
    if (reduceMotion || shown === target) paintPills(target); else stopPills = tweenNumber(pillNums, shown, target, paintPills);
    if (grew && !reduceMotion) {
      pills.forEach((el) => { el.classList.remove('bump'); void el.offsetWidth; el.classList.add('bump'); });
    }
    // the band only animates once the visitor can see it; afterwards it follows the live number
    if (bandSeen) {
      stopBand();
      if (reduceMotion || bandShown === target) paintBand(target); else stopBand = tweenNumber([bandNum], bandShown, target, paintBand);
    }
  }
  const bandEl = $('band');
  function onBandSeen() {
    bandSeen = true;
    if (!haveCount) return;
    stopBand();
    if (reduceMotion) paintBand(target); else stopBand = tweenNumber([bandNum], 0, target, paintBand);
  }
  if (bandEl && 'IntersectionObserver' in window) {
    const bio = new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) { bio.disconnect(); onBandSeen(); }
    }, { threshold: 0.35 });
    bio.observe(bandEl);
  } else {
    bandSeen = true;
  }

  async function refreshCount() {
    try {
      const res = await fetch('/api/stats', { cache: 'no-store' });
      if (res.ok) {
        const n = Number((await res.json()).downloads) || 0;
        const first = !haveCount;
        showCount(n);
        if (first && bandSeen && bandNum && bandNum.textContent === '—') onBandSeen();
      }
    } catch { /* keep the old number */ }
  }
  refreshCount();
  setInterval(() => { if (!document.hidden) refreshCount(); }, 8000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshCount(); });

  /* small burst of dots from an element (after a download starts) */
  function burst(el) {
    if (reduceMotion || !el) return;
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const frag = document.createDocumentFragment(), made = [];
    for (let i = 0; i < 18; i++) {
      const d = document.createElement('i');
      d.className = 'burst-p';
      const a = (Math.PI * 2 * i) / 18 + Math.random() * 0.4, dist = 50 + Math.random() * 70;
      d.style.cssText = `left:${cx}px;top:${cy}px;--dx:${Math.cos(a) * dist}px;--dy:${Math.sin(a) * dist - 20}px`;
      frag.appendChild(d); made.push(d);
    }
    document.body.appendChild(frag);
    setTimeout(() => made.forEach((d) => d.remove()), 1000);
  }

  /* ---------- recent links ---------- */
  const historyEl = $('history'), historyList = $('historyList');
  function renderHistory() {
    const items = store.get('rio_history', []);
    historyEl.hidden = items.length === 0;
    historyList.replaceChildren(...items.map((it) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'hchip';
      b.title = it.url;
      b.textContent = it.title || it.url;
      b.addEventListener('click', () => {
        if (dlBusy) return;
        input.value = it.url;
        syncPasteLabel();
        form.requestSubmit();
      });
      return b;
    }));
  }
  function remember(url, data) {
    const title = (data.title || '').replace(/\s+/g, ' ').slice(0, 48);
    const items = store.get('rio_history', []).filter((it) => it.url !== url);
    items.unshift({ url, title });
    store.set('rio_history', items.slice(0, 6));
    renderHistory();
  }
  $('historyClear').addEventListener('click', () => { store.set('rio_history', []); renderHistory(); });
  renderHistory();

  /* ---------- paste / clear ---------- */
  function syncPasteLabel() { pasteBtn.textContent = input.value ? 'Clear' : 'Paste'; markSite(detectSite(input.value)); }
  input.addEventListener('input', syncPasteLabel);
  input.addEventListener('change', syncPasteLabel);
  input.addEventListener('paste', () => setTimeout(syncPasteLabel, 0)); // some keyboards/browsers fire no 'input' for a paste
  pasteBtn.addEventListener('click', async () => {
    if (input.value) { input.value = ''; syncPasteLabel(); input.focus(); return; }
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (!text) { setStatus(statusEl, 'Your clipboard is empty. Copy a video link first.', true); return; }
      input.value = text;
      syncPasteLabel();
      if (detectSite(text)) form.requestSubmit();
    } catch {
      input.focus();
      setStatus(statusEl, 'Long-press the box and choose Paste.');
    }
  });

  /* ---------- result rendering ---------- */
  function formatsOf(k) { return current.formats.filter((f) => f.kind === k); }
  function pickedId() { const p = qlist.querySelector('input:checked'); return p ? p.value : ''; }
  function pickedFormat() { const id = pickedId(); return current.formats.find((f) => f.id === id); }

  function updateDownload() {
    const id = pickedId();
    if (!id) return;
    dlLabel.textContent = kind === 'video' ? 'Download MP4' : kind === 'audio' ? 'Download MP3' : (id === 'cover' && current && current.platform === 'pinterest' ? 'Download image' : (DOC_LABEL[id] || 'Download'));
    let note = kind === 'document' ? NOTES.document[id] : NOTES[kind];
    if (kind === 'document' && id === 'cover' && current && current.platform === 'pinterest') note = 'The picture in its original size.';
    if (kind === 'video' && current && current.platform === 'youtube') note += ' It is prepared on the server first, so it takes a little while.';
    noteEl.textContent = note || '';
  }

  function renderList() {
    qlist.replaceChildren(...formatsOf(kind).map((f, i) => {
      const label = document.createElement('label');
      label.className = 'q';
      const radio = document.createElement('input');
      Object.assign(radio, { type: 'radio', name: 'quality', value: f.id, checked: i === 0 });
      radio.addEventListener('change', updateDownload);
      const box = document.createElement('span');
      box.className = 'qbox';
      const name = document.createElement('span');
      name.textContent = f.label;
      const size = document.createElement('span');
      size.className = 'qsize';
      size.textContent = f.size ? '~' + fmtSize(f.size) : (f.merge ? 'takes longer' : '');
      box.append(name, size);
      label.append(radio, box);
      return label;
    }));
    qlist.style.animation = 'none';
    void qlist.offsetWidth;
    qlist.style.animation = '';
    updateDownload();
  }

  function syncWatermarkUi() {
    wmBlock.hidden = !(kind === 'video' && current && current.watermark);
    posRow.hidden = !wmBox.checked;
    $('wmWarn').hidden = !wmBox.checked;
  }

  function setKind(k) {
    if (dlBusy) return;
    kind = k;
    tabs.style.setProperty('--i', KINDS.indexOf(k));
    tabs.querySelectorAll('.tab').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.kind === k)));
    setStatus(dlStatus, '');
    syncWatermarkUi();
    renderList();
  }
  tabs.addEventListener('click', (e) => {
    const b = e.target.closest('.tab');
    if (b && !b.disabled) setKind(b.dataset.kind);
  });
  tabs.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const enabled = [...tabs.querySelectorAll('.tab')].filter((b) => !b.disabled);
    const at = enabled.findIndex((b) => b.dataset.kind === kind);
    const next = enabled[(at + (e.key === 'ArrowRight' ? 1 : enabled.length - 1)) % enabled.length];
    if (next) { setKind(next.dataset.kind); next.focus(); }
  });

  wmBox.checked = store.get('rio_wm', false); // off unless the visitor turned it on before
  wmBox.addEventListener('change', () => { store.set('rio_wm', wmBox.checked); syncWatermarkUi(); });
  posRow.addEventListener('change', (e) => { if (e.target.name === 'pos') store.set('rio_pos', e.target.value); });
  function selectedPosition() { const r = posRow.querySelector('input:checked'); return r ? r.value : 'br'; }

  function render(data) {
    current = data;
    if (data.platform) markSite(data.platform); // keep the platform colours while its result is shown
    const thumb = $('thumb');
    thumb.classList.remove('loaded');
    thumb.hidden = !data.thumbnail;
    thumb.onload = () => thumb.classList.add('loaded');
    thumb.onerror = () => { thumb.hidden = true; };
    if (data.thumbnail) thumb.src = data.thumbnail; else thumb.removeAttribute('src');

    const badge = $('badge');
    badge.hidden = !data.platform_name;
    badge.textContent = data.platform_name || '';
    badge.dataset.p = data.platform || '';
    $('title').textContent = data.title;
    $('author').textContent = [data.author, data.handle ? '@' + data.handle : ''].filter(Boolean).join('  ');

    const stats = [
      fmtDuration(data.duration),
      data.views != null ? compact.format(data.views) + ' views' : '',
      data.likes != null ? compact.format(data.likes) + ' likes' : '',
      data.comments != null ? compact.format(data.comments) + ' comments' : '',
      data.shares != null ? compact.format(data.shares) + ' shares' : '',
      data.created ? 'Posted ' + data.created : ''
    ].filter(Boolean);
    $('stats').replaceChildren(...stats.map((t, i) => {
      const li = document.createElement('li');
      li.textContent = t;
      li.style.setProperty('--i', i);
      return li;
    }));

    // remembered watermark corner, if the server still offers it
    const wanted = store.get('rio_pos', data.position || 'br');
    const radio = posRow.querySelector(`input[value="${(data.positions || []).includes(wanted) ? wanted : data.position || 'br'}"]`);
    if (radio) radio.checked = true;

    tabs.querySelectorAll('.tab').forEach((b) => { b.disabled = formatsOf(b.dataset.kind).length === 0; });
    kind = '';
    setKind(formatsOf('video').length ? 'video' : (KINDS.find((k) => formatsOf(k).length) || 'document'));

    result.hidden = false;
    result.classList.remove('show');
    void result.offsetWidth;
    result.classList.add('show');
    setTimeout(() => result.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'nearest' }), 120);
  }

  $('copyCaption').addEventListener('click', () => current && copyText(current.title, 'Title copied'));
  $('copyLink').addEventListener('click', () => lastUrl && copyText(lastUrl, 'Link copied'));

  /* ---------- look up a video ---------- */
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const typed = input.value.trim();
    if (!typed) return;
    const url = findLink(typed) || typed;
    if (dlBusy) { setStatus(statusEl, 'Finish the current download first.'); return; }
    if (!detectSite(url)) {
      let host = '';
      try { host = new URL(findLink(typed) || (/^https?:/i.test(typed) ? typed : 'https://' + typed)).hostname; } catch { /* not a link at all */ }
      setStatus(statusEl, "That doesn't look like a YouTube, Instagram, Facebook, TikTok or Pinterest link." + (/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(host) ? ` (${host} is not supported)` : ''), true);
      return;
    }

    setBusy(true);
    result.hidden = true;
    setStatus(statusEl, 'Looking up your video...');

    try {
      const data = await api('/api/info', { url });
      lastUrl = url;
      setStatus(statusEl, '');
      render(data);
      remember(url, data);
    } catch (err) {
      setStatus(statusEl, err.message, true);
    } finally {
      setBusy(false);
    }
  });

  /* ---------- download ---------- */
  function setDlBusy(busy, label) {
    dlBusy = busy;
    dl.classList.toggle('busy', busy);
    dl.disabled = busy;
    dlBar.hidden = !busy;
    if (busy) dlLabel.textContent = label;
    else updateDownload();
  }
  function startFileDownload(href) {
    const a = document.createElement('a');
    a.href = href;
    a.download = '';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function describeStage(job) {
    const pct = job.percent > 0 ? ` ${job.percent}%` : '';
    switch (job.stage) {
      case 'queued': return ['Waiting in line...', false];
      case 'downloading': return [job.percent > 0 ? `Downloading...${pct}` : 'Fetching the video...', job.percent > 0];
      case 'merging': return ['Joining picture and sound...', false];
      case 'converting': return [`Making your MP3...${pct}`, job.percent > 0];
      default: return [`Adding the watermark...${pct}`, job.percent > 0];
    }
  }

  async function download() {
    if (dlBusy || !current) return;
    const id = pickedId();
    if (!id) return;
    setStatus(dlStatus, '');
    setDlBusy(true, 'Preparing...');
    dlBar.className = 'dlbar wait';
    const bar = dlBar.querySelector('i');
    try {
      const wm = kind === 'video' && current.watermark && wmBox.checked;
      const first = await api('/api/prepare', { url: lastUrl, f: id, wm, pos: selectedPosition() });
      let file = first.file;

      if (!file) {
        const deadline = Date.now() + 10 * 60 * 1000;
        while (!file) {
          await sleep(900);
          if (Date.now() > deadline) throw new Error('This is taking too long. Try again' + (wm ? ', or switch the watermark off.' : '.'));
          const job = await api(`/api/job/${first.job}`);
          if (job.state === 'error') throw new Error(job.error || 'Something went wrong. Try again.');
          if (job.state === 'done') { file = job.file; break; }
          const [text, determinate] = describeStage(job);
          dlLabel.textContent = text;
          dlBar.className = determinate ? 'dlbar' : 'dlbar wait';
          if (determinate) bar.style.width = job.percent + '%';
        }
        dlBar.className = 'dlbar';
        bar.style.width = '100%';
        dlLabel.textContent = 'Starting download...';
        await sleep(250);
      }

      startFileDownload(file);
      toast('Your download is starting...');
      burst(dl);
      setTimeout(refreshCount, 1500);
      setTimeout(refreshCount, 6000);
    } catch (err) {
      setStatus(dlStatus, err.message || 'Something went wrong. Try again.', true);
    } finally {
      bar.style.width = '0';
      setDlBusy(false);
    }
  }
  dl.addEventListener('click', download);

  /* ---------- FAQ ---------- */
  document.querySelectorAll('.faq-q').forEach((btn) => {
    btn.addEventListener('click', () => {
      const open = btn.parentElement.classList.toggle('open');
      btn.setAttribute('aria-expanded', String(open));
    });
  });

  /* ---------- scroll reveal ---------- */
  const reveals = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); } });
    }, { threshold: 0.15 });
    reveals.forEach((el) => io.observe(el));
  } else {
    reveals.forEach((el) => el.classList.add('in'));
  }
  /* ---------- header shadow once the page scrolls ---------- */
  const topBar = $('top');
  const onScroll = () => topBar.classList.toggle('scrolled', window.scrollY > 8);
  addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  /* ---------- link shared from another app (Android share target) or ?url= ---------- */
  (function fromShare() {
    const q = new URLSearchParams(location.search);
    const text = [q.get('url'), q.get('text'), q.get('title')].filter(Boolean).join(' ');
    if (!text) return;
    const link = findLink(text);
    history.replaceState(null, '', location.pathname); // clean address bar
    if (!link) return;
    input.value = link;
    syncPasteLabel();
    if (detectSite(link)) setTimeout(() => form.requestSubmit(), 250);
  })();

  /* ---------- install as an app (PWA) ---------- */
  const installBtn = $('installBtn'), installTop = $('installTop'), installHelp = $('installHelp');
  const installSection = $('install');
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  let installEvent = null;

  function syncInstallUi() {
    const can = !!installEvent;
    installBtn.hidden = !can;
    installTop.hidden = !can;
    if (can) installHelp.textContent = '';
    else if (isIOS) installHelp.textContent = 'On iPhone or iPad: tap the Share button in Safari, then choose Add to Home Screen.';
    else installHelp.textContent = 'Open your browser menu and choose Install app or Add to Home screen.';
  }
  if (standalone) {
    installSection.hidden = true; // already running as the installed app
  } else {
    syncInstallUi();
  }
  addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); installEvent = e; syncInstallUi(); });
  addEventListener('appinstalled', () => { installEvent = null; installSection.hidden = true; installTop.hidden = true; toast('App installed. Find RIO on your home screen.'); });
  async function runInstall() {
    if (!installEvent) return;
    const ev = installEvent;
    installEvent = null;
    ev.prompt();
    try { await ev.userChoice; } catch { /* ignore */ }
    syncInstallUi();
  }
  installBtn.addEventListener('click', runInstall);
  installTop.addEventListener('click', runInstall);

  if ('serviceWorker' in navigator) {
    addEventListener('load', () => { navigator.serviceWorker.register('/sw.js').catch(() => {}); });
  }
  addEventListener('offline', () => toast('You are offline. Connect to the internet to download.'));
  addEventListener('online', () => { toast('Back online.'); refreshCount(); });
})();
