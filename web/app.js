/* ClearFrame — no network assets, build step, or automatic processing. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const paths = {
    plus: '<path d="M12 5v14M5 12h14"/>', minus: '<path d="M5 12h14"/>',
    hand: '<path d="M7 12V6a2 2 0 0 1 4 0v5-7a2 2 0 0 1 4 0v7-5a2 2 0 0 1 4 0v8c0 5-3 8-7 8-3 0-5-2-7-5l-2-3a2 2 0 0 1 3-2l1 1"/>',
    play: '<path d="m9 5 10 7-10 7Z"/>', image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="15.5" cy="8.5" r="2"/><path d="m3 17 6-7 5 6 3-3 4 4"/>',
    download: '<path d="M12 3v12m-4-4 4 4 4-4M4 16v4h16v-4"/>', compare: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 2v20M6 9l3 3-3 3m12-6-3 3 3 3"/>',
    folder: '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
    lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
    upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>', clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    layers: '<path d="m12 3 10 5-10 5L2 8l10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5"/>',
    sparkle: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Zm8-2v4m-2-2h4"/>',
    chip: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3"/>',
    shield: '<path d="m12 2 8 3v6c0 5-4 9-8 11-4-2-8-6-8-11V5l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    portrait: '<path d="M5 13c-2-9 3-11 7-11s9 2 7 11M5 12c0 5 3 8 7 8s7-3 7-8M6 10l4-5 3 5 3-3 2 3M3 22l4-4m10 0 4 4"/><path d="M9 12v1m6-1v1m-5 4h4"/>',
    landscape: '<path d="m2 20 7-12 5 8 3-4 5 8H2Z"/><circle cx="17" cy="5" r="3"/><path d="m6 13 3 1 2-2"/>',
    camera: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="m7 5 2-3h6l2 3"/><circle cx="12" cy="11" r="3"/><path d="M6 20c0-6 12-6 12 0"/>',
    mountain: '<rect x="2" y="3" width="20" height="18" rx="3"/><path d="m3 18 6-9 5 7 3-3 4 5"/><circle cx="17" cy="8" r="2"/>',
    check: '<path d="m5 12 4 4L19 6"/>', x: '<path d="m6 6 12 12M6 18 18 6"/>',
    loader: '<path d="M21 12a9 9 0 1 1-9-9"/>', power: '<path d="M12 2v10M7 5a9 9 0 1 0 10 0"/>',
  };
  const icon = (name, extra = '') => `<svg viewBox="0 0 24 24" aria-hidden="true" class="${extra}">${paths[name] || paths.image}</svg>`;
  function hydrateIcons(scope = document) { scope.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); }); }
  const state = {config: null, draft: null, items: [], selectedId: null, view: 'original', zoom: 'fit', split: 50, scale: 1, connected: false, stopped: false, closing: false, shutdownWait: null, pendingFiles: [], importing: false, polling: false, hardwarePolling: false, batchStarting: false, batchCancelled: false, hardware: null};
  const halted = () => state.stopped || state.closing;
  // Hold in-flight responses while shutdown is undecided. A failed shutdown can
  // resume the same requests; a successful shutdown discards their late results.
  async function mayUpdate() {
    if (state.closing) await state.shutdownWait;
    return !state.stopped;
  }
  const selected = () => state.items.find(item => item.id === state.selectedId) || null;
  const settings = () => selected()?.settings || state.draft;
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const active = (item) => ['submitting', 'queued', 'running'].includes(item.status);
  const signatures = (value) => JSON.stringify([value?.mode, value?.method, Number(value?.strength), value?.device, value?.method === 'custom' ? [...(value.stages || [])].sort() : []]);
  const stale = (item) => Boolean(item?.lastResult && signatures(item.settings) !== signatures(item.lastResult.settings));
  const statusNames = {ready:'待处理',submitting:'正在提交',queued:'排队中',running:'处理中',completed:'已完成',failed:'处理失败',cancelled:'已取消'};
  const niceTime = (seconds) => {
    if (!Number.isFinite(seconds)) return '—';
    if (seconds < 60) return `${Math.max(0, seconds).toFixed(1)} 秒`;
    return `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
  };
  const deviceName = (device) => device ? String(device).replace(/cuda(?::\d+)?/ig, 'NVIDIA GPU').replace(/cpu/ig, 'CPU').replace(/\+/g, ' + ') : '设备将在开始后显示';
  function toast(message, error = false) {
    const el = document.createElement('div'); el.className = `toast${error ? ' error' : ''}`; el.textContent = message; $('toastStack').append(el);
    setTimeout(() => el.remove(), error ? 9000 : 4800);
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if ((options.method || 'GET') !== 'GET') headers.set('X-Token', window.CLEARFRAME_TOKEN || '');
    let response;
    try { response = await fetch(path, {...options, headers, signal: options.signal || AbortSignal.timeout(60000), cache: 'no-store'}); }
    catch (error) { throw new Error(error.name === 'TimeoutError' ? '本地程序响应超时，请稍后重试。' : '无法连接本地程序，请确认启动器仍在运行。'); }
    let value;
    try { value = await response.json(); } catch { throw new Error(`本地程序返回了无法读取的响应（${response.status}）。`); }
    if (!response.ok) throw new Error(typeof value.error === 'string' ? value.error : value.error?.message || value.detail || `请求失败（${response.status}）`);
    return value;
  }
  function setConnection(connected, message) {
    state.connected = connected;
    $('connection').className = `connection ${connected ? 'connected' : 'disconnected'}`;
    $('connection').innerHTML = `<span></span>${escape(message || (connected ? '本地服务已连接' : '连接已断开'))}`;
    renderActions();
  }
  async function initialize() {
    $('retryConnection').disabled = true;
    try {
      const config = await api('/api/config');
      if (!(await mayUpdate())) return;
      state.config = config;
      state.draft ||= copy(state.config.defaults);
      setConnection(true); $('connectionError').hidden = true;
      $('outputDirectory').textContent = state.config.output_dir || '项目 results 文件夹';
      $('outputDirectory').title = state.config.output_dir || '';
      $('appVersion').textContent = `v${state.config.version || '1.0'}`;
      renderAll(); pollHardware(); pollJobs();
    } catch (error) {
      if (!(await mayUpdate())) return;
      setConnection(false); $('connectionError').hidden = false; $('connectionErrorDetail').textContent = error.message;
    } finally { $('retryConnection').disabled = false; }
  }
  function renderSettings() {
    if (!state.config || !settings()) return;
    const current = settings();
    const modeIcons = {anime_character:'portrait',anime_landscape:'landscape',photo_portrait:'camera',photo_landscape:'mountain'};
    $('scenarioGrid').innerHTML = state.config.modes.map(mode => `<button class="scenario-card${current.mode === mode.id ? ' selected' : ''}" data-mode="${escape(mode.id)}" role="radio" aria-checked="${current.mode === mode.id}" tabindex="${current.mode === mode.id ? 0 : -1}" title="${escape(mode.description)}">${icon(modeIcons[mode.id])}<span>${escape(mode.label)}</span></button>`).join('');
    const mode = state.config.modes.find(mode => mode.id === current.mode);
    $('modeNote').textContent = mode?.description || '';
    $('modeNote').title = [mode?.recommendation, mode?.risks].filter(Boolean).join('\n');
    $('methodGrid').innerHTML = state.config.methods.map(method => `<button class="method-option${current.method === method.id ? ' selected' : ''}" data-method="${escape(method.id)}" role="radio" aria-checked="${current.method === method.id}" tabindex="${current.method === method.id ? 0 : -1}">${escape(method.label)}</button>`).join('');
    const method = state.config.methods.find(method => method.id === current.method);
    $('methodExplanation').innerHTML = `<div class="explanation-row"><span>原理</span><p>${escape(method?.principle)}</p></div><div class="explanation-row"><span>效果</span><p>${escape(method?.effect)}</p></div><div class="explanation-row warning"><span>注意</span><p>${escape(method?.caution)}</p></div>`;
    $('customStages').hidden = current.method !== 'custom';
    const opened = [...$('stageList').querySelectorAll('details[open]')].map(el => el.dataset.stage);
    $('stageList').innerHTML = state.config.stages.map((stage, index) => `<div class="stage-item"><label><input type="checkbox" value="${escape(stage.id)}" ${(current.stages || []).includes(stage.id) ? 'checked' : ''}><span>${escape(stage.label)}</span><span class="stage-order">${String(index + 1).padStart(2, '0')}</span></label><details data-stage="${escape(stage.id)}" ${opened.includes(stage.id) ? 'open' : ''}><summary>原理与效果</summary><p>${escape(stage.principle)}</p><p>${escape(stage.effect)}</p><p class="stage-warning">${escape(stage.caution)}</p></details></div>`).join('');
    $('strength').value = current.strength;
    $('device').value = current.device || 'auto';
    $('strength').disabled = false; $('device').disabled = false;
    renderStrength();
  }
  function renderStrength() {
    if (!settings()) return;
    const amount = Number(settings().strength);
    $('strengthValue').innerHTML = `${amount} <span>/ 100</span>`;
    $('strength').style.setProperty('--fill', `${amount}%`);
    $('strengthHint').textContent = amount === 0 ? '像素保持不变' : amount <= 30 ? '轻度' : amount <= 65 ? '适中' : '较强';
    const photo = settings().mode.startsWith('photo');
    $('strengthCaution').textContent = amount === 0 ? '强度为 0 时，导出保留原始像素的 PNG，不执行处理步骤。' : photo ? '人像皮肤、发丝和风景纹理容易被抹平，建议从较低强度开始。' : '更高强度会减少细碎纹理，也可能损失淡阴影、细线与笔触。';
  }
  function changeSettings(change, full = true) {
    const current = settings(); if (!current) return;
    Object.assign(current, change);
    if (full) renderSettings(); else renderStrength();
    renderViewer(); renderActions(); renderQueue();
  }
  function renderDownload() {
    const item = selected(), available = Boolean(item?.lastResult) && state.connected && !halted();
    $('downloadButton').classList.toggle('disabled', !available);
    $('downloadButton').setAttribute('aria-disabled', String(!available));
    if (available) {
      $('downloadButton').href = item.lastResult.download_url || `${item.lastResult.result_url}?download=1`;
      $('downloadButton').download = item.name.replace(/\.[^.]+$/, '') + '_clearframe.png';
    } else $('downloadButton').removeAttribute('href');
  }
  function renderActions() {
    const item = selected(), unavailable = !state.config || !state.connected || halted();
    ['importButton', 'emptyImportButton', 'queueImportButton'].forEach(id => { $(id).disabled = unavailable; });
    $('startButton').disabled = unavailable || !item || active(item) || state.batchStarting;
    $('startButton').querySelector('span:last-child').textContent = state.stopped ? '服务已关闭' : item && active(item) ? (item.status === 'queued' ? '已加入处理队列' : '正在处理当前图片') : item?.lastResult ? '用当前参数重新处理' : '处理当前图片';
    const available = state.items.filter(item => !active(item)).length;
    $('batchButton').disabled = unavailable || !available || state.batchStarting;
    $('batchButton').textContent = state.batchStarting ? '正在加入队列…' : `处理全部图片${state.items.length ? `（${available}）` : ''}`;
    $('cancelAllButton').hidden = state.stopped || !state.items.some(active);
    $('cancelAllButton').disabled = unavailable;
    if ($('shutdownButton')) $('shutdownButton').disabled = !state.connected || halted();
    renderDownload();
  }
  let lastQueueRender = '';
  function renderQueue() {
    if (state.stopped) {
      // Keep existing thumbnails in place: rebuilding them would request images
      // from the service that has just stopped.
      $('queueSummary').textContent = '服务已关闭';
      $('imageQueue').querySelectorAll('[data-image-id]').forEach(element => {
        const item = state.items.find(item => item.id === element.dataset.imageId);
        element.setAttribute('aria-disabled', 'true'); element.tabIndex = -1;
        const remove = element.querySelector('[data-remove]'); if (remove) remove.disabled = true;
        if (item && active(item)) {
          const status = element.querySelector('.queue-status');
          status.className = 'queue-status cancelled'; status.innerHTML = `${icon('clock')}已停止`;
          element.setAttribute('aria-label', `${item.name}, 已停止`);
        }
      });
      return;
    }
    const signature = JSON.stringify([state.selectedId, state.batchStarting, state.stopped, state.items.map(item => [item.id, item.name, item.source_url, item.status, stale(item)])]);
    if (signature === lastQueueRender) return;
    lastQueueRender = signature;
    const scroll = $('imageQueue').scrollLeft;
    $('queueCount').textContent = state.items.length;
    const running = state.stopped ? 0 : state.items.filter(active).length, completed = state.items.filter(item => item.status === 'completed').length;
    $('queueSummary').textContent = !state.items.length ? '还没有图片' : running ? `${running} 张正在处理或排队` : `${completed} 张已完成`;
    if (!state.items.length) {
      $('imageQueue').innerHTML = `<div class="queue-empty">${icon('layers')}导入的图片会显示在这里，可逐张调整参数</div>`; return;
    }
    $('imageQueue').innerHTML = state.items.map(item => {
      const status = state.stopped && active(item) ? '已停止' : item.status === 'completed' && stale(item) ? '参数已修改' : statusNames[item.status] || item.status;
      const statusIcon = active(item) && !state.stopped ? icon('loader', 'spinner') : item.status === 'completed' ? icon('check') : icon('clock');
      return `<div class="queue-item${item.id === state.selectedId ? ' selected' : ''}" data-image-id="${escape(item.id)}" role="button" tabindex="0" aria-pressed="${item.id === state.selectedId}" aria-label="${escape(item.name)}, ${escape(status)}"><img class="queue-thumb" src="${escape(item.source_url)}" alt="" loading="lazy" draggable="false"><div class="queue-file"><strong title="${escape(item.name)}">${escape(item.name)}</strong><span class="queue-status ${escape(item.status)}">${statusIcon}${escape(status)}</span></div><button class="queue-remove" data-remove="${escape(item.id)}" ${active(item) || state.batchStarting ? 'disabled' : ''} title="从队列移除" aria-label="移除 ${escape(item.name)}">${icon('x')}</button></div>`;
    }).join('');
    $('imageQueue').scrollLeft = scroll;
  }
  function renderTask() {
    const item = selected(), job = item?.job, status = item?.status || 'ready';
    $('taskPill').textContent = state.stopped ? '已关闭' : statusNames[status] || '待开始';
    $('taskPill').className = `status-pill ${status === 'queued' || status === 'submitting' ? 'running' : status}`;
    $('taskStageIcon').classList.toggle('running', Boolean(item && active(item) && !state.stopped));
    $('taskStageIcon').innerHTML = icon(status === 'completed' ? 'check' : status === 'failed' ? 'x' : active(item || {}) ? 'layers' : 'sparkle');
    const titles = {ready: item ? '图片已就绪' : '准备好，再开始', submitting:'正在提交任务',queued:'等待处理',running:'正在处理',completed:'处理完成',failed:'处理未完成',cancelled:'任务已取消'};
    $('taskStage').textContent = state.stopped ? '本地服务已关闭' : job?.stage || titles[status];
    const defaults = {ready: item ? '选好处理方式与强度，然后手动开始。每张图片单独保存参数。' : '导入图片，选好处理方式后点击“处理当前图片”。',submitting:'正在把参数发送给本地处理程序。',queued:'当前只有一个处理任务运行，其余图片依次执行。',running:'正在执行本地处理步骤。',completed:'结果已保存。可切换预览模式，检查细节。',failed:'请查看下方原因，调整后可重新开始。',cancelled:'处理已停止，原图与上次完成的结果仍保留。'};
    $('taskDetail').textContent = state.stopped ? '已完成的结果保存在 results 文件夹，再次使用请重新运行启动器。' : status === 'cancelled' ? '任务已取消，保留原图与上次结果。' : item?.cancelling || item?.cancelWhenSubmitted ? '正在请求取消，当前步骤结束后停止…' : job?.detail || defaults[status];
    const progress = Number.isFinite(job?.progress) ? Math.min(100, Math.max(0, job.progress)) : 0;
    $('taskPercent').textContent = job ? `${Math.round(progress)}%` : '—';
    $('taskProgress').setAttribute('aria-valuenow', String(progress));
    $('taskProgressFill').style.width = `${progress}%`;
    $('taskElapsed').textContent = job ? `已用时 ${niceTime(job.elapsed_seconds)}` : '尚未运行';
    $('taskDevice').textContent = deviceName(job?.actual_device);
    $('cancelButton').hidden = state.stopped || !item || !['queued', 'running'].includes(status);
    $('cancelButton').disabled = halted() || !state.connected || Boolean(item?.cancelling);
    $('cancelButton').textContent = item?.cancelling ? '取消中' : '取消';
    const error = state.stopped ? null : item?.localError || job?.error || item?.pollError;
    $('taskError').hidden = !error;
    $('taskError').textContent = error || '';
  }
  function renderViewer() {
    const item = selected(), hasResult = Boolean(item?.lastResult);
    if (!hasResult && state.view !== 'original') state.view = 'original';
    $('emptyState').hidden = Boolean(item); $('imageContent').hidden = !item;
    $('viewport').classList.toggle('has-image', Boolean(item));
    $('currentName').textContent = item?.name || '图像预览';
    $('currentMeta').textContent = item ? `${item.width.toLocaleString()} × ${item.height.toLocaleString()} px${item.has_alpha ? ' · 含透明通道' : ''}` : '选择一张图片，开始整理画面';
    document.querySelectorAll('[data-view]').forEach(button => {
      button.disabled = !item || (button.dataset.view !== 'original' && !hasResult);
      button.classList.toggle('active', button.dataset.view === state.view);
      button.setAttribute('aria-pressed', String(button.dataset.view === state.view));
    });
    ['zoomOut', 'zoomIn', 'zoomSelect'].forEach(id => { $(id).disabled = !item; });
    renderDownload();
    $('staleStatus').hidden = !stale(item);
    $('canvasHelp').textContent = item ? '拖动平移 · F 适应窗口' : '支持拖入多张图片';
    $('viewStatus').innerHTML = `<span class="quiet-dot"></span>${escape(!item ? '等待导入' : state.view === 'original' ? '原始图片' : state.view === 'result' ? '处理结果' : '左侧原图 · 右侧结果')}`;
    $('compareControls').hidden = state.view !== 'compare' || !hasResult;
    if (!item) return;
    if ($('sourceImage').getAttribute('src') !== item.source_url) $('sourceImage').src = item.source_url;
    if (hasResult && $('resultImage').getAttribute('src') !== item.lastResult.result_url) $('resultImage').src = item.lastResult.result_url;
    $('sourceImage').hidden = state.view === 'result';
    $('resultImage').hidden = state.view === 'original';
    $('sourceLabel').hidden = state.view === 'result';
    $('resultLabel').hidden = state.view === 'original';
    $('splitHandle').hidden = state.view !== 'compare';
    $('sourceImage').style.clipPath = state.view === 'compare' ? `inset(0 ${100 - state.split}% 0 0)` : 'none';
    $('sourceLabel').hidden = state.view === 'result' || (state.view === 'compare' && state.split < 18);
    $('resultLabel').hidden = state.view === 'original' || (state.view === 'compare' && state.split > 82);
    updateSplit(); resizeViewer();
  }
  function updateSplit() {
    $('splitHandle').style.left = `${state.split}%`;
    $('splitHandle').setAttribute('aria-valuenow', String(state.split));
    $('compareRange').value = state.split;
    $('compareRange').style.setProperty('--fill', `${state.split}%`);
    if (state.view === 'compare') {
      $('sourceImage').style.clipPath = `inset(0 ${100 - state.split}% 0 0)`;
      $('sourceLabel').hidden = state.split < 18; $('resultLabel').hidden = state.split > 82;
    }
  }
  function resizeViewer() {
    const item = selected(); if (!item) return;
    const viewport = $('viewport'), width = viewport.clientWidth, height = viewport.clientHeight;
    const fit = Math.min((width - 48) / item.width, (height - 48) / item.height, 1);
    state.scale = state.zoom === 'fit' ? Math.max(.01, fit) : Number(state.zoom);
    $('imageFrame').style.width = `${Math.round(item.width * state.scale)}px`;
    $('imageFrame').style.height = `${Math.round(item.height * state.scale)}px`;
    $('imageContent').style.width = `${Math.max(width, item.width * state.scale + 48)}px`;
    $('imageContent').style.height = `${Math.max(height, item.height * state.scale + 48)}px`;
    $('imageFrame').classList.toggle('pixelated', state.scale >= 2);
    $('zoomSelect').querySelector('[data-custom]')?.remove();
    const zoomValue = String(state.zoom);
    if (![...$('zoomSelect').options].some(option => option.value === zoomValue)) {
      const option = new Option(`${Math.round(state.scale * 100)}%`, zoomValue); option.dataset.custom = 'true'; $('zoomSelect').add(option);
    }
    $('zoomSelect').value = zoomValue;
    $('zoomSelect').title = `当前显示 ${Math.round(state.scale * 100)}%`;
  }
  function setZoom(value) {
    const viewport = $('viewport'), oldScale = state.scale;
    const centerX = (viewport.scrollLeft + viewport.clientWidth / 2) / oldScale;
    const centerY = (viewport.scrollTop + viewport.clientHeight / 2) / oldScale;
    state.zoom = value; resizeViewer();
    if (value === 'fit') { viewport.scrollLeft = 0; viewport.scrollTop = 0; }
    else { viewport.scrollLeft = centerX * state.scale - viewport.clientWidth / 2; viewport.scrollTop = centerY * state.scale - viewport.clientHeight / 2; }
  }
  function renderAll() { renderSettings(); renderQueue(); renderTask(); renderViewer(); renderActions(); }
  function selectImage(id) {
    if (halted() || !state.items.some(item => item.id === id)) return;
    state.selectedId = id; state.zoom = 'fit';
    if (selected().lastResult) state.view = 'compare'; else state.view = 'original';
    $('viewport').scrollTop = 0; $('viewport').scrollLeft = 0; renderAll();
  }
  async function importFiles(files) {
    if (!state.connected || !state.config || halted()) return toast(state.stopped ? '本地服务已关闭，请重新运行启动器。' : '请先连接本地处理程序。', true);
    state.pendingFiles.push(...Array.from(files).map(file => ({file, settings: copy(settings())})));
    if (state.importing) return;
    state.importing = true; document.body.classList.add('loading-import');
    let count = 0;
    while (state.pendingFiles.length) {
      if (!(await mayUpdate())) break;
      const {file, settings: fileSettings} = state.pendingFiles.shift();
      try {
        if (file.size > 80 * 1024 * 1024) throw new Error('图片超过 80 MB，请先缩小文件。');
        const imported = await api(`/api/images?name=${encodeURIComponent(file.name || 'clipboard.png')}`, {method:'POST', headers:{'Content-Type':'application/octet-stream'}, body:file});
        if (!(await mayUpdate())) break;
        const item = {...imported,settings:fileSettings,status:'ready',job:null,lastResult:null,localError:null};
        state.items.push(item); count++;
        if (!state.selectedId) selectImage(item.id); else { renderQueue(); renderActions(); }
      } catch (error) { if (!(await mayUpdate())) break; toast(`${file.name || '图片'}：${error.message}`, true); }
    }
    state.importing = false; document.body.classList.remove('loading-import');
    if (state.stopped) return;
    if (count) toast(`已导入 ${count} 张图片。选择参数后，点击开始处理。`);
    renderActions();
  }
  async function startItem(item, snapshot = copy(item.settings)) {
    if (active(item) || halted()) return;
    item.status = 'submitting'; item.localError = null; item.pollError = null; item.job = null; item.cancelling = false; item.cancelWhenSubmitted = false;
    renderQueue(); renderTask(); renderActions();
    try {
      const job = await api('/api/jobs', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image_id:item.id,...snapshot})});
      if (!(await mayUpdate())) return;
      item.job = {...job, progress:0, elapsed_seconds:0, settings:snapshot}; item.status = job.status || 'queued';
      if (item.cancelWhenSubmitted) { item.cancelWhenSubmitted = false; await cancelItem(item); }
      pollJobs();
    } catch (error) { if (!(await mayUpdate())) return; item.status = 'failed'; item.localError = error.message; toast(`${item.name}：${error.message}`, true); }
    if (!(await mayUpdate())) return;
    renderQueue(); renderTask(); renderActions();
  }
  async function startAll() {
    if (state.batchStarting || halted()) return;
    const batch = state.items.filter(item => !active(item)).map(item => ({item, snapshot:copy(item.settings)}));
    state.batchStarting = true; state.batchCancelled = false; renderActions(); renderQueue();
    for (const {item, snapshot} of batch) { if (state.stopped || state.batchCancelled) break; if (state.items.includes(item)) await startItem(item, snapshot); }
    state.batchStarting = false;
    if (!(await mayUpdate())) return;
    renderActions(); renderQueue();
  }
  async function cancelItem(item) {
    if (halted()) return;
    if (item?.status === 'submitting') { item.cancelWhenSubmitted = true; renderTask(); return; }
    if (!item?.job || !active(item) || item.cancelling) return;
    item.cancelling = true; renderTask();
    try { await api(`/api/jobs/${encodeURIComponent(item.job.id)}/cancel`, {method:'POST'}); }
    catch (error) { if (!(await mayUpdate())) return; item.cancelling = false; item.localError = error.message; toast(error.message, true); }
    if (!(await mayUpdate())) return;
    renderTask(); pollJobs();
  }
  async function pollJobs() {
    if (state.polling || halted()) return;
    const waiting = state.items.filter(item => active(item) && item.job);
    if (!waiting.length) return;
    state.polling = true;
    await Promise.all(waiting.map(async item => {
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(item.job.id)}`);
        if (!(await mayUpdate())) return;
        if (item.job.id !== job.id) return;
        item.pollError = null; item.job = {...item.job,...job}; item.status = job.status;
        if (!active(item)) item.cancelling = false;
        if (job.status === 'completed' && job.result_url && item.lastResult?.id !== job.id) {
          item.lastResult = {id:job.id,result_url:job.result_url,download_url:job.download_url,settings:copy(job.settings || item.job.settings),elapsed_seconds:job.elapsed_seconds,actual_device:job.actual_device};
          if (item.id === state.selectedId) { state.view = 'compare'; renderViewer(); }
          toast(`${item.name} 处理完成，用时 ${niceTime(job.elapsed_seconds)}。`);
        }
      } catch (error) { if (!(await mayUpdate())) return; item.pollError = `任务状态暂时无法读取。${error.message} 恢复连接后将继续更新。`; }
    }));
    state.polling = false;
    if (!(await mayUpdate())) return;
    renderQueue(); renderTask(); renderActions();
  }
  const measured = value => typeof value === 'number' && Number.isFinite(value);
  function metric(id, value, suffix = '%', digits = 0) { $(id).textContent = measured(value) ? `${value.toFixed(digits)}${suffix}` : '—'; }
  function meter(id, percent) { $(id).style.width = measured(percent) ? `${Math.min(100, Math.max(0, percent))}%` : '0%'; $(id).parentElement.setAttribute('aria-label', measured(percent) ? `${percent.toFixed(1)}%` : '无可用读数'); }
  async function pollHardware() {
    if (state.hardwarePolling || halted() || !state.config) return;
    state.hardwarePolling = true;
    try {
      const data = await api('/api/hardware');
      if (!(await mayUpdate())) return;
      state.hardware = data;
      if (!state.connected) { setConnection(true); $('connectionError').hidden = true; }
      $('hardwareLive').innerHTML = '<i></i>实时更新'; $('hardwareLive').classList.add('online');
      metric('cpuUsage', data.cpu_percent); meter('cpuMeter', data.cpu_percent);
      metric('ramUsage', data.ram_used_gb, ' GB', 1);
      $('ramTotal').textContent = measured(data.ram_total_gb) ? `共 ${data.ram_total_gb.toFixed(1)} GB 系统内存` : '系统内存总量不可用';
      meter('ramMeter', measured(data.ram_used_gb) && measured(data.ram_total_gb) && data.ram_total_gb > 0 ? data.ram_used_gb / data.ram_total_gb * 100 : null);
      const gpu = data.gpu;
      $('gpuName').textContent = gpu?.name || (data.cuda_available === null ? '正在检测 NVIDIA GPU' : '未读取到 NVIDIA 显卡');
      metric('gpuUsage', gpu?.utilization); metric('gpuTemp', gpu?.temperature_c, '°C');
      metric('vramUsage', gpu?.memory_used_gb, ' GB', 1);
      $('vramTotal').textContent = measured(gpu?.memory_total_gb) ? `共 ${gpu.memory_total_gb.toFixed(1)} GB 显存` : '显存读数不可用';
      meter('vramMeter', measured(gpu?.memory_used_gb) && measured(gpu?.memory_total_gb) && gpu.memory_total_gb > 0 ? gpu.memory_used_gb / gpu.memory_total_gb * 100 : null);
      const gpuOption = $('device').querySelector('[value="cuda"]');
      gpuOption.textContent = data.cuda_available === null ? 'NVIDIA GPU（检测中）' : data.cuda_available ? 'NVIDIA GPU' : 'NVIDIA GPU（当前不可用）';
      $('hardwareNote').textContent = data.cuda_available === null ? '正在检测 GPU 计算环境；负载为整机读数。' : data.cuda_available ? '读取整机状态，包含其他程序占用。照片降噪步骤使用 CPU。' : 'GPU 计算当前不可用。自动模式将使用 CPU；“—”表示没有可用读数。';
    } catch (error) {
      if (!(await mayUpdate())) return;
      setConnection(false); $('hardwareLive').innerHTML = '<i></i>连接中断'; $('hardwareLive').classList.remove('online');
      ['cpuUsage','ramUsage','gpuUsage','gpuTemp','vramUsage'].forEach(id => { $(id).textContent = '—'; });
      ['cpuMeter','ramMeter','vramMeter'].forEach(id => meter(id, null));
      $('hardwareNote').textContent = '硬件读数已暂停。正在尝试重新连接本地服务。';
    } finally { state.hardwarePolling = false; }
  }
  function radioKeyboard(event) {
    if (!['ArrowRight','ArrowDown','ArrowLeft','ArrowUp','Home','End'].includes(event.key)) return;
    const group = event.currentTarget, buttons = [...group.querySelectorAll('[role="radio"]')];
    if (!buttons.length) return;
    event.preventDefault(); const index = buttons.indexOf(document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (['ArrowLeft','ArrowUp'].includes(event.key) ? -1 : 1) + buttons.length) % buttons.length;
    const attribute = group.id === 'scenarioGrid' ? 'mode' : 'method', value = buttons[next].dataset[attribute];
    buttons[next].click(); group.querySelector(`[data-${attribute}="${CSS.escape(value)}"]`)?.focus();
  }
  ['importButton','emptyImportButton','queueImportButton'].forEach(id => $(id).addEventListener('click', () => $('fileInput').click()));
  $('fileInput').addEventListener('change', event => { importFiles(event.target.files); event.target.value = ''; });
  $('scenarioGrid').addEventListener('click', event => {
    const button = event.target.closest('[data-mode]'); if (!button || button.dataset.mode === settings()?.mode) return;
    const mode = state.config.modes.find(mode => mode.id === button.dataset.mode);
    changeSettings({mode:mode.id,method:mode.default_method || 'default',strength:mode.default_strength,stages:mode.id.startsWith('photo') ? ['photo_denoise'] : ['anime_sr','l0']});
    $('scenarioGrid').querySelector(`[data-mode="${CSS.escape(mode.id)}"]`)?.focus();
  });
  $('methodGrid').addEventListener('click', event => {
    const button = event.target.closest('[data-method]'); if (!button) return;
    const value = button.dataset.method; changeSettings({method:value});
    $('methodGrid').querySelector(`[data-method="${CSS.escape(value)}"]`)?.focus();
  });
  ['scenarioGrid','methodGrid'].forEach(id => $(id).addEventListener('keydown', radioKeyboard));
  $('stageList').addEventListener('change', event => {
    if (!event.target.matches('input[type="checkbox"]')) return;
    const value = event.target.value;
    changeSettings({stages:[...$('stageList').querySelectorAll('input:checked')].map(input => input.value)});
    $('stageList').querySelector(`input[value="${CSS.escape(value)}"]`)?.focus();
  });
  $('strength').addEventListener('input', event => changeSettings({strength:Number(event.target.value)}, false));
  $('device').addEventListener('change', event => changeSettings({device:event.target.value}, false));
  $('startButton').addEventListener('click', () => { const item = selected(); if (item) startItem(item); });
  $('batchButton').addEventListener('click', startAll);
  $('cancelButton').addEventListener('click', () => cancelItem(selected()));
  $('cancelAllButton').addEventListener('click', async () => { state.batchCancelled = true; for (const item of state.items.filter(active)) await cancelItem(item); });
  $('imageQueue').addEventListener('click', event => {
    if (halted()) return;
    const remove = event.target.closest('[data-remove]');
    if (remove) {
      const item = state.items.find(item => item.id === remove.dataset.remove); if (!item || active(item) || state.batchStarting) return;
      const index = state.items.indexOf(item); state.items.splice(index, 1);
      if (state.selectedId === item.id) state.selectedId = state.items[Math.min(index, state.items.length - 1)]?.id || null;
      renderAll(); return;
    }
    const item = event.target.closest('[data-image-id]'); if (item) selectImage(item.dataset.imageId);
  });
  $('imageQueue').addEventListener('keydown', event => { if (event.target.matches('.queue-item') && ['Enter',' '].includes(event.key)) { event.preventDefault(); selectImage(event.target.dataset.imageId); } });
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => { state.view = button.dataset.view; renderViewer(); }));
  $('zoomSelect').addEventListener('change', event => setZoom(event.target.value === 'fit' ? 'fit' : Number(event.target.value)));
  $('zoomIn').addEventListener('click', () => setZoom(Math.min(4, Math.round(state.scale * 1.25 * 100) / 100)));
  $('zoomOut').addEventListener('click', () => setZoom(Math.max(.05, Math.round(state.scale / 1.25 * 100) / 100)));
  $('compareRange').addEventListener('input', event => { state.split = Number(event.target.value); updateSplit(); });
  $('splitHandle').addEventListener('keydown', event => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    state.split = event.key === 'Home' ? 0 : event.key === 'End' ? 100 : Math.max(0, Math.min(100, state.split + (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 10 : 1)));
    updateSplit();
  });
  let splitDrag = false;
  const moveSplit = event => { const rect = $('imageFrame').getBoundingClientRect(); state.split = Math.round(Math.min(100, Math.max(0, (event.clientX - rect.left) / rect.width * 100))); updateSplit(); };
  $('splitHandle').addEventListener('pointerdown', event => { event.preventDefault(); event.stopPropagation(); splitDrag = true; $('splitHandle').setPointerCapture(event.pointerId); moveSplit(event); });
  $('splitHandle').addEventListener('pointermove', event => { if (splitDrag) moveSplit(event); });
  ['pointerup','pointercancel','lostpointercapture'].forEach(name => $('splitHandle').addEventListener(name, () => { splitDrag = false; }));
  let pan = null;
  $('viewport').addEventListener('pointerdown', event => {
    if (!selected() || event.target.closest('.split-handle,button') || event.button !== 0) return;
    pan = {x:event.clientX,y:event.clientY,left:$('viewport').scrollLeft,top:$('viewport').scrollTop};
    $('viewport').setPointerCapture(event.pointerId); $('viewport').classList.add('panning');
  });
  $('viewport').addEventListener('pointermove', event => { if (pan) { $('viewport').scrollLeft = pan.left - event.clientX + pan.x; $('viewport').scrollTop = pan.top - event.clientY + pan.y; } });
  ['pointerup','pointercancel','lostpointercapture'].forEach(name => $('viewport').addEventListener(name, () => { pan = null; $('viewport').classList.remove('panning'); }));
  $('viewport').addEventListener('keydown', event => {
    if (!selected() || event.target !== $('viewport')) return;
    if (event.key.toLowerCase() === 'f') { event.preventDefault(); setZoom('fit'); }
    if (['+','='].includes(event.key)) { event.preventDefault(); $('zoomIn').click(); }
    if (event.key === '-') { event.preventDefault(); $('zoomOut').click(); }
  });
  let dragDepth = 0;
  const clearDrag = () => { dragDepth = 0; $('dragOverlay').hidden = true; };
  const hasDraggedFiles = event => Array.from(event.dataTransfer?.types || []).includes('Files');
  document.addEventListener('dragover', event => {
    event.preventDefault(); // Never let a file, image URI, or link navigate away.
    if (event.dataTransfer) event.dataTransfer.dropEffect = hasDraggedFiles(event) && !halted() ? 'copy' : 'none';
  });
  document.addEventListener('drop', event => {
    event.preventDefault(); clearDrag();
    if (halted()) return;
    const files = Array.from(event.dataTransfer?.files || []);
    if (files.length) importFiles(files);
    else if (event.dataTransfer?.types.length) toast('请从电脑拖入图片文件，暂不支持拖入网页链接或文件夹。');
  });
  document.addEventListener('dragenter', event => { if (hasDraggedFiles(event) && !halted()) { dragDepth++; $('dragOverlay').hidden = false; } });
  document.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('dragOverlay').hidden = true; });
  document.addEventListener('dragend', clearDrag);
  window.addEventListener('blur', clearDrag);
  document.addEventListener('paste', event => {
    if (event.target.matches('input,textarea,[contenteditable="true"]')) return;
    const files = [...(event.clipboardData?.items || [])].filter(item => item.kind === 'file' && item.type.startsWith('image/')).map(item => item.getAsFile()).filter(Boolean);
    if (files.length) { event.preventDefault(); importFiles(files); }
  });
  $('downloadButton').addEventListener('click', event => { if (halted() || !state.connected || $('downloadButton').getAttribute('aria-disabled') === 'true') event.preventDefault(); });
  $('retryConnection').addEventListener('click', initialize);
  let shutdownPending = false;
  $('shutdownButton')?.addEventListener('click', () => {
    const running = state.items.some(active);
    $('shutdownDetail').textContent = running ? '退出会取消正在处理与排队的任务。原图与已完成的结果会保留，再次使用时请重新运行启动器。' : '退出将关闭本地处理服务。已保存的结果会保留，再次使用时请重新运行启动器。';
    $('shutdownError').hidden = true;
    $('shutdownDialog').showModal();
  });
  $('cancelShutdown').addEventListener('click', () => $('shutdownDialog').close());
  $('shutdownDialog').addEventListener('cancel', event => { if (shutdownPending) event.preventDefault(); });
  $('confirmShutdown').addEventListener('click', async () => {
    if (shutdownPending) return;
    shutdownPending = true;
    let settleShutdown;
    state.shutdownWait = new Promise(resolve => { settleShutdown = resolve; });
    state.closing = true; clearDrag(); renderActions(); renderTask();
    $('confirmShutdown').disabled = true; $('cancelShutdown').disabled = true;
    $('confirmShutdown').textContent = '正在退出…'; $('shutdownError').hidden = true;
    try {
      await api('/api/shutdown', {method:'POST'});
      state.stopped = true; state.pendingFiles.length = 0; state.batchCancelled = true;
      state.importing = false; state.batchStarting = false; document.body.classList.remove('loading-import');
      clearInterval(jobTimer); clearInterval(hardwareTimer);
      setConnection(false, '服务已关闭'); renderTask(); renderQueue();
      $('shutdownDialog').close();
      $('connectionError').hidden = false; $('connectionError').querySelector('strong').textContent = '本地服务已关闭';
      $('connectionErrorDetail').textContent = '已完成的结果保存在 results 文件夹。再次使用请重新运行启动器并刷新页面。';
      $('retryConnection').hidden = true; $('hardwareLive').innerHTML = '<i></i>已停止'; $('hardwareLive').classList.remove('online');
      ['cpuUsage','ramUsage','gpuUsage','gpuTemp','vramUsage'].forEach(id => { $(id).textContent = '—'; });
      ['cpuMeter','ramMeter','vramMeter'].forEach(id => meter(id, null));
      $('hardwareNote').textContent = '本地服务已关闭，硬件状态不再更新。';
    } catch (error) { $('shutdownError').textContent = error.message; $('shutdownError').hidden = false; }
    finally {
      state.closing = false; settleShutdown();
      shutdownPending = false; $('confirmShutdown').disabled = false; $('cancelShutdown').disabled = false;
      $('confirmShutdown').textContent = '退出程序';
      if (!state.stopped) { renderActions(); renderTask(); pollJobs(); pollHardware(); }
    }
  });
  new ResizeObserver(() => resizeViewer()).observe($('viewport'));
  hydrateIcons(); initialize();
  const jobTimer = setInterval(pollJobs, 550), hardwareTimer = setInterval(pollHardware, 2200);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { pollJobs(); pollHardware(); } });
})();
