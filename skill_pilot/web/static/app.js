/* Skill Pilot 看板。
   分段：通用工具 → 页头与导航 → 技能页 → 技能详情 → 安装技能 → 接入新 agent →
        检查更新（结果显示在技能页和详情里） → 清理页 → 操作记录页 → 诊断与备份页 → 设置页 → 团队分享 → 启动。
   页面不写死任何具体 agent：名字、顺序、生效方式都来自接口。 */

/* ==================== 通用工具：请求、转义、提示、确认框、按钮忙碌态 ==================== */

const token = new URLSearchParams(location.search).get('token');
const headers = {'X-Skill-Pilot-Token': token};

const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = value => JSON.parse(JSON.stringify(value));

/* 更新同一组按钮时保留节点，让按压、选中底色和焦点连续过渡。 */
function renderButtons(container, markup, key) {
  const template = document.createElement('template');
  template.innerHTML = markup;
  const existing = new Map([...container.children].filter(button => !button.classList.contains('selection-plate')).map(button => [button.getAttribute(key), button]));
  for (const [index, next] of [...template.content.children].entries()) {
    const value = next.getAttribute(key);
    const button = existing.get(value) || next;
    if (button !== next) {
      for (const attribute of [...button.attributes]) if (!next.hasAttribute(attribute.name)) button.removeAttribute(attribute.name);
      for (const attribute of next.attributes) if (button.getAttribute(attribute.name) !== attribute.value) button.setAttribute(attribute.name, attribute.value);
      if (button.innerHTML !== next.innerHTML) button.innerHTML = next.innerHTML;
    }
    existing.delete(value);
    if (container.children[index] !== button) container.insertBefore(button, container.children[index] || null);
  }
  for (const button of existing.values()) button.remove();
  if (container.matches('#agent-rail, #skills-tabs, .filter-group-list, .filter-tag-list')) glideSelection(container);
}

const selectionMotions = new WeakMap();

/* 同一块选中底色在按钮之间移动。连点时从屏幕上正在经过的位置接着走。 */
function glideSelection(container) {
  let motion = selectionMotions.get(container);
  if (!motion) {
    const plate = document.createElement('span');
    plate.className = 'selection-plate';
    plate.setAttribute('aria-hidden', 'true');
    container.append(plate);
    container.classList.add('selection-track');
    motion = {plate, animation: null, target: null};
    selectionMotions.set(container, motion);
    const observer = new ResizeObserver(() => {
      if (!container.isConnected) { observer.disconnect(); return; }
      glideSelection(container);
    });
    observer.observe(container);
  }
  const selected = container.querySelector('button[aria-current="page"], button[aria-current="true"], button[aria-pressed="true"]');
  const {plate} = motion;
  if (!selected || !selected.offsetWidth || !container.offsetWidth) {
    motion.animation?.cancel();
    motion.target = null;
    plate.hidden = true;
    return;
  }
  const target = {x: selected.offsetLeft, y: selected.offsetTop, w: selected.offsetWidth, h: selected.offsetHeight};
  if (motion.target && Object.keys(target).every(key => target[key] === motion.target[key])) return;
  const before = motion.target && !plate.hidden ? plate.getBoundingClientRect() : null;
  const bounds = container.getBoundingClientRect();
  motion.animation?.cancel();
  plate.hidden = false;
  plate.style.width = `${target.w}px`;
  plate.style.height = `${target.h}px`;
  const finish = `translate(${target.x}px, ${target.y}px)`;
  plate.style.transform = finish;
  motion.target = target;
  if (before && !prefersReducedMotion()) {
    const x = before.left - bounds.left - container.clientLeft + container.scrollLeft;
    const y = before.top - bounds.top - container.clientTop + container.scrollTop;
    motion.animation = plate.animate([
      {transform: `translate(${x}px, ${y}px) scale(${before.width / target.w}, ${before.height / target.h})`},
      {transform: finish},
    ], {duration: 360, easing: 'cubic-bezier(0.22, 1.1, 0.36, 1)'});
  }
}

/* 图标加几个字：图标来自 index.html 顶部的 <symbol>，只用来配文字，读屏不读。 */
const icon = (name, extra = '') => `<svg class="ico${extra ? ' ' + extra : ''}" aria-hidden="true" focusable="false"><use href="#sp-${name}"/></svg>`;

/* 说明图标：长一点的解释放在这里，鼠标移上去或键盘 Tab 到它才显示；名字里带上全文，读屏也能读到。 */
const hint = text => `<button type="button" class="hint" data-hint="${escape(text)}" aria-label="${escape('说明：' + text)}">${icon('info')}</button>`;

/* 空状态：一个大图标加一句话，后面可以跟一个说明图标或一个按钮。 */
const emptyInner = (name, text, extra = '') => `${icon(name, 'ico-lg')}<span>${escape(text)}</span>${extra}`;

async function api(path, body) {
  const options = body === undefined
    ? {headers}
    : {method: 'POST', headers: {...headers, 'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  let result = null;
  try { result = await response.json(); } catch { /* 没有内容时按下面的默认文案处理 */ }
  if (!response.ok) {
    const error = new Error((result && result.error) || '操作没能完成，请稍后再试。');
    error.retry = Boolean(result && result.retry);
    error.recheck = Boolean(result && result.recheck);
    throw error;
  }
  return result;
}

/* 按钮忙碌态：按下后先禁用并换文案，做完再恢复。
   按钮里可能带图标，所以保存和恢复的是整段内容；只有图标的按钮不传 label，忙碌时图标自己转。 */
async function withBusy(button, label, work) {
  if (!button) return work();
  const original = button.innerHTML;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  if (label) button.textContent = label;
  try {
    return await work();
  } finally {
    button.removeAttribute('aria-busy');
    button.disabled = false;
    button.innerHTML = original;
  }
}

/* 确认框：全站唯一的确认方式，不用浏览器自带的弹窗。
   groups 是 [{label, items:[{name, note}]}]，按组列出这次会动到哪些技能。 */
const CONFIRM_FOLD = 8;

function confirmItems(items) {
  return items.map(item => `<li>${item.html || nameHtml(item.name)}${item.note ? `<span class="confirm-note-inline">（${escape(item.note)}）</span>` : ''}</li>`).join('');
}

function confirmGroupsHtml(groups) {
  return (groups || []).filter(group => group.items.length).map(group => {
    const shown = group.items.slice(0, CONFIRM_FOLD);
    const rest = group.items.slice(CONFIRM_FOLD);
    return `<div class="confirm-group">
      <p class="confirm-group-title">${escape(group.label)}</p>
      <ul class="confirm-items">${confirmItems(shown)}</ul>
      ${rest.length ? `<div hidden><ul class="confirm-items">${confirmItems(rest)}</ul></div>
        <button type="button" class="link-btn" data-confirm-more>展开其余 ${rest.length} 个</button>` : ''}
    </div>`;
  }).join('');
}

function confirmDialog({title = '确认这次操作', lines = [], groups = [], note = '', okText = '确认'}) {
  const dialog = $('confirm-dialog');
  $('confirm-title').textContent = title;
  $('confirm-body').innerHTML = lines.filter(Boolean).map(line => `<p class="confirm-line">${escape(line)}</p>`).join('')
    + confirmGroupsHtml(groups)
    + (note ? `<p class="confirm-note">${escape(note)}</p>` : '');
  $('confirm-ok').textContent = okText;
  return new Promise(resolve => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), {once: true});
    dialog.showModal();
  });
}

$('confirm-body').addEventListener('click', event => {
  const button = event.target.closest('[data-confirm-more]');
  if (!button) return;
  button.previousElementSibling.hidden = false;
  button.remove();
});

/* 提示条：成功会自己消失，出错留着让用户看清。
   带撤销的提示，撤销自己也会先弹确认框，用户取消时提示条留着。 */
function toast({text, tone = 'ok', undo = null, sticky = false}) {
  const item = document.createElement('div');
  item.className = 'toast' + (tone === 'error' ? ' toast-error' : '');
  const message = document.createElement('p');
  message.textContent = text;
  item.append(message);
  if (undo) {
    const button = document.createElement('button');
    button.className = 'btn btn-small';
    button.textContent = '撤销';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const done = await undo(button);
        if (done === false) { button.disabled = false; return; }
        item.remove();
      } catch (error) {
        button.disabled = false;
        toast({text: error.message, tone: 'error', sticky: true});
      }
    });
    item.append(button);
  }
  const close = document.createElement('button');
  close.className = 'toast-close';
  close.textContent = '关闭';
  close.setAttribute('aria-label', '关闭这条提示');
  close.addEventListener('click', () => item.remove());
  item.append(close);
  $('toasts').prepend(item);
  if (!sticky && tone !== 'error') autoDismiss(item, undo ? 20000 : 6000);
}

/* 自动消失：带撤销的提示至少留 20 秒，鼠标移上去或键盘聚焦时暂停。 */
function autoDismiss(item, delay) {
  let timer = setTimeout(() => item.remove(), delay);
  const pause = () => { clearTimeout(timer); timer = null; };
  const resume = () => { if (!timer && item.isConnected) timer = setTimeout(() => item.remove(), delay); };
  item.addEventListener('mouseenter', pause);
  item.addEventListener('mouseleave', resume);
  item.addEventListener('focusin', pause);
  item.addEventListener('focusout', resume);
}

/* 提示：带 data-hint 的元素（说明图标、几个小标记）共用 #tip 这一个浮层。
   鼠标停一下，或用键盘 Tab 到它，就在旁边浮出完整说明；移开、失去焦点、按 Esc、滚动都会收起。
   浮层是 popover，会顶到最上层，所以列表、侧面板、对话框里都不会被切掉。 */
const tip = $('tip');
let tipOwner = null;
let tipTimer = null;
const TIP_HOVER_MS = 250;
if (!tip.showPopover) tip.hidden = true;   // 太老的浏览器没有 popover，就用 hidden 开关

function placeTip(owner) {
  const rect = owner.getBoundingClientRect();
  const box = tip.getBoundingClientRect();
  const gap = 8;
  // 默认放在下面；下面放不下就放上面，左右都留 8px 边距。
  const top = rect.bottom + gap + box.height > innerHeight - 8 ? Math.max(8, rect.top - gap - box.height) : rect.bottom + gap;
  const left = Math.min(Math.max(8, rect.left + rect.width / 2 - box.width / 2), innerWidth - box.width - 8);
  tip.style.top = `${top}px`;
  tip.style.left = `${left}px`;
}

function showTip(owner) {
  if (!owner.isConnected || !owner.dataset.hint || tipOwner === owner) return;
  hideTip();
  tipOwner = owner;
  tip.textContent = owner.dataset.hint;
  owner.setAttribute('aria-describedby', 'tip');
  if (tip.showPopover) tip.showPopover(); else tip.hidden = false;
  placeTip(owner);
}

function hideTip() {
  clearTimeout(tipTimer);
  if (!tipOwner) return;
  tipOwner.removeAttribute('aria-describedby');
  tipOwner = null;
  if (tip.hidePopover) {
    try { tip.hidePopover(); } catch { /* 本来就没显示 */ }
  } else {
    tip.hidden = true;
  }
}

const hintOwner = event => event.target.closest?.('[data-hint]') || null;

