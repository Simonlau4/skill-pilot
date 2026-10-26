"""后台任务队列的前端接线和状态显示：只读本地文件，不启动看板，也不联网。"""
import re
import subprocess
import unittest

from tests.test_nav_static import STATIC, extract_function
from tests.test_team_static import block


def source():
    return (STATIC / 'app.js').read_text()


def line(text, prefix):
    return re.search(re.escape(prefix) + r'[^\n]+', text).group(0)


def arrow_block(text, prefix):
    """取一行开头、到空行结束的 const 箭头函数（可能跨两行）。"""
    start = text.index(prefix)
    end = text.index('\n\n', start)
    return text[start:end]


class JobsStaticTests(unittest.TestCase):
    def test_jobs_entry_panel_and_blocked_banner_exist(self):
        html = (STATIC / 'index.html').read_text()
        for marker in ('id="jobs-toggle"', 'id="jobs-panel"', 'id="jobs-list"', 'id="jobs-close"',
                       'id="jobs-clear"', 'id="jobs-blocked"', 'id="jobs-blocked-go"',
                       'aria-controls="jobs-panel"', 'role="region"', 'aria-labelledby="jobs-panel-title"'):
            self.assertIn(marker, html)
        toggle = re.search(r'<button[^>]*id="jobs-toggle"[^>]*>', html)
        self.assertIsNotNone(toggle, '页头没有后台任务入口')
        self.assertIn('hidden', toggle.group(0), '没有任务时入口应先藏着')
        live = re.search(r'<p[^>]*id="jobs-live"[^>]*>', html)
        self.assertIsNotNone(live, '任务列表没有读屏播报区域')
        self.assertIn('aria-live="polite"', live.group(0))
        # 「前往还原」指向设置里的整理与还原。
        app = (STATIC / 'app.js').read_text()
        self.assertIn("showSettingsBlock('diagnostics')", app)
        self.assertIn("$('diag-migration-box')", app)

        self.assertIn("api('/api/jobs')", app)
        self.assertIn("api('/api/jobs/retry'", app)
        self.assertIn("api('/api/jobs/clear'", app)
        # 本轮范围内的最终执行都改成提交任务，旧的同步执行接口不再从页面调用。
        for old in ('/api/assignment/apply', '/api/install/apply', '/api/updates/apply',
                    '/api/cleanup/apply', '/api/cleanup/delete/apply', '/api/withdrawn/restore'):
            self.assertNotIn(old, app)

    def test_status_text_and_available_actions(self):
        app = source()
        parts = [
            line(app, 'const JOB_ACTIVE = '),
            line(app, 'const jobActive = '),
            extract_function(app, 'function jobStatusLine('),
            extract_function(app, 'function jobUndoAvailable('),
            extract_function(app, 'function jobActions('),
            extract_function(app, 'function pastTense('),
        ]
        script = '\n'.join(parts) + r'''
const state = {logs: []};
function fail(message) { console.error(message); process.exit(1); }
if (jobStatusLine({status: 'failed', message: '这个技能已经删除'}) !== '没完成：这个技能已经删除') fail('没完成的说明不对');
if (jobStatusLine({status: 'failed'}) !== '没完成：原因未知') fail('没有原因时应说明原因未知');
if (jobStatusLine({status: 'attention', message: '影响有变化'}) !== '需要处理：影响有变化') fail('需要处理的说明不对');
if (jobStatusLine({status: 'done', message: '将给 Codex 加 1 个'}) !== '给 Codex 加了 1 个') fail('完成说明应改成过去时');
if (jobStatusLine({status: 'waiting', message: ''}) !== '等待处理。') fail('排队中的默认说明不对');

const done = {status: 'done', operations: ['op-a'], message: '将给 Codex 加 1 个'};
if (jobActions(done).join(',') !== 'undo') fail('已完成且有操作记录时应能撤销');
state.logs = [{id: 'op-a', undone: true}];
if (jobActions(done).length) fail('已经撤销过的操作不应再给撤销');
state.logs = [];
if (jobActions({status: 'waiting'}).length) fail('排队中的任务不能撤销或重试');
if (jobActions({status: 'failed'}).join(',') !== 'retry') fail('没完成的任务应能重试');
if (jobActions({status: 'attention', attention: 'confirmation'}).join(',') !== 'confirm') fail('需要重新确认时应给重新确认');
if (jobActions({status: 'attention', attention: 'conflict', kind: 'assignment'}).join(',') !== 'retry') fail('分配冲突应给重试');
if (jobActions({status: 'attention', attention: 'conflict', kind: 'install'}).join(',') !== 'compare') fail('安装同名冲突应给对比同名技能');
if (jobActions({status: 'attention', attention: 'conflict', kind: 'cleanup_delete'}).length) fail('其他冲突不应乱给操作');
if (jobActions({status: 'done', operations: []}).length) fail('没有写入的完成任务不应给撤销');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_jobs_cover_switches_cleanup_rows_and_update_arrows(self):
        app = source()
        parts = [
            line(app, 'const JOB_ACTIVE = '),
            line(app, 'const jobActive = '),
            line(app, 'const CLEANUP_JOB_KINDS = '),
            extract_function(app, 'function latestActiveJob('),
            extract_function(app, 'function assignmentJobAt('),
            extract_function(app, 'function assignmentJobChange('),
            arrow_block(app, 'const cleanupJobFor = '),
            line(app, 'const cleanupSelectableRows = '),
        ]
        script = '\n'.join(parts) + r'''
