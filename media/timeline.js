(() => {
  'use strict';
  const vscode = acquireVsCodeApi();
  const app = document.getElementById('app');
  const notice = document.getElementById('notice');
  const saved = vscode.getState() || {};
  const expanded = new Set(Array.isArray(saved.expanded) ? saved.expanded.filter(id => typeof id === 'string') : []);
  const children = new Map();
  let repositories = [];
  let menu;
  let busy = false;
  let managementDialog;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function send(type, id, action) { vscode.postMessage({ type, id, action }); }
  function button(label, action, item, className = '', disabled = false) {
    const node = element('button', className, label);
    node.type = 'button'; node.disabled = disabled || busy;
    node.addEventListener('click', event => {
      event.stopPropagation(); closeMenu(); send('action', item.id, action);
      notice.textContent = '처리 중…'; busy = true;
      app.querySelectorAll('button').forEach(b => { b.disabled = true; });
    });
    return node;
  }
  function closeMenu() { if (menu) { menu.remove(); menu = undefined; } }
  function management(snapshot, repo) {
    return [button('이름 변경', 'renameSnapshot', snapshot, 'quiet', !repo.writable),
      button(snapshot.pinned ? '고정 해제' : '기록 고정', 'pinSnapshot', snapshot, 'quiet', !repo.writable),
      button('기록 삭제', 'deleteSnapshot', snapshot, 'danger', !repo.writable)];
  }
  function showMenu(event, snapshot, repo) {
    event.preventDefault(); closeMenu();
    menu = element('div', 'menu'); menu.setAttribute('role', 'menu');
    for (const item of management(snapshot, repo)) { item.setAttribute('role', 'menuitem'); menu.append(item); }
    document.body.append(menu);
    const bounds = event.currentTarget.getBoundingClientRect();
    menu.style.left = `${Math.max(4, Math.min(event.clientX || bounds.left, innerWidth - menu.offsetWidth - 4))}px`;
    menu.style.top = `${Math.max(4, Math.min(event.clientY || bounds.bottom, innerHeight - menu.offsetHeight - 4))}px`;
    menu.firstElementChild?.focus();
  }
  function dateLabel(value) {
    const date = new Date(value), now = new Date(), yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    if (date.toDateString() === now.toDateString()) return '오늘';
    if (date.toDateString() === yesterday.toDateString()) return '어제';
    return date.toLocaleDateString('ko-KR', { month: 'long', day: 'numeric', weekday: 'short', ...(date.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) });
  }
  function populateFiles(body, snapshot) {
    const files = body.querySelector('.files'); files.replaceChildren();
    const items = children.get(snapshot.id);
    if (!items) { files.append(element('p', 'hint', '변경 파일을 불러오는 중…')); return; }
    if (items.error) {
      files.append(element('p', 'error', items.error));
      const retry = element('button', 'secondary', '다시 불러오기'); retry.onclick = () => send('expand', snapshot.id); files.append(retry); return;
    }
    const changes = items.filter(item => item.kind === 'file');
    for (const warning of items.filter(item => item.description === 'commit-warning')) files.append(element('p', 'hint', warning.label));
    const selectable = changes.filter(item => typeof item.commitIncluded === 'boolean');
    if (selectable.length) files.append(element('p', 'hint selection-summary', `커밋 포함 ${selectable.filter(item => item.commitIncluded).length}개 · 제외 ${selectable.filter(item => !item.commitIncluded).length}개 · − 제외 / + 포함`));
    if (!changes.length) files.append(element('p', 'hint', '이번 기록에 변경된 파일이 없습니다.'));
    for (const item of changes) {
      const row = element('div', `file-row${item.commitIncluded === false ? ' commit-excluded' : ''}`);
      const file = button('', 'openDiff', item, `file ${item.change}`);
      file.title = item.path;
      file.setAttribute('aria-label', `${item.path} 변경 비교`);
      file.append(element('span', 'file-name', item.label), element('span', 'file-kind', item.change === 'added' ? '추가' : item.change === 'deleted' ? '삭제' : '수정'));
      if (item.commitOnly || item.commitIncluded === false) {
        const notes = element('span', 'file-notes');
        if (item.commitOnly) notes.append(element('span', 'file-note', '이전 기록'));
        if (item.commitIncluded === false) notes.append(element('span', 'file-note', '커밋 제외'));
        file.append(notes);
      }
      row.append(file);
      if (typeof item.commitIncluded === 'boolean') {
        const toggle = button(item.commitIncluded ? '−' : '+', 'toggleCommitFile', item, 'file-include');
        toggle.title = `${item.path} · ${item.commitIncluded ? '이번 커밋에서 제외' : '이번 커밋에 포함'}`;
        toggle.setAttribute('aria-label', toggle.title); toggle.setAttribute('aria-pressed', String(item.commitIncluded));
        row.append(toggle);
      }
      files.append(row);
    }
  }
  function record(snapshot, repo) {
    const row = element('article', `record ${snapshot.color}`); row.dataset.id = snapshot.id;
    const details = element('details'); details.open = expanded.has(snapshot.id);
    const summary = element('summary'); summary.title = [snapshot.label, new Date(snapshot.endedAt).toLocaleString(), `브랜치: ${snapshot.branch}`, `HEAD: ${snapshot.head}`, snapshot.rangeError || ''].filter(Boolean).join('\n');
    const heading = element('div', 'record-title');
    const time = element('time', '', new Date(snapshot.endedAt).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false }));
    time.dateTime = snapshot.endedAt;
    heading.append(element('strong', '', snapshot.label), time);
    const meta = element('div', 'record-meta');
    meta.append(element('span', 'branch-tag', snapshot.branch), element('span', '', `파일 ${snapshot.changedFiles}개`));
    if (snapshot.pinned) meta.append(element('span', 'badge', '고정'));
    if (snapshot.committed) meta.append(element('span', 'badge', '커밋됨'));
    else if (snapshot.partial) meta.append(element('span', 'badge', '일부 커밋'));
    const disclosure = element('span', 'disclosure'); disclosure.append(element('span', 'closed-label', '펼치기'), element('span', 'open-label', '접기')); meta.append(disclosure);
    summary.append(heading, meta); details.append(summary);
    const body = element('div', 'details-body');
    const context = element('div', 'context'); context.append(element('span', 'branch-tag', snapshot.branch), element('code', '', snapshot.head));
    const actions = element('div', 'actions');
    actions.append(button('변경 비교', 'openDiff', snapshot, 'secondary'),
      button('복원', 'restoreSnapshot', snapshot, 'secondary', !repo.writable),
      button('커밋', 'prepareCommit', snapshot, 'primary', !repo.writable || snapshot.committed));
    body.append(context, actions);
    if (repo.showAgentReports && snapshot.agentSource) body.append(element('p', 'hint', snapshot.agentSource), element('p', 'agent-excerpt', snapshot.agentDescription || ''));
    if (snapshot.commitSummary && snapshot.commitChangedFiles != null && !snapshot.committed) body.append(element('p', 'hint', `커밋 구간: ${snapshot.commitSummary} · 파일 ${snapshot.commitChangedFiles}개`));
    if (snapshot.comparisonPruned) body.append(element('p', 'hint', '이전 비교 기록이 삭제되어 포함 파일 기준으로 표시합니다.'));
    if (snapshot.rangeError) body.append(element('p', 'hint', '커밋 구간을 계산할 수 없습니다. 자세한 내용은 기록에 마우스를 올려 확인하세요.'));
    body.append(element('div', 'files'));
    const tools = element('div', 'record-tools'); tools.append(...management(snapshot, repo)); body.append(tools);
    details.append(body); row.append(details);
    details.addEventListener('toggle', () => {
      if (details.open) { expanded.add(snapshot.id); if (!children.has(snapshot.id)) send('expand', snapshot.id); }
      else expanded.delete(snapshot.id);
      vscode.setState({ expanded: [...expanded], scroll: window.scrollY });
    });
    summary.addEventListener('contextmenu', event => showMenu(event, snapshot, repo));
    summary.addEventListener('keydown', event => { if (event.key === 'F10' && event.shiftKey) showMenu(event, snapshot, repo); });
    if (details.open) { populateFiles(body, snapshot); send('expand', snapshot.id); }
    return row;
  }
  function agentRecord(report, repo) {
    const row = element('article', 'record agent-report'); row.dataset.id = report.id;
    const details = element('details'); details.open = expanded.has(report.id);
    const summary = element('summary'); summary.title = report.tooltip;
    const heading = element('div', 'record-title');
    heading.append(element('strong', '', report.label), element('time', '', new Date(report.collectedAt).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false })));
    summary.append(heading, element('p', 'record-meta', report.description));
    const body = element('div', 'details-body');
    body.append(element('p', 'agent-excerpt', report.reportDescription || ''), element('p', 'hint', '수집 시각 기준 · 에이전트 보고 · 작성자 및 테스트 성공 미검증'));
    const actions = element('div', 'actions');
    actions.append(button('상세 설명', 'agentReportDetails', report, 'secondary'), button('연결 검토', 'linkAgentReport', report, 'secondary', !repo.writable));
    if (report.linkedCount) actions.append(button('연결된 기록', 'openAgentSnapshots', report, 'secondary'));
    body.append(actions); details.append(summary, body); row.append(details);
    details.addEventListener('toggle', () => {
      if (details.open) expanded.add(report.id); else expanded.delete(report.id);
      vscode.setState({ expanded: [...expanded], scroll: window.scrollY });
    });
    return row;
  }
  function closeManagement() {
    if (!managementDialog || managementDialog.busy) return;
    const id = managementDialog.id;
    managementDialog.element.close(); managementDialog.element.remove(); managementDialog = undefined;
    send('managementClose', id);
    const row = [...app.querySelectorAll('[data-id]')].find(node => node.dataset.id === id);
    [...(row?.querySelectorAll('button') || [])].find(node => node.textContent === '관리')?.focus();
  }
  function managementBusy(value) {
    if (!managementDialog) return;
    managementDialog.busy = value;
    for (const control of managementDialog.element.querySelectorAll('input, select, button')) {
      control.disabled = value || control.dataset.locked === 'true';
    }
  }
  function managementRequest(type, extra = {}) {
    if (!managementDialog || managementDialog.busy) return;
    managementDialog.pendingFocus = document.activeElement.dataset.focus;
    managementBusy(true); managementDialog.status.className = 'dialog-status';
    managementDialog.status.textContent = '처리 중…';
    vscode.postMessage({ type, id: managementDialog.id, ...extra });
  }
  function renderManagement(id, state, noticeText = '') {
    const focused = managementDialog?.pendingFocus || (managementDialog?.element.contains(document.activeElement) ? document.activeElement.dataset.focus : undefined);
    if (managementDialog) { managementDialog.element.close(); managementDialog.element.remove(); }
    const dialog = element('dialog', 'management-dialog'); dialog.setAttribute('aria-labelledby', 'management-title');
    const header = element('div', 'dialog-header');
    const title = element('h2', '', '기록 관리'); title.id = 'management-title';
    const close = element('button', 'quiet dialog-close', '×'); close.type = 'button'; close.dataset.focus = 'close';
    close.setAttribute('aria-label', '관리 창 닫기'); close.addEventListener('click', closeManagement); header.append(title, close);
    const description = element('p', 'hint', state.name + (state.writable ? '' : ' · 읽기 전용'));
    const status = element('p', 'dialog-status', noticeText); status.tabIndex = -1;
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    managementDialog = { element: dialog, id, status, busy: false };
    const form = element('form', 'recording-form'); form.noValidate = true;
    form.append(element('h3', '', '기록 주기'));
    const inputs = new Map();
    for (const field of state.recording) {
      const label = element('label', 'recording-field'); label.append(element('span', 'field-title', field.label));
      const controls = element('span', 'field-control');
      const input = element('input'); input.type = 'number'; input.name = field.key; input.dataset.focus = field.key;
      input.min = String(field.step); input.max = String(field.maximum); input.step = String(field.step); input.value = String(field.value);
      input.disabled = !state.writable; input.dataset.locked = String(!state.writable);
      const help = element('span', 'hint field-help', field.detail); help.id = `management-${field.key}-help`;
      input.setAttribute('aria-describedby', help.id); inputs.set(field.key, input);
      controls.append(input, element('span', 'field-unit', field.unit)); label.append(controls, help); form.append(label);
    }
    const footer = element('div', 'dialog-actions');
    const save = element('button', 'primary', '주기 저장'); save.type = 'submit'; save.dataset.focus = 'save';
    save.disabled = !state.writable; save.dataset.locked = String(!state.writable);
    const cancel = element('button', 'secondary', '닫기'); cancel.type = 'button'; cancel.dataset.focus = 'cancel'; cancel.addEventListener('click', closeManagement);
    footer.append(save, cancel); form.append(footer);
    form.addEventListener('submit', event => {
      event.preventDefault();
      managementRequest('saveRecording', { values: Object.fromEntries([...inputs].map(([key, input]) => [key, input.value])) });
    });
    const history = element('section', 'dialog-history'); history.append(element('h3', '', '기록 및 저장 공간'));
    history.append(element('p', 'hint', `${(state.usage.bytes / 1048576).toFixed(1)} / ${(state.usage.limit / 1048576).toFixed(0)} MiB 사용 · 기록 ${state.usage.snapshots.length}개`));
    const actions = element('div', 'dialog-actions');
    for (const [text, action] of [[state.paused ? '자동 기록 재개' : '자동 기록 일시 중지', state.paused ? 'resume' : 'pause'], ['보존 정책으로 정리', 'prune']]) {
      const button = element('button', 'secondary', text); button.type = 'button'; button.dataset.focus = action;
      button.disabled = !state.writable; button.dataset.locked = String(!state.writable);
      button.addEventListener('click', () => managementRequest('managementAction', { action })); actions.append(button);
    }
    history.append(actions);
    const deletion = element('label', 'recording-field'); deletion.append(element('span', 'field-title', '스냅샷 삭제'));
    const select = element('select'); select.dataset.focus = 'snapshot';
    const empty = element('option', '', '삭제할 기록을 선택하세요'); empty.value = ''; select.append(empty);
    for (const snapshot of state.usage.snapshots) {
      const option = element('option', '', `${snapshot.protected ? '[보호됨] ' : ''}${snapshot.title} · ${new Date(snapshot.endedAt).toLocaleString()}`);
      option.value = snapshot.id; option.disabled = snapshot.protected; select.append(option);
    }
    select.disabled = !state.writable; select.dataset.locked = String(!state.writable); deletion.append(select);
    const remove = element('button', 'danger secondary', '선택한 기록 삭제'); remove.type = 'button'; remove.dataset.focus = 'delete';
    remove.disabled = true; remove.dataset.locked = 'true';
    select.addEventListener('change', () => { remove.dataset.locked = String(!state.writable || !select.value); remove.disabled = managementDialog.busy || remove.dataset.locked === 'true'; });
    remove.addEventListener('click', () => managementRequest('managementAction', { action: 'delete', snapshotId: select.value }));
    history.append(deletion, remove, element('p', 'hint', '보호된 기록은 삭제할 수 없습니다. 삭제 전 최종 확인 창이 표시됩니다.'));
    dialog.append(header, description, status, form, history);
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeManagement(); });
    dialog.addEventListener('click', event => {
      const bounds = dialog.getBoundingClientRect();
      if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) closeManagement();
    });
    document.body.append(dialog); dialog.showModal();
    if (focused) [...dialog.querySelectorAll('[data-focus]')].find(node => node.dataset.focus === focused)?.focus();
  }
  function render() {
    const focused = document.activeElement;
    const focusRecord = focused?.closest('[data-id]')?.dataset.id;
    const focusText = focused?.textContent;
    const scroll = window.scrollY;
    closeMenu(); app.replaceChildren();
    const header = element('header', 'topbar');
    const title = element('div'); title.append(element('h1', '', 'AmberTrail'), element('p', '', '작업 타임라인'));
    header.append(title);
    const first = repositories.find(repo => repo.kind === 'repository');
    if (first) header.append(button('새 기록', 'captureNow', first, 'primary', !first.writable));
    app.append(header);
    for (const repo of repositories) {
      if (repo.kind !== 'repository') { app.append(element('p', 'empty', repo.label)); continue; }
      const section = element('section', 'repository'); section.dataset.id = repo.id;
      const heading = element('div', 'repo-heading'); heading.append(element('h2', '', `저장소 · ${repo.label}`), button('관리', 'manageHistory', repo, 'quiet'));
      const meta = element('div', 'repo-meta');
      const attention = repo.writable && !repo.paused && repo.status && !repo.status.startsWith('기록 중');
      meta.append(element('span', `recording${repo.paused || !repo.writable ? ' paused' : attention ? ' attention' : ''}`, !repo.writable ? '읽기 전용' : repo.paused ? '일시 중지' : attention ? repo.status : '자동 기록 중'), element('span', 'repo-branch', repo.branch));
      const toolbar = element('div', 'repo-toolbar'); toolbar.append(button('새로고침', 'refresh', repo, 'quiet'));
      if (repo.writable && repo.showAgentReports) toolbar.append(button(repo.agentHistoryEnabled ? 'Codex 연동 관리' : 'Codex 연동 설정', 'agentHistorySettings', repo, 'quiet'));
      if (repo.writable) toolbar.append(button(repo.paused ? '기록 재개' : '일시 중지', repo.paused ? 'resumeRecording' : 'pauseRecording', repo, 'quiet'));
      if (repositories.filter(r => r.kind === 'repository').length > 1) toolbar.append(button('새 기록', 'captureNow', repo, 'quiet', !repo.writable));
      section.append(heading, meta, toolbar);
      section.append(element('p', 'hint', '저장된 변경 내용을 로컬에서 분석해 작업 이름을 자동 생성합니다.'));
      if (repo.showAgentReports && repo.agentHistoryProblem) section.append(element('p', 'hint', repo.agentHistoryProblem));
      if (repo.status?.startsWith('기록 중 ·')) section.append(element('p', 'hint', repo.status));
      if (repo.dirtyCount || repo.excludedCount) section.append(element('p', 'hint', [repo.dirtyCount ? `미저장 편집 ${repo.dirtyCount}개` : '', repo.excludedCount ? `제외 파일 ${repo.excludedCount}개` : ''].filter(Boolean).join(' · ')));
      let day;
      for (const item of repo.children) {
        if (item.kind === 'snapshot' || item.kind === 'agent') {
          const date = item.kind === 'agent' ? item.collectedAt : item.endedAt;
          const nextDay = new Date(date).toDateString();
          if (nextDay !== day) { section.append(element('h3', 'day-heading', dateLabel(date))); day = nextDay; }
          section.append(item.kind === 'agent' ? agentRecord(item, repo) : record(item, repo));
        } else if (item.kind === 'recovery') section.append(button(item.label, 'recover', item, 'recovery'));
        else if (item.kind === 'more') section.append(button('이전 기록 더 보기', 'more', item, 'more secondary'));
        else section.append(element('p', 'empty', item.label));
      }
      section.append(element('footer', 'footer', '로컬에 보관 · 정상 동작 미검증 · 원격 백업 별도'));
      app.append(section);
    }
    if (focusRecord) {
      const row = [...app.querySelectorAll('[data-id]')].find(node => node.dataset.id === focusRecord);
      const target = focused?.tagName === 'SUMMARY' ? row?.querySelector('summary') : [...(row?.querySelectorAll('button') || [])].find(node => node.textContent === focusText);
      target?.focus({ preventScroll: true });
    }
    window.scrollTo(0, scroll || saved.scroll || 0);
  }
  window.addEventListener('message', event => {
    const value = event.data;
    if (value.type === 'state') {
      repositories = value.repositories; busy = false; notice.textContent = ''; children.clear(); render();
    } else if (value.type === 'children') {
      children.set(value.id, value.children);
      const row = [...app.querySelectorAll('[data-id]')].find(node => node.dataset.id === value.id);
      if (row) populateFiles(row.querySelector('.details-body'), { id: value.id });
    } else if (value.type === 'management') {
      busy = false; notice.textContent = ''; render();
      renderManagement(value.id, value.state, value.notice);
    } else if (value.type === 'managementError') {
      if (managementDialog?.id === value.id) {
        managementBusy(false); managementDialog.status.className = 'dialog-status dialog-error';
        managementDialog.status.textContent = value.message;
        managementDialog.status.focus();
      }
    } else if (value.type === 'error') {
      busy = false; notice.textContent = value.message;
      if (value.id) { children.set(value.id, { error: value.message }); const row = [...app.querySelectorAll('[data-id]')].find(node => node.dataset.id === value.id); if (row) populateFiles(row.querySelector('.details-body'), { id: value.id }); }
      else {
        render(); const retry = element('button', 'secondary', '다시 시도');
        retry.addEventListener('click', () => send('ready')); notice.append(retry);
      }
    }
  });
  document.addEventListener('click', event => { if (menu && !menu.contains(event.target)) closeMenu(); });
  document.addEventListener('keydown', event => {
    if (!menu) return;
    if (event.key === 'Escape') { closeMenu(); app.querySelector('summary')?.focus(); }
    else if (['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
      event.preventDefault(); const items = [...menu.querySelectorAll('button:not(:disabled)')];
      const index = items.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }
  });
  window.addEventListener('scroll', () => vscode.setState({ expanded: [...expanded], scroll: window.scrollY }), { passive: true });
  send('ready');
})();