document.addEventListener('mouseover', event => {
  const owner = hintOwner(event);
  if (!owner || owner === tipOwner) return;
  clearTimeout(tipTimer);
  tipTimer = setTimeout(() => showTip(owner), TIP_HOVER_MS);
});
document.addEventListener('mouseout', event => {
  const owner = hintOwner(event);
  if (!owner || owner.contains(event.relatedTarget)) return;
  clearTimeout(tipTimer);
  if (owner === tipOwner && !owner.matches(':focus-visible')) hideTip();
});
// 键盘 Tab 到的才立刻显示；鼠标点中不算，免得点一下就弹出来。
document.addEventListener('focusin', event => {
  const owner = hintOwner(event);
  if (owner?.matches(':focus-visible')) showTip(owner);
});
document.addEventListener('focusout', event => { if (event.target === tipOwner) hideTip(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') hideTip(); });
document.addEventListener('scroll', hideTip, true);
// 列表重画后，原来那个图标已经不在页面上了，别让浮层留在原地。
setInterval(() => { if (tipOwner && !tipOwner.isConnected) hideTip(); }, 400);

/* index.html 里写死的说明图标只有 data-hint，这里补上图标和读屏用的名字。 */
for (const button of document.querySelectorAll('button.hint:empty')) {
  button.innerHTML = icon('info');
  button.setAttribute('aria-label', '说明：' + button.dataset.hint);
}

/* 页头任务条：后台在做什么都显示在这里。
   做完直接消失，不显示「完成」那一行（那一行会把下面的内容顶下去）。 */
const tasks = new Map();

function setTask(id, task) {
  tasks.set(id, {...task});
  renderTasks();
}

function clearTask(id) {
  tasks.delete(id);
  renderTasks();
}

function renderTasks() {
  const bar = $('task-bar');
  const entries = [...tasks.entries()];
  bar.hidden = !entries.length;
  bar.innerHTML = entries.map(([id, task]) => {
    const ratio = typeof task.ratio === 'number' ? task.ratio : null;
    const action = task.retry
      ? `<button class="btn btn-small" data-task-retry="${escape(id)}">重试</button>`
      : task.tone === 'error' ? `<button class="btn btn-small" data-task-dismiss="${escape(id)}">关闭</button>` : '';
    // 有具体进度就画进度条，没有就画一条来回走的细条，表示还在做。
    const track = ratio !== null
      ? `<span class="task-track"><span class="task-fill" data-ratio="${Math.max(0, Math.min(1, ratio))}"></span></span>`
      : task.tone === 'error' ? '' : '<span class="task-track task-track-busy"><span class="task-fill"></span></span>';
    return `<div class="task task-${escape(task.tone || 'busy')}">
      <span class="task-text">${escape(task.text)}</span>
      ${track}
      ${action}
    </div>`;
  }).join('');
  for (const fill of bar.querySelectorAll('[data-ratio]')) {
    fill.style.setProperty('--ratio', fill.dataset.ratio);
  }
}

$('task-bar').addEventListener('click', event => {
  const retryId = event.target.dataset.taskRetry;
  const dismissId = event.target.dataset.taskDismiss;
  if (retryId) {
    const task = tasks.get(retryId);
    clearTask(retryId);
    task?.retry?.();
  } else if (dismissId) {
    clearTask(dismissId);
  }
});

/* 写入过程的统一反馈。确认框点过之后调用：
   - 触发这次写入的按钮（底部操作栏或列表里的那个，不是确认框里的）换文案并禁用；
   - 全站会改动这次写入内容的控件一并禁用，中途重新渲染也保持禁用；
   - 页头任务条显示同一句话；
   - 成功：先恢复控件，再弹绿色提示；失败：先恢复控件，再弹红色提示，
     任务条转成错误样式（写入不给「重试」，让用户看清原因自己决定）。
   返回 work() 的结果；失败返回 null，不往外抛。 */
const WRITE_LOCK_SELECTORS = [
  '#team-form input', '#team-save', '#store-publish', '#store-list [data-store-republish]', '#store-list [data-store-unlist]',
  '#store-shelves [data-store-publish]', '#store-publish-dialog input', '#store-publish-dialog button:not(.hint)',
  '#skills-list button[data-skill]', '#skills-list button[data-update-up]', '#action-bar button', '#skills-refresh',
  '#install-url', '#install-rail button:not(.hint)',
  '#cleanup-rows input[data-cleanup]', '#withdrawn-list button', '#withdrawn-refresh',
  '#updates-check', '#updates-all',
  '#program-check', '#program-apply', '#program-refresh', '#rail-notes button',
  '#config-export', '#config-agent', '#config-import', '#config-import-dialog input', '#config-import-dialog select', '#config-import-dialog button',
  '#log-list button', '#log-refresh', '#log-export', '#detail-body button:not(.hint)', '#detail-body select',
  '#new-absorb', '#new-toggle', '#install-dialog button:not(.hint)',
  '#agents-scan', '#agent-add-submit', '#agents-managed button:not(.hint)', '#agents-suggested button:not(.hint)', '#agents-ignored button:not(.hint)', '#agents-join-dialog input', '#agents-join-dialog button:not(.hint)',
  '#migration-preview', '#migration-apply', '#migration-back', '#migration-agents input',
  '#migration-versions input', '#migration-backups', '#migration-restore', '#migration-backup-list button',
];

let writeBusy = null;

function lockControls(selectors) {
  const changed = [];
  for (const element of document.querySelectorAll(selectors.join(','))) {
    if (element.disabled) continue;
    element.disabled = true;
    changed.push(element);
  }
  return () => { for (const element of changed) element.disabled = false; };
}

/* 正在写入时把各区重画一遍，让新画出来的控件也是禁用的。 */
function renderBusyAreas() {
  renderProgram();
  renderConfigExport();
  renderSkillTable();
  renderSkillsActionBar();
  renderCleanup();
  renderCleanupActionBar();
  renderUpdateBar();
  renderLog();
  renderInstallRail();
  if (state.detail) renderDetail();
  renderRailNotes();
  renderAgents();
  renderTeamSettings();
  renderTeam();
}

async function runWrite({button = null, busyText, taskText = busyText, success = null, onError = null, work}) {
  const original = button?.innerHTML ?? null;
  writeBusy = busyText;
  if (button) {
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = busyText;
  }
  // 写入是用户按下的，任务条立刻出现；防闪烁只用在后台任务上。
  setTask('write', {text: taskText});
  // 这里不重画各区：重画会把刚换成「正在…」的按钮换掉。
  // 禁用交给 lockControls；万一中途有重画，各区自己会看 writeBusy 保持禁用。
  const unlock = lockControls(WRITE_LOCK_SELECTORS);

  let result = null;
  let failure = null;
  try {
    result = await work();
  } catch (error) {
    failure = error;
  }

  writeBusy = null;
  unlock();
  if (button) {
    button.removeAttribute('aria-busy');
    button.innerHTML = original;
  }
  renderBusyAreas();

  if (failure) {
    setTask('write', {text: failure.message, tone: 'error'});
    onError?.(failure);
    toast({text: failure.message, tone: 'error', sticky: true});
    return null;
  }
  clearTask('write');
  const notice = success?.(result);
  if (notice) toast(notice);
  return result;
}

/* 底部固定操作栏：只在有事情可做时出现。 */
function showActionBar({text, note = '', buttons = [], items = '', extra = ''}) {
  $('action-summary').textContent = text;
  $('action-note').textContent = note;
  $('action-note').hidden = !note;
  const box = $('action-buttons');
  box.innerHTML = '';
  for (const item of buttons) {
    const button = document.createElement('button');
    button.className = item.kind || 'btn';
    button.textContent = item.label;
    button.disabled = Boolean(item.disabled);
    button.addEventListener('click', () => item.action(button));
    box.append(button);
  }
  $('action-list').innerHTML = items;
  $('action-list').hidden = !items;
  const wasOpen = $('action-extra').querySelector('details')?.open;
  $('action-extra').innerHTML = extra;
  $('action-extra').hidden = !extra;
  if (wasOpen && $('action-extra').querySelector('details')) $('action-extra').querySelector('details').open = true;
  const bar = $('action-bar');
  bar.hidden = false;
  document.body.classList.add('has-action-bar');
  requestAnimationFrame(() => bar.classList.add('is-open'));
}

function hideActionBar() {
  const bar = $('action-bar');
  bar.classList.remove('is-open');
  document.body.classList.remove('has-action-bar');
  $('action-buttons').innerHTML = '';
  $('action-list').innerHTML = '';
  $('action-extra').innerHTML = '';
  setTimeout(() => { if (!$('action-bar').classList.contains('is-open')) bar.hidden = true; }, 200);
}

/* 心跳：看板关掉后立刻告诉用户。
   浏览器会放慢甚至暂停后台标签页的计时，所以每个页面带上自己的编号，关闭时再说一声，
   后台据此判断还有没有页面开着，而不是只看心跳间隔。 */
const pageId = Math.random().toString(36).slice(2);
async function heartbeat() {
  try {
    const response = await fetch('/api/heartbeat?page=' + pageId, {method: 'POST', headers});
    if (!response.ok) throw new Error();
    $('connection').hidden = true;
  } catch {
    $('connection').hidden = false;
  }
}

/* 撤销：先确认，再取最新状态，避免用过期版本号。
   button 是触发这次撤销的那个按钮（提示条上的或操作记录里的），由 runWrite 负责忙碌态。
   返回 false 表示用户取消了。 */
async function undoOperation(id, {skills = [], message = '', button = null} = {}) {
  const ids = Array.isArray(id) ? id : [id];
  const ok = await confirmDialog({
    title: '撤销这次操作',
    lines: [message ? `要撤销的是：${message}` : '会恢复相应的技能内容、分配入口和工具添加的停用设置。'],
    groups: skills.length ? [{label: `这次操作动过 ${skills.length} 个技能`, items: skills.map(name => ({name}))}] : [],
    note: '后来的操作和这次有重叠时，会提示先撤销后来的操作。撤销本身也会记在操作记录里。',
    okText: '撤销',
  });
  if (!ok) return false;
  const result = await runWrite({
    button,
    busyText: '正在撤销…',
    work: async () => {
      const history = ids.length > 1 ? await api('/api/log') : [];
      let done = 0;
      try {
        for (const operationId of ids) {
          if (history.some(record => record.id === operationId && record.undone)) continue;
          const table = await api('/api/table');
          await api('/api/undo', {id: operationId, revision: table.revision, confirmed: true});
          done++;
        }
      } catch (error) {
        if (done) error.message = `已撤销 ${done} 项，其余还没撤销：${error.message}`;
        throw error;
      } finally {
        await refreshEverything();
        await refreshUpdates();
      }
      return true;
    },
    success: () => ({text: '已经撤销这次操作'}),
  });
  return result !== null;
}

/* ==================== 页头与导航：摘要条、页面切换、模式徽标 ==================== */

/* agent 的一切都来自接口：/api/table 给当前表里的 agent（顺序就是显示顺序），
   /api/agents 补齐名字、目录、生效方式、用量和接入建议。页面不认识任何具体 agent。 */
const state = {
  page: 'skills',
  writeAllowed: false,
  inventory: null,
  table: null,
  agents: [],
  agentsView: null,
  catalog: new Map(),
  skillIndex: new Map(),
  groupIndex: new Map(),
  saved: {},
  draft: {},
  pending: null,
  assignmentIntents: new Map(), // 只记用户亲手选的改动，依赖始终根据最新状态重算。
  autoAdded: new Map(),   // 「技能|agent」→ 需要它的那个技能（预览的 auto_added）
  detail: null,
  agent: null,
  tab: 'on',
  settingsBlock: 'ai',   // 设置页当前看到的块；地址里记成 settings/<块名>
  chip: '',   // 分组筛选：'' 是全部，UPDATES_CHIP 是“有新版”，其余是分组名
  tag: '',    // 标签筛选，'' 是不筛
  filtersCollapsed: false,
  cleanup: null,
  cleanupSelected: new Set(),
  logs: [],
  team: null,          // /api/team：有没有连上分享站、我是谁
  store: null,         // /api/team/catalog：团队技能商店的全部卡片；null 是还没读
  storeError: '',
  storeSort: 'latest', // 全部技能的排序：latest / rating / popular
  storeGroup: '',      // 选中的分组；'-' 是没分组
  storeMine: false,    // 只看我上架的
  storeDetail: null,   // 详情面板里正在看的卡片编号
};

/* 编号翻名字：先看接口给的目录（操作记录、已撤下的技能不在当前表里，也从这里兜底），
   再退回表里的名字，都查不到就直接显示编号。 */
function nameOf(id) {
  const key = String(id ?? '');
  return state.catalog.get(key)?.name || state.agents.find(agent => agent.id === key)?.name || key;
}

function indexCatalog(view) {
  state.agentsView = view;
  const catalog = new Map();
  for (const item of [...(view?.managed || []), ...(view?.suggested || []), ...(view?.ignored || [])]) {
    if (item?.id) catalog.set(item.id, item);
  }
  state.catalog = catalog;
}

function homePath() {
  return state.table?.home || state.inventory?.home || '';
}

/* 同一个位置在 macOS 上可能写成 /var/… 也可能写成 /private/var/…，
   两个接口给的 home 写法不一定一样，所以两种都匹配。 */
function homeVariants() {
  const variants = new Set();
  for (const home of [state.table?.home, state.inventory?.home]) {
    if (!home) continue;
    variants.add(home);
    if (home.startsWith('/private/')) variants.add(home.slice('/private'.length));
    else if (home.startsWith('/var/') || home.startsWith('/tmp/')) variants.add('/private' + home);
  }
  return [...variants].sort((a, b) => b.length - a.length);
}

/* 技能目录在接口里是相对 home 的，先拼上 home 再缩写成 ~/…。
   Text 结尾的是没转义的纯文字（放进 data-hint 这类属性里用），Label 结尾的可以直接放进 HTML。 */
const rootsText = roots => (roots || []).map(root => pathText(homePath() + '/' + root)).join('、');
const rootsLabel = roots => escape(rootsText(roots));

function pathText(path) {
  const text = String(path ?? '');
  for (const home of homeVariants()) {
    if (text === home) return '~';
    if (text.startsWith(home + '/')) return '~' + text.slice(home.length);
  }
  return text;
}
const pathLabel = path => escape(pathText(path));

const number = value => Number(value || 0).toLocaleString('zh-CN');

/* Token 数：不到 1000 写原数，之后写成 7.2k、12k。 */
const tokenText = value => {
  const n = Math.round(Number(value || 0));
  return n < 1000 ? String(n) : `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
};

/* 技能的中文名来自本机整理结果（labels.json），没写的就用原名。
   nameHtml 把原名用小号灰字跟在中文名后面，方便和 agent 里看到的名字对上。 */
const titleOf = name => state.skillIndex.get(name)?.title || name;

function nameHtml(name) {
  const title = state.skillIndex.get(name)?.title;
  return title ? `${escape(title)} <span class="skill-id">${escape(name)}</span>` : escape(name);
}

/* 原始时间格式换成大白话日期；跨年才写年份。 */
function friendlyDate(value) {
  if (!value) return '';
  const time = Date.parse(value);
  if (Number.isNaN(time)) return String(value);
  const date = new Date(time);
  const text = `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
  return date.getFullYear() === new Date().getFullYear() ? text : `${date.getFullYear()} 年 ${text}`;
}

/* 地址里记着在哪一页、技能页选的哪个 agent、设置页看的哪一块，刷新后回到原处。 */
function rememberPlace() {
  const place = state.page === 'skills' && state.agent ? `skills/${state.agent}`
    : state.page === 'settings' ? `settings/${state.settingsBlock}`
    : state.page;
  history.replaceState(null, '', location.pathname + location.search + '#' + place);
}

function showPage(page) {
  state.page = page;
  rememberPlace();
  for (const section of document.querySelectorAll('.page-section')) {
    section.hidden = section.id !== 'page-' + page;
  }
  for (const button of document.querySelectorAll('.nav-item')) {
    if (button.dataset.page === page) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  glideSelection(document.querySelector('.nav'));
  if (page === 'cleanup') { renderCleanup(); loadCleanup(); }
  if (page === 'settings') renderSettings();
  if (page === 'team') enterTeam();
  renderActionBar();
  window.scrollTo({top: 0, behavior: 'auto'});
  if (page === 'skills') fitFilterRail();
}

/* 跳到某一页；设置页还可以点名去哪一块（data-goto="settings/agents"）。 */
function gotoPlace(place) {
  const [page, block] = String(place).split('/');
  if (page === 'settings') {
    showPage('settings');
    showSettingsBlock(SETTINGS_BLOCKS.includes(block) ? block : state.settingsBlock);
    return;
  }
  showPage(page);
}

/* ==================== 设置页：左边一列目录，右边六块连在一起（用户选的 B 版） ==================== */

const SETTINGS_BLOCKS = ['ai', 'agents', 'share', 'team', 'log', 'diagnostics', 'version'];
const prefersReducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', event => {
  if (event.matches) {
    for (const plate of document.querySelectorAll('.selection-plate')) plate.getAnimations().forEach(animation => animation.cancel());
    skillListMotion?.cancel();
    filterLayoutMotion?.cancel();
  }
});

function highlightSettingsBlock(name) {
  for (const button of document.querySelectorAll('#settings-nav [data-block]')) {
    if (button.dataset.block === name) button.setAttribute('aria-current', 'true');
    else button.removeAttribute('aria-current');
  }
}

/* 当前看到的块：标题线（约 140px）以上、离它最近的那一块。
   滚到页面底部时，最后一块还没到标题线，但已经全在眼前了，所以直接算最后一块。 */
function currentSettingsBlock() {
  const blocks = [...document.querySelectorAll('.settings-main > .set-block')];
  const doc = document.documentElement;
  if (innerHeight + scrollY >= Math.max(doc.scrollHeight, document.body.scrollHeight) - 4) return blocks[blocks.length - 1].dataset.block;
  let found = blocks[0].dataset.block;
  for (const block of blocks) if (block.getBoundingClientRect().top <= 140) found = block.dataset.block;
  return found;
}

let settingsScrollTarget = null;
let settingsSettleTimer = null;
let settingsSpyQueued = false;

/* 点目录滚过去以后，等滚动完全停下来再按真实位置定一次高亮；
   这一段路途中先不跟，免得高亮在中间那几块之间闪来闪去。 */
function armSettingsSettle() {
  clearTimeout(settingsSettleTimer);
  settingsSettleTimer = setTimeout(() => {
    settingsScrollTarget = null;
    trackSettingsBlock();
  }, 200);
}

function trackSettingsBlock() {
  if (state.page !== 'settings') return;
  if (settingsScrollTarget) { armSettingsSettle(); return; }
  const name = currentSettingsBlock();
  if (name === state.settingsBlock) return;
  state.settingsBlock = name;
  highlightSettingsBlock(name);
  rememberPlace();
  if (name === 'diagnostics' && !state.inventory) refreshInventory();
}

function showSettingsBlock(name, {scroll = true} = {}) {
  if (!SETTINGS_BLOCKS.includes(name)) return;
  state.settingsBlock = name;
  highlightSettingsBlock(name);
  if (name === 'diagnostics' && !state.inventory) refreshInventory();
  if (state.page === 'settings') rememberPlace();
  if (!scroll) return;
  const block = document.querySelector(`.settings-main > .set-block[data-block="${name}"]`);
  if (!block) return;
  settingsScrollTarget = name;
  block.scrollIntoView({behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start'});
  armSettingsSettle();
}

/* 进设置页先刷新常用内容，详细诊断在实际进入时读取。 */
function renderSettings() {
  renderConfigExport();
  renderAgents();
  refreshSettings();
  refreshTeam();
  refreshProgramUpdate();
  refreshLog();
  renderDiagnostics();
  if (state.settingsBlock === 'diagnostics' && !state.inventory) refreshInventory();
  renderMigrationStatus();
  highlightSettingsBlock(state.settingsBlock);
}

$('settings-nav').addEventListener('click', event => {
  const button = event.target.closest('[data-block]');
  if (button) showSettingsBlock(button.dataset.block);
});

addEventListener('scroll', () => {
  if (settingsSpyQueued) return;
  settingsSpyQueued = true;
  requestAnimationFrame(() => { settingsSpyQueued = false; trackSettingsBlock(); });
}, {passive: true});
addEventListener('resize', trackSettingsBlock);

document.querySelector('.nav').addEventListener('click', event => {
  const button = event.target.closest('.nav-item');
  if (button) showPage(button.dataset.page);
});

function renderActionBar() {
  if (state.page === 'skills') return renderSkillsActionBar();
  if (state.page === 'cleanup') return renderCleanupActionBar();
  return hideActionBar();
}

/* ==================== 技能页：左边选 agent，右边是它的技能，改动先进底部托盘 ==================== */

const PREVIEW_DEBOUNCE_MS = 100;
let previewBusy = false;
let previewSequence = 0;
let previewTimer = null;

/* agent 的颜色：按表里的顺序从一组颜色里取，全页同一个 agent 用同一种颜色。 */
const AGENT_COLORS = ['#2F5BD3', '#C4552B', '#6D4BC2', '#0F7C6E', '#93650A', '#B0346B', '#3D7A1F', '#5B6B85'];
function agentColor(id) {
  const index = state.agents.findIndex(agent => agent.id === id);
  return AGENT_COLORS[(index < 0 ? state.agents.length : index) % AGENT_COLORS.length];
}
const swatch = id => `<span class="swatch" style="--c:${agentColor(id)}" aria-hidden="true"></span>`;

function currentAgent() {
  return state.agents.find(agent => agent.id === state.agent) || state.agents[0] || null;
}

const groupOf = name => state.groupIndex.get(name) || '未分组';

/* 搜索：名字、中文名、一句话、说明（含中文翻译）、标签里有就算。query 已转小写。 */
const matchesQuery = (skill, query) => !query || [skill.name, skill.title, skill.summary, skill.description, skill.description_zh, ...(skill.tags || [])]
  .some(text => (text || '').toLowerCase().includes(query));
const assignedNow = (name, agentId) => (state.saved[name] || []).includes(agentId);
const assignedAfter = (name, agentId) => (state.draft[name] || []).includes(agentId);

/* 每个 agent 每次新对话占用的 Token：现在的（已应用）和应用后的（草稿），已应用部分按分组分段。 */
function agentLoad(agentId) {
  const groups = new Map();
  let now = 0, after = 0, plus = 0, minus = 0, countNow = 0, countAfter = 0;
  for (const skill of state.table?.skills || []) {
    const was = assignedNow(skill.name, agentId), will = assignedAfter(skill.name, agentId);
    const cost = skill.description_tokens || 0;
    if (was) { now += cost; countNow++; }
    if (will) { after += cost; countAfter++; }
    if (was && will) groups.set(groupOf(skill.name), (groups.get(groupOf(skill.name)) || 0) + cost);
    if (!was && will) plus += cost;
    if (was && !will) minus += cost;
  }
  return {groups, now, after, plus, minus, countNow, countAfter};
}

const TOKEN_HINT = '每开一次新对话，agent 都要先读一遍分给它的技能说明，大约占这么多 Token。按说明长度粗算，只用来互相比较。';

/* 条的长度取各 agent 里最长的，向上取到 2k 的整数倍；每 5k 一条刻度。 */
function loadMax(loads) {
  const top = Math.max(2000, ...Object.values(loads).flatMap(load => [load.now, load.after]));
  return Math.ceil(top / 2000) * 2000;
}

/* 占用条：已应用的按分组分段；没应用的改动里，加上的部分是黄色斜纹，撤下的部分是空心斜纹。 */
function loadHtml(agentId, load, max, big = false) {
  const pct = value => `${(value / max * 100).toFixed(2)}%`;
  const segs = [...load.groups].sort((a, b) => b[1] - a[1]).map(([group, cost]) =>
    `<span class="load-seg" style="width:${pct(cost)}" title="${escape(group)}：${tokenText(cost)} Token"></span>`).join('');
  const minus = load.minus ? `<span class="load-minus" style="width:${pct(load.minus)}" title="会撤下 ${tokenText(load.minus)} Token"></span>` : '';
  const plus = load.plus ? `<span class="load-plus" style="width:${pct(load.plus)}" title="会加上 ${tokenText(load.plus)} Token"></span>` : '';
  const ticks = [];
  for (let at = 5000; at < max; at += 5000) ticks.push(`<i style="left:${pct(at)}"></i>`);
  const delta = load.after - load.now;
  const deltaText = delta ? ` → <span class="delta">${tokenText(load.after)}（${delta > 0 ? '+' : '−'}${tokenText(Math.abs(delta))}）</span>` : '';
  return `<div class="load${big ? ' load-big' : ''}" style="--c:${agentColor(agentId)}">
    <div class="load-track" role="img" aria-label="现在占用 ${tokenText(load.now)} Token${delta ? `，应用后 ${tokenText(load.after)} Token` : ''}">${segs}${minus}${plus}<span class="load-ticks">${ticks.join('')}</span></div>
    <span class="load-text"${big ? '' : ` title="${escape(TOKEN_HINT)}"`}>${big ? '每次新对话占用约' : '占用'} <b>${tokenText(load.now)} Token</b>${deltaText}${big ? hint(TOKEN_HINT) : ''}</span>
  </div>`;
}

/* 左栏：每个 agent 一行，带 Token 占用条。选中的那个决定右边显示谁的技能。 */
function renderAgentSummary() {
  const rail = $('agent-rail');
  if (!state.table || !state.agents.length) {
    rail.innerHTML = '<p class="item-note">正在读取各 agent…</p>';
    return;
  }
  const loads = Object.fromEntries(state.agents.map(agent => [agent.id, agentLoad(agent.id)]));
  const max = loadMax(loads);
  const current = currentAgent()?.id;
  renderButtons(rail, state.agents.map(agent => {
    const load = loads[agent.id];
    const count = load.countAfter !== load.countNow ? `${load.countNow} → ${load.countAfter} 个` : `${load.countNow} 个`;
    return `<button class="rail-item" data-pick-agent="${escape(agent.id)}" aria-current="${agent.id === current}">
      <span class="rail-name"><span>${swatch(agent.id)}${escape(agent.name)}</span><small>${count}</small></span>
      ${loadHtml(agent.id, load, max)}
    </button>`;
  }).join(''), 'data-pick-agent');
}

function pickAgent(id) {
  state.agent = id;
  rememberPlace();
  renderAgentSummary();
  renderSkillTable();
  renderInstallRail();   // 「装给谁」和「全部装给」跟着当前 agent 换
}

/* 左栏的 SP 新版提醒：和「设置 → 版本」共用同一份 programUpdate，
   在哪边升级两边都跟着变。已是最新、还没查到、正在检查、只读预览时都不出这张卡片。 */
function railUpdateNote() {
  const progress = programUpdate;
  if (!progress.write_allowed) return '';
  const lock = writeBusy ? ' disabled' : '';
  const button = (action, label) => `<button class="btn btn-small btn-primary" data-program-rail="${action}"${lock}>${label}</button>`;
  if (progress.phase === 'ready') {
    return `<div class="rail-note">
      <p class="rail-note-title">${icon('check-circle')}<strong>升级好了</strong></p>
      <div class="rail-note-row"><small>刷新页面就能用新版</small>${button('refresh', '刷新生效')}</div>
    </div>`;
  }
  if (progress.running && progress.phase === 'apply') {
    return `<div class="rail-note">
      <p class="rail-note-title">${icon('update-up')}<strong>正在升级…</strong></p>
      <div class="rail-note-row"><small>等当前操作完成后自动继续</small><button class="btn btn-small" disabled>升级</button></div>
    </div>`;
  }
  if (progress.available) {
    const latest = progress.latest ? new Date(progress.latest).toLocaleDateString('zh-CN') : '';
    return `<div class="rail-note">
      <p class="rail-note-title">${icon('update-up')}<strong>SP 有新版</strong></p>
      <div class="rail-note-row">${latest ? `<small>新版 ${escape(latest)}</small>` : ''}${button('apply', '升级')}</div>
    </div>`;
  }
  if (progress.phase === 'error' && programFailedAction === 'apply') {
    return `<div class="rail-note">
      <p class="rail-note-title">${icon('warning')}<strong>升级没完成</strong></p>
      <div class="rail-note-row"><small>${escape(progress.error || '稍后再试一次')}</small>${button('apply', '升级')}</div>
    </div>`;
  }
  return '';
}

/* 左栏下面的提醒：SP 有新版在最上面，然后是可以直接接入的新 agent。 */
let railNotesHtml = null;

function agentJoinButton(agent) {
  const busy = Boolean(writeBusy) && joiningAgent?.id === agent.id;
  const label = busy ? joiningAgent.text : '接入';
  return `<button class="btn btn-small btn-primary" data-agent-join="${escape(agent.id)}"
    aria-label="${escape(label + ' ' + agent.name)}" aria-busy="${busy}"${writeBusy || !state.table?.write_allowed ? ' disabled' : ''}>${label}</button>`;
}

function renderRailNotes() {
  const ignored = new Set((state.agentsView?.ignored || []).map(item => (item && typeof item === 'object') ? item.id : item));
  const suggested = (state.agentsView?.suggested || []).filter(item => !ignored.has(item.id));
  const notes = [railUpdateNote(), ...suggested.map(item => `<div class="rail-note">
      <p class="rail-note-title">${icon('plus')}<strong>发现新 agent：${escape(item.name)}</strong></p>
      <div class="rail-note-row">
        ${item.skill_count ? `<small>${number(item.skill_count)} 个技能</small>` : ''}${hint('接入后一起管理技能；同名内容沿用统一仓库里的版本，完成后可以撤销。')}
        ${agentJoinButton(item)}
      </div>
    </div>`)];
  const html = notes.filter(Boolean).join('');
  // 内容没变就不重画：自动检查在跑时每 1.5 秒读一次，别把正在点的按钮换掉。
  if (html === railNotesHtml) return;
  railNotesHtml = html;
  $('rail-notes').innerHTML = html;
}

/* 这个格子的说明：有草稿看预览给的 cell_notes，没草稿看表里带的。 */
function cellNote(skill, agent) {
  const notes = state.pending ? state.pending.cell_notes?.[skill.name] : skill.cell_notes;
  return notes?.[agent.id] || '';
}

/* 依赖自动带上的：后台给的 auto_added 直接说清是哪个技能需要它。 */
function autoAddedBy(skillName, agentId) {
  return state.autoAdded.get(skillName + '|' + agentId) || '';
}

function pendingNote(name, agentId) {
  const because = autoAddedBy(name, agentId);
  if (!assignedAfter(name, agentId)) return '待应用：会撤下，文件还留着';
  return because ? `待应用：自动带上，因为「${titleOf(because)}」要用它` : '待应用：会加上';
}

function switchHtml(skill, agent, on, pending, locked) {
  return `<button class="switch${pending ? ' is-pending' : ''}" role="switch" aria-checked="${on}" style="--c:${agentColor(agent.id)}"
        data-skill="${escape(skill.name)}" data-agent="${escape(agent.id)}"
        aria-label="${escape(agent.name + ' 使用 ' + titleOf(skill.name))}" ${locked ? 'disabled' : ''}></button>`;
}

function skillRowHtml(skill, agent) {
  const on = assignedAfter(skill.name, agent.id);
  const pending = on !== assignedNow(skill.name, agent.id);
  const error = state.pending?.conflicts?.find(c => c.skill === skill.name && c.agent === agent.id)?.message;
  const note = cellNote(skill, agent);
  const locked = Boolean(writeBusy) || !state.table.write_allowed;
  const status = error ? `<span class="row-note is-error">${escape(error)}</span>`
    : pending ? `<span class="row-note is-pending">${escape(pendingNote(skill.name, agent.id))}</span>`
    : note ? `<span class="row-note">${escape(note)}</span>` : '';
  // 小标记：图标加两三个字，完整的意思在鼠标移上去的提示里；详情里也有同样的信息。
  const needs = skill.requires.length;
  const flags = [
    skill.local_modified ? `<span class="flag" data-hint="你在这台电脑上改过它，更新时会问你保留哪一份。">${icon('edit')}你改过</span>` : '',
    needs ? `<span class="flag" role="img" aria-label="${escape(`会一起带上 ${needs} 个别的技能`)}" data-hint="${escape(`给 agent 加上它时，会自动一起加上 ${needs} 个别的技能。`)}">${icon('link')}${needs}</span>` : '',
  ].join('');
  const tags = (skill.tags || []).map(tag => tagPickHtml(tag)).join('');
  const sum = skill.summary || skill.description;
  return `<div class="skill-row${pending ? ' is-pending' : ''}" data-row="${escape(skill.name)}">
    <div class="skill-cell">
      <button class="skill-name" data-open="${escape(skill.name)}"><span class="t">${escape(skill.title || skill.name)}</span>${skill.title ? `<span class="skill-id">${escape(skill.name)}</span>` : ''}${flags}</button>
      <span class="skill-sub"><span class="skill-sum" title="${escape(sum)}">${escape(sum)}</span>${tags ? `<span class="row-tags">${tags}</span>` : ''}</span>
      ${status}
    </div>
    <span class="meta num" data-hint="${escape(TOKEN_HINT)}">${tokenText(skill.description_tokens)} Token</span>
    <span class="row-end">
      ${updateRow(skill.name) ? `<button class="update-up" data-update-up="${escape(skill.name)}" title="有新版，点一下更新"
        aria-label="${escape('更新「' + titleOf(skill.name) + '」')}" ${writeBusy ? 'disabled' : ''}><svg class="btn-icon" aria-hidden="true" focusable="false"><use href="#sp-update-up"/></svg></button>` : ''}
      ${switchHtml(skill, agent, on, pending, locked)}
    </span>
  </div>`;
}

/* 小标签：点一下按它筛，再点取消。行里、详情里、筛选条里都是同一个意思。 */
const tagPickHtml = tag => `<button class="tag-pick" data-tag-pick="${escape(tag)}" aria-pressed="${state.tag === tag}">${escape(tag)}</button>`;

/* 用户标签：标签由用户自己加，详情里改一个技能，结果标题那一行批量改。
   接口只返回这次动过的技能；拿到后只更新本地这一份数据，不重新读整张表。 */
let detailTagBusy = false;
let tagDialogNames = [];
let tagDialogBusy = false;
let listedSkillNames = [];

function skillByName(name) {
  return (state.table?.skills || []).find(skill => skill.name === name) || state.skillIndex.get(name) || null;
}

/* 和后台一致：去掉首尾空白及开头的半角或全角井号，其余交给后台校验。 */
function cleanTagInput(value) {
  return String(value ?? '').trim().replace(/^[#＃\s]+/, '').trim();
}

const tagKey = value => String(value ?? '').toLocaleLowerCase();

/* 所有技能里已经用过的标签，给输入框的候选提示；英文大小写只留第一次出现的写法。 */
function usedTags() {
  const seen = new Set();
  const list = [];
  for (const skill of state.table?.skills || []) {
    for (const tag of skill.tags || []) {
      const key = tagKey(tag);
      if (seen.has(key)) continue;
      seen.add(key);
      list.push(tag);
    }
  }
  return list.sort((a, b) => a.localeCompare(b, 'zh-CN'));
}

function renderUsedTags() {
  const list = $('used-tags');
  if (!list) return;
  list.innerHTML = usedTags().map(tag => `<option value="${escape(tag)}"></option>`).join('');
}

/* 后台保存时会沿用已有标签的写法，提示里的标签名也跟着它走。 */
function canonicalTag(value) {
  const key = tagKey(cleanTagInput(value));
  for (const skill of state.table?.skills || []) {
    const existing = (skill.tags || []).find(tag => tagKey(tag) === key);
    if (existing) return existing;
  }
  return cleanTagInput(value);
}

function canonicalTagFromResult(result, value) {
  const key = tagKey(cleanTagInput(value));
  for (const list of Object.values(result?.tags || {})) {
    const found = list.find(tag => tagKey(tag) === key);
    if (found) return found;
  }
  return canonicalTag(value);
}

/* 只改本地这份列表；选中的标签已经没人用了，就回到“不限标签”，不留 0 个结果。 */
function applyTagResult(result) {
  for (const [name, tags] of Object.entries(result?.tags || {})) {
    const skill = skillByName(name);
    if (skill) skill.tags = Array.isArray(tags) ? [...tags] : [];
  }
  if (state.tag && !(state.table?.skills || []).some(skill => (skill.tags || []).includes(state.tag))) state.tag = '';
  renderUsedTags();
  renderSkillTable();
  if (state.detail) renderDetail();
}

/* 详情里的一句话下面：标签本身照常能点了筛选，右边多一个小的去掉按钮。
   只读时不出现输入框和去掉按钮，标签照常显示。 */
function detailTagsHtml(skill, locked) {
  const canWrite = Boolean(state.table?.write_allowed);
  const tags = (skill.tags || []).map(tag => `<span class="detail-tag">
      ${tagPickHtml(tag)}
      ${canWrite ? `<button type="button" class="tag-remove" data-tag-remove="${escape(tag)}"
        aria-label="${escape(`去掉标签「${tag}」`)}" title="${escape(`去掉标签「${tag}」`)}"${locked ? ' disabled' : ''}>${icon('close')}</button>` : ''}
    </span>`).join('');
  const tagList = tags ? `<div class="detail-tags">${tags}</div>` : '';
  if (!canWrite) return tagList;
  return `${tagList}
    <form id="detail-tag-form" class="tag-editor" novalidate>
      <div class="tag-editor-row">
        <input id="detail-tag-input" class="input" list="used-tags" autocomplete="off" placeholder="加个标签，比如项目名" aria-label="新标签"${locked ? ' disabled' : ''}>
        <button id="detail-tag-add" class="btn btn-small" type="submit" disabled>加上</button>
        ${hint('标签由你自己打，用来按项目、客户这类事找技能，可以不打。')}
      </div>
      <p id="detail-tag-error" class="row-note is-error" role="alert" hidden></p>
    </form>`;
}

function detailTagLocked() {
  return detailTagBusy || Boolean(writeBusy) || !state.table?.write_allowed;
}

function updateDetailTagAdd() {
  const input = $('detail-tag-input');
  const button = $('detail-tag-add');
  if (!input || !button) return;
  button.disabled = detailTagLocked() || !cleanTagInput(input.value);
}

function setDetailTagBusy(busy) {
  detailTagBusy = busy;
  const body = $('detail-body');
  if (!body) return;
  const locked = detailTagLocked();
  const input = $('detail-tag-input');
  if (input) input.disabled = locked;
  for (const button of body.querySelectorAll('[data-tag-remove]')) button.disabled = locked;
  updateDetailTagAdd();
}

function showDetailTagError(message) {
  const error = $('detail-tag-error');
  if (!error) return;
  error.textContent = message;
  error.hidden = false;
}

async function writeDetailTag(action, value) {
  if (detailTagBusy || writeBusy || !state.detail || !state.table?.write_allowed) return;
  const name = state.detail;
  if (!cleanTagInput(value)) {
    showDetailTagError('请先写一个标签。');
    $('detail-tag-input')?.focus();
    return;
  }
  $('detail-tag-error').hidden = true;
  setDetailTagBusy(true);
  try {
    const result = await api('/api/tags', action === 'add' ? {skills: [name], add: value} : {skills: [name], remove: value});
    detailTagBusy = false;
    applyTagResult(result);
    if (state.detail === name) $('detail-tag-input')?.focus();
  } catch (error) {
    setDetailTagBusy(false);
    if (state.detail === name) {
      showDetailTagError(error.message);
      $('detail-tag-input')?.focus();
    } else {
      toast({text: error.message, tone: 'error'});
    }
  }
}

/* 批量窗口：勾选当前列表里的技能，默认全选；没填标签或没勾选都不能提交。 */
function tagDialogChecked() {
  return [...$('tag-dialog-skills').querySelectorAll('input[data-tag-skill]:checked')].map(input => input.dataset.tagSkill);
}

function renderTagDialogControls() {
  const checked = tagDialogChecked();
  const tag = cleanTagInput($('tag-dialog-input').value);
  const key = tagKey(tag);
  const hasTag = Boolean(tag) && checked.some(name => (skillByName(name)?.tags || []).some(item => tagKey(item) === key));
  $('tag-dialog-count').textContent = `已选 ${checked.length} 个`;
  $('tag-dialog-add').disabled = tagDialogBusy || !tag || !checked.length;
  $('tag-dialog-remove').disabled = tagDialogBusy || !tag || !checked.length || !hasTag;
  $('tag-dialog-close').disabled = tagDialogBusy;
  $('tag-dialog-input').disabled = tagDialogBusy;
  for (const input of $('tag-dialog-skills').querySelectorAll('input[data-tag-skill]')) input.disabled = tagDialogBusy;
}

function openTagDialog() {
  if (writeBusy || !state.table?.write_allowed || listedSkillNames.length < 2) return;
  tagDialogNames = listedSkillNames.filter(name => skillByName(name));
  if (tagDialogNames.length < 2) return;
  renderUsedTags();
  $('tag-dialog-title').textContent = `给这 ${tagDialogNames.length} 个技能打标签`;
  $('tag-dialog-input').value = '';
  $('tag-dialog-error').hidden = true;
  $('tag-dialog-skills').innerHTML = tagDialogNames.map(name => `<label class="check tag-skill-row">
      <input type="checkbox" data-tag-skill="${escape(name)}" checked>
      <span class="tag-skill-name">${nameHtml(name)}</span>
    </label>`).join('');
  tagDialogBusy = false;
  renderTagDialogControls();
  $('tag-dialog').showModal();
  $('tag-dialog-input').focus();
}

async function applyBatchTag(action) {
  if (tagDialogBusy) return;
  const names = tagDialogChecked();
  const value = $('tag-dialog-input').value;
  const tag = cleanTagInput(value);
  if (!tag || !names.length) return;
  const button = action === 'add' ? $('tag-dialog-add') : $('tag-dialog-remove');
  tagDialogBusy = true;
  $('tag-dialog-error').hidden = true;
  renderTagDialogControls();
  try {
    const result = await withBusy(button, '正在保存…', () => api('/api/tags',
      action === 'add' ? {skills: names, add: value} : {skills: names, remove: value}));
    const canonical = canonicalTagFromResult(result, value);
    tagDialogBusy = false;
    $('tag-dialog').close();
    applyTagResult(result);
    toast({text: action === 'add'
      ? `已给 ${names.length} 个技能加上「${canonical}」`
      : `已从 ${names.length} 个技能去掉「${canonical}」`});
  } catch (error) {
    tagDialogBusy = false;
    $('tag-dialog-error').textContent = error.message;
    $('tag-dialog-error').hidden = false;
    renderTagDialogControls();
    $('tag-dialog-input').focus();
  } finally {
    tagDialogBusy = false;
    renderTagDialogControls();
  }
}

$('tags-batch').addEventListener('click', openTagDialog);
$('tag-dialog-add').addEventListener('click', () => applyBatchTag('add'));
$('tag-dialog-remove').addEventListener('click', () => applyBatchTag('remove'));
$('tag-dialog-close').addEventListener('click', () => { if (!tagDialogBusy) $('tag-dialog').close(); });
$('tag-dialog-input').addEventListener('input', () => {
  $('tag-dialog-error').hidden = true;
  renderTagDialogControls();
});
$('tag-dialog-skills').addEventListener('change', renderTagDialogControls);
$('tag-dialog').addEventListener('cancel', event => { if (tagDialogBusy) event.preventDefault(); });
$('tag-dialog').querySelector('form').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('tag-dialog-add').disabled) applyBatchTag('add');
});

/* 分组：组按这次列出的技能数排，「未分组」放最后；组内按中文名排。
   列表按已应用的分配分“在用 / 可以加的”，开关拨了也不会跳到另一边。 */
function groupedRows(rows) {
  const groups = new Map();
  for (const skill of rows) {
    const name = groupOf(skill.name);
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(skill);
  }
  return [...groups].map(([name, skills]) => ({name, skills: skills.sort((a, b) => titleOf(a.name).localeCompare(titleOf(b.name), 'zh-CN'))}))
    .sort((a, b) => (a.name === '未分组') - (b.name === '未分组') || b.skills.length - a.skills.length);
}

/* 左侧目录：分组纵向排列，标签直接浏览。
   每个分组的数字是点了它会看到几个：按当前的“在用 / 可以加的”和搜索算，再叠上已选的标签；
   标签的数字则叠上已选的分组。已选中的就算数字是 0 也留着，免得点完不知道怎么退。 */
function filtersHtml({skills, inTab, withUpdate, scope, hasTag, inChip}) {
  const count = (rows, keysOf) => {
    const counts = new Map();
    for (const row of rows) for (const key of keysOf(row)) counts.set(key, (counts.get(key) || 0) + 1);
    return counts;
  };
  const option = (attr, value, label, n, pressed, extra = '') =>
    `<button type="button" class="filter-option${extra}" ${attr}="${escape(value)}" aria-pressed="${pressed}" title="${escape(label)}${pressed && value ? '；再点一下取消' : ''}"><span class="filter-name">${escape(label)}</span><span class="n">${n}</span></button>`;

  const groups = count(inTab.filter(hasTag), skill => [groupOf(skill.name)]);
  if (state.chip && state.chip !== UPDATES_CHIP && !groups.has(state.chip)) groups.set(state.chip, 0);
  const several = new Set(skills.map(skill => groupOf(skill.name))).size > 1;
  const groupRow = `<div class="filter-group-list" data-filter-list="groups" role="group" aria-label="按分组看">
      ${option('data-chip', '', '全部技能', inTab.filter(hasTag).length, !state.chip)}
      ${withUpdate.length || state.chip === UPDATES_CHIP ? option('data-chip', UPDATES_CHIP, '有新版', withUpdate.filter(hasTag).length, state.chip === UPDATES_CHIP, ' filter-update') : ''}
      ${several ? [...groups].sort(([a, x], [b, y]) => (a === '未分组') - (b === '未分组') || y - x)
        .map(([name, n]) => option('data-chip', name, name, n, state.chip === name)).join('') : ''}
    </div>`;

  const tags = count(scope.filter(inChip), skill => skill.tags || []);
  if (state.tag && !tags.has(state.tag)) tags.set(state.tag, 0);
  const tagRow = skills.some(skill => skill.tags?.length) || state.tag ? `<section class="filter-tags" aria-labelledby="filter-tags-title">
    <h3 id="filter-tags-title">标签</h3>
    <div class="filter-tag-list" data-filter-list="tags" role="group" aria-label="按标签看">
      ${option('data-tag-chip', '', '不限标签', scope.filter(inChip).length, !state.tag, ' filter-tag-option')}
      ${[...tags].sort(([a, x], [b, y]) => y - x || a.localeCompare(b, 'zh-CN')).map(([name, n]) => option('data-tag-chip', name, name, n, state.tag === name, ' filter-tag-option')).join('')}
    </div>
    <p id="filter-tags-empty" class="filter-empty" role="status" hidden>没有可选标签</p>
  </section>` : '';
  return several || withUpdate.length || state.chip || tagRow ? groupRow + tagRow : '';
}

let filterLayoutMotion = null;

function renderFilterLayout() {
  const available = Boolean($('skills-filters').innerHTML);
  const collapsed = !available || state.filtersCollapsed;
  const browser = $('skills-browser');
  const results = browser.querySelector('.skills-results');
  const changing = browser.classList.contains('without-filters') !== collapsed;
  const before = changing ? results.getBoundingClientRect() : null;
  if (changing) filterLayoutMotion?.cancel();
  $('skills-filter-slot').hidden = !available;
  $('skills-filter-rail').hidden = !available;
  $('skills-filter-rail').inert = collapsed;
  $('skills-filter-rail').setAttribute('aria-hidden', String(collapsed));
  $('filters-show').hidden = !available || !state.filtersCollapsed;
  browser.classList.toggle('without-filters', collapsed);
  if (before && !prefersReducedMotion()) {
    const distance = before.left - results.getBoundingClientRect().left;
    filterLayoutMotion = results.animate([{transform: `translateX(${distance}px)`}, {transform: 'none'}],
      {duration: 300, easing: 'cubic-bezier(0.2, 0.65, 0.3, 1)'});
  }
}

function fitFilterRail() {
  const slot = $('skills-filter-slot');
  if (slot.hidden || state.page !== 'skills') return;
  const top = Math.max(16, slot.getBoundingClientRect().top);
  slot.style.setProperty('--filter-space', `${Math.max(0, innerHeight - top - 16)}px`);
}

addEventListener('scroll', fitFilterRail, {passive: true});
addEventListener('resize', fitFilterRail);

/* 重画筛选时保留焦点和目录滚动位置。 */
function renderSkillFilters(options) {
  const filters = $('skills-filters');
  const active = document.activeElement;
  const selector = filters.contains(active) && active.hasAttribute('data-chip') ? `[data-chip="${CSS.escape(active.dataset.chip)}"]`
    : filters.contains(active) && active.hasAttribute('data-tag-chip') ? `[data-tag-chip="${CSS.escape(active.dataset.tagChip)}"]` : null;
  const scrolls = [...filters.querySelectorAll('[data-filter-list]')].map(list => [list.dataset.filterList, list.scrollTop]);
  const template = document.createElement('template');
  template.innerHTML = filtersHtml(options);
  const lists = [...template.content.querySelectorAll('[data-filter-list]')];
  if (lists.length && lists.length === filters.querySelectorAll('[data-filter-list]').length) {
    for (const next of lists) {
      const list = filters.querySelector(`[data-filter-list="${next.dataset.filterList}"]`);
      renderButtons(list, next.innerHTML, next.dataset.filterList === 'groups' ? 'data-chip' : 'data-tag-chip');
    }
  } else filters.replaceChildren(template.content);
  for (const list of filters.querySelectorAll('[data-filter-list]')) glideSelection(list);
  const empty = $('filter-tags-empty');
  if (empty) empty.hidden = Boolean(filters.querySelector('[data-tag-chip]:not([data-tag-chip=""])'));
  renderFilterLayout();
  for (const [name, top] of scrolls) filters.querySelector(`[data-filter-list="${name}"]`)?.scrollTo(0, top);
  if (selector) {
    const target = filters.querySelector(selector);
    target?.focus({preventScroll: true});
  }
}

let skillScope = '', skillListMotion = null;

function animateSkillResults(agent) {
  const scope = JSON.stringify([agent.id, state.tab, state.chip, state.tag, $('skills-search').value]);
  if (skillScope === scope) return;
  const list = $('skills-list');
  const opacity = getComputedStyle(list).opacity;
  const transform = getComputedStyle(list).transform;
  skillListMotion?.cancel();
  if (skillScope && skillScope !== scope && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
    skillListMotion = list.animate([
      {opacity: Math.min(Number(opacity), 0.88), transform: transform === 'none' ? 'translateY(5px)' : transform},
      {opacity: 1, transform: 'none'},
    ], {duration: 240, easing: 'cubic-bezier(0.2, 0.65, 0.3, 1)'});
  }
  skillScope = scope;
}

/* 同一批技能只更新变动的格子，保留开关节点，滑块才能走完过渡。 */
function renderSkillList(markup) {
  const list = $('skills-list');
  const template = document.createElement('template');
  template.innerHTML = markup;
  const current = [...list.children], next = [...template.content.children];
  const key = node => node.dataset.row ? `skill:${node.dataset.row}` : `group:${node.querySelector('strong')?.textContent}`;
  if (current.length !== next.length || current.some((node, index) => key(node) !== key(next[index]))) {
    list.replaceChildren(template.content);
    return;
  }
  current.forEach((row, index) => {
    const updated = next[index];
    row.className = updated.className;
    if (!row.dataset.row) { if (row.innerHTML !== updated.innerHTML) row.innerHTML = updated.innerHTML; return; }
    for (let cell = 0; cell < 2; cell++) {
      if (row.children[cell].innerHTML !== updated.children[cell].innerHTML) row.children[cell].innerHTML = updated.children[cell].innerHTML;
    }
    renderButtons(row.querySelector('.row-end'), updated.querySelector('.row-end').innerHTML, 'data-skill');
  });
}

/* 右边顶部：agent 名字、技能数和生效方式、占用条。
   “和谁共用一个技能文件夹”要等 /api/agents 回来才知道，所以单独成一个函数，回来后再画一次。 */
function shareHtml(names, note) {
  if (!names.length) return '';
  return `<span class="agent-share">${icon('link')}和 ${escape(names.join('、'))} 共用${hint(note)}</span>`;
}

function renderAgentHead() {
  const agent = currentAgent();
  if (!state.table || !agent) { $('agent-head').innerHTML = ''; return; }
  const loads = Object.fromEntries(state.agents.map(item => [item.id, agentLoad(item.id)]));
  const load = loads[agent.id];
  const facts = [`${load.countNow} 个技能`, agent.activation_text || ''].filter(Boolean).join(' · ');
  const shared = (state.catalog.get(agent.id)?.shared_with || []).map(nameOf);
  // 后台写好的说明（共用时哪些技能会自动停用）挑出提到这个 agent 的几句
  const notes = (state.table.notes || []).filter(note => note.includes(agent.name)).join('');
  const sharing = shareHtml(shared, notes || `${agent.name} 和 ${shared.join('、')} 读的是同一个技能文件夹。`);
  $('agent-head').innerHTML = `<h2>${swatch(agent.id)}${escape(agent.name)}</h2>
    <p><span>${escape(facts)}</span>${sharing}</p>
    ${loadHtml(agent.id, load, loadMax(loads), true)}`;
}

function renderSkillTable() {
  const agent = currentAgent();
  if (!state.table || !agent) {
    $('agent-head').innerHTML = '';
    $('skills-tabs').innerHTML = '';
    $('skills-filters').innerHTML = '';
    $('skills-list').innerHTML = '<p class="empty">正在读取技能列表…</p>';
    listedSkillNames = [];
    $('tags-batch').hidden = true;
    return;
  }
  renderAgentHead();

  const skills = state.table.skills;
  const nOn = skills.filter(skill => assignedNow(skill.name, agent.id)).length;
  renderButtons($('skills-tabs'), `<button class="seg" data-tab="on" aria-pressed="${state.tab === 'on'}">在用<span class="n">${nOn}</span></button>
    <button class="seg" data-tab="off" aria-pressed="${state.tab === 'off'}">可以加的<span class="n">${skills.length - nOn}</span></button>`, 'data-tab');
  $('skills-search').placeholder = `在 ${agent.name} 的技能里搜`;

  const query = $('skills-search').value.trim().toLowerCase();
  const matches = skill => matchesQuery(skill, query);
  const pending = skills.filter(skill => updateRow(skill.name));
  if (state.chip === UPDATES_CHIP && !pending.length) state.chip = '';   // 都更新完了，回到全部
  const inTab = skills.filter(skill => (state.tab === 'on') === assignedNow(skill.name, agent.id)).filter(matches);
  const withUpdate = pending.filter(matches);   // 有新版的不分“在用 / 可以加的”
  const scope = state.chip === UPDATES_CHIP ? withUpdate : inTab;
  const hasTag = skill => !state.tag || (skill.tags || []).includes(state.tag);
  const inChip = skill => !state.chip || state.chip === UPDATES_CHIP || groupOf(skill.name) === state.chip;
  const rows = scope.filter(inChip).filter(hasTag);
  $('skills-browser').style.setProperty('--c', agentColor(agent.id));
  renderSkillFilters({skills, inTab, withUpdate, scope, hasTag, inChip});
  $('skills-result-title').textContent = [state.chip === UPDATES_CHIP ? '有新版' : state.chip || '全部技能', state.tag].filter(Boolean).join(' · ');
  $('skills-result-count').textContent = `${rows.length} 个技能`;
  $('skills-clear-filters').hidden = !state.chip && !state.tag;

  // 空的时候只说一句话，需要下一步的给一个按钮，不写操作说明
  const empty = query ? emptyInner('search', '没找到，换个词试试')
    : state.chip || state.tag ? emptyInner('skills', '这一类里没有技能', '<button class="link-btn" data-empty="all">看全部</button>')
    : state.tab === 'on' ? emptyInner('skills', `${agent.name} 还没有技能`, '<button class="link-btn" data-empty="off">去「可以加的」</button>')
    : emptyInner('check-circle', '都已经加上了');
  renderSkillList(groupedRows(rows).map(group =>
    `<div class="list-group-head"><strong>${escape(group.name)}</strong><span>${group.skills.length} 个</span></div>`
    + group.skills.map(skill => skillRowHtml(skill, agent)).join('')).join('') || `<div class="empty">${empty}</div>`);
  listedSkillNames = rows.map(skill => skill.name);
  $('tags-batch').hidden = !state.table.write_allowed || listedSkillNames.length < 2;
  $('tags-batch').disabled = Boolean(writeBusy);
  animateSkillResults(agent);
  $('labels-hint').hidden = Boolean(state.table.groups?.length) || state.table.llm_configured;
  fitFilterRail();
}

function pendingChanges() {
  if (!state.table) return [];
  const changes = state.table.skills.flatMap(skill => state.agents
    .filter(agent => assignedAfter(skill.name, agent.id) !== assignedNow(skill.name, agent.id))
    .map(agent => ({skill: skill.name, agent: agent.id, enabled: assignedAfter(skill.name, agent.id)})));
  for (const change of state.pending?.changes || []) {
    if (!state.skillIndex.has(change.skill) && assignedAfter(change.skill, change.agent) !== assignedNow(change.skill, change.agent)) changes.push(change);
  }
  for (const intent of state.assignmentIntents.values()) {
    if (!changes.some(c => c.skill === intent.skill && c.agent === intent.agent) &&
        (state.pending?.conflicts || []).some(c => c.skill === intent.skill && c.agent === intent.agent)) changes.push(intent);
  }
  return changes;
}

/* “给 Claude 加 2 个、从 Codex 撤下 1 个”。 */
function changeSummary(changes) {
  const parts = [];
  for (const agent of state.agents) {
    const added = changes.filter(c => c.agent === agent.id && c.enabled).length;
    const removed = changes.filter(c => c.agent === agent.id && !c.enabled).length;
    if (added) parts.push(`给 ${agent.name} 加 ${added} 个`);
    if (removed) parts.push(`从 ${agent.name} 撤下 ${removed} 个`);
  }
  return parts.join('、');
}

/* 后台的说法是“将给 Claude 加 2 个”；做完以后改成过去时。 */
function pastTense(message) {
  return String(message || '').replace(/将给/g, '给').replace(/加 (\d+) 个/g, '加了 $1 个').replace(/撤下 (\d+) 个/g, '撤下了 $1 个');
}

/* 底部托盘：每一处改动一行，自动带上的写明原因；手动改的每处都能单独“不改这处”。 */
function renderSkillsActionBar() {
  const changes = pendingChanges();
  if (!changes.length || !state.table) return hideActionBar();
  const items = changes.map(change => {
    const agent = state.agents.find(item => item.id === change.agent);
    const because = autoAddedBy(change.skill, change.agent);
    const users = change.enabled ? [] : state.table.skills
      .filter(skill => skill.requires.includes(change.skill) && assignedAfter(skill.name, change.agent))
      .map(skill => `「${titleOf(skill.name)}」`);
    const conflict = state.pending?.conflicts?.find(c => c.skill === change.skill && c.agent === change.agent);
    const why = conflict ? `<small class="warn">${escape(conflict.message)}</small>`
      : because ? `<small>自动带上：「${escape(titleOf(because))}」要用它</small>`
      : users.length ? `<small class="warn">注意：${escape(users.join(''))}还要用它</small>` : '';
    return `<li><span class="action-agent">${swatch(change.agent)}${escape(agent?.name || change.agent)}</span>
      <p>${change.enabled ? '加上' : '<span class="verb-off">撤下</span>'}「${escape(titleOf(change.skill))}」${why}</p>
      ${because && !conflict ? '' : `<button class="btn btn-quiet btn-small" data-revert-skill="${escape(change.skill)}" data-revert-agent="${escape(change.agent)}">不改这处</button>`}</li>`;
  }).join('');
  const paths = state.pending?.path_warnings || [];
  const extra = paths.length ? `<details><summary>有 ${paths.length} 份你的文件里写着这些技能现在的位置，应用后位置会变，那些文件里的说法可能要改</summary>
      <ul>${paths.map(warning => `<li class="mono">${pathLabel(warning.file)}</li>`).join('')}</ul></details>` : '';
  const removing = changes.some(change => !change.enabled);
  showActionBar({
    text: `改了 ${changes.length} 处，还没生效`,
    note: state.pending?.conflicts?.length ? '需要处理的原因写在对应改动下，其他选择已保留。'
      : changeSummary(changes) + (removing ? '。撤下的技能文件还留着，随时能加回来' : ''),
    items,
    extra,
    buttons: [
      {label: '全部不改', kind: 'btn btn-quiet', action: () => discardPending()},
      {label: `应用这 ${changes.length} 处`, kind: 'btn btn-primary', disabled: previewBusy || Boolean(writeBusy) || !state.table.write_allowed, action: button => applyAssignment(button)},
    ],
  });
}

/* 拨开关：先改草稿并立刻重画（不等网络），再合并着发预览请求。
   每次拨动都会让在途的预览结果作废，所以旧结果永远不会盖掉新的改动；
   100 毫秒内的连续拨动还会合并成一次请求。 */
function invalidatePreview() {
  return ++previewSequence;
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, PREVIEW_DEBOUNCE_MS);
}

function renderAssignmentViews() {
  renderSkillTable();
  renderAgentSummary();
  renderSkillsActionBar();
  if (state.detail) renderDetail(true);
}

async function runPreview() {
  // 这次请求认领当前的序号；期间只要有人再拨，序号就会变，结果直接作废。
  const sequence = previewSequence;
  previewBusy = true;
  renderSkillsActionBar();
  try {
    const result = await api('/api/assignment/preview', {changes: [...state.assignmentIntents.values()], recheck: true});
    if (sequence !== previewSequence) return;
    acceptAssignmentPlan(result);
  } catch (error) {
    if (sequence !== previewSequence) return;
    // 网络或读取失败不撤销用户的选择；再次应用时会重新读取。
    state.pending ||= {changed: pendingChanges().map(c => c.skill)};
    toast({text: error.message, tone: 'error'});
  } finally {
    // 只有最新那次请求负责收起忙碌态；被作废的旧请求什么都不动。
    if (sequence === previewSequence) previewBusy = false;
    renderAssignmentViews();
  }
}

function acceptAssignmentPlan(plan) {
  state.pending = plan;
  state.saved = plan.base_assignments || state.saved;
  state.draft = plan.assignments;
  state.autoAdded = new Map((plan.auto_added || []).map(item => [item.skill + '|' + item.agent, item.because]));
  if (plan.changes?.length === 0 && !plan.conflicts?.length) state.assignmentIntents.clear();
}

function discardPending() {
  clearTimeout(previewTimer);
  invalidatePreview();
  previewBusy = false;
  state.draft = clone(state.saved);
  state.pending = null;
  state.assignmentIntents.clear();
  state.autoAdded = new Map();
  renderAssignmentViews();
}

function setDraft(name, agentId, enabled) {
  const selected = new Set(state.draft[name] || []);
  if (enabled) selected.add(agentId); else selected.delete(agentId);
  state.draft[name] = [...selected];
}

/* 关掉一个技能时，它自动带上的技能也一起退回原样，不留孤零零的依赖。 */
function revertAutos(name, agentId) {
  for (const [key, because] of [...state.autoAdded]) {
    const [skill, owner] = key.split('|');
    if (because !== name || owner !== agentId) continue;
    state.autoAdded.delete(key);
    setDraft(skill, owner, assignedNow(skill, owner));
    revertAutos(skill, owner);
  }
}

function changeAssignment(name, agentId, enabled) {
  if (writeBusy || !state.table?.write_allowed) return;
  const active = document.activeElement;
  const refocus = active?.dataset?.skill === name && active?.dataset?.agent === agentId
    ? (active.closest('#detail-body') ? '#detail-body' : '#skills-list') : null;
  setDraft(name, agentId, enabled);
  const key = name + '|' + agentId;
  if (enabled === assignedNow(name, agentId)) state.assignmentIntents.delete(key);
  else state.assignmentIntents.set(key, {skill: name, agent: agentId, enabled});
  if (!enabled) revertAutos(name, agentId);
  if (!pendingChanges().length) {
    discardPending();
  } else {
    // 先作废在途的预览结果，再记下这一次；这样第二次拨动不会被第一个请求的返回盖掉。
    invalidatePreview();
    renderAssignmentViews();
    schedulePreview();
  }
  if (refocus) document.querySelector(`${refocus} [data-skill="${CSS.escape(name)}"][data-agent="${CSS.escape(agentId)}"]`)?.focus();
}

const toggleAssignment = (name, agentId) => changeAssignment(name, agentId, !assignedAfter(name, agentId));
const revertChange = (name, agentId) => changeAssignment(name, agentId, assignedNow(name, agentId));

/* 分配确认：按 agent 列出具体技能名。只有撤下才多问一句，加技能直接应用、事后可撤销。 */
function assignmentGroups(changes) {
  const groups = [];
  for (const agent of state.agents) {
    const removed = changes.filter(c => c.agent === agent.id && !c.enabled).map(c => ({name: c.skill}));
    if (removed.length) groups.push({label: `从 ${agent.name} 撤下 ${removed.length} 个`, items: removed});
  }
  return groups;
}

async function applyAssignment(button) {
  if (!state.assignmentIntents.size) return;
  const previous = state.pending || {};
  const effectKey = c => c.skill + '|' + c.agent + '|' + c.enabled;
  const seenEffects = new Set((previous.changes || pendingChanges()).map(effectKey));
  const seenWarnings = new Set(previous.dependency_warnings || []);
  const seenPaths = new Set((previous.path_warnings || []).map(w => JSON.stringify(w)));
  clearTimeout(previewTimer);
  invalidatePreview();
  previewBusy = false;
  const written = await runWrite({
    button,
    busyText: '正在应用…',
    work: async () => {
      let consent = '';
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const plan = await api('/api/assignment/preview', {changes: [...state.assignmentIntents.values()], recheck: true});
          acceptAssignmentPlan(plan);
          renderAssignmentViews();
          if (plan.conflicts?.length) return {needs_attention: true, message: '有几处无法直接应用，原因已列在对应改动下；其他选择已保留。'};
          const changes = plan.changes || pendingChanges();
          const removed = changes.filter(c => !c.enabled);
          const extra = changes.filter(c => !seenEffects.has(effectKey(c)));
          const newWarnings = (plan.dependency_warnings || []).filter(w => !seenWarnings.has(w));
          const newPaths = (plan.path_warnings || []).filter(w => !seenPaths.has(JSON.stringify(w)));
          const newImpact = extra.length || newWarnings.length || newPaths.length;
          const review = JSON.stringify([changes, plan.dependency_warnings, plan.path_warnings]);
          if (changes.length && review !== consent && (removed.length || newImpact)) {
            const ok = await confirmDialog({
              title: newImpact ? '最新状态里有新的影响' : '撤下这些技能',
              lines: [changeSummary(changes) + (removed.length ? '。撤下的技能文件还留着，随时能加回来。' : ''), ...(plan.dependency_warnings || []), ...(newPaths.length ? ['新发现有文件引用会受影响，位置提示已保留在改动托盘里。'] : [])],
              groups: [...assignmentGroups(removed), ...(extra.filter(c => c.enabled).length ? [{label: '需要一起加上', items: extra.filter(c => c.enabled).map(c => ({name: c.skill, note: nameOf(c.agent)}))}] : [])],
              note: '你的选择已保留；取消也不用重选。应用后仍可撤销。',
              okText: newImpact ? '继续应用' : '撤下',
            });
            if (!ok) return {cancelled: true};
            consent = review;
          }
          const result = await api('/api/assignment/apply', {changes: [...state.assignmentIntents.values()], revision: plan.revision, recheck: true, confirmed: true});
          if (result.needs_attention) {
            acceptAssignmentPlan(result);
            renderAssignmentViews();
            return {...result, message: '有几处无法直接应用，原因已列在对应改动下；其他选择已保留。'};
          }
          state.assignmentIntents.clear();
          state.pending = null;
          await refreshEverything();
          return {...result, appliedSkills: plan.changed};
        } catch (error) {
          if (!error.recheck) throw error;
        }
      }
      return {needs_attention: true, message: '其他调整仍在进行，暂时无法保存。你的选择已保留，稍后再点应用即可。'};
    },
    success: result => result.cancelled ? null : result.needs_attention ? {text: result.message, tone: 'error', sticky: true} : ({
      text: pastTense(result.message || '技能分配已保存'),
      undo: result.id ? undoButton => undoOperation(result.id, {skills: result.appliedSkills || [], message: pastTense(result.message), button: undoButton}) : null,
    }),
  });
  if (written?.needs_attention) {
    $('action-summary').tabIndex = -1;
    $('action-summary').focus();
  }
}