const state = {jobs: []};
function fail(message) { console.error(message); process.exit(1); }
state.jobs = [
  {id: 'a', kind: 'assignment', status: 'waiting', changes: [{skill: 'acme-a', agent: 'codex', enabled: true}]},
  {id: 'b', kind: 'assignment', status: 'running', changes: [{skill: 'acme-a', agent: 'codex', enabled: false}]},
  {id: 'c', kind: 'cleanup_withdraw', status: 'waiting', skills: ['acme-b']},
  {id: 'd', kind: 'update', status: 'waiting', skills: ['acme-c']},
  {id: 'e', kind: 'update', status: 'done', skills: ['acme-d']},
];
const job = assignmentJobAt('acme-a', 'codex');
if (!job || job.id !== 'b') fail('同一处有多项任务时应取最后提交的那项');
if (assignmentJobChange(job, 'acme-a', 'codex').enabled !== false) fail('开关应显示最后一项任务的目标状态');
if (assignmentJobAt('acme-a', 'claude')) fail('其他 Agent 不应被这项任务覆盖');
if (assignmentJobAt('acme-b', 'codex')) fail('清理任务不应覆盖分配开关');
if (!cleanupJobFor('acme-b') || cleanupJobFor('acme-b').id !== 'c') fail('清理任务应覆盖对应技能');
if (cleanupJobFor('acme-a')) fail('分配任务不应覆盖清理行');
const rows = [{name: 'acme-a'}, {name: 'acme-b'}, {name: 'acme-c'}];
if (cleanupSelectableRows(rows).map(row => row.name).join(',') !== 'acme-a,acme-c') fail('全选只能排除在清理任务里的技能');
state.jobs = [];
if (cleanupJobFor('acme-b')) fail('任务结束后不应再覆盖清理行');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_submit_job_adds_confirmed_body_and_handles_duplicate_and_blocked(self):
        app = source()
        parts = [
            block(app, 'function adoptJob('),
            block(app, 'async function submitJob('),
        ]
        script = r'''
let jobsReadSequence = 0;
const state = {jobs: [], jobsVersion: -1, jobsBlocked: ''};
const calls = [];
const toasts = [];
async function api(path, body) {
  calls.push({path, body});
  if (body.review_id === 'blocked') {
    const error = new Error('这项操作没能完全恢复，后续任务已停止，请先用备份还原。');
    error.blocked = true;
    throw error;
  }
  return {
    job: {id: 'job-1', kind: body.kind, status: 'waiting', summary: '安装「示例技能」', message: '等待处理。'},
    duplicate: body.review_id === 'duplicate',
    version: 1,
  };
}
const withBusy = (button, label, work) => work();
const toast = item => toasts.push(item);
const renderJobs = () => {};
const renderJobState = () => {};
const scheduleJobsPoll = () => {};
const readJobs = () => {};
const renderJobBlocked = () => {};
const renderJobAvailability = () => {};
function fail(message) { console.error(message); process.exit(1); }

''' + '\n'.join(parts) + r'''

(async () => {
  const first = await submitJob({kind: 'install', review_id: 'one', skills: ['acme-example'], agents: ['codex']}, {});
  if (!first) fail('提交成功应有返回');
  if (calls[0].path !== '/api/jobs') fail('提交应走 /api/jobs');
  const body = calls[0].body;
  if (body.confirmed !== true) fail('提交必须带 confirmed: true');
  if (body.kind !== 'install' || body.review_id !== 'one') fail('提交参数没有原样带上');
  if (JSON.stringify(body.skills) !== '["acme-example"]') fail('技能列表没有带上');
  if (toasts.at(-1).text !== '已加入队列') fail('提交成功应提示已加入队列');
  if (state.jobs.length !== 1 || state.jobs[0].id !== 'job-1') fail('提交成功后应立刻显示这项任务');

  await submitJob({kind: 'install', review_id: 'duplicate', skills: ['acme-example'], agents: []}, {});
  if (toasts.at(-1).text !== '这项已在队列中') fail('重复提交应提示这项已在队列中');
  if (state.jobs.length !== 1) fail('重复提交不应再加一条');

  const blocked = await submitJob({kind: 'install', review_id: 'blocked', skills: ['acme-example'], agents: []}, {});
  if (blocked !== null) fail('提交失败应返回 null');
  if (state.jobsBlocked !== '这项操作没能完全恢复，后续任务已停止，请先用备份还原。') fail('409 的停队原因没有记下来');
  if (toasts.at(-1).tone !== 'error' || !toasts.at(-1).sticky) fail('停队原因应醒目保留');
})().catch(error => { console.error(error); process.exit(1); });
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_scope_submissions_use_job_parameters(self):
        app = source()
        assignment = extract_function(app, 'async function applyAssignment(')
        for marker in ("kind: 'assignment'", 'changes: submitted', 'seen:', 'plan.dependency_warnings', 'plan.path_warnings'):
            self.assertIn(marker, assignment)
        self.assertNotIn('runWrite', assignment)

        install = extract_function(app, 'async function installAllForCurrent(')
        self.assertIn("kind: 'install'", install)
        self.assertIn('review_id: review.id', install)
        self.assertNotIn('runWrite', install)

        updates = extract_function(app, 'async function applyUpdates(')
        for marker in ("kind: 'update'", 'review_id: review.id', 'choices,', 'merge_ids: mergeIds', 'confirm_lark_cli: lark'):
            self.assertIn(marker, updates)
        self.assertNotIn('runWrite', updates)

        withdraw = extract_function(app, 'async function applyCleanup(')
        self.assertIn("kind: 'cleanup_withdraw'", withdraw)
        self.assertIn('plan.dependency_warnings', withdraw)
        self.assertNotIn('runWrite', withdraw)

        delete = extract_function(app, 'async function applyCleanupDelete(')
        self.assertIn("kind: 'cleanup_delete'", delete)
        self.assertIn('irreplaceable: plan.irreplaceable', delete)
        self.assertNotIn('runWrite', delete)

        restore = extract_function(app, 'async function applyCleanupRestore(')
        self.assertIn("kind: 'restore'", restore)
        self.assertNotIn('runWrite', restore)

    def test_retry_confirmation_comparison_and_clear_follow_report(self):
        app = source()
        retry = extract_function(app, 'async function retryJob(')
        self.assertIn("api('/api/jobs/retry', body)", retry)
        self.assertIn('checkUpdatesForRetry()', retry)
        self.assertIn('body.review_id = updateState.review.id', retry)
        self.assertIn('replaces: job.id', retry)

        confirm = extract_function(app, 'async function confirmRetryJob(')
        self.assertIn('new_impacts', confirm)
        self.assertIn('jobImpactGroups(job.new_impacts)', confirm)
        self.assertIn('seen: job.preview', confirm)

        compare = extract_function(app, 'async function compareJobConflict(')
        self.assertIn('showIncomingConflicts({conflicts: job.conflicts || []}, job.review_id)', compare)
        self.assertIn('请重新读取安装链接', compare)

        clear = extract_function(app, 'async function clearEndedJobs(')
        self.assertIn("api('/api/jobs/clear', {})", clear)
        self.assertIn('applyJobs(result, {force: true})', clear)

        undo = extract_function(app, 'async function undoJob(')
        self.assertIn('[...(job.operations || [])].reverse()', undo)
        self.assertIn('undoOperation(ids', undo)

    def test_skills_tray_does_not_replace_cleanup_bar_on_cleanup_page(self):
        app = source()
        function = block(app, 'function renderSkillsActionBar(')
        script = r'''
