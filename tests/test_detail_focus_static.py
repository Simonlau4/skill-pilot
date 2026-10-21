"""技能详情的键盘焦点：只读页面文件，不启动看板，也不联网。"""
import re
import subprocess
import unittest
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / 'skill_pilot' / 'web' / 'static'


def extract_function(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth = 0
    index = opening
    while index < len(source):
        char = source[index]
        if char in '"\'':
            quote = char
            index += 1
            while index < len(source):
                if source[index] == '\\':
                    index += 2
                    continue
                if source[index] == quote:
                    break
                index += 1
        elif char == '`':
            index += 1
            while index < len(source) and source[index] != '`':
                if source[index] == '\\':
                    index += 2
                    continue
                index += 1
        elif source.startswith('//', index):
            index = source.index('\n', index)
        elif source.startswith('/*', index):
            index = source.index('*/', index) + 1
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
        index += 1
    raise AssertionError('没有找到完整的函数：' + signature)


class DetailFocusStaticTests(unittest.TestCase):
    def test_detail_panel_is_a_modal_dialog_with_a_title(self):
        html = (STATIC / 'index.html').read_text()
        tag = re.search(r'<aside id="detail-panel"[^>]*>', html)
        self.assertIsNotNone(tag, '技能详情面板不见了')
        opening = tag.group(0)
        self.assertIn('role="dialog"', opening)
        self.assertIn('aria-modal="true"', opening)
        self.assertIn('aria-labelledby="detail-title"', opening)
        title = re.search(r'<h2 id="detail-title">([^<]+)</h2>', html)
        self.assertIsNotNone(title, '详情面板没有可读标题')
        self.assertTrue(title.group(1).strip())

    def test_tab_stays_inside_detail_and_escape_returns_focus(self):
        source = (STATIC / 'app.js').read_text()
        self.assertIn("document.addEventListener('keydown', onDetailKey)", source)
        names = [
            'function closedDetailsHides(',
            'function detailTabbables(',
            'function trapDetailTab(',
            'function detailBackground(',
            'function setDetailScrollLock(',
            'function holdDetailBackground(',
            'function openDetail(',
            'function showDetailPanel(',
            'function closeDetail(',
            'function onDetailKey(',
        ]
        functions = '\n'.join(extract_function(source, name) for name in names)
        script = r'''
let detailOpener = null;
let storeGet = null;
let storeOpener = null;
let storeOpenerArea = null;
let detailHeld = [];
let detailScrollLock = null;
const state = {detail: null};
const window = {scrollY: 400};
const pending = [];
const frames = [];
function setTimeout(fn) { pending.push(fn); return pending.length; }
function requestAnimationFrame(fn) { frames.push(fn); return frames.length; }
function flush() { while (pending.length) pending.shift()(); }
function flushFrames() { const queued = frames.splice(0); for (const fn of queued) fn(); }
const CSS = {escape: value => value};

function matches(node, selector) {
  if (selector === '*') return true;
  if (selector === '[hidden]') return node.hidden;
  if (selector === 'summary') return node.tagName === 'SUMMARY';
  if (selector === '#skills-list') return node.id === 'skills-list';
  if (selector === 'dialog[open]') return node.tagName === 'DIALOG' && node.open;
  const data = selector.match(/^\[data-open="(.*)"\]$/);
  if (data) return node.dataset.open === data[1];
  return false;
}

function createElement(tag) {
  const node = {
    tagName: tag.toUpperCase(), id: '', hidden: false, disabled: false, type: '',
    open: false, inert: false, isConnected: true, offscreen: false, scrollTop: 0,
    parentElement: null, childNodes: [], dataset: {}, attrs: new Map(), className: '',
  };
  node.children = node.childNodes;
  node.classList = {
    add(name) { node.className = (node.className + ' ' + name).trim(); },
    remove(name) {
      node.className = node.className.split(/\s+/).filter(part => part && part !== name).join(' ');
    },
  };
  node.hasAttribute = name => node.attrs.has(name);
  node.setAttribute = (name, value) => {
    node.attrs.set(name, String(value));
    if (name === 'id') node.id = String(value);
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      node.dataset[key] = String(value);
    }
  };
  node.append = (...kids) => {
    for (const kid of kids) { kid.parentElement = node; node.childNodes.push(kid); }
  };
  node.contains = other => {
    let current = other;
    while (current) { if (current === node) return true; current = current.parentElement; }
    return false;
  };
  node.closest = selector => {
    let current = node;
    while (current) { if (matches(current, selector)) return current; current = current.parentElement; }
    return null;
  };
  node.querySelectorAll = selector => {
    const found = [];
    const walk = current => {
      for (const child of current.childNodes) {
        if (selector === '*' || matches(child, selector)) found.push(child);
        walk(child);
      }
    };
    walk(node);
    return found;
  };
  node.querySelector = selector => node.querySelectorAll(selector)[0] || null;
  Object.defineProperty(node, 'tabIndex', {get() {
    if (node.attrs.has('tabindex')) return Number(node.attrs.get('tabindex'));
    if (node.disabled) return -1;
    if (['BUTTON', 'SELECT', 'TEXTAREA', 'SUMMARY'].includes(node.tagName)) return 0;
    if (node.tagName === 'A' && node.attrs.has('href')) return 0;
    if (node.tagName === 'INPUT' && node.type !== 'hidden') return 0;
    return -1;
  }});
  node.focus = () => {
    document.activeElement = node;
    if (node.offscreen) window.scrollY += 800;
  };
  return node;
}

function h(tag, attrs = {}, ...children) {
  const node = createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'disabled') node.disabled = value;
    else if (key === 'hidden') node.hidden = value;
    else if (key === 'open') node.open = value;
    else if (key === 'type') node.type = value;
    else if (key === 'offscreen') node.offscreen = value;
    else if (key === 'inert') node.inert = value;
    else node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}

const skill = h('button', {id: 'skill-01', 'data-open': 'skill-01', class: 'skill-name'});
const footerHint = h('button', {id: 'footer-hint', class: 'hint', offscreen: true});
const already = h('div', {id: 'already', inert: true});
const folded = h('button', {id: 'folded'});
const panel = h('aside', {id: 'detail-panel', hidden: true},
  h('h2', {id: 'detail-title'}),
  h('button', {id: 'detail-close'}),
  h('div', {id: 'detail-body'},
    h('button', {id: 'inside'}),
    h('button', {id: 'disabled', disabled: true}),
    h('details', {},
      h('summary', {id: 'fold-summary'}),
      folded,
    ),
    h('select', {id: 'dep-select'}),
    h('input', {id: 'secret', type: 'hidden'}),
    h('a', {id: 'dead-link'}),
    h('a', {id: 'real-link', href: '#more'}),
    h('div', {id: 'skip', tabindex: '-1'}),
  ),
);
const scrim = h('div', {id: 'panel-scrim', hidden: true});
const dialog = h('dialog', {id: 'confirm-dialog'}, h('button', {id: 'confirm-ok'}));
const body = h('body', {},
  h('header', {}, h('button', {id: 'nav'})),
  h('main', {}, h('div', {id: 'skills-list'}, skill)),
  h('footer', {}, footerHint),
  already,
  scrim,
  panel,
  dialog,
);
const document = {
  body,
  documentElement: {style: {overflow: ''}},
  activeElement: body,
  querySelector(selector) { return body.querySelector(selector); },
};
body.style = {overflow: ''};
function $(id) {
  const found = body.querySelectorAll('*').find(node => node.id === id);
  if (!found) throw new Error('找不到 ' + id);
  return found;
}
function renderDetail() { $('detail-title').textContent = '示例技能'; }

''' + functions + r'''

function fail(message) { console.error(message); process.exit(1); }
function pageTabbables() {
  return body.querySelectorAll('*').filter(element => {
    if (element.tabIndex < 0 || element.disabled) return false;
    if (element.closest('[hidden]')) return false;
    if (element.tagName === 'A' && !element.hasAttribute('href')) return false;
    if (element.tagName === 'INPUT' && String(element.type || '').toLowerCase() === 'hidden') return false;
    if (closedDetailsHides(element)) return false;
    return true;
  });
}
function moveFocus(shift) {
  const items = pageTabbables();
  const index = items.indexOf(document.activeElement);
  const next = index < 0
    ? items[shift ? items.length - 1 : 0]
    : items[(index + (shift ? -1 : 1) + items.length) % items.length];
  next.focus();
}
function press(key, shift) {
  const event = {key, shiftKey: Boolean(shift), defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
  onDetailKey(event);
  if (!event.defaultPrevented && key === 'Tab' && !document.querySelector('dialog[open]')) moveFocus(shift);
  return event;
}
function ids(nodes) { return nodes.map(node => node.id).join(','); }
function assert(condition, message) { if (!condition) fail(message); }

document.activeElement = skill;
openDetail('skill-01');
press('Escape');
flushFrames();
assert(!panel.className.includes('is-open'), '关掉之后，迟到的展开不该把详情又打开');
assert(document.activeElement === skill, '马上按 Esc，焦点应回到原来的技能');
assert(window.scrollY === 400, '马上按 Esc 不该让页面滚动');
flush();
assert(panel.hidden === true && scrim.hidden === true, '马上按 Esc 后详情应收起');
assert(document.documentElement.style.overflow === '' && body.style.overflow === '', '马上按 Esc 后页面应能重新滚动');

document.activeElement = skill;
openDetail('skill-01');
flushFrames();
assert(detailOpener === skill, '打开详情时没有记住原来的技能');
assert($('detail-title').textContent === '示例技能', '详情标题没有换成技能名');
assert(panel.hidden === false && scrim.hidden === false, '详情或遮罩没有打开');
assert(panel.className.includes('is-open'), '详情没有展开');
assert(document.activeElement === $('detail-close'), '打开后焦点应在关闭按钮上');
assert(window.scrollY === 400, '打开详情不该让后面的页面滚动');
assert(ids(detailTabbables(panel)) === 'detail-close,inside,fold-summary,dep-select,real-link',
  '能走到的控件不对：' + ids(detailTabbables(panel)));
for (const id of ['header', 'main', 'footer']) {
  const node = body.childNodes.find(child => child.tagName === id.toUpperCase());
  assert(node.inert === true, id + ' 在详情打开后仍能被点到');
}
assert(dialog.inert === false, '确认框不该被详情一起关掉交互');
assert(already.inert === true, '本来就不能点的区域被改动了');
assert(body.childNodes.find(child => child.tagName === 'FOOTER').inert === true, '页脚在详情打开后仍能被键盘走到');
assert(document.documentElement.style.overflow === 'hidden' && body.style.overflow === 'hidden',
  '详情打开后，后面的页面仍会跟着翻页键滚动');

const cycle = ['detail-close', 'inside', 'fold-summary', 'dep-select', 'real-link'];
press('Tab', true);
assert(document.activeElement.id === 'real-link', 'Shift+Tab 应从关闭按钮回到面板里的最后一个控件，实际在 ' + document.activeElement.id);
assert(window.scrollY === 400, 'Shift+Tab 不该把后面的页面滚走');
assert(!panel.contains(footerHint) && document.activeElement !== footerHint, '焦点跑到了页脚');
press('Tab', false);
assert(document.activeElement.id === 'detail-close', '在最后一个控件上按 Tab 应回到关闭按钮');
assert(window.scrollY === 400, 'Tab 不该把后面的页面滚走');
for (let step = 0; step < 8; step += 1) {
  press('Tab', false);
  assert(panel.contains(document.activeElement), 'Tab 把焦点带出了详情，落到 ' + document.activeElement.id);
  assert(cycle.includes(document.activeElement.id), '焦点落到了不该走到的控件 ' + document.activeElement.id);
  assert(window.scrollY === 400, '连续按 Tab 时后面的页面滚动了');
}

dialog.open = true;
const duringDialog = document.activeElement;
const dialogTab = press('Tab', false);
assert(dialogTab.defaultPrevented === false, '确认框开着时，详情不该抢走 Tab');
assert(document.activeElement === duringDialog, '确认框开着时，详情不该移动焦点');
press('Escape');
assert(state.detail === 'skill-01' && panel.hidden === false, '确认框开着时，Esc 不该关掉详情');
dialog.open = false;

const detached = h('button', {'data-open': 'skill-01'});
detached.isConnected = false;
detailOpener = detached;
press('Escape');
assert(state.detail === null, 'Esc 没有关掉详情');
assert(document.activeElement === skill, '关掉详情后焦点没有回到打开它的技能');
assert(window.scrollY === 400, '关掉详情不该让页面滚动');
assert(body.childNodes.find(child => child.tagName === 'FOOTER').inert === false, '关掉详情后页脚仍不能被键盘走到');
assert(document.documentElement.style.overflow === '' && body.style.overflow === '', '关掉详情后页面应能重新滚动');
assert(already.inert === true, '关掉详情时不该恢复本来就不能点的区域');
assert(panel.hidden === false, '关闭动画还没结束，面板不该立刻消失');
document.activeElement = skill;
const duringClose = press('Tab', false);
assert(duringClose.defaultPrevented === false, '详情正在收起时，Tab 不该被拦住');
assert(document.activeElement === footerHint, '详情正在收起时，焦点不该被拉回面板');
window.scrollY = 400;
document.activeElement = skill;
flush();
assert(panel.hidden === true && scrim.hidden === true, '关闭动画结束后详情和遮罩应收起');

document.activeElement = skill;
press('Tab', false);
assert(document.activeElement === footerHint, '详情关掉后，Tab 应能回到页面上的其他按钮');
assert(window.scrollY === 1200, '焦点落到页面下方的按钮时，页面才会跟着滚');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