$('skills-list').addEventListener('click', event => {
  const toggle = event.target.closest('button[data-skill][data-agent]');
  if (toggle) { toggleAssignment(toggle.dataset.skill, toggle.dataset.agent); return; }
  const up = event.target.closest('[data-update-up]');
  if (up) { clickUpdateUp(up.dataset.updateUp); return; }
  const tag = event.target.closest('[data-tag-pick]');
  if (tag) { toggleTag(tag.dataset.tagPick); return; }
  const empty = event.target.closest('[data-empty]');
  if (empty) {   // 空状态里的出路：看全部，或者去“可以加的”
    if (empty.dataset.empty === 'off') state.tab = 'off'; else { state.chip = ''; state.tag = ''; }
    renderSkillTable();
    return;
  }
  const open = event.target.closest('[data-open]');
  if (open) openDetail(open.dataset.open);
});

/* 筛选：点已选中的就取消。 */
function toggleTag(tag) {
  state.tag = state.tag === tag ? '' : tag;
  renderSkillTable();
}

/* 点完列表重画，把焦点还给同一颗，键盘用户不会丢位置。 */
$('skills-filters').addEventListener('click', event => {
  const group = event.target.closest('[data-chip]');
  const tag = event.target.closest('[data-tag-chip]');
  if (group) { state.chip = state.chip === group.dataset.chip ? '' : group.dataset.chip; renderSkillTable(); }
  else if (tag) toggleTag(tag.dataset.tagChip);
  else return;
  const selector = group ? `[data-chip="${CSS.escape(group.dataset.chip)}"]` : `[data-tag-chip="${CSS.escape(tag.dataset.tagChip)}"]`;
  $('skills-filters').querySelector(selector)?.focus({preventScroll: true});
});

for (const id of ['filters-hide', 'filters-show']) $(id).addEventListener('click', () => {
  state.filtersCollapsed = !state.filtersCollapsed;
  renderFilterLayout();
  $(state.filtersCollapsed ? 'filters-show' : 'filters-hide').focus({preventScroll: true});
});

$('skills-clear-filters').addEventListener('click', () => {
  state.chip = '';
  state.tag = '';
  renderSkillTable();
  ($('filters-show').hidden ? $('skills-filters').querySelector('[data-chip=""]') : $('filters-show'))?.focus({preventScroll: true});
});

$('agent-rail').addEventListener('click', event => {
  const button = event.target.closest('[data-pick-agent]');
  if (button) pickAgent(button.dataset.pickAgent);
});

$('skills-tabs').addEventListener('click', event => {
  const button = event.target.closest('[data-tab]');
  if (!button) return;
  state.tab = button.dataset.tab;
  renderSkillTable();
});

$('page-skills').addEventListener('click', event => {
  const target = event.target.closest('[data-goto]');
  if (target) gotoPlace(target.dataset.goto);
});

$('action-list').addEventListener('click', event => {
  const revert = event.target.closest('[data-revert-skill]');
  if (revert) { revertChange(revert.dataset.revertSkill, revert.dataset.revertAgent); return; }
  const unpick = event.target.closest('[data-cleanup-unpick]');
  if (unpick) { state.cleanupSelected.delete(unpick.dataset.cleanupUnpick); renderCleanup(); }
});

$('skills-search').addEventListener('input', renderSkillTable);

// 重新盘点是只有图标的按钮，所以不换文案，忙碌时图标自己转
$('skills-refresh').addEventListener('click', event => withBusy(event.currentTarget, '', async () => {
  await refreshInventory();
  await refreshTable(true);
}));

/* 新技能横幅：发现没收进仓库的，先在这里提醒。 */
function renderNewSkills() {
  const list = state.table?.new_skills || [];
  $('new-banner').hidden = !list.length;
  if (!list.length) return;
  $('new-text').innerHTML = `发现 ${list.length} 个新装的技能${hint('它们还没交给 Skill Pilot 管。收编后，就能在这里统一分配、检查更新。')}`;
  $('new-list').innerHTML = list.map(s => `<p>${escape(s.name)} <span class="mono muted">${pathLabel(s.relative)}</span></p>`).join('');
}


$('new-toggle').addEventListener('click', event => {
  const list = $('new-list');
  list.hidden = !list.hidden;
  event.currentTarget.setAttribute('aria-expanded', String(!list.hidden));
});

$('new-absorb').addEventListener('click', async event => {
  const button = event.currentTarget;
  const list = state.table.new_skills;
  const names = list.map(s => s.name);
  const ok = await confirmDialog({
    title: '收编新技能',
    lines: ['会把这些新装的技能收进统一仓库，保持原来的可见范围。'],
    groups: [{label: `收进仓库 ${names.length} 个`, items: names.map(name => ({name}))}],
    note: '可以在操作记录里撤销。',
    okText: '收编',
  });
  if (!ok) return;
  const result = await runWrite({
    button,
    busyText: '正在收编…',
    work: async () => {
      const written = await api('/api/absorb', {all: true, revision: state.table.revision, confirmed: true});
      await refreshEverything();
      return written;
    },
    success: written => (written.conflicts?.length ? null : {
      text: '新技能已经收进仓库',
      undo: undoButton => undoOperation(written.id, {skills: names, message: written.message, button: undoButton}),
    }),
  });
  if (result?.conflicts?.length) {
    toast({text: '有同名技能需要先对比，请到安装技能里查看差异。', tone: 'error', sticky: true});
    openInstall();
    await showIncomingConflicts(result, null);
  }
});

/* 重新加载数据。 */
let inventoryRequest = null;

async function refreshInventory() {
  if (inventoryRequest) return inventoryRequest;
  inventoryRequest = (async () => {
    try {
      if (!state.inventory) setTask('inventory', {text: '正在读取技能盘点…'});
      state.inventory = await api('/api/scan');
      renderDiagnostics();
      renderFooter();
    } catch (error) {
      toast({text: '技能清单没能读出来：' + error.message, tone: 'error', sticky: true});
    } finally {
      clearTask('inventory');
      inventoryRequest = null;
    }
  })();
  return inventoryRequest;
}

async function refreshTable(preserveDraft = true) {
  const table = await api('/api/table');
  if (!preserveDraft) state.assignmentIntents.clear();
  clearTimeout(previewTimer);
  invalidatePreview();
  previewBusy = false;
  const pending = state.pending;
  state.table = table;
  state.tableReadAt = new Date().toISOString();
  state.skillIndex = new Map(table.skills.map(s => [s.name, s]));
  state.groupIndex = new Map((table.groups || []).flatMap(group => group.skills.map(name => [name, group.name])));
  state.saved = table.actual_assignments || Object.fromEntries(table.skills.map(s => [s.name, [...s.assigned_agents]]));
  state.draft = clone(state.saved);
  state.pending = null;
  state.autoAdded = new Map();
  if (state.assignmentIntents.size) {
    for (const change of state.assignmentIntents.values()) setDraft(change.skill, change.agent, change.enabled);
    state.pending = pending;
    schedulePreview();
  }
  state.agents = table.agents || [];
  renderConfigExport();
  // 只有只读预览才显示标记；能改的时候右边什么都不放，导航仍是居中的。
  $('mode-badge').hidden = table.write_allowed;
  renderInstallRail();
  renderNewSkills();
  renderRailNotes();
  renderAgentSummary();
  renderUsedTags();
  renderSkillTable();
  if (state.detail) renderDetail();
  renderCleanup();
  renderActionBar();
  renderLabelCounts();
  if (table.write_allowed && table.llm_configured && table.labels_pending > 0 && !labelsAutoAttempted && !labelsRunning) {
    labelsAutoAttempted = true;
    startLabels('missing');
  }
  renderFooter();
}

/* agent 目录与接入建议。页面里翻名字、设置页那一栏、新 agent 横幅都看它。 */
let agentsPollTimer = null;

async function refreshAgents() {
  indexCatalog(await api('/api/agents'));
  renderRailNotes();
  renderAgentHead();
  renderAgents();
  if (!state.table) renderMigrationStatus();
  clearTimeout(agentsPollTimer);
  if (state.agentsView?.discovery?.running) {
    agentsPollTimer = setTimeout(() => refreshAgents().catch(error => {
      state.agentsView.discovery = {...state.agentsView.discovery, running: false, error: error.message + '，请重新扫描。'};
      renderAgents();
    }), 500);
  }
}

/* ==================== 设置与中文名：只改变显示，不锁技能操作 ==================== */

let programUpdate = {running: false, phase: 'idle', available: false, write_allowed: false};
let programActionRunning = false;
let programFailedAction = '';   // 本次打开页面里哪一步没成；左栏据此决定要不要留「升级」让用户再试

function renderProgram() {
  const progress = programUpdate;
  const date = value => value ? new Date(value).toLocaleString('zh-CN') : '首次安装版本';
  $('program-version').hidden = !progress.current && !progress.latest;
  $('program-version').textContent = `已安装：${date(progress.current)}`
    + (progress.available ? ` · 新版：${date(progress.latest)}` : '');
  $('program-message').textContent = progress.message || '从 GitHub 官方项目获取新版';
  $('program-error').textContent = progress.error || '';
  $('program-error').hidden = !progress.error;
  $('program-check').disabled = progress.running || Boolean(writeBusy);
  $('program-check').setAttribute('aria-busy', String(progress.running && progress.phase === 'check'));
  $('program-check').querySelector('span').textContent = progress.running && progress.phase === 'check'
    ? '正在检查…' : progress.phase === 'error' ? '重新检查' : '检查更新';
  $('program-apply').hidden = !progress.available && progress.phase !== 'apply';
  $('program-apply').disabled = progress.running || Boolean(writeBusy) || !progress.write_allowed;
  $('program-apply').setAttribute('aria-busy', String(progress.running && progress.phase === 'apply'));
  $('program-apply').textContent = progress.phase === 'apply' ? '正在升级…' : '一键升级';
  $('program-refresh').hidden = progress.phase !== 'ready';
  $('program-refresh').disabled = Boolean(writeBusy);
  // 打开看板时的自动检查不占页头任务条（左栏照常提醒）；手动检查和升级照旧显示。
  const quiet = progress.phase === 'check' && progress.automatic && !programActionRunning;
  if (progress.running && !quiet && !(progress.phase === 'apply' && programActionRunning)) setTask('self-update', {text: progress.message});
  else clearTask('self-update');
  renderRailNotes();
}

async function refreshProgramUpdate() {
  try {
    programUpdate = await api('/api/program/progress');
    renderProgram();
  } catch (error) {
    $('program-error').textContent = error.message;
    $('program-error').hidden = false;
  }
}

async function programAction(action) {
  if (programUpdate.running || writeBusy) return;
  programFailedAction = '';
  programUpdate = {...programUpdate, running: true, phase: action, error: '',
    message: action === 'check' ? '正在下载并检查新版…' : '正在升级，等当前操作完成…'};
  programActionRunning = true;
  renderProgram();
  const fail = error => {
    programFailedAction = action;
    programUpdate = {...programUpdate, running: false, phase: 'error', available: false, error: error.message};
  };
  const work = async () => {
    const result = await api('/api/program/' + action,
      action === 'apply' ? {target: programUpdate.target, confirmed: true} : {});
    programUpdate = {...programUpdate, ...result};
    while (programUpdate.running) {
      await new Promise(resolve => setTimeout(resolve, 800));
      programUpdate = await api('/api/program/progress');
      renderProgram();
    }
    if (programUpdate.error) throw new Error(programUpdate.error);
    return programUpdate;
  };
  try {
    if (action === 'apply') {
      await runWrite({button: $('program-apply'), busyText: '正在升级 SP…', work, onError: fail});
    } else await work();
  } catch (error) {
    fail(error);
  } finally {
    programActionRunning = false;
    renderProgram();
  }
}

$('program-check').addEventListener('click', () => programAction('check'));
$('program-apply').addEventListener('click', () => programAction('apply'));
$('program-refresh').addEventListener('click', () => location.reload());
// 技能页左栏那张卡片上的按钮：升级走同一套流程，装好后刷新页面生效。
$('rail-notes').addEventListener('click', event => {
  const button = event.target.closest('[data-program-rail]');
  if (!button) return;
  if (button.dataset.programRail === 'refresh') location.reload();
  else programAction('apply');
});
setInterval(() => { if (programUpdate.running && !programActionRunning) refreshProgramUpdate(); }, 1500);

const LLM_DEFAULTS = {
  deepseek: ['https://api.deepseek.com', 'deepseek-chat'],
  qwen: ['https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen-plus'],
  kimi: ['https://api.moonshot.cn/v1', 'moonshot-v1-32k'],
  glm: ['https://open.bigmodel.cn/api/paas/v4', 'glm-4-flash'],
  openai: ['https://api.openai.com/v1', 'gpt-4o-mini'],
  other: ['', ''],
};

let llmSettings = {};
let extraSourcePaths = [];
let labelsAutoAttempted = false;
let labelsRunning = false;
let labelsDone = 0;   // 上一次轮询看到的整理进度

function renderLabelCounts() {
  const table = state.table;
  if (!table) return;
  const remaining = table.labels_pending;
  const done = table.skills.length - remaining;
  $('labels-count').innerHTML = remaining
    ? `${icon('warning')}<span>已整理 ${done} 个，还差 ${remaining} 个</span>${hint('还差的是缺中文名或中文说明的技能。')}`
    : `${icon('check-circle')}<span>已整理 ${done} 个，都齐了</span>`;
  $('labels-missing').hidden = !remaining;
  $('labels-missing').querySelector('span').textContent = `整理剩下的 ${remaining} 个`;
  for (const id of ['labels-missing', 'labels-all']) {
    $(id).disabled = !table.write_allowed || !table.llm_configured || labelsRunning;
  }
}