const state = {page: 'cleanup', table: null};
let cleanupCalls = 0;
let pendingCalls = 0;
let skillBarCalls = 0;
function cleanupSelection() { return new Set(['acme-example']); }
function renderCleanupActionBar() { cleanupCalls += 1; }
function pendingChanges() { pendingCalls += 1; return []; }
function hideActionBar() { skillBarCalls += 1; }
function showActionBar() { skillBarCalls += 1; }
function fail(message) { console.error(message); process.exit(1); }
''' + function + r'''
renderSkillsActionBar();
if (cleanupCalls !== 1) fail('清理页有勾选时应交给清理页操作栏');
if (pendingCalls !== 0 || skillBarCalls !== 0) fail('清理页有勾选时不应重画技能页托盘');

state.page = 'skills';
renderSkillsActionBar();
if (cleanupCalls !== 1) fail('回到技能页后不应再画清理页操作栏');
if (pendingCalls !== 1 || skillBarCalls !== 1) fail('回到技能页后应照常处理技能页托盘');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_open_jobs_panel_puts_toasts_behind_it(self):
        app = source()
        open_panel = block(app, 'function openJobsPanel(')
        close_panel = block(app, 'function closeJobsPanel(')
        self.assertIn("document.body.classList.add('jobs-open')", open_panel)
        self.assertIn("document.body.classList.remove('jobs-open')", close_panel)

        css = (STATIC / 'app.css').read_text()
        rule = re.search(r'body\.jobs-open \.toasts\s*\{([^}]*)\}', css)
        self.assertIsNotNone(rule, '任务列表打开时提示条的层级没有单独处理')
        self.assertIn('z-index: calc(var(--z-header) - 1)', rule.group(1))
        # 任务入口所在的页头比这个临时层级高，任务列表的标题和关闭按钮才不会被提示条挡住。
        self.assertRegex(css, r'\.topbar \{[^}]*z-index: var\(--z-header\)')


if __name__ == '__main__':
    unittest.main()
