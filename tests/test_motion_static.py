"""选择过渡：打断后从当前画面继续，重复绘制不重启动画，开关保持节点。"""
import subprocess
import unittest

from tests.test_detail_focus_static import STATIC, extract_function


class MotionStaticTests(unittest.TestCase):
    def run_script(self, source):
        result = subprocess.run(['node', '-e', source], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_selection_interruption_resize_and_reduced_motion(self):
        source = (STATIC / 'app.js').read_text()
        function = extract_function(source, 'function glideSelection(')
        self.run_script(r"""
const assert = require('node:assert/strict');
const selectionMotions = new WeakMap();
let reduced = false, painted = null, observer;
const prefersReducedMotion = () => reduced;
const animations = [];
const selected = {offsetLeft: 10, offsetTop: 20, offsetWidth: 80, offsetHeight: 32};
const container = {
  isConnected: true, offsetWidth: 300, clientLeft: 1, clientTop: 2, scrollLeft: 7, scrollTop: 30,
  classList: {add() {}}, append(plate) {this.plate = plate;},
  querySelector() {return selected;}, getBoundingClientRect() {return {left: 100, top: 50};},
};
const document = {createElement() {return {
  hidden: false, style: {}, setAttribute() {},
  getBoundingClientRect() {
    if (painted) return painted;
    const [x, y] = this.style.transform.match(/-?[\d.]+/g).map(Number);
    return {left: 100 + 1 - 7 + x, top: 50 + 2 - 30 + y,
      width: parseFloat(this.style.width), height: parseFloat(this.style.height)};
  },
  animate(frames, options) {
    const animation = {frames, options, cancelled: false, cancel() {this.cancelled = true;}};
    animations.push(animation);
    return animation;
  },
};}};
class ResizeObserver {
  constructor(callback) {observer = this; this.callback = callback;}
  observe() {} disconnect() {this.disconnected = true;}
}
""" + function + r"""
glideSelection(container);
assert.equal(animations.length, 0, '首次出现应直接就位');
selected.offsetLeft = 110;
glideSelection(container);
assert.equal(animations.length, 1);
assert.match(animations[0].frames[0].transform, /translate\(10px, 20px\)/);
glideSelection(container);
assert.equal(animations.length, 1, '重复绘制不能重启动画');
assert.equal(animations[0].cancelled, false);
painted = {left: 129, top: 42, width: 80, height: 32};
selected.offsetLeft = 20;
glideSelection(container);
assert.equal(animations[0].cancelled, true);
assert.match(animations[1].frames[0].transform, /translate\(35px, 20px\)/,
  '反向选择应从当前画面继续，不能从上次目标跳回来');
painted = null;
selected.offsetWidth = 120;
observer.callback();
assert.equal(container.plate.style.width, '120px', '窗口或标签尺寸变化要重新对齐');
reduced = true;
selected.offsetLeft = 90;
const count = animations.length;
glideSelection(container);
assert.equal(animations.length, count, '减少动态效果时不能创建动画');
assert.equal(container.plate.style.transform, 'translate(90px, 20px)');
container.offsetWidth = 0;
glideSelection(container);
assert.equal(container.plate.hidden, true);
container.offsetWidth = 300;
glideSelection(container);
assert.equal(animations.length, count, '隐藏后再出现不应从过时的位置飞回来');
container.isConnected = false;
observer.callback();
assert.equal(observer.disconnected, true, '移除目录后应停止观察尺寸');
""")

    def test_render_keeps_switch_and_selection_plate_without_duplicate_update_buttons(self):
        source = (STATIC / 'app.js').read_text()
        function = extract_function(source, 'function renderButtons(')
        self.run_script(r"""
const assert = require('node:assert/strict');
function button(attributes = {}, html = '') {
  return {
    values: {...attributes}, innerHTML: html,
    classList: {contains(name) {return attributes.class === name;}},
    get attributes() {return Object.entries(this.values).map(([name, value]) => ({name, value}));},
    getAttribute(name) {return this.values[name] ?? null;},
    hasAttribute(name) {return name in this.values;},
    setAttribute(name, value) {this.values[name] = value;},
    removeAttribute(name) {delete this.values[name];},
    remove() {container.children.splice(container.children.indexOf(this), 1);},
  };
}
const document = {createElement() {
  return {content: {children: []}, set innerHTML(value) {
    this.content.children = JSON.parse(value).map(attrs => button(attrs));
  }};
}};
const update = {'data-update-up': 'sample', class: 'update-up'};
const on = {'data-skill': 'sample', class: 'switch', 'aria-checked': 'true'};
const off = {...on, 'aria-checked': 'false'};
const arrow = button(update), toggle = button(on), plate = button({class: 'selection-plate'});
const container = {
  children: [arrow, toggle, plate], matches() {return false;},
  insertBefore(child, before) {
    const old = this.children.indexOf(child);
    if (old >= 0) this.children.splice(old, 1);
    const index = before ? this.children.indexOf(before) : this.children.length;
    this.children.splice(index, 0, child);
  },
};
""" + function + r"""
renderButtons(container, JSON.stringify([update, off]), 'data-skill');
assert.equal(container.children[1], toggle, '拨动时应保留开关本身');
assert.equal(toggle.getAttribute('aria-checked'), 'false');
assert.equal(container.children[0], arrow, '更新按钮也应保留，不能复制一颗');
assert.equal(container.children[2], plate, '选中底板不应被当作旧按钮删除');
renderButtons(container, JSON.stringify([on]), 'data-skill');
assert.deepEqual(container.children, [toggle, plate]);
renderButtons(container, JSON.stringify([update, off]), 'data-skill');
assert.equal(container.children.length, 3);
assert.equal(container.children[1], toggle);
assert.equal(container.children[2], plate);
""")