function renderExtraSources() {
  const writable = Boolean(state.table?.write_allowed);
  $('extra-sources').innerHTML = extraSourcePaths.map((path, index) =>
    `<div class="item-row"><span>${escape(path)}</span><span class="item-end"><button type="button" class="link-btn" data-extra-index="${index}">去掉</button></span></div>`).join('');
  $('extra-source-path').disabled = !writable;
  $('extra-source-add').disabled = !writable;
  for (const button of $('extra-sources').querySelectorAll('button')) button.disabled = !writable;
}

async function saveExtraSource(path, remove) {
  const result = await api('/api/settings/extra-sources', remove ? {path, remove: true} : {path});
  $('extra-source-message').textContent = result.message;
  extraSourcePaths = result.extra_sources || [];
  renderExtraSources();
  await refreshInventory();
}

async function refreshSettings() {
  try {
    const settings = await api('/api/settings');
    llmSettings = settings.llm;
    extraSourcePaths = settings.extra_sources || [];
    renderExtraSources();
    const provider = llmSettings.provider || 'deepseek';
    $('llm-provider').value = provider in LLM_DEFAULTS ? provider : 'other';
    const defaults = LLM_DEFAULTS[$('llm-provider').value];
    $('llm-base-url').value = llmSettings.base_url || defaults[0];
    $('llm-model').value = llmSettings.model || defaults[1];
    $('llm-key').placeholder = llmSettings.has_key ? `已保存，末尾 ${llmSettings.key_tail}；不改就留空` : '粘贴服务商提供的 key';
    $('llm-clear').hidden = !llmSettings.has_key;
    for (const input of $('llm-form').querySelectorAll('input, select, button')) input.disabled = !state.table?.write_allowed;
    renderLabelCounts();
  } catch (error) {
    toast({text: error.message, tone: 'error'});
  }
}

function renderConfigExport() {
  const selected = $('config-agent').value;
  $('config-agent').innerHTML = '<option value="">全部在用技能</option>' + state.agents.map(agent =>
    `<option value="${escape(agent.id)}">${escape(agent.name)} 在用的技能</option>`).join('');
  $('config-agent').value = state.agents.some(agent => agent.id === selected) ? selected : '';
  $('config-export').disabled = !state.table || Boolean(writeBusy);
  $('config-agent').disabled = !state.table || Boolean(writeBusy);
  $('config-import').disabled = !state.table || Boolean(writeBusy);
  renderConfigImportControls();
}

$('config-export').addEventListener('click', async () => {
  try {
    await withBusy($('config-export'), '正在导出…', async () => {
      const agent = $('config-agent').value;
      const config = await api('/api/config/export' + (agent ? '?agent=' + encodeURIComponent(agent) : ''));
      const url = URL.createObjectURL(new Blob([JSON.stringify(config, null, 2) + '\n'], {type: 'application/json'}));
      const link = document.createElement('a');
      link.href = url;
      link.download = 'skill-pilot-config.json';
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      const manual = config.skills.filter(skill => skill.local_modified || skill.source.kind === 'manual').length;
      toast({text: `已导出 ${config.skills.length} 个技能的配置。${manual ? `其中 ${manual} 个需要对方另行补齐，文件里已标明。` : '把文件交给对方的 agent 即可。'}`});
    });
  } catch (error) {
    toast({text: error.message, tone: 'error'});
  }
});

let configImportData = null;
let configImportPlan = null;
let configImportResult = null;
let configImportSequence = 0;
let configImportReading = false;

function configImportAgents() {
  const agent = $('config-import-agent').value;
  return agent ? [agent] : null;
}

function renderConfigImportControls() {
  const busy = Boolean(writeBusy);
  for (const id of ['config-import-file', 'config-import-agent']) {
    $(id).disabled = busy || configImportReading || Boolean(configImportResult);
  }
  const count = (configImportPlan?.install.length || 0) + (configImportPlan?.reuse.length || 0);
  $('config-import-apply').disabled = busy || !state.table?.write_allowed || !configImportPlan || !count;
  $('config-import-apply').hidden = Boolean(configImportResult);
  if (!busy) $('config-import-apply').textContent = count ? `导入 ${count} 个` : '导入';
  $('config-import-close').disabled = busy;
  $('config-import-retry').disabled = busy || configImportReading;
  $('config-import-undo').disabled = busy || !state.table?.write_allowed;
  $('config-import-undo').hidden = !configImportResult?.id;
}

function importReviewHtml(plan, done, unmappedTip, html = () => '') {
  const items = names => names.map(name => ({name, html: html(name), note: [
    (plan.assignments[name] || []).map(nameOf).join('、'),
    plan.dependencies[name]?.length ? `补上所需技能：${plan.dependencies[name].join('、')}` : '',
  ].filter(Boolean).join('；')}));
  return [
    [done ? '新装好了' : '将安装', items(plan.install)],
    ['沿用本机', items(plan.reuse)],
    ['待补齐，本次跳过', plan.skipped.map(row => ({name: row.name, html: html(row.name), note: row.reason}))],
  ].filter(([, rows]) => rows.length).map(([label, rows]) =>
    `<details class="fold fold-inner" ${rows.length <= CONFIRM_FOLD ? 'open' : ''}>
      <summary>${escape(label)} ${rows.length} 个</summary><ul class="confirm-items">${confirmItems(rows)}</ul>
    </details>`).join('') + (plan.unmapped_agents.length
      ? `<p class="muted">这些 agent 还没接入，${done ? '相应分配未导入' : unmappedTip}：${escape(plan.unmapped_agents.map(nameOf).join('、'))}。</p>` : '');
}

function renderConfigImportReview(plan, done = false) {
  $('config-import-review').innerHTML = importReviewHtml(plan, done, '可在上方指定接收方');
}

async function previewConfigImport() {
  if (!configImportData) return;
  const sequence = ++configImportSequence;
  configImportPlan = null;
  configImportReading = true;
  $('config-import-review').replaceChildren();
  $('config-import-retry').hidden = true;
  $('config-import-message').textContent = '正在核对配置…';
  renderConfigImportControls();
  try {
    const plan = await api('/api/config/preview', {config: configImportData, agents: configImportAgents()});
    if (sequence !== configImportSequence) return;
    configImportPlan = plan;
    $('config-import-message').textContent = plan.message + (state.table?.write_allowed ? '。' : '。当前为只读模式，只能预览。');
    renderConfigImportReview(plan);
  } catch (error) {
    if (sequence !== configImportSequence) return;
    $('config-import-message').textContent = error.message;
    $('config-import-retry').hidden = false;
  } finally {
    if (sequence === configImportSequence) {
      configImportReading = false;
      renderConfigImportControls();
    }
  }
}

$('config-import').addEventListener('click', () => {
  configImportSequence++;
  configImportData = configImportPlan = configImportResult = null;
  configImportReading = false;
  $('config-import-title').textContent = '导入技能配置';
  $('config-import-file').value = '';
  $('config-import-agent').innerHTML = '<option value="">按配置中的分配</option>' + state.agents.map(agent =>
    `<option value="${escape(agent.id)}">全部给 ${escape(agent.name)}</option>`).join('');
  $('config-import-message').textContent = '选好文件后，先核对可导入和待补齐的技能。';
  $('config-import-review').replaceChildren();
  $('config-import-retry').hidden = true;
  renderConfigImportControls();
  $('config-import-dialog').showModal();
});

$('config-import-file').addEventListener('change', async event => {
  const sequence = ++configImportSequence;
  const file = event.target.files[0];
  configImportData = configImportPlan = null;
  configImportReading = true;
  $('config-import-review').replaceChildren();
  $('config-import-retry').hidden = true;
  $('config-import-message').textContent = file ? '正在读取文件…' : '请先选择配置文件。';
  renderConfigImportControls();
  try {
    if (!file) return;
    if (file.size > 1024 * 1024) throw new Error('配置文件太大，请使用 Skill Pilot 导出的技能清单。');
    const text = await file.text();
    if (sequence !== configImportSequence) return;
    try { configImportData = JSON.parse(text); }
    catch { throw new Error('配置文件没能读懂，请选择 Skill Pilot 导出的文件。'); }
    if (!configImportData || typeof configImportData !== 'object' || Array.isArray(configImportData)) {
      configImportData = null;
      throw new Error('这不是 Skill Pilot 配置文件，请重新导出。');
    }
  } catch (error) {
    if (sequence === configImportSequence) $('config-import-message').textContent = error.message;
  } finally {
    if (sequence === configImportSequence) {
      configImportReading = false;
      renderConfigImportControls();
      if (configImportData !== null) await previewConfigImport();
    }
  }
});

$('config-import-agent').addEventListener('change', previewConfigImport);
$('config-import-retry').addEventListener('click', previewConfigImport);
$('config-import-close').addEventListener('click', () => $('config-import-dialog').close());
$('config-import-dialog').addEventListener('cancel', event => { if (writeBusy) event.preventDefault(); });
$('config-import-dialog').addEventListener('close', () => { configImportSequence++; });
$('config-import-dialog').querySelector('form').addEventListener('submit', event => event.preventDefault());

async function undoConfigImport(result, button) {
  if (await undoOperation(result.id, {skills: [...result.install, ...result.reuse], message: result.message, button})) {
    if (result === configImportResult) $('config-import-dialog').close();
  }
}

$('config-import-undo').addEventListener('click', event => undoConfigImport(configImportResult, event.currentTarget));
$('config-import-apply').addEventListener('click', async event => {
  if (!configImportPlan || writeBusy) return;
  const plan = configImportPlan;
  await runWrite({
    button: event.currentTarget, busyText: '正在下载并导入…',
    onError: error => {
      configImportPlan = null;
      $('config-import-message').textContent = error.message;
      $('config-import-retry').hidden = false;
      renderConfigImportControls();
    },
    work: async () => {
      const result = await api('/api/config/apply', {config: configImportData, agents: configImportAgents(), revision: plan.revision, confirmed: true});
      configImportResult = result;
      $('config-import-title').textContent = '导入结果';
      $('config-import-message').textContent = result.message + (result.complete ? '。' : '。未完成项见下方明细。');
      renderConfigImportReview({...plan, ...result}, true);
      await refreshEverything();
      return result;
    },
    success: result => ({text: result.complete ? result.message : '可用部分已处理，未完成项见导入结果。',
      undo: result.id ? button => undoConfigImport(result, button) : null}),
  });
});

/* ==================== 团队技能商店：设置里连上分享站；「团队」页浏览、装上、评价、上架 ==================== */

/* 商店里的名字、说明、推荐语和评价都是同事写的：一律当纯文字转义。
   技能可能本机还没有，所以名字用上架者给的中文名，不查本机的整理结果。 */
const STORE_STATUS = {
  installed: ['tag-ok', '已装'], update: ['tag', '有新版'], installable: ['tag-quiet', '可以装'],
  lark: ['tag-quiet', '飞书官方'], conflict: ['tag-warn', '本机有同名技能'], blocked: ['tag-warn', '暂时装不了'],
  mine: ['tag-quiet', '我上架的'],
};
const storeTitle = card => card.title || card.name;
const teamCanWrite = () => Boolean(state.table?.write_allowed) && !writeBusy;
// 还没整理过时没有技能表，就看 /api/status 给的只读状态。
const teamSettingsWritable = () => state.table ? Boolean(state.table.write_allowed) : state.writeAllowed;
let teamUrlEdited = false;

async function refreshTeam() {
  try {
    state.team = await api('/api/team');
  } catch (error) {
    state.team = {configured: false, url: '', has_key: false, member: null, error: error.message};
  }
  document.querySelector('.nav-item[data-page="team"]').hidden = !state.team.configured;
  renderTeamSettings();
  renderTeam();
}

function renderTeamSettings() {
  const team = state.team;
  if (!team) return;
  if (!teamUrlEdited) $('team-url').value = team.url || '';
  const connected = Boolean(team.member);
  $('team-name-label').hidden = connected;
  $('team-name').hidden = connected;
  $('team-key').placeholder = team.has_key ? '已保存；不改就留空' : '粘贴管理员给你的口令';
  $('team-message').textContent = [team.error || (team.member ? `已连接，你是 ${team.member.name}。` : ''),
    teamSettingsWritable() ? '' : '只读模式下不能修改。'].filter(Boolean).join(' ');
  $('team-goto').hidden = !team.configured;
  const locked = !teamSettingsWritable() || Boolean(writeBusy);
  for (const element of $('team-form').querySelectorAll('input, #team-save, #team-key-toggle')) element.disabled = locked;
}

function setTeamKeyOpen(open) {
  $('team-key-box').hidden = !open;
  $('team-key-toggle').setAttribute('aria-expanded', String(open));
}

$('team-url').addEventListener('input', () => { teamUrlEdited = true; });
$('team-key-toggle').addEventListener('click', () => setTeamKeyOpen($('team-key-box').hidden));
$('team-goto').addEventListener('click', () => showPage('team'));
$('team-setup').addEventListener('click', () => gotoPlace('settings/team'));

$('team-form').addEventListener('submit', event => {
  event.preventDefault();
  let failed = false;
  withBusy($('team-save'), '正在连接…', async () => {
    try {
      const result = await api('/api/team/settings', {
        url: $('team-url').value.trim(),
        name: $('team-name').value.trim(),
        key: $('team-key').value.trim(),
      });
      teamUrlEdited = false;
      $('team-name').value = '';
      $('team-key').value = '';
      setTeamKeyOpen(false);
      state.team = result;
      state.store = null;
      storeCandidates = null;
      document.querySelector('.nav-item[data-page="team"]').hidden = false;
      toast({text: `已连上分享站，你是 ${result.member.name}。`});
    } catch (error) {
      failed = true;
      $('team-message').textContent = error.message;
      toast({text: error.message, tone: 'error'});
    }
  }).finally(() => {
    // withBusy 会放开按钮，这里按最新状态重画（只读、写入中会重新禁用），并保留刚才的报错。
    const message = $('team-message').textContent;
    renderTeamSettings();
    if (failed) $('team-message').textContent = message;
  });
});

/* 进团队页：先画已知的，再读最新状态和商店目录。 */
async function enterTeam() {
  renderTeam();
  await refreshTeam();
  if (state.team?.configured && !state.team.error) await loadStore();
}

let storeSequence = 0;
async function loadStore() {
  const sequence = ++storeSequence;
  try {
    const result = await api('/api/team/catalog');
    if (sequence !== storeSequence) return;
    state.store = result;
    state.storeError = '';
  } catch (error) {
    if (sequence !== storeSequence) return;
    state.storeError = error.message;
  }
  renderTeam();
  if (state.storeDetail) renderStoreDetail();
}

/* 自己能上架的技能（含已上架的是否改过）：打开上架窗口或看“我上架的”时才读。 */
let storeCandidates = null;
async function loadCandidates() {
  storeCandidates = (await api('/api/team/listings/candidates')).skills;
  return storeCandidates;
}

/* ---------- 排序与筛选：只看数据，不碰页面，方便单独测试 ---------- */

const storeTime = card => Date.parse(card.updated_at) || 0;
const storeScore = card => card.rating?.count ? card.rating.average : -1;
const STORE_SORTS = {
  latest: (a, b) => storeTime(b) - storeTime(a),
  rating: (a, b) => storeScore(b) - storeScore(a) || (b.rating?.count || 0) - (a.rating?.count || 0) || storeTime(b) - storeTime(a),
  popular: (a, b) => b.listings.length - a.listings.length || storeScore(b) - storeScore(a) || storeTime(b) - storeTime(a),
};

function storeFilter(cards, {query = '', group = '', mine = false} = {}) {
  const words = query.trim().toLowerCase();
  return cards.filter(card =>
    (!mine || card.local?.status === 'mine') &&
    (!group || (group === '-' ? !card.group : card.group === group)) &&
    (!words || [card.name, card.title, card.summary, card.description, card.group,
      ...card.listings.flatMap(item => [item.note, item.member?.name])]
      .some(value => String(value || '').toLowerCase().includes(words))));
}

function storeGroups(cards) {
  const counts = new Map();
  for (const card of cards) counts.set(card.group || '-', (counts.get(card.group || '-') || 0) + 1);
  return [...counts].sort((a, b) => (a[0] === '-') - (b[0] === '-') || b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'));
}

/* 一句话：没整理过中文名的技能没有，就用技能自带说明的第一句。 */
function storeSummary(card) {
  if (card.summary) return card.summary;
  const first = String(card.description || '').split(/[\n。！？!?]/).map(part => part.trim()).find(Boolean) || '';
  return first.length > 60 ? first.slice(0, 60) + '…' : first;
}

/* 谁推荐的：两个人以内写全名，多了写“某某等 N 人”。 */
function storeRecommenders(card) {
  const names = card.listings.map(item => item.member.name);
  return names.length > 2 ? `${names[0]} 等 ${names.length} 人推荐` : `${names.join('、')} 推荐`;
}

/* ---------- 小部件：星级、状态标签、卡片、列表行 ---------- */

const starIcon = on => `<svg class="star${on ? ' is-on' : ''}" aria-hidden="true" focusable="false"><use href="#sp-star"/></svg>`;
const starsHtml = stars => `<span class="stars" role="img" aria-label="${stars} 星">${[1, 2, 3, 4, 5].map(n => starIcon(n <= stars)).join('')}</span>`;

function storeRatingHtml(rating) {
  if (!rating?.count) return '<span class="rating is-empty">还没有评价</span>';
  return `<span class="rating" aria-label="${rating.average.toFixed(1)} 分，${rating.count} 人评价">${starIcon(true)}<b>${rating.average.toFixed(1)}</b><span>${rating.count} 人评</span></span>`;
}

function storeStatusTag(card) {
  const [tone, label] = STORE_STATUS[card.local?.status] || ['tag-quiet', card.local?.label || ''];
  return label ? `<span class="tag ${tone}">${escape(label)}</span>` : '';
}

const sizeText = bytes => bytes < 1024 ? `${bytes} 字节` : bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;

function storeCardHtml(card) {
  const quote = card.listings.find(item => item.note);
  const people = card.listings.length > 1 ? `<span>${card.listings.length} 人推荐</span>` : '';
  return `<button class="store-card" type="button" data-store-open="${escape(card.key)}">
    <span class="store-card-top"><span class="store-card-title">${escape(storeTitle(card))}</span>${storeStatusTag(card)}</span>
    <span class="store-card-sum">${escape(storeSummary(card))}</span>
    <span class="store-card-quote">${quote
      ? `“${escape(quote.note)}”<span class="by"> — ${escape(quote.member.name)}</span>`
      : `<span class="by">${escape(storeRecommenders(card))}</span>`}</span>
    <span class="store-card-foot">${storeRatingHtml(card.rating)}${people}${card.group ? `<span>${escape(card.group)}</span>` : ''}</span>
  </button>`;
}

function storeRowHtml(card) {
  const mine = state.storeMine;
  const candidate = mine ? storeCandidates?.find(item => item.name === card.name) : null;
  const locked = teamCanWrite() ? '' : ' disabled';
  const end = mine
    ? `${candidate?.changed ? `<button class="btn btn-small btn-update" type="button" data-store-republish="${escape(card.name)}"${locked}>上架新版</button>` : ''}
       <button class="btn btn-small btn-quiet" type="button" data-store-unlist="${escape(card.name)}"${locked}>下架…</button>`
    : storeStatusTag(card);
  return `<div class="store-row">
    <button class="store-row-main" type="button" data-store-open="${escape(card.key)}">
      <span class="store-row-name"><span class="t">${escape(storeTitle(card))}</span>${card.title ? `<span class="skill-id">${escape(card.name)}</span>` : ''}</span>
      <span class="store-row-sub">${escape(storeSummary(card) || card.listings.find(item => item.note)?.note || '')}</span>
    </button>
    <span class="store-row-rating">${storeRatingHtml(card.rating)}</span>
    <span class="store-row-by">${escape(mine ? `${friendlyDate(storeMyListing(card)?.updated_at)}上架` : storeRecommenders(card))}</span>
    <span class="store-row-end">${end}</span>
  </div>`;
}

/* ---------- 商店首页：几排推荐 + 全部技能 ---------- */

/* 几排推荐按这个顺序挑卡片；同一张卡片只出现在第一排挑中它的地方，免得几排看起来一样。 */
const STORE_SHELVES = [
  {sort: 'rating', title: '最受好评', pick: cards => cards.filter(card => card.rating?.count)},
  {sort: 'popular', title: '同事推荐最多', pick: cards => cards.filter(card => card.listings.length > 1)},
  {sort: 'latest', title: '最新上架', pick: cards => cards},
];
const SHELF_SIZE = 4;
const SHELF_MIN = 8;   // 技能少于这个数时不分几排，直接看全部

const storeQuery = () => $('store-search').value.trim();
const storeBrowsing = () => !storeQuery() && !state.storeGroup && !state.storeMine;

function renderTeam() {
  if (state.page !== 'team') return;
  const team = state.team;
  $('team-unset').hidden = !team || team.configured;
  $('team-ready').hidden = Boolean(team) && !team.configured;
  if (team && !team.configured) return;
  const problem = (team && team.error) || state.storeError;
  $('store-error').hidden = !problem;
  $('store-error-text').textContent = problem || '';
  const cards = state.store?.cards || null;
  const ready = Boolean(team?.member) && !team?.error;
  const readOnly = state.table ? !state.table.write_allowed : !state.writeAllowed;
  $('store-publish').disabled = !ready || !teamCanWrite();
  $('store-publish').title = !state.table ? '整理过技能后才能上架' : readOnly ? '只读预览，不能上架' : '';
  const mineCount = (cards || []).filter(card => card.local?.status === 'mine').length;
  $('store-mine').disabled = !cards;
  $('store-mine').setAttribute('aria-pressed', String(state.storeMine));
  $('store-mine-count').textContent = mineCount ? number(mineCount) : '';
  renderStoreShelves(cards);
  renderStoreAll(cards);
}

function renderStoreShelves(cards) {
  const box = $('store-shelves');
  if (!cards) {
    box.innerHTML = state.storeError || state.team?.error ? '' : `<section class="shelf">
      <div class="shelf-head"><h2 class="is-loading" role="status">正在读取团队技能…</h2></div>
      <div class="shelf-cards" aria-hidden="true">${'<span class="store-card is-skeleton"></span>'.repeat(SHELF_SIZE)}</div>
    </section>`;
    return;
  }
  if (!cards.length) {
    box.innerHTML = `<div class="empty store-empty">${emptyInner('team', '还没有人上架技能')}
      <button class="btn btn-primary" type="button" data-store-publish${teamCanWrite() ? '' : ' disabled'}>${icon('plus', 'btn-icon')}上架第一个技能</button></div>`;
    return;
  }
  if (!storeBrowsing() || cards.length < SHELF_MIN) {
    box.innerHTML = '';
    return;
  }
  const shown = new Set();
  box.innerHTML = STORE_SHELVES.map(shelf => {
    const picked = shelf.pick(cards).filter(card => !shown.has(card.key)).sort(STORE_SORTS[shelf.sort]).slice(0, SHELF_SIZE);
    for (const card of picked) shown.add(card.key);
    return picked.length ? `<section class="shelf" aria-labelledby="shelf-${shelf.sort}">
      <div class="shelf-head">
        <h2 id="shelf-${shelf.sort}">${shelf.title}</h2>
        <button class="link-btn" type="button" data-store-more="${shelf.sort}">查看全部</button>
      </div>
      <div class="shelf-cards">${picked.map(storeCardHtml).join('')}</div>
    </section>` : '';
  }).join('');
}

function renderStoreAll(cards) {
  $('store-all').hidden = !cards?.length;
  if (!cards?.length) return;
  const query = storeQuery();
  const base = storeFilter(cards, {query, mine: state.storeMine});
  const groups = storeGroups(base);
  // 选中的分组搜不到时也留着，数量写 0，免得筛选条件悄悄消失。
  if (state.storeGroup && !groups.some(([name]) => name === state.storeGroup)) groups.push([state.storeGroup, 0]);
  renderButtons($('store-groups'), [['', base.length], ...groups].map(([name, count]) =>
    `<button class="store-group" type="button" data-store-group="${escape(name)}" aria-pressed="${name === state.storeGroup}">${
      escape(name === '' ? '全部' : name === '-' ? '没分组' : name)}<span class="n">${number(count)}</span></button>`).join(''), 'data-store-group');
  $('store-groups').hidden = groups.length < 2 && !state.storeGroup;
  const rows = storeFilter(base, {group: state.storeGroup}).sort(STORE_SORTS[state.storeSort]);
  $('store-all-title').textContent = state.storeMine ? '我上架的' : query ? '搜索结果' : '全部技能';
  $('store-count').textContent = `${number(rows.length)} 个`;
  for (const button of $('store-sort').querySelectorAll('[data-store-sort]')) {
    button.setAttribute('aria-pressed', String(button.dataset.storeSort === state.storeSort));
  }
  const empty = state.storeMine && !base.length
    ? emptyInner('team', '你还没上架技能', `<button type="button" class="link-btn" data-store-publish${teamCanWrite() ? '' : ' disabled'}>上架技能</button>`)
    : emptyInner('search', '没找到，换个词试试');
  $('store-list').innerHTML = rows.length
    ? `<div class="store-list-inner${state.storeMine ? ' is-mine' : ''}">${rows.map(storeRowHtml).join('')}</div>`
    : `<div class="empty">${empty}</div>`;
}

$('store-search').addEventListener('input', renderTeam);

$('store-sort').addEventListener('click', event => {
  const button = event.target.closest('[data-store-sort]');
  if (!button) return;
  state.storeSort = button.dataset.storeSort;
  renderTeam();
});

$('store-groups').addEventListener('click', event => {
  const button = event.target.closest('[data-store-group]');
  if (!button) return;
  // 再点一次当前分组就回到全部。
  state.storeGroup = button.dataset.storeGroup === state.storeGroup ? '' : button.dataset.storeGroup;
  renderTeam();
});

$('store-mine').addEventListener('click', async () => {
  state.storeMine = !state.storeMine;
  state.storeGroup = '';
  renderTeam();
  if (!state.storeMine || storeCandidates) return;
  try {
    await loadCandidates();
  } catch (error) {
    toast({text: error.message, tone: 'error'});
  }
  renderTeam();
});

async function reloadStorePage(button) {
  await withBusy(button, '', async () => {
    await enterTeam();
    if (state.storeMine || storeCandidates) await loadCandidates().catch(() => { storeCandidates = null; });
    renderTeam();
  });
}
$('store-refresh').addEventListener('click', event => reloadStorePage(event.currentTarget));
$('store-retry').addEventListener('click', event => reloadStorePage(event.currentTarget));
$('store-publish').addEventListener('click', () => openPublish());

/* 卡片、列表行和空状态里的按钮都在团队页里，统一在这里分派。 */
$('page-team').addEventListener('click', event => {
  const open = event.target.closest('[data-store-open]');
  const more = event.target.closest('[data-store-more]');
  const publish = event.target.closest('[data-store-publish]');
  const republish = event.target.closest('[data-store-republish]');
  const unlist = event.target.closest('[data-store-unlist]');
  if (open) openStoreDetail(open.dataset.storeOpen, open);
  else if (more) {
    state.storeSort = more.dataset.storeMore;
    renderTeam();
    $('store-all').scrollIntoView({behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start'});
    $('store-all-title').focus({preventScroll: true});
  } else if (publish) openPublish();
  else if (republish) openPublish([republish.dataset.storeRepublish]);
  else if (unlist) unlistStore(unlist.dataset.storeUnlist, unlist);
});

async function unlistStore(name, button) {
  const cards = (state.store?.cards || []).filter(item => item.local?.status === 'mine');
  const card = cards.find(item => item.name === name);
  // 自己上架的别的技能需要它时，一起下架，先在确认框里说清楚。
  const names = [name];
  for (let index = 0; index < names.length; index++) {
    for (const item of cards) if (item.requires?.includes(names[index]) && !names.includes(item.name)) names.push(item.name);
  }
  const extra = cards.filter(item => names.slice(1).includes(item.name)).map(storeTitle);
  const ok = await confirmDialog({
    title: `下架「${card ? storeTitle(card) : name}」`,
    lines: [extra.length ? `${extra.join('、')} 需要它，会一起下架。` : '', '同事在商店里将看不到它。',
      '已经装上的同事不受影响；你随时可以重新上架。'],
    okText: extra.length ? `下架 ${names.length} 个` : '下架',
  });
  if (!ok) return;
  const rows = [...$('store-list').querySelectorAll('.store-row-main')];
  const place = rows.findIndex(row => row.closest('.store-row')?.contains(button));
  await withBusy(button, '正在下架…', async () => {
    try {
      await api('/api/team/listings/remove', {names});
      if (state.storeDetail === card?.key) closeDetail();
      storeCandidates = null;
      await loadStore();
      if (state.storeMine) await loadCandidates().catch(() => {});
      renderTeam();
      toast({text: `已下架「${card ? storeTitle(card) : name}」${extra.length ? `等 ${names.length} 个技能` : ''}。`});
    } catch (error) {
      toast({text: error.message, tone: 'error', sticky: true});
    }
  });
  // 这一行没了：焦点交给顶上来的那一行，没有就交给列表标题。
  if (!button.isConnected) {
    const next = $('store-list').querySelectorAll('.store-row-main')[Math.max(0, place)];
    (next || $('store-all-title')).focus();
  }
}

/* ---------- 详情：沿用技能页右侧的详情面板 ---------- */

let storeOpener = null;
let storeOpenerArea = null;   // store-shelves 或 store-list：卡片重画后按编号在同一处找回
let storeGet = null;   // 详情里的「装上」：{key, agents, plan, reading, error}

const storeCard = key => state.store?.cards.find(card => card.key === key) || null;
const storeMyListing = card => card.listings.find(item => item.member.id === state.store?.member?.id) || card.listings[0];

function openStoreDetail(key, opener = null) {
  if (state.storeDetail !== key || !storeGet) storeGet = {key, agents: new Set(state.agents.map(agent => agent.id)), plan: null, reading: false, error: ''};
  storeOpener = opener;
  storeOpenerArea = opener?.closest('#store-shelves, #store-list')?.id || null;
  state.storeDetail = key;
  renderStoreDetail(true);
  showDetailPanel(() => state.storeDetail === key);
  if (storeCard(key)?.local?.status === 'mine' && !storeCandidates) {
    loadCandidates().then(() => { if (state.storeDetail === key) renderStoreDetail(); }).catch(() => {});
  }
}

function renderStoreDetail(fresh = false) {
  const card = storeCard(state.storeDetail);
  if (!card) {
    // 同事下架了，或者分享站换了人：详情跟着关掉。
    closeDetail();
    return;
  }
  const body = $('detail-body');
  const scroll = fresh ? 0 : body.scrollTop;
  $('detail-title').textContent = storeTitle(card);
  body.innerHTML = `
    ${card.title ? `<p class="detail-id">${escape(card.name)}</p>` : ''}
    ${storeSummary(card) ? `<p class="detail-summary">${escape(storeSummary(card))}</p>` : ''}
    <p class="store-facts">${storeRatingHtml(card.rating)}<span>${escape(storeRecommenders(card))}</span>${card.group ? `<span>${escape(card.group)}</span>` : ''}</p>
    ${storeGetHtml(card)}
    <h3 class="sub-title">谁推荐的</h3>
    <ul class="store-recs">${card.listings.map(item => `<li>
      <p class="store-rec-head"><strong>${escape(item.member.name)}</strong><span>${escape(friendlyDate(item.updated_at))}上架</span></p>
      ${item.note ? `<p class="store-rec-note">“${escape(item.note)}”</p>` : ''}
    </li>`).join('')}</ul>
    ${storeReviewsHtml(card)}
    ${card.description ? `<h3 class="sub-title">说明</h3><p class="detail-desc">${escape(card.description)}</p>` : ''}
    ${card.requires?.length ? `<h3 class="sub-title">所需技能${hint('装上它时，这些技能会一起带上；本机已有同名的就直接用本机的。')}</h3>
      <p class="store-requires">${card.requires.map(name => escape(storeRequireTitle(card, name))).join('、')}</p>` : ''}
    <h3 class="sub-title">来源</h3>
    <p class="store-source">${escape(storeSourceText(card))}</p>`;
  body.scrollTop = scroll;
}

/* 所需技能优先用同一作者上架的那张卡片的中文名，没有就写原名。 */
function storeRequireTitle(card, name) {
  const authors = new Set(card.listings.map(item => item.member.id));
  const cards = state.store?.cards.filter(item => item.name === name) || [];
  const found = cards.find(item => item.listings.some(listing => authors.has(listing.member.id))) || cards[0];
  return found?.title || name;
}

function storeSourceText(card) {
  if (card.kind === 'github') return '网上的公开技能，装的时候从原作者那里下载。';
  if (card.kind === 'lark') return '飞书官方技能。';
  const latest = card.listings[0];
  return `${latest.member.name} 上传的文件${latest.file_count ? `，共 ${number(latest.file_count)} 个、${sizeText(latest.size || 0)}` : ''}。`;
}

/* 详情里最重要的一块：能不能装、装给谁。 */
function storeGetHtml(card) {
  const local = card.local || {};
  const users = (local.agents || []).map(nameOf).join('、');
  const box = (tone, text, action = '') => `<section class="store-get store-get-${tone}">
    <p class="store-get-line">${text}</p>${action ? `<div class="store-get-actions">${action}</div>` : ''}</section>`;
  const locked = teamCanWrite() ? '' : ' disabled';
  if (local.status === 'installed') {
    return box('ok', `${icon('check-circle')}<span><strong>已装</strong>${users ? `，${escape(users)} 在用` : '，还没有 agent 在用'}</span>`,
      `<button class="link-btn" type="button" data-store-goto="${escape(card.name)}">在技能页看看</button>`);
  }
  if (local.status === 'update') {
    return box('info', `${icon('update-up')}<span><strong>有新版</strong>${users ? `，${escape(users)} 在用` : ''}</span>`,
      `<button class="btn btn-small btn-update" type="button" data-store-goto="${escape(card.name)}" data-store-check>去技能页更新</button>`);
  }
  if (local.status === 'mine') {
    const candidate = storeCandidates?.find(item => item.name === card.name);
    return box('quiet', `${icon('team')}<span><strong>这是你上架的</strong>${candidate?.changed ? '，本机改过，可以上架新版' : ''}</span>`,
      `${candidate?.changed ? `<button class="btn btn-small btn-update" type="button" data-store-republish="${escape(card.name)}"${locked}>上架新版</button>` : ''}
       <button class="btn btn-small btn-quiet" type="button" data-store-unlist="${escape(card.name)}"${locked}>下架…</button>`);
  }
  if (local.status === 'lark') {
    return box('quiet', `${icon('info')}<span><strong>要用飞书官方方式装</strong>${local.reason ? hint(local.reason) : ''}</span>`);
  }
  if (local.status !== 'installable') {
    return box('warn', `${icon('warning')}<span><strong>${escape(local.label || '暂时装不了')}</strong>${local.reason ? `：${escape(local.reason)}` : ''}</span>`);
  }
  return storeInstallHtml(card);
}

function storeInstallHtml(card) {
  const get = storeGet;
  const plan = get.plan;
  const locked = !teamCanWrite();
  const agents = state.agents.map(agent => `<label class="store-agent">
      <input type="checkbox" data-store-agent="${escape(agent.id)}" ${get.agents.has(agent.id) ? 'checked' : ''}${locked || get.reading ? ' disabled' : ''}>
      ${swatch(agent.id)}<span>${escape(agent.name)}</span></label>`).join('');
  const count = get.agents.size;
  const cost = `每个 agent 每次新对话多占约 <b>${tokenText(card.tokens)} Token</b>${hint(TOKEN_HINT)}`;
  let review = '';
  let actions = `<button class="btn btn-primary" type="button" data-store-install${locked || !count || get.reading ? ' disabled' : ''}>${get.reading ? '正在核对…' : `装上${count ? `（给 ${count} 个 agent）` : ''}`}</button>`;
  if (plan) {
    const names = list => list.map(item => escape(item.title || item.name || item)).join('、');
    review = `<div class="store-plan" role="status">
      ${plan.install?.length ? `<p>会装上 ${plan.install.length} 个：${names(plan.install)}</p>` : ''}
      ${plan.reuse?.length ? `<p>本机已有，直接用：${names(plan.reuse)}</p>` : ''}
      ${plan.skipped?.length ? `<p class="is-warn">${icon('warning')}跳过 ${plan.skipped.length} 个：${plan.skipped.map(item => `${escape(item.name)}（${escape(item.reason)}）`).join('、')}</p>` : ''}
      <p class="muted">给 ${escape((plan.agents || []).map(nameOf).join('、'))} 用</p>
    </div>`;
    const empty = !plan.install?.length && !plan.reuse?.length;
    actions = `<button class="btn" type="button" data-store-plan-cancel>改一改</button>
      <button class="btn btn-primary" type="button" data-store-install-apply${locked || empty ? ' disabled' : ''}>确认装上</button>`;
  }
  return `<section class="store-get store-get-install" aria-labelledby="store-get-title">
    <h3 id="store-get-title" class="store-get-title">装到哪些 agent</h3>
    <div class="store-agents">${agents}</div>
    <p class="store-cost">${cost}</p>
    ${get.error ? `<p class="store-get-error" role="alert">${escape(get.error)}</p>` : ''}
    ${review}
    <div class="store-get-actions">${actions}</div>
    ${locked && !writeBusy ? '<p class="row-note">只读预览，不能装上。</p>' : ''}
  </section>`;
}

function storeReviewsHtml(card) {
  const me = state.store?.member?.id;
  const reviews = card.reviews || [];
  const others = card.can_rate && !(state.table ? !state.table.write_allowed : !state.writeAllowed) ? reviews.filter(review => review.member.id !== me) : reviews;
  const readOnly = state.table ? !state.table.write_allowed : !state.writeAllowed;
  const editor = card.can_rate && !readOnly ? storeRateHtml(card)
    : `<p class="store-rate-note">${icon('info')}${card.local?.status === 'mine' ? '自己上架的不能评价'
      : readOnly ? '只读预览，不能评价' : '装上以后可以打分'}</p>`;
  const summary = card.rating?.count ? `<span class="sub-note">· ${card.rating.average.toFixed(1)} 分，${card.rating.count} 人</span>` : '';
  return `<h3 class="sub-title">评价 ${summary}</h3>
    ${editor}
    ${others.length ? `<ul class="store-reviews">${others.map(review => `<li>
      <p class="store-review-head"><strong>${escape(review.member.name)}</strong>${starsHtml(review.stars)}<span>${escape(friendlyDate(review.updated_at))}</span></p>
      ${review.text ? `<p class="store-review-text">${escape(review.text)}</p>` : ''}
    </li>`).join('')}</ul>` : card.can_rate ? '' : '<p class="muted store-rate-empty">还没有人评价。</p>'}`;
}

/* 打分：五颗星是一组单选，倒着排再用 CSS 反过来，鼠标移到哪颗，它和左边的都亮。 */
function storeRateHtml(card) {
  const mine = card.my_rating;
  const stars = [5, 4, 3, 2, 1].map(n => `<input class="sr-only" type="radio" name="store-stars" id="store-star-${n}" value="${n}"${mine?.stars === n ? ' checked' : ''}>
    <label for="store-star-${n}" title="${n} 星">${starIcon(false)}<span class="sr-only">${n} 星</span></label>`).join('');
  return `<form class="store-rate" data-store-rate>
    <fieldset class="star-input"><legend>${mine ? '我的评价' : '给它打分'}</legend><span class="star-input-row">${stars}</span></fieldset>
    <input class="input store-rate-text" name="text" maxlength="200" placeholder="一句话说说好不好用（可不填）" aria-label="一句话评价" value="${escape(mine?.text || '')}">
    <span class="store-rate-actions">
      <button class="btn btn-small btn-primary" type="submit">${mine ? '保存' : '提交评价'}</button>
      ${mine ? '<button class="btn btn-small btn-quiet" type="button" data-store-rate-remove>删除</button>' : ''}
    </span>
    <p class="row-note" data-store-rate-message role="status"></p>
  </form>`;
}

$('detail-body').addEventListener('change', event => {
  const box = event.target.closest('[data-store-agent]');
  if (!box || !storeGet) return;
  if (box.checked) storeGet.agents.add(box.dataset.storeAgent);
  else storeGet.agents.delete(box.dataset.storeAgent);
  storeGet.plan = null;
  storeGet.error = '';
  renderStoreDetail();
  $('detail-body').querySelector(`[data-store-agent="${CSS.escape(box.dataset.storeAgent)}"]`)?.focus();
});

$('detail-body').addEventListener('click', async event => {
  if (!state.storeDetail) return;
  const target = event.target;
  if (target.closest('[data-store-install]')) previewStoreInstall();
  else if (target.closest('[data-store-plan-cancel]')) {
    storeGet.plan = null;
    renderStoreDetail();
    $('detail-body').querySelector('[data-store-agent]')?.focus();
  } else if (target.closest('[data-store-install-apply]')) applyStoreInstall(target.closest('[data-store-install-apply]'));
  else if (target.closest('[data-store-goto]')) {
    const button = target.closest('[data-store-goto]');
    const name = button.dataset.storeGoto;
    closeDetail();
    showPage('skills');
    if (state.table?.skills.some(skill => skill.name === name)) openDetail(name);
    // 有新版时顺便检查更新，详情里随后出现“有新版”和更新按钮。
    if (button.hasAttribute('data-store-check') && !updateState.review?.running && !updateState.starting) checkUpdates();
  } else if (target.closest('[data-store-republish]')) {
    const name = target.closest('[data-store-republish]').dataset.storeRepublish;
    closeDetail();
    openPublish([name]);
  } else if (target.closest('[data-store-unlist]')) {
    const button = target.closest('[data-store-unlist]');
    unlistStore(button.dataset.storeUnlist, button);
  } else if (target.closest('[data-store-rate-remove]')) rateStore(target.closest('form'), true);
});

$('detail-body').addEventListener('submit', event => {
  const form = event.target.closest('[data-store-rate]');
  if (!form) return;
  event.preventDefault();
  rateStore(form, false);
});

async function previewStoreInstall() {
  const get = storeGet;
  const card = storeCard(get?.key);
  if (!card || get.reading) return;
  get.reading = true;
  get.error = '';
  renderStoreDetail();
  try {
    get.plan = await api('/api/team/install/preview', {key: card.key, agents: state.agents.map(agent => agent.id).filter(id => get.agents.has(id))});
  } catch (error) {
    get.error = error.message;
  }
  get.reading = false;
  if (storeGet !== get) return;
  renderStoreDetail();
  $('detail-body').querySelector('[data-store-install-apply], [data-store-install]')?.focus();
}

async function applyStoreInstall(button) {
  const get = storeGet;
  const plan = get?.plan;
  const card = storeCard(get?.key);
  if (!plan || !card || writeBusy) return;
  await runWrite({
    button, busyText: '正在装上…',
    onError: error => {
      if (storeGet !== get) return;
      get.plan = null;
      get.error = error.message;
      renderStoreDetail();
    },
    work: async () => {
      const result = await api('/api/team/install/apply', {review: plan.review});
      get.plan = null;
      await refreshEverything();
      await loadStore();
      return result;
    },
    success: result => {
      focusStorePanel('.store-get button, .store-get a');
      return {
      text: result.message || `装上了「${storeTitle(card)}」`,
      undo: result.id ? undoButton => undoStoreInstall(result, undoButton) : null,
      };
    },
  });
}

/* 详情重画会换掉刚按过的按钮：焦点放到面板里的下一个合适位置，找不到就放在「关闭」上。 */
function focusStorePanel(selector) {
  if (!state.storeDetail || $('detail-body').contains(document.activeElement)) return;
  ($('detail-body').querySelector(selector) || $('detail-close')).focus();
}

async function undoStoreInstall(result, button) {
  const done = await undoOperation(result.id, {skills: result.installed || [], message: result.message, button});
  if (done) await loadStore();
  return done;
}

async function rateStore(form, remove) {
  const card = storeCard(state.storeDetail);
  if (!card || !form) return;
  const message = form.querySelector('[data-store-rate-message]');
  const stars = Number(form.querySelector('input[name="store-stars"]:checked')?.value || 0);
  if (!remove && !stars) {
    message.textContent = '先点星星打分。';
    form.querySelector('input[name="store-stars"]').focus();
    return;
  }
  const button = remove ? form.querySelector('[data-store-rate-remove]') : form.querySelector('[type="submit"]');
  await withBusy(button, remove ? '正在删除…' : '正在保存…', async () => {
    try {
      const text = form.elements.text.value.trim();
      await api(remove ? '/api/team/ratings/remove' : '/api/team/ratings',
        remove ? {key: card.key} : {key: card.key, stars, ...(text ? {text} : {})});
      toast({text: remove ? '已删除你的评价。' : '已保存你的评价，同事都能看到。'});
      await loadStore();
      focusStorePanel('[data-store-rate] [type="submit"]');
    } catch (error) {
      if (form.isConnected) message.textContent = error.message;
      else toast({text: error.message, tone: 'error'});
    }
  });
}

/* ---------- 上架：先挑技能、写推荐语，再核对要传的文件 ---------- */

let storePick = null;   // {selected: Map(技能名 → 推荐语), plan}
let storePublishing = false;

async function openPublish(preselect = []) {
  storePick = {selected: new Map(), plan: null};
  $('store-pick-search').value = '';
  showPublishStep('pick');
  $('store-pick-list').innerHTML = '<p class="muted store-pick-loading">正在读取你在用的技能…</p>';
  $('store-publish-message').textContent = '';
  $('store-publish-dialog').showModal();
  try {
    await loadCandidates();
  } catch (error) {
    $('store-pick-list').replaceChildren();
    $('store-publish-message').textContent = error.message;
    return;
  }
  for (const name of preselect) {
    const candidate = storeCandidates.find(item => item.name === name);
    if (candidate && !candidate.unavailable) storePick.selected.set(name, candidate.note || '');
  }
  renderPick();
  if (preselect.length) $('store-pick-list').querySelector('input:checked')?.scrollIntoView({block: 'nearest'});
  $('store-pick-search').focus();
}

function showPublishStep(step) {
  $('store-pick').hidden = step !== 'pick';
  $('store-review').hidden = step !== 'review';
  $('store-publish-back').hidden = step !== 'review';
  $('store-publish-next').hidden = step !== 'pick';
  $('store-publish-apply').hidden = step !== 'review';
  $('store-publish-title').textContent = step === 'pick' ? '上架技能' : '核对要上架的技能';
}

function storePickRowHtml(candidate) {
  const checked = storePick.selected.has(candidate.name);
  const name = candidate.title || candidate.name;
  const state = candidate.listed ? candidate.changed ? '<span class="tag">改过，可上架新版</span>' : '<span class="tag tag-ok">已上架</span>' : '';
  const kind = candidate.unavailable ? '' : candidate.kind === 'files' ? `<span class="flag">${icon('file-kept')}连文件一起传</span>`
    : candidate.kind === 'lark' ? '<span class="flag">飞书官方</span>' : '';
  return `<div class="store-pick-row${checked ? ' is-checked' : ''}">
    <label class="store-pick-main">
      <input type="checkbox" data-store-pick="${escape(candidate.name)}"${checked ? ' checked' : ''}${candidate.unavailable ? ' disabled' : ''}>
      <span class="store-pick-name"><span class="t">${escape(name)}</span>${candidate.title ? `<span class="skill-id">${escape(candidate.name)}</span>` : ''}</span>
      <span class="store-pick-tags">${kind}${state}</span>
    </label>
    ${candidate.unavailable ? `<p class="row-note">${escape(candidate.unavailable)}</p>` : ''}
    ${checked ? `<input class="input store-pick-note" data-store-note="${escape(candidate.name)}" maxlength="200"
      placeholder="${escape(candidate.summary ? `推荐语（不写就用：${candidate.summary}）` : '推荐语：一句话说说它适合干什么（可不填）')}"
      aria-label="${escape(`给「${name}」写推荐语`)}" value="${escape(storePick.selected.get(candidate.name))}">` : ''}
  </div>`;
}

function renderPick() {
  const query = $('store-pick-search').value.trim().toLowerCase();
  const rows = (storeCandidates || []).filter(item => !query ||
    [item.name, item.title, item.summary, item.group].some(value => String(value || '').toLowerCase().includes(query)));
  $('store-pick-list').innerHTML = rows.map(storePickRowHtml).join('')
    || `<div class="empty">${emptyInner('search', storeCandidates?.length ? '没找到，换个词试试' : '还没有在用的技能')}</div>`;
  const count = storePick.selected.size;
  $('store-publish-next').disabled = !count;
  $('store-publish-next').textContent = count ? `下一步：核对 ${count} 个` : '下一步：核对';
  $('store-publish-message').textContent = count ? '' : '挑出你觉得好用、值得推荐给同事的技能。';
}

$('store-pick-search').addEventListener('input', renderPick);

$('store-pick-list').addEventListener('change', event => {
  const box = event.target.closest('[data-store-pick]');
  if (!box) return;
  const name = box.dataset.storePick;
  if (box.checked) storePick.selected.set(name, storeCandidates.find(item => item.name === name)?.note || '');
  else storePick.selected.delete(name);
  renderPick();
  const again = $('store-pick-list').querySelector(`[data-store-pick="${CSS.escape(name)}"]`);
  again?.focus();
});

$('store-pick-list').addEventListener('input', event => {
  const note = event.target.closest('[data-store-note]');
  if (note) storePick.selected.set(note.dataset.storeNote, note.value);
});

function storeReviewHtml(plan) {
  const errors = plan.errors?.length ? `<div class="banner banner-warn">
      <p>${icon('warning')} 这些暂时不能上架，不会上传：</p>
      <ul class="confirm-items">${plan.errors.map(item => `<li>${escape(item.name)}：${escape(item.reason)}</li>`).join('')}</ul>
    </div>` : '';
  const rows = (plan.listings || []).map(item => {
    const what = item.kind === 'files' ? `连文件一起传：${number(item.files?.count || 0)} 个文件，${sizeText(item.files?.size || 0)}`
      : item.kind === 'lark' ? '飞书官方技能，只传名字和说明' : '网上的公开技能，只传名字和来源';
    const blocked = item.files?.blocked || [];
    return `<li class="store-review-row">
      <p class="store-review-name"><strong>${escape(item.title || item.name)}</strong>${item.title && item.title !== item.name ? ` <span class="skill-id">${escape(item.name)}</span>` : ''}</p>
      <p class="row-note">${escape(what)}</p>
      <p class="store-review-note">${item.note ? `“${escape(item.note)}”` : `<span class="muted">没写推荐语，同事看到的是技能自带的一句话。</span>`}</p>
      ${item.auto?.length ? `<p class="row-note">${icon('link')} ${escape(item.auto.map(name => storeCandidates?.find(row => row.name === name)?.title || name).join('、'))} 需要它，一起上架</p>` : ''}
      ${blocked.length ? `<details class="fold fold-inner store-blocked">
        <summary>${icon('lock')} 拦下 ${blocked.length} 个疑似密钥或密码的文件，不会上传</summary>
        <ul class="confirm-items">${blocked.map(file => `<li><span class="mono">${escape(file.path)}</span>：${escape(file.reason)}</li>`).join('')}</ul>
      </details>` : ''}
    </li>`;
  }).join('');
  return errors + (rows ? `<ul class="store-review-list">${rows}</ul>` : '');
}

$('store-publish-next').addEventListener('click', async () => {
  const skills = [...storePick.selected].map(([name, note]) => ({name, ...(note.trim() ? {note: note.trim()} : {})}));
  showPublishStep('review');
  storePick.plan = null;
  $('store-review').replaceChildren();
  $('store-publish-message').textContent = '正在核对要传的文件…';
  $('store-publish-apply').disabled = true;
  $('store-publish-apply').textContent = '上架';
  try {
    const plan = await api('/api/team/listings/preview', {skills});
    if ($('store-review').hidden) return;
    storePick.plan = plan;
    $('store-review').innerHTML = storeReviewHtml(plan);
    const count = plan.listings?.length || 0;
    $('store-publish-message').textContent = plan.message || '';
    $('store-publish-apply').disabled = !count || !teamCanWrite();
    $('store-publish-apply').textContent = count ? `上架 ${count} 个` : '上架';
    $('store-publish-apply').focus();
  } catch (error) {
    $('store-publish-message').textContent = error.message;
  }
});

$('store-publish-back').addEventListener('click', () => {
  storePick.plan = null;
  showPublishStep('pick');
  renderPick();
  $('store-pick-search').focus();
});

$('store-publish-apply').addEventListener('click', async event => {
  const plan = storePick?.plan;
  if (!plan || storePublishing) return;
  storePublishing = true;
  $('store-publish-back').disabled = $('store-publish-close').disabled = true;
  await withBusy(event.currentTarget, '正在上架…', async () => {
    try {
      await api('/api/team/listings/apply', {review: plan.review});
      storePublishing = false;
      $('store-publish-dialog').close();
      toast({text: `已上架 ${plan.listings.length} 个技能，同事现在能看到了。`});
      storeCandidates = null;
      await loadStore();
      if (state.storeMine) await loadCandidates().catch(() => {});
      renderTeam();
    } catch (error) {
      $('store-publish-message').textContent = error.message;
    }
  });
  storePublishing = false;
  $('store-publish-back').disabled = $('store-publish-close').disabled = false;
});

$('store-publish-close').addEventListener('click', () => $('store-publish-dialog').close());
$('store-publish-dialog').addEventListener('cancel', event => { if (storePublishing) event.preventDefault(); });
$('store-publish-dialog').querySelector('form').addEventListener('submit', event => event.preventDefault());

function llmBody() {
  return {provider: $('llm-provider').value, base_url: $('llm-base-url').value, model: $('llm-model').value};
}

$('extra-source-form').addEventListener('submit', event => {
  event.preventDefault();
  const path = $('extra-source-path').value.trim();
  withBusy($('extra-source-add'), '正在记下…', async () => {
    try {
      await saveExtraSource(path, false);
      $('extra-source-path').value = '';
    } catch (error) {
      $('extra-source-message').textContent = error.message;
      toast({text: error.message, tone: 'error'});
    }
  });
});

$('extra-sources').addEventListener('click', event => {
  const button = event.target.closest('[data-extra-index]');
  if (!button) return;
  const path = extraSourcePaths[Number(button.dataset.extraIndex)];
  if (!path) return;
  withBusy(button, '正在去掉…', async () => {
    try {
      await saveExtraSource(path, true);
    } catch (error) {
      $('extra-source-message').textContent = error.message;
      toast({text: error.message, tone: 'error'});
    }
  });
});

$('llm-provider').addEventListener('change', () => {
  [$('llm-base-url').value, $('llm-model').value] = LLM_DEFAULTS[$('llm-provider').value];
});

$('labels-settings').addEventListener('click', () => gotoPlace('settings/ai'));

/* 保存并试一下：连得上就接着整理还没整理完的技能。 */
$('llm-form').addEventListener('submit', event => {
  event.preventDefault();
  withBusy($('llm-save'), '正在保存并测试…', async () => {
    const unlock = lockControls(['#llm-form input', '#llm-form select', '#llm-clear']);
    labelsAutoAttempted = true;
    try {
      await api('/api/settings/llm', {...llmBody(), api_key: $('llm-key').value});
      $('llm-key').value = '';
      const result = await api('/api/settings/llm/test', {});
      $('llm-message').textContent = result.message;
      toast({text: result.message, tone: result.ok ? 'ok' : 'error'});
      await refreshTable(true);
      if (result.ok && state.table.labels_pending) startLabels('missing');
    } catch (error) {
      $('llm-message').textContent = error.message;
      toast({text: error.message, tone: 'error'});
    } finally {
      unlock();
      await refreshSettings();
    }
  });
});

$('llm-clear').addEventListener('click', async event => {
  const ok = await confirmDialog({
    title: '删除已保存的 key？',
    lines: ['删除后不会再自动整理技能的中文名、说明和分组，已经整理好的会保留。'],
    okText: '删除 key',
  });
  if (!ok) return;
  await withBusy(event.currentTarget, '正在删除…', async () => {
    try {
      await api('/api/settings/llm', {...llmBody(), clear_key: true});
      $('llm-key').value = '';
      await refreshTable(true);
      await refreshSettings();
      toast({text: '已删除 key。'});
    } catch (error) {
      toast({text: error.message, tone: 'error'});
    }
  });
});

/* 整理中文名、说明和分组在后台跑，进度显示在页头任务条上。 */
async function startLabels(mode) {
  if (labelsRunning) return;
  labelsRunning = true;
  labelsDone = 0;   // 新的一轮，进度从头看
  labelsAutoAttempted = true;
  renderLabelCounts();
  setTask('labels', {text: '正在准备技能名单…'});
  try {
    const progress = await api('/api/labels/progress');
    if (!progress.running) await api('/api/labels/run', {mode});
    pollLabels();
  } catch (error) {
    labelsRunning = false;
    renderLabelCounts();
    setTask('labels', {text: error.message, tone: 'error', retry: () => startLabels('missing')});
  }
}

async function pollLabels() {
  try {
    const progress = await api('/api/labels/progress');
    if (progress.running) {
      setTask('labels', progress.total
        ? {text: `正在整理中文名和说明 ${progress.done}/${progress.total}`, ratio: progress.done / progress.total}
        : {text: '正在准备技能名单…'});
      // 每整理完一批，后台就存一次；这时刷一下列表，新装的技能不用等整轮做完就有中文名。
      // 只在 done 变了的那一次刷，不是每秒都刷。
      if (progress.done > labelsDone) {
        labelsDone = progress.done;
        await refreshTable(true);
      }
      setTimeout(pollLabels, 1000);
      return;
    }
    labelsRunning = false;
    await refreshTable(true);
    if (progress.error) throw new Error(progress.error);
    clearTask('labels');
    toast({text: progress.message});
  } catch (error) {
    labelsRunning = false;
    renderLabelCounts();
    setTask('labels', {text: error.message, tone: 'error', retry: () => startLabels('missing')});
    toast({text: error.message, tone: 'error'});
  }
}

$('labels-missing').addEventListener('click', () => startLabels('missing'));

$('labels-all').addEventListener('click', async () => {
  const ok = await confirmDialog({
    title: '重新整理全部中文名？',
    lines: ['会重新整理全部中文名、分组和中文说明，原来的会留一份备份。'],
    okText: '重新整理全部',
  });
  if (ok) startLabels('all');
});

async function refreshLog() {
  try {
    state.logs = await api('/api/log');
    renderLog();
  } catch (error) {
    toast({text: error.message, tone: 'error', sticky: true});
  }
}

async function refreshEverything() {
  // Agent 名单独立读取，不阻挡写操作的完成提示。
  await Promise.all([refreshTable(), refreshInventory()]);
  await Promise.all([refreshLog(), loadCleanup()]);
  refreshAgents().catch(error => toast({text: error.message, tone: 'error', sticky: true}));
}

/* ==================== 设置页的 agent 一栏：接入新 agent、不再管理 ==================== */

/* 正在管理的 agent 一行一个；下面是接口发现的新 agent 和已经忽略的。
   忽略名单后台会改成对象数组（带名字、目录、技能数、原因）；还是编号数组时跳过不显示。 */
function ignoredAgents(view) {
  return (view?.ignored || []).filter(item => item && typeof item === 'object');
}

function renderAgents() {
  const view = state.agentsView;
  const discovery = view?.discovery || {};
  const locked = writeBusy || !state.table?.write_allowed ? ' disabled' : '';
  $('agents-scan').disabled = Boolean(discovery.running || writeBusy);
  $('agents-scan').setAttribute('aria-busy', String(Boolean(discovery.running)));
  $('agents-scan').querySelector('span').textContent = discovery.running ? '正在扫描…' : '重新扫描 agent';
  const checked = discovery.checked_at ? `上次检查：${new Date(discovery.checked_at).toLocaleString('zh-CN')}` : '';
  $('agents-scan-status').textContent = discovery.error || [discovery.message, checked].filter(Boolean).join(' · ')
    || '打开时会检查新安装的软件；新装 agent 后，也可以在这里重新扫描。';
  const managed = view?.managed || [];
  const suggested = view?.suggested || [];
  const ignored = ignoredAgents(view);
  $('agents-empty').hidden = !view || managed.length > 0;
  $('agents-managed').innerHTML = managed.map(agent => {
    const shared = (agent.shared_with || []).map(nameOf);
    const info = [
      escape(agent.activation_text || ''),
      shareHtml(shared, `${nameOf(agent.id)} 与${shared.join('、')} 存在共用的技能文件夹。`),
    ].filter(Boolean).join(' · ');
    const leave = agent.can_leave
      ? `<button class="btn btn-small btn-quiet" data-agent-leave="${escape(agent.id)}"${locked}>不再管理…</button>`
      : `<span class="item-note agent-share">${icon('lock')}至少留一个</span>`;
    return `<div class="item-row">
      <div>
        <div class="item-title">${swatch(agent.id)} ${escape(agent.name)}</div>
        <div class="item-note">${info}</div>
      </div>
      <span class="item-end">${leave}</span>
    </div>`;
  }).join('');

  // 新发现的 agent：先列名字与识别依据，接入时再核对技能数量。
  const row = (agent, action) => {
    const found = agent.skill_count === undefined ? '' : `找到 ${number(agent.skill_count)} 个技能`;
    const where = rootsText(agent.skill_roots);
    const why = [where ? `在 ${where} 里${found}` : found, agent.reason].filter(Boolean).join('。');
    return `<div class="item-row">
      <div>
        <div class="item-title">${escape(agent.name)}${agent.ignored ? ' <span class="tag tag-quiet">已忽略</span>' : ''}</div>
        <div class="item-note">${agent.skill_count === undefined ? '' : `${number(agent.skill_count)} 个技能`}${why ? hint(why) : ''}</div>
      </div>
      <span class="item-end">${action}</span>
    </div>`;
  };
  $('agents-suggested-box').hidden = !suggested.length;
  $('agents-suggested').innerHTML = suggested.map(agent => row(agent,
    `${agentJoinButton(agent)}<button class="btn btn-small" data-agent-ignore="${escape(agent.id)}"${locked}>忽略</button>`)).join('');
  $('agents-ignored-box').hidden = !ignored.length;
  $('agents-ignored').innerHTML = ignored.map(agent => row({...agent, ignored: true}, agentJoinButton(agent))).join('');
}

$('agents-scan').addEventListener('click', async () => {
  if (state.agentsView?.discovery?.running || writeBusy) return;
  $('agents-scan').disabled = true;
  $('agents-scan-status').textContent = '正在扫描 agent…';
  try {
    await api('/api/agents/scan', {});
    await refreshAgents();
  } catch (error) {
    state.agentsView.discovery = {...state.agentsView.discovery, running: false, error: error.message + '，请重试。'};
    renderAgents();
  }
});

/* 还没整理过时，agent 一栏指回技能页的首次整理。 */
$('agents-migration-link').addEventListener('click', () => showPage('skills'));

/* 不再管理：先预览，确认框里说明会失去哪些技能，再写入。 */
async function leaveAgent(id) {
  let plan;
  try {
    plan = await api('/api/agents/leave/preview', {agent: id});
  } catch (error) {
    toast({text: error.message, tone: 'error', sticky: true});
    return;
  }
  const lost = plan.lost_skills || [];
  const ok = await confirmDialog({
    title: `不再管理 ${nameOf(id)}？`,
    lines: [plan.message],
    groups: lost.length ? [{label: `${nameOf(id)} 以后看不到这几个技能`, items: lost.map(name => ({name}))}] : [],
    note: '以后可以在设置里重新接入。',
    okText: '不再管理',
  });
  if (!ok) return;
  await runWrite({
    button: document.querySelector(`[data-agent-leave="${id}"]`),
    busyText: '正在处理…',
    work: async () => {
      const result = await api('/api/agents/leave/apply', {agent: id, plan_id: plan.plan_id, revision: plan.revision, confirmed: true});
      await refreshEverything();
      return result;
    },
    success: result => ({
      text: result.message || `不再管理 ${nameOf(id)}`,
      undo: undoButton => undoOperation(result.id, {skills: lost, message: result.message, button: undoButton}),
    }),
  });
}

/* 忽略建议：不弹确认框，写完在提示条上给撤销。 */
async function ignoreAgent(id) {
  const button = document.querySelector(`[data-agent-ignore="${id}"]`);
  await runWrite({
    button,
    busyText: '正在忽略…',
    work: async () => {
      const plan = await api('/api/agents/ignore/preview', {agent: id});
      const result = await api('/api/agents/ignore/apply', {agent: id, plan_id: plan.plan_id, revision: plan.revision, confirmed: true});
      await refreshEverything();
      return result;
    },
    success: result => ({
      text: result.message || `已忽略 ${nameOf(id)}`,
      undo: undoButton => undoOperation(result.id, {message: result.message, button: undoButton}),
    }),
  });
}

let joinPlan = null;
let joiningAgent = null;

/* 已发现的 agent 一次点击完成读取和接入；只有新技能存在多份不同内容时才需要选版本。 */
async function joinAgent(id, button) {
  if (writeBusy || !state.table?.write_allowed) return;
  joiningAgent = {id, text: '正在读取…'};
  let choicePlan = null;
  let sharedNames = [];
  await runWrite({
    button, busyText: '正在读取…', taskText: `正在读取 ${nameOf(id)} 的技能…`,
    work: async () => {
      renderRailNotes();
      renderAgents();
      const plan = await api('/api/agents/join/preview', {agents: [id]});
      if (plan.skills.some(skill => skill.action === 'choose' && !skill.options.some(option => option.id === 'store'))) {
        choicePlan = {...plan, requests: [id]};
        return null;
      }
      const profiles = plan.profiles || [];
      sharedNames = profiles.filter(profile => profile.id !== id).map(profile => profile.name).filter(Boolean);
      if (!profiles.length) sharedNames = (plan.agents || []).filter(agent => agent !== id).map(nameOf);
      const together = sharedNames.join('、');
      joiningAgent.text = together ? `正在接入，一并接入 ${together}…` : '正在接入…';
      setTask('write', {text: together ? `正在接入 ${nameOf(id)}，一并接入 ${together}…` : `正在接入 ${nameOf(id)}…`});
      renderRailNotes();
      renderAgents();
      const result = await api('/api/agents/join/apply', {
        agents: [id], plan_id: plan.plan_id, revision: plan.revision, confirmed: true,
      });
      await refreshEverything();
      await refreshAgents();
      return result;
    },
    success: result => {
      if (!result) return null;
      const missing = sharedNames.filter(name => !(result.message || '').includes(name));
      const text = result.message || '新 agent 已经接入';
      return {
        text: missing.length ? `${text}，一并接入 ${missing.join('、')}` : text,
        undo: undoButton => undoOperation(result.id, {message: result.message, button: undoButton}),
      };
    },
  });
  joiningAgent = null;
  if (choicePlan) {
    joinPlan = choicePlan;
    renderJoin(joinPlan);
    $('agents-join-dialog').showModal();
  }
}

async function showJoinPreview(agents) {
  if (writeBusy) return;
  try {
    joinPlan = await api('/api/agents/join/preview', {agents});
    joinPlan.requests = agents;
  } catch (error) {
    toast({text: error.message, tone: 'error', sticky: true});
    return;
  }
  renderJoin(joinPlan);
  $('agents-join-dialog').showModal();
}

$('agent-add-submit').addEventListener('click', async () => {
  const name = $('agent-add-name').value.trim();
  const folder = $('agent-add-folder').value.trim();
  if (!name || !folder) {
    toast({text: '请填写名字和技能文件夹。', tone: 'error', sticky: true});
    return;
  }
  await showJoinPreview([{name, skill_root: folder}]);
});

$('agents-managed').addEventListener('click', event => {
  const button = event.target.closest('[data-agent-leave]');
  if (button) leaveAgent(button.dataset.agentLeave);
});

/* 只提交用户点选的 agent；后台计算共用目录需要一起接入的完整名单。 */
for (const id of ['rail-notes', 'agents-suggested', 'agents-ignored']) {
  $(id).addEventListener('click', event => {
    const join = event.target.closest('[data-agent-join]');
    const ignore = event.target.closest('[data-agent-ignore]');
    if (join) joinAgent(join.dataset.agentJoin, join);
    else if (ignore) ignoreAgent(ignore.dataset.agentIgnore);
  });
}

/* 每一类的解释放进说明图标，主行只留“这一类：几个”。 */
const JOIN_ACTIONS = [
  ['linked', '已经在用统一仓库', '直接登记分配。'],
  ['link', '内容一致，改成统一管理', '改为使用仓库里的那份。'],
  ['import', '收进统一仓库', '仓库里还没有，收进来。'],
  ['choose', '内容不同，需要你选', '每一条单独选，默认保留仓库里的版本。'],
  ['skipped', '原样留着', '这些内容不动。'],
];

function joinRows(plan, action) {
  return plan.skills.filter(skill => skill.action === action);
}

function joinChoiceRow(skill) {
  const options = skill.options.map(option => `<p class="join-option"><label class="check">
    <input type="radio" name="join-choice-${escape(skill.name)}" data-join-choice="${escape(skill.name)}" value="${escape(option.id)}" ${option.id === skill.default ? 'checked' : ''}>
    ${option.origin === 'store' ? '用统一仓库里的（默认）' : '用这个 agent 里的'}</label>
    <span class="mono">${pathLabel(option.path)}</span></p>`).join('');
  return `<details class="fold fold-inner join-choice" data-join-row="${escape(skill.name)}">
    <summary>${escape(skill.name)} <span class="tag tag-quiet">内容不同</span></summary>
    <p class="item-note">${escape(skill.reason)}</p>
    ${options}
    ${skill.comparison ? `<details class="fold fold-inner"><summary data-join-compare="${escape(skill.name)}">看看差别</summary><pre></pre></details>` : ''}
    <p class="item-note" data-join-warn hidden>选了这一份，会更新所有 agent 用的这份技能。</p>
    <p class="item-note">${(skill.entries || []).map(pathLabel).join('、')}</p>
  </details>`;
}

function renderJoin(plan) {
  $('join-summary').textContent = (plan.profiles || []).map(profile => `${profile.name}（${rootsLabel(profile.skill_roots)}）`).join('；')
    || '这次没有要接入的 agent。';
  $('join-counts').innerHTML = JOIN_ACTIONS.map(([action, label, note]) => {
    const count = joinRows(plan, action).length;
    return count ? `<p><strong>${escape(label)}：${count} 个</strong>${hint(note)}</p>` : '';
  }).join('') || '<p class="item-note">没有读到任何技能。</p>';
  $('join-choices').innerHTML = joinRows(plan, 'choose').map(joinChoiceRow).join('');
  const skipped = plan.skipped || [];
  $('join-skipped-box').hidden = !skipped.length;
  $('join-skipped').innerHTML = skipped.map(item => `<p class="mono">${pathLabel(item.path)} · ${escape(item.reason)}</p>`).join('');
  $('join-message').textContent = `${(plan.skills || []).length} 个技能会登记给 ${(plan.profiles || []).map(profile => profile.name).join('、')}。`;
  $('join-apply').disabled = false;
}

$('join-close').addEventListener('click', () => $('agents-join-dialog').close());

$('join-choices').addEventListener('change', event => {
  const input = event.target.closest('[data-join-choice]');
  if (!input) return;
  const row = input.closest('[data-join-row]');
  if (row) row.querySelector('[data-join-warn]').hidden = input.value === 'store';
});

$('join-choices').addEventListener('click', async event => {
  const name = event.target.dataset.joinCompare;
  if (!name) return;
  const pre = event.target.parentElement.querySelector('pre');
  if (pre.textContent) return;
  const skill = (joinPlan?.skills || []).find(item => item.name === name);
  pre.textContent = skill?.comparison || '两份内容没有文本差别。';
});

$('join-apply').addEventListener('click', async event => {
  if (!joinPlan) return;
  const choices = Object.fromEntries([...document.querySelectorAll('[data-join-choice]:checked')].map(input => [input.dataset.joinChoice, input.value]));
  await runWrite({
    button: event.currentTarget,
    busyText: '正在接入…',
    onError: error => { $('join-message').textContent = error.message; },
    work: async () => {
      const result = await api('/api/agents/join/apply', {agents: joinPlan.requests, plan_id: joinPlan.plan_id, revision: joinPlan.revision, choices, confirmed: true});
      $('agents-join-dialog').close();
      joinPlan = null;
      await refreshEverything();
      return result;
    },
    success: result => ({
      text: result.message || '新 agent 已经接入',
      undo: undoButton => undoOperation(result.id, {message: result.message, button: undoButton}),
    }),
  });
});

/* ==================== 技能详情：右侧面板 ==================== */

function entriesFor(name) {
  const rows = [];
  for (const root of state.inventory?.roots || []) {
    if (root.category !== 'user') continue;
    for (const skill of root.skills || []) {
      if (skill.name === name) rows.push({root, skill});
    }
  }
  return rows;
}

function renderDetail(assignmentsOnly = false) {
  const skill = state.table?.skills.find(s => s.name === state.detail);
  if (!skill) { closeDetail(); return; }
  $('detail-title').textContent = skill.title || skill.name;
  const locked = Boolean(writeBusy) || !state.table.write_allowed;
  const disabledFor = new Set(state.agents.filter(agent => (state.inventory?.disabled?.[agent.id]?.names || []).includes(skill.name)).map(agent => agent.id));

  const who = state.agents.map(agent => {
    const on = assignedAfter(skill.name, agent.id);
    const pending = on !== assignedNow(skill.name, agent.id);
    let text;
    if (pending) text = pendingNote(skill.name, agent.id);
    else if (!on) text = cellNote(skill, agent) || (disabledFor.has(agent.id) ? `${agent.name} 里停用了` : '没用');
    else text = '在用';
    return `<div class="who-row${pending ? ' is-pending' : ''}">
      <span class="who-name">${swatch(agent.id)}${escape(agent.name)}</span>
      <p${pending ? ' class="is-pending"' : ''}>${escape(text)}</p>
      ${switchHtml(skill, agent, on, pending, locked)}
    </div>`;
  }).join('');

  // 拨动详情里的开关时，只更新状态和说明，保留正在滑动的按钮。
  if (assignmentsOnly) {
    const rows = [...$('detail-body').querySelectorAll('.who-row')];
    const template = document.createElement('template');
    template.innerHTML = who;
    const next = [...template.content.children];
    if (rows.length === next.length && rows.every((row, index) => row.querySelector('.switch').dataset.agent === next[index].querySelector('.switch').dataset.agent)) {
      rows.forEach((row, index) => {
        row.className = next[index].className;
        row.querySelector('p').replaceWith(next[index].querySelector('p'));
        const button = row.querySelector('.switch'), updated = next[index].querySelector('.switch');
        button.className = updated.className;
        button.setAttribute('aria-checked', updated.getAttribute('aria-checked'));
        button.disabled = updated.disabled;
      });
      return;
    }
  }

  const requires = skill.requires.map(name => `<span class="chip"><button data-open="${escape(name)}">${escape(titleOf(name))}</button>
      <button class="chip-x" data-dep-remove="${escape(skill.name)}" data-dep-name="${escape(name)}"
        aria-label="${escape('不再一起带上 ' + titleOf(name))}" ${locked ? 'disabled' : ''}>移除</button></span>`).join('');
  const requiredBy = (state.table?.skills || []).filter(s => s.requires.includes(skill.name))
    .map(s => `<span class="chip chip-solo"><button data-open="${escape(s.name)}">${escape(titleOf(s.name))}</button></span>`).join('');
  const options = (state.table?.skills || [])
    .filter(s => s.name !== skill.name && !skill.requires.includes(s.name))
    .sort((a, b) => titleOf(a.name).localeCompare(titleOf(b.name), 'zh-CN'))
    .map(s => `<option value="${escape(s.name)}">${escape(s.title ? `${s.title}（${s.name}）` : s.name)}</option>`).join('');

  const entries = entriesFor(skill.name).map(({root, skill: entry}) => {
    const manual = (entry.manual_only_for || []).map(nameOf);
    const status = [
      entry.broken ? '链接已失效' : entry.kind === 'link' ? '链接到统一仓库' : '存放完整文件',
      (root.readers || []).map(nameOf).join(' / '),
      manual.length ? `${manual.join('、')} 只允许手动调用` : '',
      entry.model_invocable === false ? '只允许手动调用' : '',
      entry.name_mismatch ? '名称与文件夹名称不同' : '',
      entry.warning || '',
    ].filter(Boolean).join(' · ');
    return `<li><p class="entry-path">${pathLabel(entry.path)}</p><p class="entry-status">${escape(status)}</p></li>`;
  }).join('');

  const update = updateRow(skill.name);
  const tags = detailTagsHtml(skill, locked || detailTagBusy);
  const zh = skill.description_zh;
  // 有些说明本来就是中文，这时没有“英文原文”可说
  const original = /[\u4e00-\u9fff]/.test(skill.description) ? '看原文' : '看英文原文';

  $('detail-body').innerHTML = `
    ${skill.title ? `<p class="detail-id">${escape(skill.name)}</p>` : ''}
    ${update ? detailUpdateHtml(update) : ''}
    ${skill.summary ? `<p class="detail-summary">${escape(skill.summary)}</p>` : ''}
    ${tags}

    <h3 class="sub-title">谁在用</h3>
    <div class="who">${who}</div>

    <h3 class="sub-title">会一起带上${hint('给某个 agent 加上它时，这些技能会自动一起加上。')}</h3>
    ${requires ? `<div class="chips">${requires}</div>` : `<p class="item-note">${icon('link')} 不需要别的技能</p>`}
    <details class="fold fold-inner">
      <summary>加一个要一起带上的技能</summary>
      <p class="row-wrap">
        <select id="dep-select" class="input" aria-label="选择要一起带上的技能"><option value="">选一个技能…</option>${options}</select>
        <button class="btn btn-small" id="dep-add" data-dep-add="${escape(skill.name)}" disabled>${icon('plus')}<span>加上</span></button>
        ${hint(`加上后，用「${titleOf(skill.name)}」的 agent 会自动一起分到它。可以撤销。`)}
      </p>
    </details>
    ${requiredBy ? `<h3 class="sub-title">谁要用它</h3><div class="chips">${requiredBy}</div>` : ''}

    <h3 class="sub-title">${zh ? '说明' : 'agent 实际读到的说明'} <span class="sub-note">· 约 ${tokenText(skill.description_tokens)} Token</span>${hint('每次开新对话，agent 都要先读一遍这段说明，大约占这么多 Token。')}</h3>
    ${zh ? `<p class="detail-desc">${escape(zh)}</p>
      <details class="fold fold-inner"><summary>${original}（agent 实际读到的）</summary><p class="detail-desc">${escape(skill.description)}</p></details>`
      : `<p class="detail-desc">${escape(skill.description)}</p>`}

    <h3 class="sub-title">其他</h3>
    <dl class="detail-list">
      <dt>从哪来</dt><dd>${escape(skill.source_label || '没记录')}</dd>
      <dt>改过没有</dt><dd>${skill.local_modified ? `${icon('edit')} 你改过${hint('你在这台电脑上改过它，更新时会问你要哪一份。')}` : `${icon('check-circle')} 没改过`}</dd>
      <dt>更新提醒</dt><dd class="row-wrap">${['all', 'description', 'off'].map(mode => {
        const label = {all: '照常提醒', description: '只看说明有没有变', off: '先不提醒'}[mode];
        const on = (skill.update_watch || 'all') === mode;
        return `<button class="btn btn-small${on ? '' : ' btn-quiet'}" data-watch="${mode}" data-watch-skill="${escape(skill.name)}" ${on || locked ? 'disabled' : ''}>${escape(label)}</button>`;
      }).join('')}</dd>
      <dt>分组</dt><dd>${escape(groupOf(skill.name))}</dd>
    </dl>

    <details class="fold fold-inner">
      <summary data-hint="排查问题时用，平时不用看。">技术细节</summary>
      <dl class="detail-list">
        <dt>存放位置</dt><dd class="mono">${pathLabel(homePath() + '/.skill-pilot/store/' + skill.name)}</dd>
      </dl>
      ${entries ? `<ul class="entry-list">${entries}</ul>` : '<p class="item-note">没找到这个技能在 agent 文件夹里的入口。</p>'}
    </details>`;
}

let detailOpener = null;
let detailHeld = [];
let detailScrollLock = null;

/* 折叠起来的内容不能走到；折叠标题本身还在，可以展开。 */
function closedDetailsHides(element) {
  const summary = element.closest('summary');
  let node = summary ? summary.parentElement?.parentElement : element.parentElement;
  while (node) {
    if (node.tagName === 'DETAILS' && !node.open) return true;
    node = node.parentElement;
  }
  return false;
}

/* 面板里能用 Tab 走到的控件。禁用的、藏起来的、折叠起来的都不算。 */
function detailTabbables(panel) {
  return [...panel.querySelectorAll('*')].filter(element => {
    if (element.tabIndex < 0 || element.disabled) return false;
    if (element.tagName === 'INPUT' && String(element.type || '').toLowerCase() === 'hidden') return false;
    if (element.tagName === 'A' && !element.hasAttribute('href')) return false;
    if (element.closest('[hidden]')) return false;
    if (closedDetailsHides(element)) return false;
    return true;
  });
}

/* 详情开着时，Tab 和 Shift+Tab 只在面板里转圈，焦点不落到遮罩后面。 */
function trapDetailTab(event) {
  const panel = $('detail-panel');
  // 开始收起后就不再拦 Tab，免得焦点被拉回正在消失的面板。
  if (!(state.detail || state.storeDetail) || panel.hidden) return;
  const items = detailTabbables(panel);
  if (!items.length) {
    event.preventDefault();
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  const inside = panel.contains(active);
  const atStart = !inside || active === first;
  const atEnd = !inside || active === last;
  if (event.shiftKey ? atStart : atEnd) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  }
}

/* 详情开着时，后面的页面不能被点到，也不能被键盘走到。
   确认框另外盖在上面，不在这里拦。 */
function detailBackground() {
  return [...document.body.children].filter(element =>
    element.id !== 'detail-panel' && element.id !== 'panel-scrim' && element.id !== 'tip'
      && element.tagName !== 'DIALOG' && element.tagName !== 'DATALIST');
}

/* 详情开着时，后面的页面不再跟着翻页键滚动；面板里的长内容仍可自己滚动。 */
function setDetailScrollLock(lock) {
  const root = document.documentElement;
  if (lock) {
    if (detailScrollLock) return;
    detailScrollLock = {root: root.style.overflow, body: document.body.style.overflow};
    root.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
    return;
  }
  if (!detailScrollLock) return;
  root.style.overflow = detailScrollLock.root;
  document.body.style.overflow = detailScrollLock.body;
  detailScrollLock = null;
}

function holdDetailBackground(hold) {
  if (hold) {
    if (detailHeld.length) return;
    detailHeld = detailBackground().filter(element => !element.inert);
    for (const element of detailHeld) element.inert = true;
    setDetailScrollLock(true);
    return;
  }
  for (const element of detailHeld) if (element.isConnected) element.inert = false;
  detailHeld = [];
  setDetailScrollLock(false);
}

function openDetail(name) {
  if (state.detail !== name || $('detail-panel').hidden) detailOpener = document.activeElement?.closest('#skills-list') ? document.activeElement : null;
  state.detail = name;
  renderDetail();
  showDetailPanel(() => state.detail === name);
}

/* 技能详情和团队商店的详情共用这块面板。stillOpen 判断动画开始前用户有没有换走。 */
function showDetailPanel(stillOpen) {
  const panel = $('detail-panel');
  panel.hidden = false;
  $('panel-scrim').hidden = false;
  $('detail-body').scrollTop = 0;
  requestAnimationFrame(() => {
    if (!stillOpen() || panel.hidden) return;
    panel.classList.add('is-open');
  });
  // 先把焦点放进面板，再锁住后面的页面，避免焦点在中途落到页脚并带着页面滚。
  $('detail-close').focus();
  holdDetailBackground(true);
}

function closeDetail() {
  const panel = $('detail-panel');
  panel.classList.remove('is-open');
  const storeKey = state.storeDetail;
  state.detail = null;
  state.storeDetail = null;
  storeGet = null;
  setTimeout(() => {
    if (state.detail || state.storeDetail) return;
    panel.classList.remove('is-open');
    panel.hidden = true;
    $('panel-scrim').hidden = true;
  }, 200);
  holdDetailBackground(false);
  if (storeKey) {
    // 商店的卡片或列表行可能已经重画过：按卡片编号找回来，把焦点还给它。
    const selector = `[data-store-open="${CSS.escape(storeKey)}"]`;
    const opener = storeOpener?.isConnected ? storeOpener
      : (storeOpenerArea && $(storeOpenerArea)?.querySelector(selector)) || $('page-team').querySelector(selector);
    opener?.focus();
    storeOpener = null;
    return;
  }
  // 列表可能已经重画过：按技能名找回打开详情的那一行，把焦点还给它。
  const openerName = detailOpener?.dataset?.open || detailOpener?.dataset?.updateUp;   // 从技能名或更新小箭头打开的
  const opener = detailOpener?.isConnected ? detailOpener
    : openerName ? $('skills-list').querySelector(`[data-open="${CSS.escape(openerName)}"]`) : null;
  opener?.focus();
  detailOpener = null;
}

function onDetailKey(event) {
  if (document.querySelector('dialog[open]')) return;
  if (event.key === 'Tab') trapDetailTab(event);
  else if (event.key === 'Escape' && !$('detail-panel').hidden) closeDetail();
}

$('detail-close').addEventListener('click', closeDetail);
$('panel-scrim').addEventListener('click', closeDetail);
document.addEventListener('keydown', onDetailKey);

$('detail-body').addEventListener('change', event => {
  if (event.target.id === 'dep-select') $('dep-add').disabled = !event.target.value || Boolean(writeBusy);
  else if (event.target.dataset.updateChoice) chooseUpdate(event.target);
});

$('detail-body').addEventListener('input', event => {
  if (event.target.id !== 'detail-tag-input') return;
  $('detail-tag-error').hidden = true;
  updateDetailTagAdd();
});

$('detail-body').addEventListener('submit', event => {
  if (event.target.id !== 'detail-tag-form') return;
  event.preventDefault();
  const input = event.target.querySelector('#detail-tag-input');
  writeDetailTag('add', input?.value || '');
});

$('detail-body').addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.skill && button.dataset.agent) { toggleAssignment(button.dataset.skill, button.dataset.agent); return; }
  if (button.dataset.open) { openDetail(button.dataset.open); return; }
  if (button.dataset.tagRemove) { await writeDetailTag('remove', button.dataset.tagRemove); return; }
  if (button.dataset.tagPick) {
    // 点详情里的标签：关掉面板，回到列表按它筛
    closeDetail();
    state.tag = button.dataset.tagPick;
    renderSkillTable();
    window.scrollTo({top: 0, behavior: 'auto'});
    return;
  }
  if (button.dataset.watch) {
    const mode = button.dataset.watch;
    const name = button.dataset.watchSkill;
    const words = {
      all: '以后照常提醒有新版',
      description: '以后只看技能说明有没有变，旁边的资料变了不提醒',
      off: '以后检查更新时先不提醒',
    };
    const ok = await confirmDialog({
      title: '更新提醒',
      lines: [`「${titleOf(name)}」：${words[mode]}。`, '其他技能不变。'],
      note: '可以撤销。下次检查更新时生效。',
      okText: '保存',
    });
    if (!ok) return;
    await runWrite({
      button,
      busyText: '正在保存…',
      work: async () => {
        await api('/api/updates/watch', {name, mode, revision: state.table.revision, confirmed: true});
        await refreshEverything();
        return true;
      },
      success: () => ({text: `「${titleOf(name)}」${words[mode]}`}),
    });
    return;
  }
  if (await handleUpdateButton(button)) return;
  const addName = button.dataset.depAdd;
  const removeName = button.dataset.depRemove;
  if (addName) {
    const requires = $('dep-select')?.value;
    if (!requires) return;
    const ok = await confirmDialog({
      title: '加一个要一起带上的技能',
      lines: [`以后给 agent 加上「${titleOf(addName)}」时，会自动一起加上「${titleOf(requires)}」。现在用着「${titleOf(addName)}」的 agent 也会一起分到它。`],
      note: '可以撤销。', okText: '加上',
    });
    if (!ok) return;
    await decideDependency(addName, requires, true, button);
  } else if (removeName) {
    const ok = await confirmDialog({
      title: '不再一起带上',
      lines: [`以后给 agent 加上「${titleOf(removeName)}」时，不再自动带上「${titleOf(button.dataset.depName)}」。现在的分配保持不变。`],
      note: '可以撤销。', okText: '移除',
    });
    if (!ok) return;
    await decideDependency(removeName, button.dataset.depName, false, button);
  }
});

async function decideDependency(skill, requires, confirmed, button) {
  await runWrite({
    button,
    busyText: '正在处理…',
    work: async () => {
      await api('/api/dependencies', {decisions: [{skill, requires, confirmed}], revision: state.table.revision, confirmed: true});
      await refreshEverything();
      return true;
    },
    success: () => ({text: confirmed ? '以后会一起带上这个技能' : '以后不再一起带上它'}),
  });
}

/* ==================== 安装技能：左栏粘贴框 + 概要卡片，详细挑选再开窗口 ==================== */

let railReview = null;            // 左栏卡片记着的这次读取结果
let installSummaryExpanded = false; // 读到多个技能时，卡片是否已经展开全部
let dialogReview = null;          // 安装窗口这次显示的结果，点「安装」时用它
let installReadError = '';        // 读取失败的原因；卡片上带「重试」
let installInspectSequence = 0;   // 读取的先后顺序：后发的记更大的号，旧结果回来时丢掉
let installInspectedUrl = '';     // 已经读过（或正在读）的链接，同一个链接不重复读
let installReadingUrl = '';       // 正在读的链接，避免同一个链接同时读两次
let installInspectTimer = null;   // 输入停下来之后再读
const INSTALL_URL_PATTERN = /^https:\/\/github\.com\/\S+\/\S+/;

/* 能直接装的：不在同名名单里的。已经装过和要先对比的都不算。 */
function installReadyNames(review) {
  const conflicts = new Set((review.conflicts || []).map(item => item.name));
  return (review.skills || []).filter(skill => !conflicts.has(skill.name)).map(skill => skill.name);
}

/* 一句话用途：取说明里的第一句。太长就截断，卡片上不铺开整篇说明。 */
function installPurpose(description) {
  const text = String(description || '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  const end = text.search(/[。！？!?]|\.(?!\d)/);
  const sentence = end < 0 ? text : text.slice(0, end + 1);
  return sentence.length > 160 ? sentence.slice(0, 160).trimEnd() + '…' : sentence;
}

/* 概要：每个技能的名字和一句话用途，再加上几个能直接装、几个已经装过、几个同名要先对比。 */
function installSummary(review) {
  const skills = review.skills || [];
  const conflicts = review.conflicts || [];
  const installed = conflicts.filter(item => item.same_content).length;
  const compare = conflicts.length - installed;
  const ready = installReadyNames(review).length;
  if (!skills.length) return {title: '这个链接里没找到技能', notes: [], items: []};
  const notes = [];
  if (ready) notes.push(`${ready} 个可以直接装`);
  if (installed) notes.push(`${installed} 个已经装过`);
  if (compare) notes.push(`${compare} 个同名，要先对比`);
  const byName = new Map(conflicts.map(item => [item.name, item]));
  const items = skills.map(skill => {
    const conflict = byName.get(skill.name);
    const status = conflict ? (conflict.same_content ? '已经装过' : '要先对比') : '';
    return {name: skill.name, purpose: installPurpose(skill.description), status};
  });
  return {title: `读到 ${skills.length} 个技能`, notes, items};
}

/* 左栏卡片：不另开窗口就能看到装什么、有什么用、给谁。多于三个先显示前三个。 */
function installSummaryCard(review, agentName, expanded, writable) {
  const summary = installSummary(review);
  const ready = installReadyNames(review);
  const conflicts = review.conflicts || [];
  const pickable = ready.length + conflicts.filter(item => !item.same_content).length > 0;
  const preview = 3;
  const shown = expanded ? summary.items : summary.items.slice(0, preview);
  const hidden = summary.items.length - shown.length;
  const showTarget = Boolean(agentName) && (ready.length > 0 || conflicts.some(item => !item.same_content));
  const items = shown.map(item => {
    const tag = item.status
      ? `<span class="tag ${item.status === '要先对比' ? 'tag-warn' : 'tag-quiet'}">${escape(item.status)}</span>`
      : '';
    const purpose = item.purpose ? `<span class="install-summary-purpose">${escape(item.purpose)}</span>` : '';
    const detail = purpose || tag ? `<p class="item-note install-summary-detail">${purpose}${tag}</p>` : '';
    return `<li><p class="install-summary-name">${escape(item.name)}</p>${detail}</li>`;
  }).join('');
  const more = hidden
    ? '<button type="button" class="link-btn" data-install-more>查看全部</button>'
    : (expanded && summary.items.length > preview ? '<button type="button" class="link-btn" data-install-more="open">收起</button>' : '');
  const facts = [];
  if (showTarget) facts.push(`装给 ${agentName}`);
  if (summary.notes.length) facts.push(summary.notes.join('，'));
  return `<p class="rail-note-title">${icon('plus')}<strong>${escape(summary.title)}</strong></p>
    ${items ? `<ul class="install-summary">${items}</ul>` : ''}
    ${more}
    ${facts.length ? `<p class="item-note">${escape(facts.join(' · '))}。</p>` : ''}
    <div class="install-actions">
      ${ready.length && agentName ? `<button class="btn btn-small btn-primary" data-install-all${writable ? '' : ' disabled'}>全部装给 ${escape(agentName)}</button>` : ''}
      <div class="install-actions-row">
        ${pickable ? '<button class="btn btn-small" data-install-pick>挑一挑</button>' : ''}
        ${installDismissButton()}
      </div>
    </div>`;
}

const installDismissButton = () =>
  `<button class="btn btn-quiet btn-iconic" data-install-dismiss aria-label="关闭这张卡片" title="关闭">${icon('close')}</button>`;

/* 左栏安装区：粘贴框下面按状态显示一张卡片（正在读取 / 概要 / 没读成），读取不挡住页面。 */
function renderInstallRail() {
  const card = $('install-card');
  const writable = Boolean(state.table?.write_allowed) && !writeBusy;
  $('install-url').disabled = !writable;
  if (installReadingUrl) {
    card.hidden = false;
    card.innerHTML = `<p class="rail-note-title"><strong>正在读取…</strong></p>
      <p class="item-note">页面照常能用，读完这里显示概要。</p>`;
    return;
  }
  if (installReadError) {
    card.hidden = false;
    card.innerHTML = `<p class="rail-note-title">${icon('warning')}<strong>没读成</strong></p>
      <p class="item-note">${escape(installReadError)}</p>
      <div class="install-actions-row">
        <button class="btn btn-small" data-install-retry${writable ? '' : ' disabled'}>重试</button>
        ${installDismissButton()}
      </div>`;
    return;
  }
  if (!railReview) {
    card.hidden = true;
    card.innerHTML = '';
    return;
  }
  const agent = currentAgent();
  card.hidden = false;
  card.innerHTML = installSummaryCard(railReview, agent ? agent.name : '', installSummaryExpanded, writable);
}

/* 收起卡片。装好了连粘贴框一起清空；用户点关闭只收卡片，链接留着还能再按回车重读。 */
function clearInstallRail(clearUrl = false) {
  railReview = null;
  installReadError = '';
  installInspectedUrl = '';
  installSummaryExpanded = false;
  if (clearUrl) $('install-url').value = '';
  renderInstallRail();
}

/* 打开安装窗口：挑一挑时带上左栏刚读到的结果；收编遇到同名时不带，只看差别。 */
function openInstall(review = null) {
  dialogReview = review;
  $('install-message').textContent = '';
  $('install-skills').innerHTML = '';
  $('incoming-conflicts').hidden = true;
  $('incoming-comparisons').innerHTML = '';
  $('install-apply').disabled = true;
  // 默认勾上技能页正在看的那个 agent，用户可以多勾或取消。
  const agent = currentAgent();
  $('install-agents').innerHTML = state.agents
    .map(a => `<label class="check"><input type="checkbox" data-install-agent="${escape(a.id)}"${a.id === agent?.id ? ' checked' : ''}> ${swatch(a.id)}${escape(a.name)}</label>`).join('');
  $('install-dialog').showModal();
  if (review) renderInstallSkills(review);
}

/* 读取链接：粘贴、按回车，或输入停下来约 0.6 秒且内容像完整的 GitHub 链接时自动进行。
   同一个链接读过就不再读；读取中换了链接，旧的结果回来直接丢掉。 */
async function inspectInstall(force = false) {
  const url = $('install-url').value.trim();
  if (!url || url === installReadingUrl) return;
  if (!force && (url === installInspectedUrl || !INSTALL_URL_PATTERN.test(url))) return;
  installInspectedUrl = url;
  installReadingUrl = url;
  const sequence = ++installInspectSequence;
  railReview = null;
  installReadError = '';
  installSummaryExpanded = false;
  renderInstallRail();
  try {
    const review = await api('/api/install/inspect', {url});
    if (sequence !== installInspectSequence) return;
    railReview = review;
    installReadingUrl = '';
    renderInstallRail();
  } catch (error) {
    if (sequence !== installInspectSequence) return;
    railReview = null;
    installInspectedUrl = '';   // 没读成，再粘贴同一个链接还要能重读（不只是靠「重试」）
    installReadingUrl = '';
    installReadError = error.message;
    renderInstallRail();
  }
}

/* 「全部装给 X」：只装能直接装的，点了直接装，不弹确认框，完成提示带撤销。 */
async function installAllForCurrent(button) {
  const review = railReview;
  const agent = currentAgent();
  const skills = review && agent ? installReadyNames(review) : [];
  if (!skills.length) return;
  const result = await runWrite({
    button,
    busyText: '正在安装…',
    work: async () => {
      const written = await api('/api/install/apply', {review_id: review.id, skills, agents: [agent.id], revision: state.table.revision, confirmed: true});
      if (written.conflicts?.length) return written;
      clearInstallRail(true);
      await refreshEverything();
      // 装完不用刷新：配了 AI 服务就让后台接着补中文名，正在整理的那批做完就轮到新技能。
      if (state.table.llm_configured) startLabels('missing');
      return written;
    },
    success: written => (written.conflicts?.length ? null : {
      text: installDoneText(skills, [agent.id]),
      undo: undoButton => undoOperation(written.id, {skills, message: written.message, button: undoButton}),
    }),
  });
  if (result?.conflicts?.length) {
    // 能直接装的都不在同名名单里，正常不会走到这里；真遇到就打开窗口让用户对比。
    openInstall(review);
    $('install-message').textContent = '有同名技能需要先对比，请看下面的差别。';
    await showIncomingConflicts(result, review.id);
  }
}

$('install-url').addEventListener('input', () => {
  clearTimeout(installInspectTimer);
  installInspectTimer = setTimeout(() => inspectInstall(), 600);
});
$('install-url').addEventListener('paste', () => {
  clearTimeout(installInspectTimer);
  setTimeout(() => inspectInstall(), 0);
});
$('install-url').addEventListener('keydown', event => {
  if (event.key !== 'Enter') return;
  event.preventDefault();   // 回车＝明确要读，链接不完全像也读一次，读不成由后台说明原因
  clearTimeout(installInspectTimer);
  inspectInstall(true);
});

/* 卡片上的按钮：重试、全部装给、查看全部、挑一挑、关闭。 */
$('install-rail').addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.installRetry !== undefined) { clearTimeout(installInspectTimer); inspectInstall(true); }
  else if (button.dataset.installAll !== undefined) installAllForCurrent(button);
  else if (button.dataset.installMore !== undefined) {
    installSummaryExpanded = button.dataset.installMore !== 'open';
    renderInstallRail();
    const moreButton = $('install-card').querySelector('[data-install-more]');
    if (moreButton) moreButton.focus();
  }
  else if (button.dataset.installPick !== undefined) openInstall(railReview);
  else if (button.dataset.installDismiss !== undefined) clearInstallRail();
});

/* 找到的技能默认全勾；已经装过的显示“已经装过”且不能勾，同名但内容不同的默认不勾。 */
function renderInstallSkills(review) {
  const conflicts = new Map((review.conflicts || []).map(item => [item.name, item]));
  const skills = review.skills || [];
  $('install-skills').innerHTML = skills.map(skill => {
    const conflict = conflicts.get(skill.name);
    if (conflict?.same_content) {
      return `<div>
        <label class="check"><input type="checkbox" data-install-skill="${escape(skill.name)}" disabled> ${escape(skill.name)}</label>
        <p class="item-note"><span class="tag tag-quiet">${icon('check-circle')}已经装过</span>${hint('这份已经装过了，不用再装。')}</p>
      </div>`;
    }
    return `<div>
      <label class="check"><input type="checkbox" data-install-skill="${escape(skill.name)}"${conflict ? '' : ' checked'}> ${escape(skill.name)}</label>
      <p class="item-note">${escape((skill.description || '').slice(0, 160))}</p>
      ${conflict ? `<p class="item-note"><span class="tag tag-warn">${icon('warning')}已有同名技能</span>${hint('安装时会先让你对比两份内容。')}</p>` : ''}
    </div>`;
  }).join('') || '<p class="item-note">这个链接里没找到技能。</p>';
  const allInstalled = skills.length > 0 && skills.every(skill => conflicts.get(skill.name)?.same_content);
  $('install-message').textContent = !skills.length ? '这个链接里没找到技能。'
    : allInstalled ? '这些技能都已经装过了'
    : `找到 ${skills.length} 个技能。`;
  renderInstallApply();
  // 焦点落到「安装」上，接着按一下回车就能装；全装过时按钮点不动，焦点留在原来的位置。
  if (!$('install-apply').disabled) $('install-apply').focus();
}

/* 至少勾了一个技能、而且当前能改也没在写入，才能点「安装」。 */
function renderInstallApply() {
  $('install-apply').disabled = !dialogReview || Boolean(writeBusy) || !state.table?.write_allowed
    || !document.querySelector('[data-install-skill]:checked');
}

$('install-skills').addEventListener('change', renderInstallApply);
$('install-close').addEventListener('click', () => $('install-dialog').close());

async function showIncomingConflicts(result, reviewId) {
  $('incoming-conflicts').hidden = false;
  $('incoming-comparisons').innerHTML = '';
  for (const conflict of result.conflicts || []) {
    const incoming = state.table?.new_skills?.find(s => s.name === conflict.name);
    try {
      const comparison = await api('/api/incoming/compare', {name: conflict.name, review_id: reviewId, relative: incoming?.relative});
      const item = document.createElement('details');
      item.className = 'fold fold-inner';
      item.open = true;
      const summary = document.createElement('summary');
      summary.textContent = conflict.name;
      const pre = document.createElement('pre');
      pre.textContent = comparison.comparison;
      item.append(summary, pre);
      $('incoming-comparisons').append(item);
    } catch (error) {
      toast({text: error.message, tone: 'error', sticky: true});
    }
  }
}

/* 完成提示：一个技能写名字，多个写数量；给谁用照 agent 的名字写。 */
function installDoneText(skills, agents) {
  const what = skills.length === 1 ? `装好了 ${skills[0]}` : `装好了 ${skills.length} 个技能`;
  return what + (agents.length ? `，给了 ${agents.map(nameOf).join('、')}` : '，还没给 agent 用');
}

/* 只点一次安装：不再弹确认框，装完直接关窗、刷新列表，中文名在后台补。 */
$('install-apply').addEventListener('click', async event => {
  const button = event.currentTarget;
  if (!dialogReview) return;
  const skills = [...document.querySelectorAll('[data-install-skill]:checked')].map(input => input.dataset.installSkill);
  const agents = [...document.querySelectorAll('[data-install-agent]:checked')].map(input => input.dataset.installAgent);
  if (!skills.length) { $('install-message').textContent = '请至少勾选一个技能。'; return; }
  const result = await runWrite({
    button,
    busyText: '正在安装…',
    onError: error => { $('install-message').textContent = error.message; },
    work: async () => {
      const written = await api('/api/install/apply', {review_id: dialogReview.id, skills, agents, revision: state.table.revision, confirmed: true});
      if (written.conflicts?.length) return written;
      $('install-dialog').close();
      clearInstallRail(true);
      await refreshEverything();
      // 装完不用刷新：配了 AI 服务就让后台接着补中文名，正在整理的那批做完就轮到新技能。
      if (state.table.llm_configured) startLabels('missing');
      return written;
    },
    success: written => (written.conflicts?.length ? null : {
      text: installDoneText(skills, agents),
      undo: undoButton => undoOperation(written.id, {skills, message: written.message, button: undoButton}),
    }),
  });
  if (result?.conflicts?.length) {
    $('install-message').textContent = '有同名技能需要先对比，请看下面的差别。';
    await showIncomingConflicts(result, dialogReview.id);
  }
});

/* ==================== 检查更新：结果显示在技能列表和详情里 ==================== */

/* 检查只查有 agent 在用的技能。查出有新版的，列表里在开关左边出现一个小箭头；
   点它：简单的直接更新（能撤销），改过、有脚本、飞书、被卡住的打开详情，让用户看了再定。 */
const updateState = {
  review: null,
  rows: new Map(),      // 技能名 → 检查结果的那一行
  choices: new Map(),   // 技能名 → upstream / keep / merge（你改过时怎么处理）
  open: new Set(),      // 展开了完整对比的技能
  merges: new Map(),
  explanations: new Map(),
  notices: new Map(),
  signature: '',
  failedOpen: false,
  starting: false,
  busy: false,
};

const UPDATES_CHIP = '__updates__';

/* 有新版、还没更新的那一行；没有就是 null。更新成功后后台会把 has_update 改成 false。 */
const updateRow = name => {
  const row = updateState.rows.get(name);
  return row?.has_update ? row : null;
};

/* 简单的更新：本机没改过、没有脚本变化、不是飞书、没被卡住。
   拿不准你改没改过时 local_modified 是空值，也不算简单。 */
const isSimpleUpdate = row => row.local_modified === false && !row.script_files?.length
  && row.source?.kind !== 'lark' && !row.blocked_reason;

const updatesWritable = () => Boolean(state.table?.write_allowed && updateState.review?.write_allowed);

async function refreshUpdates() {
  try {
    const review = await api('/api/updates/progress');
    if (review) showReview(review);
  } catch (error) {
    setTask('updates', {text: error.message, tone: 'error', retry: () => refreshUpdates()});
  }
}

function showReview(review) {
  if (review.id !== updateState.review?.id) {
    updateState.choices = new Map();
    updateState.open = new Set();
    updateState.merges = new Map();
    updateState.notices = new Map();
    updateState.explanations = new Map();
    updateState.failedOpen = false;
  }
  updateState.review = review;
  updateState.rows = new Map(review.skills.map(row => [row.name, row]));
  if (review.running) {
    // 只写进度，不列正在查哪些来源：来源名对用户没有用，还会让这一行忽长忽短
    setTask('updates', {
      text: review.total ? `正在检查更新 ${review.done}/${review.total}` : '正在检查更新…',
      ratio: review.total ? review.done / review.total : null,
    });
  } else {
    clearTask('updates');
  }
  renderUpdateBar();
  // 查出新结果、或更新完一批，列表和详情才重画；进度没变就不动，免得打断正在做的事。
  const signature = [review.id, review.done, review.running, review.update_count].join('|');
  if (signature !== updateState.signature) {
    updateState.signature = signature;
    renderSkillTable();
    if (state.detail) renderDetail();
  }
}

/* 这次没查的技能和原因。名单在开始检查时已经记下，这里只读。 */
function uncheckedReasons(review) {
  const items = (Array.isArray(review.unchecked) ? review.unchecked : []).filter(item => Number(item.count) > 0);
  const count = items.reduce((sum, item) => sum + Number(item.count), 0);
  const text = items.length === 1
    ? items[0].reason
    : items.map(item => `${number(item.count)} 个${item.reason}`).join('，');
  return {count, text, single: items.length === 1, known: Array.isArray(review.unchecked)};
}

/* 检查时间用这次检查自己的记录，不用页脚的盘点时间。 */
function updateCheckedAtText(value) {
  const time = Date.parse(value);
  if (Number.isNaN(time)) return '';
  const date = new Date(time);
  const clock = `${date.getHours()}:${String(date.getMinutes()).padStart(2, '0')}`;
  return `${friendlyDate(value)} ${clock}`;
}

/* 查了几个、几个没查、原因、什么时候查的。没查过的不说成最新。 */
function updateResultSentence(review) {
  const rows = review.skills || [];
  const waiting = rows.filter(row => row.has_update).length;
  const failed = rows.filter(row => row.error).length;
  const skipped = uncheckedReasons(review);
  const when = updateCheckedAtText(review.checked_at);
  const finish = text => when ? `${text}。${when} 检查` : `${text}。`;
  if (!review.total) {
    let why = '还没有技能';
    if (!skipped.known) why = '暂时说不清为什么没查';
    else if (skipped.count) {
      const detail = skipped.single ? `这 ${number(skipped.count)} 个${skipped.text}` : skipped.text;
      why = `${detail}，所以没检查`;
    }
    return finish(`没有可查的技能：${why}`);
  }
  const outcome = waiting ? `${number(waiting)} 个有新版`
    : (failed || skipped.count || !skipped.known) ? '没发现新版'
    : '都是最新的';
  const lead = `查了 ${number(review.total)} 个，${outcome}`;
  if (!skipped.count) return finish(lead);
  return finish(`${lead}。另外 ${number(skipped.count)} 个没查：${skipped.text}`);
}

/* 工具栏：检查按钮（带进度）和“全部更新”。
   查完后在下面写清范围；有新版时数量仍在“全部更新”上。 */
function renderUpdateBar() {
  const review = updateState.review;
  const checking = updateState.starting || Boolean(review?.running);
  const rows = review?.skills || [];
  const waiting = rows.filter(row => row.has_update);
  const batch = waiting.filter(row => !row.blocked_reason);
  const failed = rows.filter(row => row.error);
  const notUpdated = rows.filter(row => row.update_error || (row.has_update && row.blocked_reason));
  const problems = [...failed, ...notUpdated];
  const failureCounts = [failed.length ? `${failed.length} 个没查成` : '',
    notUpdated.length ? `${notUpdated.length} 个没更新` : ''].filter(Boolean).join('，');

  const check = $('updates-check');
  check.disabled = checking || Boolean(writeBusy);
  check.setAttribute('aria-busy', String(checking));
  check.querySelector('span').textContent = !checking ? '检查更新'
    : review?.running && review.total ? `检查中 ${review.done}/${review.total}` : '检查中…';

  const all = $('updates-all');
  all.hidden = !batch.length;
  all.innerHTML = `${icon('update-up', 'btn-icon')}<span>全部更新</span><span class="n">${batch.length}</span>`;
  all.disabled = checking || updateState.busy || Boolean(writeBusy) || !updatesWritable();

  const sentence = !review || checking ? '' : updateResultSentence(review);
  const skipped = review ? uncheckedReasons(review) : {count: 0, known: false};
  const allCurrent = sentence && review.total && !waiting.length && !failed.length && !skipped.count && skipped.known;
  const mark = !sentence ? '' : waiting.length ? icon('update-up', 'ico-up') : allCurrent ? icon('check-circle', 'ico-ok') : icon('info');
  const status = sentence ? `<span>${mark}${escape(sentence)}</span>` : '';
  $('updates-status').innerHTML = status
    + (problems.length && !checking ? `<button class="link-btn" data-failed-toggle aria-expanded="${updateState.failedOpen}">${icon('warning')}${failureCounts}</button>` : '');
  const panel = $('updates-failed');
  panel.hidden = !problems.length || checking || !updateState.failedOpen;
  panel.innerHTML = problems.map(row => `<p><strong>${nameHtml(row.name)}</strong>：${escape(row.error || row.update_error || row.blocked_reason)}</p>`).join('');
}

$('updates-status').addEventListener('click', event => {
  if (!event.target.closest('[data-failed-toggle]')) return;
  updateState.failedOpen = !updateState.failedOpen;
  renderUpdateBar();
});

async function checkUpdates() {
  updateState.starting = true;
  renderUpdateBar();
  setTask('updates', {text: '正在准备检查名单…'});
  try {
    showReview(await api('/api/updates/check', {}));
  } catch (error) {
    setTask('updates', {text: error.message, tone: 'error', retry: () => checkUpdates()});
  } finally {
    updateState.starting = false;
    renderUpdateBar();
  }
}

$('updates-check').addEventListener('click', checkUpdates);
setInterval(() => { if (updateState.review?.running && !updateState.busy) refreshUpdates(); }, 1200);

$('updates-all').addEventListener('click', event => {
  const names = (updateState.review?.skills || []).filter(row => row.has_update && !row.blocked_reason).map(row => row.name);
  applyUpdates(names, event.currentTarget);
});

/* 列表里的小箭头。 */
function clickUpdateUp(name) {
  const row = updateRow(name);
  if (!row) return;
  if (isSimpleUpdate(row) && updatesWritable()) applyUpdates([name]);
  else openDetail(name);
}

/* 详情里的“有新版”：看改了什么，再决定怎么更新。
   “让 Codex 解释 / 交给 Codex 合并”实际是交给 Codex 这个工具做的，
   和被管理的 agent 无关，所以名字照写 Codex。 */
function detailUpdateHtml(row) {
  const choice = updateState.choices.get(row.name) || 'upstream';
  const merge = updateState.merges.get(row.name);
  const notice = updateState.notices.get(row.name);
  const explanation = updateState.explanations.get(row.name);
  const canAct = updatesWritable() && !row.blocked_reason && !updateState.busy && !writeBusy;
  const asks = row.local_modified !== false;   // 改过、或拿不准时，让用户选这份怎么处理
  return `<section class="detail-update" aria-labelledby="detail-update-title">
    <h3 id="detail-update-title"><svg class="btn-icon" aria-hidden="true" focusable="false"><use href="#sp-update-up"/></svg>有新版</h3>
    <p class="update-summary">${row.file_count ? `${number(row.file_count)} 个文件有变化，新增 ${number(row.added_lines)} 行，删除 ${number(row.removed_lines)} 行` : escape(row.status)}${row.updated_at ? ' · 上游 ' + escape(friendlyDate(row.updated_at)) + '更新' : ''}</p>
    ${row.blocked_reason ? `<div class="risk-block"><p>${escape(row.blocked_reason)}</p></div>` : ''}
    ${row.script_files?.length ? `<div class="risk-block">
      <p><strong>脚本或执行权限有变化，请先看</strong></p>
      <p>${row.script_files.map(escape).join('、')}</p>
      ${(row.risk_hits || []).map(hit => `<p class="risk-line">${escape(hit.file)}：${escape(hit.line)}</p>`).join('')}
    </div>` : ''}
    ${row.baseline_uncertain ? '<p class="item-note">无法判断你是否改过这个技能，默认用新版。</p>' : ''}
    <div class="update-links">
      <button class="link-btn" data-update-diff="${escape(row.name)}">${updateState.open.has(row.name) ? '收起完整对比' : '看完整对比'}</button>
      <button class="link-btn" data-update-explain="${escape(row.name)}" ${canAct ? '' : 'disabled'}>让 Codex 解释</button>
      ${asks ? `<button class="link-btn" data-update-merge="${escape(row.name)}" ${canAct ? '' : 'disabled'}>交给 Codex 合并</button>` : ''}
    </div>
    <div class="update-actions">
      ${asks ? `<label class="update-choice">${row.baseline_uncertain ? '不确定你改没改过' : '你改过这个技能'}，更新时
        <select class="input" data-update-choice="${escape(row.name)}" ${canAct ? '' : 'disabled'}>
          <option value="upstream" ${choice === 'upstream' ? 'selected' : ''}>用新版</option>
          <option value="keep" ${choice === 'keep' ? 'selected' : ''}>保留我的</option>
          ${merge ? `<option value="merge" ${choice === 'merge' ? 'selected' : ''}>采用已核对的合并结果</option>` : ''}
        </select></label>` : '<span></span>'}
      <button class="btn btn-primary btn-small" data-update-one="${escape(row.name)}" ${canAct ? '' : 'disabled'}>${row.source?.kind === 'lark' ? '更新整组飞书技能' : '更新'}</button>
    </div>
    ${explanation ? `<div class="update-extra"><p>${escape(explanation)}</p></div>` : ''}
    ${notice ? `<div class="update-extra">${notice.pre ? `<pre>${escape(notice.pre)}</pre>` : `<p class="item-note">${escape(notice.text)}</p>`}</div>` : ''}
    ${merge ? `<div class="update-extra">
      <p><strong>合并预览</strong></p>
      <p>${escape(merge.explanation)}</p>
      ${merge.script_files?.length ? `<div class="risk-block"><p><strong>合并后脚本或执行权限有变化</strong></p><p>${merge.script_files.map(escape).join('、')}</p></div>` : ''}
      <details class="fold fold-inner" open><summary>合并后与本机的差别</summary><pre>${escape(merge.comparison || '内容相同')}</pre></details>
      ${merge.accepted ? '<p class="item-note">已选用这份合并结果，点「更新」后写入。</p>'
        : `<p><button class="btn btn-small" data-update-accept="${escape(row.name)}" ${canAct ? '' : 'disabled'}>我已看过这份合并结果</button></p>`}
    </div>` : ''}
    ${updateState.open.has(row.name) ? `<details class="fold fold-inner" open><summary>完整对比</summary>
      <div class="comparisons">
        ${asks && row.your_changes ? `<div><h4>你的改动</h4><pre>${escape(row.your_changes.comparison || '没有文本变化')}</pre></div>` : ''}
        <div><h4>${row.baseline_uncertain ? '新版与本机的差别' : '新版改了什么'}</h4><pre>${escape(row.comparison || '内容相同')}</pre></div>
      </div>
    </details>` : ''}
  </section>`;
}

/* “你改过”的选择：选“保留我的”时先让后台找出你改过的地方，找不到就退回用新版。 */
async function chooseUpdate(select) {
  const name = select.dataset.updateChoice;
  updateState.choices.set(name, select.value);
  if (select.value !== 'keep') return;
  updateState.busy = true;
  updateState.notices.set(name, {text: '正在找你改过的地方…'});
  renderDetail();
  try {
    const result = await api('/api/updates/calibrate', {review_id: updateState.review.id, name});
    updateState.notices.set(name, result.comparison ? {pre: result.comparison} : {text: '没有发现你改过的地方。'});
  } catch (error) {
    updateState.choices.set(name, 'upstream');
    updateState.notices.set(name, {text: error.message});
    toast({text: error.message, tone: 'error', sticky: true});
  } finally {
    updateState.busy = false;
    renderDetail();
    renderUpdateBar();
  }
}

async function withDetailWork(button, label, work) {
  await withBusy(button, label, async () => {
    try {
      await work();
    } catch (error) {
      toast({text: error.message, tone: 'error', sticky: true});
    }
    renderDetail();
  });
}

/* 详情里“有新版”区的按钮。处理了返回 true。 */
async function handleUpdateButton(button) {
  const {updateDiff, updateExplain, updateMerge, updateAccept, updateOne} = button.dataset;
  if (updateDiff) {
    if (updateState.open.has(updateDiff)) updateState.open.delete(updateDiff); else updateState.open.add(updateDiff);
    renderDetail();
  } else if (updateOne) {
    await applyUpdates([updateOne], button);
  } else if (updateExplain) {
    await withDetailWork(button, '正在解释…', async () => {
      updateState.explanations.set(updateExplain, (await api('/api/updates/explain', {review_id: updateState.review.id, name: updateExplain})).explanation);
    });
  } else if (updateMerge) {
    await withDetailWork(button, '正在合并…', async () => {
      const result = await api('/api/updates/merge', {review_id: updateState.review.id, name: updateMerge});
      updateState.merges.set(updateMerge, {id: result.merge_id, accepted: false, explanation: result.explanation,
        script_files: result.script_files, comparison: result.comparison});
    });
  } else if (updateAccept) {
    updateState.merges.get(updateAccept).accepted = true;
    updateState.choices.set(updateAccept, 'merge');
    renderDetail();
  } else {
    return false;
  }
  return true;
}

/* 一次更新一个直接做（能撤销）；一次多个、或飞书整组，先列出来问一句。 */
async function applyUpdates(names, button = null) {
  const review = updateState.review;
  if (!review || updateState.busy || !names.length) return null;
  const lark = names.some(name => updateState.rows.get(name)?.source?.kind === 'lark');
  if (lark) names = [...new Set([...names, ...review.skills.filter(s => s.has_update && s.source?.kind === 'lark').map(s => s.name)])];
  const choices = {};
  const mergeIds = {};
  for (const name of names) {
    const choice = updateState.choices.get(name) || 'upstream';
    choices[name] = choice;
    if (choice === 'merge') {
      const merge = updateState.merges.get(name);
      if (!merge?.accepted) {
        if (names.length > 1) continue;
        toast({text: `「${titleOf(name)}」：请先生成合并结果，并确认已看过。`, tone: 'error', sticky: true});
        return null;
      }
      mergeIds[name] = merge.id;
    }
  }
  if (names.length > 1 || lark) {
    const items = names.map(name => {
      const row = updateState.rows.get(name);
      const notes = [
        choices[name] === 'keep' ? '保留我的' : choices[name] === 'merge'
          ? mergeIds[name] ? '用已核对的合并结果' : '合并结果尚未确认，本次会跳过'
          : row.local_modified ? '你改过，会换成新版' : row.baseline_uncertain ? '不确定你改没改过，会换成新版' : '',
        row.script_files?.length ? '脚本有变化' : '',
        row.source?.kind === 'lark' ? '飞书技能，整组一起更新' : '',
      ].filter(Boolean);
      return {name, note: notes.join('；')};
    });
    const ok = await confirmDialog({
      title: '更新这些技能',
      lines: [`会尝试更新 ${names.length} 个技能，没成功的会列出原因，其他技能继续更新。`, lark ? '飞书技能要整组一起更新，会同时升级飞书工具和全部飞书技能。' : ''],
      groups: [{label: `更新 ${names.length} 个技能`, items}],
      note: '更新后可以撤销。', okText: '更新',
    });
    if (!ok) return null;
  }
  // 只置防重入标记，不在这里重画：重画会把刚拿到的那颗按钮换成新节点，
  // 后面 runWrite 的「正在更新…」就写到旧节点上了。
  updateState.busy = true;
  const result = await runWrite({
    button,
    busyText: '正在更新…',
    work: async () => {
      const written = await api('/api/updates/apply', {
        review_id: review.id, skills: names, choices, merge_ids: mergeIds,
        confirm_lark_cli: lark, confirmed: true,
      });
      if (written.failed?.length) updateState.failedOpen = true;
      await refreshEverything();
      await refreshUpdates();
      return written;
    },
    success: written => {
      const updated = written.updated || names;
      const ids = written.operations ? written.operations.map(operation => operation.id).reverse() : [written.id];
      return {
        text: written.failed?.length ? written.message : names.length > 1 ? `${updated.length} 个技能已更新到新版`
          : choices[names[0]] === 'keep' ? `「${titleOf(names[0])}」保留了你的版本` : `「${titleOf(names[0])}」已更新到新版`,
        tone: written.failed?.length ? 'error' : 'ok',
        sticky: Boolean(written.failed?.length),
        undo: updated.length && ids.length ? undoButton => undoOperation(ids, {skills: updated, message: written.message, button: undoButton}) : null,
      };
    },
  });
  updateState.busy = false;
  renderUpdateBar();
  if (state.detail) renderDetail();
  return result;
}

/* ==================== 清理页：自己挑要从所有 agent 撤下的技能 ==================== */

/* 能撤下的：至少给了一个 agent 的技能，占用 Token 多的排前面，可以按名字或用途搜。 */
function cleanupRows() {
  const query = $('cleanup-search').value.trim().toLowerCase();
  return (state.table?.skills || [])
    .filter(skill => (state.saved[skill.name] || []).length)
    .filter(skill => matchesQuery(skill, query))
    .sort((a, b) => b.description_tokens - a.description_tokens || titleOf(a.name).localeCompare(titleOf(b.name), 'zh-CN'));
}

function renderCleanup() {
  if (state.page !== 'cleanup') return;
  // 还没整理过就没有可清理的名单。先说明，不去读，免得报错后两个列表一直停在“正在读取…”。
  const pending = awaitingFirstOrganization();
  $('cleanup-pending').hidden = !pending;
  $('cleanup-ready').hidden = pending;
  if (pending) {
    hideActionBar();
    return;
  }
  const locked = Boolean(writeBusy) || !state.table?.write_allowed;
  // 已经不再分给任何 agent 的（比如刚在技能页撤掉了）不算选中。
  for (const name of [...state.cleanupSelected]) {
    if (!(state.saved[name] || []).length) state.cleanupSelected.delete(name);
  }
  const searching = Boolean($('cleanup-search').value.trim());
  $('cleanup-rows').innerHTML = !state.table ? '<tr><td class="empty" colspan="4">正在读取技能列表…</td></tr>'
    : cleanupRows().map(skill => `<tr>
      <td class="col-check"><label class="check-hit"><input type="checkbox" data-cleanup="${escape(skill.name)}"
        aria-label="${escape('撤下 ' + titleOf(skill.name))}" ${state.cleanupSelected.has(skill.name) ? 'checked' : ''} ${locked ? 'disabled' : ''}></label></td>
      <td><strong>${nameHtml(skill.name)}</strong>${skill.summary ? `<p class="skill-desc">${escape(skill.summary)}</p>` : ''}</td>
      <td>${(state.saved[skill.name] || []).map(id => `<span class="agent-tag">${swatch(id)}${escape(nameOf(id))}</span>`).join('')}</td>
      <td class="col-num">${tokenText(skill.description_tokens)}</td>
    </tr>`).join('') || `<tr><td class="empty" colspan="4">${searching ? emptyInner('search', '没找到，换个词试试') : emptyInner('check-circle', '没有可撤下的技能')}</td></tr>`;

  // 已撤下的：原来给了谁用颜色块和名字，和上面“现在给了谁”一个样子
  const view = state.cleanup;
  const was = row => (row.previous_agents || []).map(id => `<span class="agent-tag">${swatch(id)}${escape(nameOf(id))}</span>`).join('') || '没有记录';
  $('withdrawn-list').innerHTML = !view ? '<p class="item-note">正在读取…</p>' : view.withdrawn.map(row => `<div class="item-row">
    <div>
      <div class="item-title">${nameHtml(row.name)}</div>
      <div class="item-note">原来给 ${was(row)}${row.withdrawn_at ? ` · ${escape(friendlyDate(row.withdrawn_at))}撤下` : ''}</div>
    </div>
    <span class="item-end"><button class="btn btn-small" data-restore-withdrawn="${escape(row.name)}" ${view.write_allowed && !writeBusy ? '' : 'disabled'}>${icon('undo')}<span>恢复…</span></button></span>
  </div>`).join('') || `<div class="empty">${emptyInner('cleanup', '还没有撤下过技能')}</div>`;
  renderCleanupActionBar();
}

/* 底部托盘：选了哪些、各 agent 每次新对话能少占多少 Token；每个都能单独“不撤这个”。 */
function renderCleanupActionBar() {
  if (state.page !== 'cleanup') return;
  const names = [...state.cleanupSelected];
  if (!names.length) return hideActionBar();
  const savings = state.agents.map(agent => {
    const cost = names.filter(name => (state.saved[name] || []).includes(agent.id))
      .reduce((sum, name) => sum + (state.skillIndex.get(name)?.description_tokens || 0), 0);
    return cost ? `${agent.name} 少占 ${tokenText(cost)} Token` : '';
  }).filter(Boolean);
  showActionBar({
    text: `选了 ${names.length} 个，还没撤下`,
    note: [savings.join('、'), '撤下只是不给 agent 用，文件还留着，随时能恢复'].filter(Boolean).join('。'),
    items: names.map(name => `<li><p>「${escape(titleOf(name))}」<small>${escape((state.saved[name] || []).map(nameOf).join('、'))}</small></p>
      <button class="btn btn-quiet btn-small" data-cleanup-unpick="${escape(name)}">不撤这个</button></li>`).join(''),
    buttons: [
      {label: '全部不选', kind: 'btn btn-quiet', action: () => { state.cleanupSelected.clear(); renderCleanup(); }},
      {label: `撤下这 ${names.length} 个`, kind: 'btn btn-primary', disabled: !state.table?.write_allowed || Boolean(writeBusy), action: button => applyCleanup(button)},
    ],
  });
}

async function loadCleanup() {
  if (!state.agentsView) {
    try { await refreshAgents(); } catch { /* 没读到 agent 状态时，下面仍按清理名单的结果说明原因。 */ }
  }
  if (awaitingFirstOrganization()) {
    state.cleanup = null;
    renderCleanup();
    return;
  }
  try {
    state.cleanup = await api('/api/cleanup');
  } catch (error) {
    toast({text: '已撤下的技能没能读出来：' + error.message, tone: 'error'});
  }
  renderCleanup();
}

/* 先请后台核对一遍（依赖、你的文件里提到的位置），确认后再撤下。 */
async function applyCleanup(button) {
  const skills = [...state.cleanupSelected];
  if (!skills.length) return;
  let plan;
  try {
    plan = await withBusy(button, '正在核对…', () => api('/api/cleanup/preview', {skills}));
  } catch (error) {
    toast({text: error.message, tone: 'error', sticky: true});
    return;
  }
  const files = new Set((plan.path_warnings || []).map(warning => warning.file));
  const ok = await confirmDialog({
    title: '撤下这些技能',
    lines: [`从所有 agent 撤下这 ${skills.length} 个技能，文件还留着，随时能恢复。`,
            ...(plan.dependency_warnings || []),
            files.size ? `有 ${files.size} 份你的文件里写着这些技能现在的位置，撤下后那些说法可能要改。` : ''],
    groups: [{label: '要撤下的技能', items: skills.map(name => ({name}))}],
    note: '撤下后也能在提示条或操作记录里撤销。', okText: '撤下',
  });
  if (!ok) return;
  await runWrite({
    button,
    busyText: '正在撤下…',
    work: async () => {
      const written = await api('/api/cleanup/apply', {skills, revision: plan.revision, confirmed: true});
      state.cleanupSelected.clear();
      await refreshEverything();
      return written;
    },
    success: written => ({
      text: pastTense(written.message || `撤下了 ${skills.length} 个技能`),
      undo: undoButton => undoOperation(written.id, {skills, message: pastTense(written.message), button: undoButton}),
    }),
  });
}

$('cleanup-search').addEventListener('input', renderCleanup);

$('cleanup-rows').addEventListener('change', event => {
  const name = event.target.dataset.cleanup;
  if (!name) return;
  if (event.target.checked) state.cleanupSelected.add(name); else state.cleanupSelected.delete(name);
  renderCleanupActionBar();
});

$('cleanup-rows').addEventListener('click', event => {
  const row = event.target.closest('tr');
  if (row && !event.target.closest('input, label')) row.querySelector('input')?.click();
});

$('withdrawn-refresh').addEventListener('click', event => withBusy(event.currentTarget, '正在刷新…', loadCleanup));
$('cleanup-organize').addEventListener('click', () => showPage('skills'));

$('withdrawn-list').addEventListener('click', async event => {
  const button = event.target.closest('button');
  const name = button?.dataset.restoreWithdrawn;
  if (!name) return;
  const row = state.cleanup.withdrawn.find(item => item.name === name);
  const back = (row?.previous_agents || []).map(nameOf).join('、') || '原来的 agent';
  const ok = await confirmDialog({
    title: '恢复这个技能',
    lines: [`会把它重新给 ${back} 用。`],
    groups: [{label: '要恢复的技能', items: [{name}]}],
    note: '可以在操作记录里撤销。', okText: '恢复',
  });
  if (!ok) return;
  await runWrite({
    button,
    busyText: '正在恢复…',
    work: async () => {
      const written = await api('/api/withdrawn/restore', {skills: [name], revision: state.cleanup.revision, confirmed: true});
      await refreshEverything();
      return written;
    },
    success: written => ({
      text: '已经恢复原来的分配',
      undo: undoButton => undoOperation(written.id, {skills: [name], message: written.message, button: undoButton}),
    }),
  });
});

/* ==================== 操作记录页 ==================== */

function renderLog() {
  if (state.page !== 'settings') return;
  $('log-list').innerHTML = state.logs.slice(0, 10).map(record => `<li class="${record.undone ? 'is-undone' : ''}">
    <span class="log-time">${escape(new Date(record.time).toLocaleString('zh-CN'))}</span>
    <div class="log-body">
      <p>${escape(pastTense(record.message))}</p>
      ${record.undone ? `<p class="item-note"><span class="tag tag-quiet">${icon('undo')}已撤销</span></p>` : record.undo_of ? `<p class="item-note"><span class="tag tag-quiet">${icon('undo')}这是一次撤销</span></p>` : ''}
      ${(record.skills || []).length ? `<details class="fold fold-inner"><summary>涉及 ${record.skills.length} 个技能</summary>
        <p class="item-note">${record.skills.map(name => escape(titleOf(name))).join('、')}</p></details>` : ''}
    </div>
    <span class="log-end">${record.undone || record.undo_of || !state.table?.write_allowed || writeBusy ? ''
      : `<button class="btn btn-small btn-quiet" data-undo="${escape(record.id)}">${icon('undo')}<span>撤销…</span></button>`}</span>
  </li>`).join('') || `<li><div class="empty">${emptyInner('log', '还没有操作记录', hint('做过分配、更新、清理之后，都会记在这里。'))}</div></li>`;
}

$('log-refresh').addEventListener('click', event => withBusy(event.currentTarget, '正在刷新…', refreshLog));

$('log-export').addEventListener('click', async () => {
  try {
    await withBusy($('log-export'), '正在导出…', async () => {
      const records = await api('/api/log');
      const url = URL.createObjectURL(new Blob([JSON.stringify(records, null, 2) + '\n'], {type: 'application/json'}));
      const link = document.createElement('a');
      link.href = url;
      link.download = 'skill-pilot-operations.json';
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast({text: `已导出全部 ${records.length} 条操作记录。`});
    });
  } catch (error) {
    toast({text: error.message, tone: 'error'});
  }
});

$('log-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-undo]');
  if (!button) return;
  const id = button.dataset.undo;
  const record = state.logs.find(item => item.id === id);
  await undoOperation(id, {skills: record?.skills || [], message: record?.message || '', button});
});

/* ==================== 诊断与备份页 ==================== */

const CATEGORIES = {user: '用户技能', builtin: '软件内置', plugin: '插件技能', plugin_cache: '插件或市场缓存',
                    vendor: '下载的候选库', unknown_agent: '疑似新 agent', ignored: '已忽略',
                    backup_temp: '备份或临时目录', reference: '参考资料'};

/* 扫描条目里的 agent 编号翻成名字，再写成一句给人看的话。 */
function manualOnlyText(skill) {
  const names = (skill.manual_only_for || []).map(nameOf);
  return names.length ? `<p class="item-note">${escape(names.join('、') + ' 只允许手动调用')}</p>` : '';
}

function disabledText(skill, data = state.inventory) {
  const names = Object.entries(data?.disabled || {})
    .filter(([, value]) => (value?.names || []).includes(skill.name))
    .map(([id]) => nameOf(id));
  return names.length ? `<p class="item-note">${escape(names.join('、') + ' 已停用')}</p>` : '';
}

function renderDiagnostics() {
  if (state.page !== 'settings' || !state.inventory) return;
  const data = state.inventory;
  renderMigrationStatus();

  $('conflict-count').textContent = data.conflicts.length ? `${data.conflicts.length} 组` : '没有';
  $('conflict-list').innerHTML = data.conflicts.map(conflict => `<details class="fold fold-inner">
    <summary>${escape(conflict.name)}<span class="tag tag-quiet">${conflict.versions.length} 种内容</span></summary>
    ${conflict.versions.map((version, index) => `<div class="version">
      <p><strong>第 ${index + 1} 份</strong>${version.copies[0].version ? ' · 版本 ' + escape(version.copies[0].version) : ''}</p>
      ${version.copies.map(copy => `<p class="mono">${pathLabel(copy.path)}</p>`).join('')}
    </div>`).join('')}
  </details>`).join('') || '<p class="empty">没有发现同名不同内容的技能。</p>';

  const rootSelect = $('root-filter');
  const current = rootSelect.value;
  rootSelect.innerHTML = '<option value="">全部用户技能目录</option>'
    + data.roots.map(root => `<option value="${escape(root.path)}">${pathLabel(root.path)}（${root.entry_count} 个入口）</option>`).join('');
  rootSelect.value = current;

  const search = $('inventory-search')?.value.toLowerCase() || '';
  const includeReadonly = $('include-readonly').checked;
  const chosen = rootSelect.value;
  const rows = data.roots
    .filter(root => (!chosen || root.path === chosen) && (includeReadonly || root.category === 'user'))
    .flatMap(root => (root.skills || []).map(skill => ({skill, root})))
    .filter(({skill}) => (skill.name + ' ' + skill.description).toLowerCase().includes(search));
  $('inventory-count').textContent = `显示 ${rows.length} 个目录入口。同一个技能在不同目录里会各列一行。`;
  $('inventory-rows').innerHTML = rows.map(({skill, root}) => `<tr>
    <td><div class="skill-line"><strong>${escape(skill.name)}</strong>${skill.name_mismatch ? '<span class="tag tag-quiet">名称与文件夹名称不同</span>' : ''}</div>
      <p class="skill-desc" title="${escape(skill.description)}">${escape(skill.description)}</p></td>
    <td><p class="mono">${pathLabel(skill.path)}</p>${skill.link_target ? `<p class="item-note">指向 ${pathLabel(skill.link_target)}</p>` : ''}
      <p class="item-note">${escape((root.readers || []).map(nameOf).join(' / '))} · ${escape(CATEGORIES[root.category] || root.category)}</p></td>
    <td>${escape(skill.broken ? '链接已失效' : skill.kind === 'link' ? '链接到另一位置' : '存放完整文件')}
      ${manualOnlyText(skill)}${disabledText(skill, data)}
      ${skill.model_invocable === false ? '<p class="item-note">只允许手动调用</p>' : ''}
      ${skill.warning ? `<p class="item-note">${escape(skill.warning)}</p>` : ''}</td>
    <td class="col-num">${number(skill.description_chars)}</td>
  </tr>`).join('') || '<tr><td class="empty" colspan="4">没有符合条件的技能。</td></tr>';

  $('root-list').innerHTML = data.roots.map(root => `<tr>
    <td class="mono">${pathLabel(root.path)}${root.exists ? '' : '<p class="item-note">目录还不存在</p>'}
      ${root.unresolved_links.length ? `<p class="item-note">另有 ${root.unresolved_links.length} 个目录链接已失效。</p>` : ''}</td>
    <td>${escape(CATEGORIES[root.category] || root.category)}</td>
    <td class="col-num">${number(root.entry_count)}</td>
    <td class="col-num">${number(root.broken_count)}</td>
  </tr>`).join('');

  $('agent-list').innerHTML = data.agents.map(agent => `<details class="fold fold-inner">
    <summary>${escape(agent.name)}
      <span class="tag tag-quiet">${agent.status === 'ignored' ? '已忽略' : agent.status === 'discovered' ? '已发现，还没接入' : '有盘点档案'}</span></summary>
    <p>${escape(agent.visibility_note || '')}</p>
    ${(agent.skill_roots?.length ? agent.skill_roots : agent.evidence || []).map(path => `<p class="mono">${pathLabel(path)}</p>`).join('')}
  </details>`).join('');

  $('discovery-note').textContent = data.discovery_note;
  $('candidate-list').innerHTML = data.candidates.filter(c => !c.default_hidden).map(candidate => `<details class="fold fold-inner">
    <summary>${escape(CATEGORIES[candidate.category] || candidate.category)}
      <span class="tag tag-quiet">${candidate.entry_count} 个目录入口</span></summary>
    <p class="mono">${pathLabel(candidate.path)}</p>
    <p>${candidate.skills.map(s => escape(s.name)).join('、')}</p>
  </details>`).join('')
  + (data.errors.length ? `<details class="fold fold-inner"><summary>有 ${data.errors.length} 个位置没读完整</summary>
      <p class="item-note">系统限制或文件读取失败，已保留为“未知”，不会当作空目录。</p>
      ${data.errors.map(error => `<p class="mono">${pathLabel(error.path)}<br>${escape(error.message)}</p>`).join('')}
    </details>` : '');
}

$('include-readonly').addEventListener('change', () => {
  if ($('include-readonly').checked === false && $('root-filter').value) {
    const root = state.inventory.roots.find(r => r.path === $('root-filter').value);
    if (root && root.category !== 'user') $('root-filter').value = '';
  }
  renderDiagnostics();
});
$('inventory-search').addEventListener('input', renderDiagnostics);
$('root-filter').addEventListener('change', () => {
  const root = state.inventory?.roots.find(r => r.path === $('root-filter').value);
  if (root && root.category !== 'user') $('include-readonly').checked = true;
  renderDiagnostics();
});

/* 整理与还原：还没整理过时，技能页主区域直接显示首次整理（选 agent → 核对保留哪份 → 完成）；
   整理过以后技能页照常列技能，「还原到整理前」和「已有备份」留在设置 → 诊断与备份 → 整理与还原。 */
let migrationPlan = null;
let backupId = null;

/* 首次整理能选的 agent：只列有盘点档案的（后台接得了的）。
   只是「发现的新 agent」要等整理完，在设置 → agent 里按文件夹接入。 */
function migrationChoices() {
  const suggested = state.agentsView?.suggested;
  if (suggested?.length) {
    const known = suggested.filter(item => item.profile_available);
    return (known.length ? known : suggested).map(item => ({id: item.id, name: item.name}));
  }
  return (migrationPlan?.agent_catalog || []).map(item => ({id: item.id, name: item.name}));
}

function renderMigrationChoices() {
  $('migration-agents').innerHTML = migrationChoices()
    .map(item => `<label class="check"><input type="checkbox" data-migration-agent="${escape(item.id)}" checked> ${escape(item.name)}</label>`).join('');
  const groups = state.agentsView?.must_select_together || [];
  $('migration-groups').textContent = groups.map(group => {
    const names = group.map(nameOf);
    return names.length > 1 ? `${names.join('、')} 的技能文件夹有关联，要一起整理。` : '';
  }).filter(Boolean).join(' ');
}

/* 共用一个目录的 agent：勾一个就一起勾上，取消时一起取消。 */
$('migration-agents').addEventListener('change', event => {
  const id = event.target.dataset.migrationAgent;
  if (!id) return;
  for (const group of state.agentsView?.must_select_together || []) {
    if (!group.includes(id)) continue;
    for (const other of group) {
      const input = document.querySelector(`[data-migration-agent="${other}"]`);
      if (input) input.checked = event.target.checked;
    }
  }
});

/* 出错时写在看得见的那一块：技能页的引导，或者设置里的整理与还原。 */
function migrationError(error) {
  const box = $(state.page === 'settings' ? 'migration-error-settings' : 'migration-error');
  box.textContent = error.message;
  box.hidden = false;
}

function clearMigrationError() {
  for (const id of ['migration-error', 'migration-error-settings']) {
    $(id).hidden = true;
    $(id).textContent = '';
  }
}

function migrationStep(step) {
  for (const id of ['select', 'review', 'complete']) $('migration-' + id).hidden = id !== step;
  clearMigrationError();
}

/* 还没整理过：技能页主区域显示首次整理，技能列表先收起来。
   整理过：引导不再出现，设置里的整理与还原留一句说明和已有备份。 */
function awaitingFirstOrganization() {
  return Boolean(state.agentsView) && !(state.agentsView.managed || []).length;
}

function renderFirstRun() {
  const fresh = awaitingFirstOrganization();
  $('first-run').hidden = !fresh;
  $('skills-split').hidden = fresh;
  return fresh;
}

function renderMigrationStatus() {
  const migrated = !renderFirstRun();
  $('migration-done').hidden = !migrated;
  $('migration-pointer').hidden = migrated;
  if (migrated) {
    for (const id of ['select', 'review', 'complete']) $('migration-' + id).hidden = true;
    $('migration-restore').hidden = !backupId;
    $('migration-done-text').innerHTML = `${icon('check-circle')}<span>已经整理好了</span>${hint('以后接入新 agent，请到设置里的 agent 一栏。')}`;
  } else {
    renderMigrationChoices();
    migrationStep('select');
  }
  return migrated;
}

$('migration-preview').addEventListener('click', event => withBusy(event.currentTarget, '正在核对…', async () => {
  try {
    const agents = [...document.querySelectorAll('[data-migration-agent]:checked')].map(input => input.dataset.migrationAgent);
    if (!agents.length) throw new Error('请至少选择一个 agent。');
    migrationPlan = await api('/api/migration/plan?agents=' + agents.join(','));
    renderMigrationReview(migrationPlan);
    migrationStep('review');
  } catch (error) {
    migrationError(error);
  }
}));

/* 引导开头已经写了「整理前会先备份，完成后能还原」，这一行只在只读模式下出现。 */
function renderMigrationMode(writeAllowed) {
  $('migration-mode').hidden = writeAllowed;
  $('migration-mode').textContent = writeAllowed ? '' : '只读模式下可以预览，不能整理。';
}

function renderMigrationReview(plan) {
  renderMigrationMode(plan.write_allowed);
  $('migration-summary').textContent = `共 ${plan.skills.length} 个技能，${plan.conflicts.length} 组内容需要核对。相同内容会直接合并，原来的技能入口会保留。`;
  $('migration-counts').innerHTML = (plan.agents || []).map(id => {
    const before = plan.before[id];
    const after = plan.after[id];
    if (!before || !after) return '';
    return `<details class="fold fold-inner"><summary>${escape(nameOf(id))}：可调用 ${before.available.length} → ${after.available.length} 个；自动可用 ${before.automatic.length} → ${after.automatic.length} 个</summary>
      <p class="item-note">整理前：${escape(before.available.join('、'))}</p>
      <p class="item-note">整理后：${escape(after.available.join('、'))}</p></details>`;
  }).join('') || '<p class="item-note">没有需要核对的 agent。</p>';
  $('migration-versions').innerHTML = plan.conflicts.map(skill => `<details class="fold fold-inner" open>
    <summary>${escape(skill.name)}<span class="tag tag-quiet">${escape(skill.reason)}</span></summary>
    ${skill.options.map((option, index) => `<p><label class="check">
      <input type="radio" name="choice-${escape(skill.name)}" data-choice="${escape(skill.name)}" value="${escape(option.id)}" ${option.id === skill.default ? 'checked' : ''}>
      第 ${index + 1} 份${option.version ? ' · 版本 ' + escape(option.version) : ''}${option.id === skill.default ? ' · 默认选择' : ''}</label></p>
      <p class="mono">${option.paths.map(pathLabel).join('<br>')}</p>`).join('')}
    <details class="fold fold-inner"><summary data-compare="${escape(skill.name)}">查看完整差异</summary><pre></pre></details>
  </details>`).join('') || '<p class="item-note">没有同名不同内容的技能。</p>';
  $('migration-names').innerHTML = plan.skills.filter(s => s.name_mismatch)
    .map(s => `<p>${escape(s.name)}：${s.entries.map(escape).join('、')}</p>`).join('') || '<p class="item-note">所有名称都一致。</p>';
  $('migration-skipped').innerHTML = plan.skipped
    .map(s => `<p class="mono">${pathLabel(s.path)} · ${escape(s.reason)}</p>`).join('') || '<p class="item-note">没有跳过的目录。</p>';
  $('migration-apply').disabled = !plan.write_allowed;
}

$('migration-versions').addEventListener('click', async event => {
  const name = event.target.dataset.compare;
  if (!name) return;
  const pre = event.target.parentElement.querySelector('pre');
  if (pre.textContent) return;
  try {
    const query = new URLSearchParams({agents: migrationPlan.agents.join(','), plan_id: migrationPlan.plan_id, name});
    pre.textContent = (await api('/api/migration/compare?' + query)).comparison;
  } catch (error) {
    migrationError(error);
  }
});

$('migration-back').addEventListener('click', () => migrationStep('select'));

/* 整理完：引导里的结果写一句，再刷新各区块，最后把引导收起来（技能页照常显示）。 */
/* 整理完、或者还原到整理前：先刷新 agent 状态，按「有没有在管理的 agent」决定要不要读技能表。
   还原以后还没有技能表，这里不能去读（否则会报「请先完成首次整理」），技能页要回到首次整理。 */
async function finishMigration(title, result) {
  $('migration-result').textContent = title;
  $('migration-result-detail').textContent = result.message;
  migrationStep('complete');
  await refreshInventory();
  await refreshAgents().catch(() => {});
  if (renderMigrationStatus()) {
    try {
      await refreshTable();
    } catch (error) {
      toast({text: '技能清单没能读出来：' + error.message + ' 关掉页面重新打开看板再试。', tone: 'error', sticky: true});
    }
    await loadCleanup();
  } else {
    state.table = null;
  }
  await refreshLog();
  await refreshMigrationStatus();
  return result;
}

$('migration-apply').addEventListener('click', async event => {
  const button = event.currentTarget;
  if (!migrationPlan?.write_allowed) return;
  const ok = await confirmDialog({
    title: '开始整理',
    lines: ['会先备份，再把各 agent 现在用的技能收进一份技能库。每个 agent 原来能用的技能名单会保留。'],
    note: '完成后可以还原成现在这样。', okText: '开始整理',
  });
  if (!ok) return;
  await runWrite({
    button,
    busyText: '正在整理…',
    onError: migrationError,
    work: async () => {
      const choices = Object.fromEntries([...document.querySelectorAll('[data-choice]:checked')].map(input => [input.dataset.choice, input.value]));
      const result = await api('/api/migration/apply', {confirmed: true, agents: migrationPlan.agents, plan_id: migrationPlan.plan_id, choices});
      backupId = result.backup_id;
      return finishMigration('整理好了', result);
    },
    // 完成提示里带“撤销”：撤销就是回到整理前，用的还是同一份备份。
    success: result => ({text: '整理好了', undo: button => restoreBackup(result.backup_id, button)}),
  });
});

async function restoreBackup(id, button = null) {
  const ok = await confirmDialog({
    title: '还原到整理前？',
    lines: ['会把各 agent 的技能恢复成整理前的样子。'],
    note: '还原本身也会记在操作记录里。', okText: '还原',
  });
  if (!ok) return false;
  await runWrite({
    button,
    busyText: '正在还原…',
    onError: migrationError,
    work: async () => {
      const result = await api('/api/restore', {confirmed: true, backup_id: id});
      return finishMigration('已还原到整理前', result);
    },
    success: () => ({text: '已经还原到整理前的样子'}),
  });
  return true;
}

$('migration-restore').addEventListener('click', event => restoreBackup(backupId, event.currentTarget));

$('migration-backups').addEventListener('click', event => withBusy(event.currentTarget, '正在读取…', async () => {
  try {
    const saved = await api('/api/backups');
    $('migration-backup-list').innerHTML = saved.map(backup => `<div class="item-row">
      <span class="mono">${escape(backup.backup_id)}</span>
      <span class="item-end"><button class="btn btn-small" data-restore-backup="${escape(backup.backup_id)}" ${state.writeAllowed ? '' : 'disabled'}>还原这份备份</button></span>
    </div>`).join('') || '<p class="item-note">还没有整理备份。</p>';
  } catch (error) {
    migrationError(error);
  }
}));

$('migration-backup-list').addEventListener('click', event => {
  const button = event.target.closest('[data-restore-backup]');
  if (button) restoreBackup(button.dataset.restoreBackup, button);
});

/* 整理与还原的按钮状态看 /api/status。
   还没整理过时不会读技能表，右上角「只读预览」也要跟着只读状态走。已经有技能表时，标记仍由技能表决定。 */
async function refreshMigrationStatus() {
  try {
    const status = await api('/api/status');
    state.writeAllowed = !status.read_only;
    renderTeamSettings();
    if (!state.table) $('mode-badge').hidden = state.writeAllowed;
    renderMigrationMode(state.writeAllowed);
    $('migration-apply').disabled = !state.writeAllowed;
  } catch (error) {
    migrationError(error);
  }
}

/* ==================== 启动 ==================== */

function renderFooter() {
  const inventoried = state.inventory?.generated_at;
  const generated = inventoried || state.tableReadAt;
  const label = inventoried ? '盘点于' : '名单读取于';
  $('footer-time').textContent = generated ? `${label} ${new Date(generated).toLocaleString('zh-CN')}` : '';
}

async function refreshProgram() {
  const response = await fetch('/api/runtime/refresh', {method: 'POST', headers});
  if (response.status === 404 || response.status === 405) {
    toast({text: '本次需要关闭全部看板页面后重开一次，以后程序更新只需刷新。', tone: 'error', sticky: true});
    return true;
  }
  const change = await response.json();
  if (!response.ok) throw new Error(change.error);
  if (!change.supported || !change.changed) return true;
  setTask('program', {text: change.busy ? '等当前操作完成后，自动应用新版…' : '正在应用新版…'});
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 400));
    let status;
    try { status = await api('/api/status'); } catch { continue; }
    if (status.reload_error) throw new Error(status.reload_error);
    if (status.version !== change.version || !status.reloading) {
      location.reload();
      return false;
    }
  }
}

/* 地址里记着在哪一页、技能页看哪个 agent、设置页看哪一块。
   旧地址 #log、#diagnostics 也认，换成设置页对应的那一块。 */
function placeFromHash() {
  const [page, arg] = location.hash.slice(1).split('/');
  if (page === 'settings') return {page: 'settings', block: SETTINGS_BLOCKS.includes(arg) ? arg : 'ai'};
  if (page === 'log' || page === 'diagnostics') return {page: 'settings', block: page};
  if (page === 'cleanup') return {page: 'cleanup'};
  if (page === 'team') return {page: 'team'};
  if (page === 'skills' && arg) return {page: 'skills', agent: decodeURIComponent(arg)};
  return {page: 'skills'};
}

async function start() {
  const startPlace = placeFromHash();
  if (startPlace.agent) state.agent = startPlace.agent;
  if (startPlace.block) state.settingsBlock = startPlace.block;
  heartbeat();
  setInterval(heartbeat, 10000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) heartbeat(); });
  addEventListener('pagehide', () => {
    fetch('/api/heartbeat?closing=1&page=' + pageId, {method: 'POST', headers, keepalive: true}).catch(() => {});
  });
  try {
    if (!await refreshProgram()) return;
  } catch (error) {
    setTask('program', {text: error.message || '新版没能应用，请刷新重试。', tone: 'error', retry: () => location.reload()});
    return;
  }
  renderAgentSummary();
  renderSkillTable();
  renderTasks();
  // 打开页面就读一次 SP 更新进度：有新版时左栏直接提醒，不用进设置里点。
  refreshProgramUpdate();
  // 先问 /api/agents：还没整理过时，技能页要显示首次整理，不去读还没有的技能表。
  await refreshAgents().catch(() => {});
  renderFooter();
  try {
    if (renderMigrationStatus()) await refreshTable();
  } catch (error) {
    toast({text: '技能清单没能读出来：' + error.message + ' 关掉页面重新打开看板再试。', tone: 'error', sticky: true});
  }
  await refreshLog();
  refreshMigrationStatus();
  refreshUpdates();
  // 连没连上分享站决定页头有没有「团队」；分享站慢也不挡页面。
  if (startPlace.page !== 'team') refreshTeam();
  showPage(startPlace.page);
  // 地址点名了设置页的哪一块，等内容都画完再滚过去。
  if (startPlace.page === 'settings') setTimeout(() => showSettingsBlock(state.settingsBlock), 300);
}

start();
