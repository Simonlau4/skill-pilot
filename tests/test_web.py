from skill_pilot.platform_support import rmtree
from tests import set_up_home
import json
import threading
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from skill_pilot.web.server import Dashboard
from skill_pilot.store import physical_state


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skill pilot web ")
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, self.temp.name)
        self.server = Dashboard({"read_only": True, "home": str(self.home)})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path="/api/scan", headers=None, method="GET"):
        request = urllib.request.Request(self.server.origin + path, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_token_host_and_origin(self):
        self.assertEqual(self.request()[0], 403)
        headers = {"X-Skill-Pilot-Token": self.server.token}
        self.assertEqual(json.loads(self.request(headers=headers)[1]), {"read_only": True, "home": str(self.home)})
        self.assertEqual(self.request(headers={**headers, "Host":"attacker.example"})[0], 403)
        self.assertEqual(self.request(headers={**headers, "Origin":"https://attacker.example"})[0], 403)
        self.assertEqual(self.request(headers={**headers, "Sec-Fetch-Site":"cross-site"})[0], 403)

    def test_only_heartbeat_post_is_allowed(self):
        headers = {"X-Skill-Pilot-Token": self.server.token, "Origin":self.server.origin}
        self.assertEqual(self.request("/api/heartbeat", headers, "POST")[0], 200)
        self.assertEqual(self.request("/api/assign", headers, "POST")[0], 405)
        del headers["Origin"]
        self.assertEqual(self.request("/api/heartbeat", headers, "POST")[0], 403)

    def test_open_page_keeps_dashboard_until_it_says_goodbye(self):
        headers = {"X-Skill-Pilot-Token": self.server.token, "Origin":self.server.origin}
        with patch("skill_pilot.web.server.HEARTBEAT_TIMEOUT", .02):
            self.assertEqual(self.request("/api/heartbeat?page=a", headers, "POST")[0], 200)
            self.server.last_heartbeat -= 120  # background tabs may stay quiet for minutes
            watcher = threading.Thread(target=self.server.watch)
            watcher.start()
            time.sleep(.15)
            self.assertTrue(watcher.is_alive())
            self.assertEqual(self.request("/api/heartbeat?page=a&closing=1", headers, "POST")[0], 200)
            watcher.join(2)
            self.assertFalse(watcher.is_alive())

    def test_silent_open_page_eventually_stops_dashboard(self):
        self.server.pages.add("a")
        self.server.last_heartbeat -= 120
        with patch("skill_pilot.web.server.OPEN_PAGE_TIMEOUT", 60):
            watcher = threading.Thread(target=self.server.watch)
            watcher.start()
            watcher.join(2)
        self.assertFalse(watcher.is_alive())

    def test_binds_loopback_and_has_no_static_path_traversal(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        headers = {"X-Skill-Pilot-Token": self.server.token}
        self.assertEqual(self.request("/../PLAN.md", headers)[0], 404)
        self.assertEqual(self.request('/?token=' + self.server.token)[0], 200)

    def test_favicon_requires_session_and_uses_svg_content_type(self):
        self.assertEqual(self.request('/favicon.svg')[0], 403)
        with urllib.request.urlopen(self.server.origin + '/favicon.svg?token=' + self.server.token) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), 'image/svg+xml')
            self.assertIn(b'<svg', response.read())
        headers = {"X-Skill-Pilot-Token": self.server.token}
        self.assertEqual(self.request('/brand/../favicon.svg', headers)[0], 404)

    def test_gsap_assets_require_session_and_use_javascript_type(self):
        for path in ("/vendor/gsap.min.js", "/vendor/ScrollTrigger.min.js"):
            self.assertEqual(self.request(path)[0], 403, path)
            with urllib.request.urlopen(self.server.origin + path + '?token=' + self.server.token) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), 'text/javascript')
                body = response.read()
                self.assertTrue(body.startswith(b'/*!'), path + ' 的开头版权说明不见了')
                self.assertIn(b'@license', body[:400])
        headers = {"X-Skill-Pilot-Token": self.server.token}
        for path in ("/vendor/", "/vendor/README.md", "/vendor/gsap.min.js.map", "/vendor/other.js"):
            self.assertEqual(self.request(path, headers)[0], 404, path)

    def test_browser_favicon_ico_needs_no_token(self):
        status, body = self.request("/favicon.ico")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        for path in ("/", "/app.js", "/app.css", "/favicon.svg", "/vendor/gsap.min.js",
                     "/vendor/ScrollTrigger.min.js", "/api/scan", "/favicon.ico/../"):
            self.assertEqual(self.request(path)[0], 403, path)


class MigrationWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skill-pilot-web-m2-")
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, Path(self.temp.name))
        for root, body in [(".codex/skills", "one"), (".gemini/config/skills", "two")]:
            path = self.home / root / "sample"
            path.mkdir(parents=True)
            (path / "SKILL.md").write_text(f'---\nname: sample\ndescription: example\n---\n{body}\n')
        self.server = Dashboard({"home": str(self.home)})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path, body=None, extra=None):
        headers = {"X-Skill-Pilot-Token": self.server.token, "Origin":self.server.origin, "Content-Type":"application/json"}
        headers.update(extra or {})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.server.origin + path, headers=headers, data=data)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_writes_require_confirmation_and_same_origin(self):
        status, plan = self.request('/api/migration/plan')
        self.assertEqual(status, 200)
        body = {"plan_id":plan['plan_id'],"agents":plan['agents']}
        self.assertEqual(self.request('/api/migration/apply',body)[0],400)
        self.assertEqual(self.request('/api/migration/apply',{**body,"confirmed":True},{"Origin":"http://attacker.example"})[0],403)
        self.assertEqual(self.request('/api/migration/apply',{**body,"confirmed":True},{"Host":"attacker.example"})[0],403)
        self.assertFalse((self.home/'.skill-pilot').exists())
    def test_changed_plan_rejected_and_complete_compare_available(self):
        _, plan = self.request('/api/migration/plan')
        query = urllib.parse.urlencode({'agents':','.join(plan['agents']),'plan_id':plan['plan_id'],'name':'sample'})
        status, result = self.request('/api/migration/compare?'+query)
        self.assertEqual(status,200)
        self.assertIn('SKILL.md',result['comparison'])
        (self.home/'.codex/skills/sample/SKILL.md').write_text('changed')
        status, result = self.request('/api/migration/apply',{"confirmed":True,"plan_id":plan['plan_id'],"agents":plan['agents']})
        self.assertEqual(status,409)
        self.assertIn('重新预览',result['error'])
        self.assertFalse((self.home/'.skill-pilot').exists())

    def test_changed_choice_applies_and_confirmed_restore_recovers(self):
        before = physical_state(self.home/'.codex/skills')
        _, plan = self.request('/api/migration/plan')
        skill=plan['skills'][0]
        alternative=next(o for o in skill['options'] if o['id']!=skill['default'])
        expected=(Path(alternative['path'])/'SKILL.md').read_bytes()
        status,result=self.request('/api/migration/apply',{"confirmed":True,"plan_id":plan['plan_id'],"agents":plan['agents'],"choices":{"sample":alternative['id']}})
        self.assertEqual(status,200,result)
        self.assertEqual((self.home/'.skill-pilot/store/sample/SKILL.md').read_bytes(),expected)
        self.assertEqual(self.request('/api/restore',{"backup_id":result['backup_id']})[0],400)
        self.assertEqual(self.request('/api/restore',{"confirmed":True,"backup_id":result['backup_id']})[0],200)
        self.assertEqual(physical_state(self.home/'.codex/skills'),before)

    def test_readonly_target_write_denied(self):
        _, plan=self.request('/api/migration/plan')
        with patch('skill_pilot.web.server.preview_allowed',return_value=False):
            self.assertEqual(self.request('/api/migration/apply',{"confirmed":True,"plan_id":plan['plan_id'],"agents":plan['agents']})[0],403)
        self.assertFalse((self.home/'.skill-pilot').exists())


if __name__ == "__main__":
    unittest.main()


class M3WebTests(MigrationWebTests):
    def setUp(self):
        super().setUp()
        from skill_pilot import store
        plan=store.preview(self.home)
        store.apply(plan['plan_id'],self.home)

    # The inherited migration scenarios belong to the unmigrated fixture.
    def test_writes_require_confirmation_and_same_origin(self):
        _,table=self.request('/api/table')
        body={'changes':[{'skill':'sample','agent':'claude','enabled':True}], 'revision':table['revision']}
        self.assertEqual(self.request('/api/assignment/apply',body)[0],400)
        self.assertEqual(self.request('/api/assignment/apply',{**body,'confirmed':True},{'Origin':'http://attacker.example'})[0],403)
        status, result=self.request('/api/assignment/apply',{**body,'confirmed':True})
        self.assertEqual(status,200,result)
        self.assertEqual(self.request('/api/log')[1][0]['id'],result['id'])
        _,view=self.request('/api/table')
        status,result=self.request('/api/undo',{'id':result['id'],'revision':view['revision'],'confirmed':True})
        self.assertEqual(status,200,result)

    def test_changed_plan_rejected_and_complete_compare_available(self):
        _,table=self.request('/api/table')
        changes=[{'skill':'sample','agent':'claude','enabled':True}]
        self.assertEqual(self.request('/api/assignment/preview',{'changes':changes})[0],200)
        (self.home/'.codex/config.toml').write_text('# external change\n')
        status,result=self.request('/api/assignment/apply',{'changes':changes,'revision':table['revision'],'confirmed':True})
        self.assertEqual(status,409)
        self.assertIn('刷新',result['error'])

    def test_assignment_keeps_pending_choices_after_unrelated_config_change(self):
        config = self.home / '.codex/config.toml'
        config.write_text('# original\n[skills]\nconfig = []\n')
        _, table = self.request('/api/table')
        changes = [{'skill': 'sample', 'agent': 'codex', 'enabled': False}]
        self.assertEqual(self.request('/api/assignment/preview', {'changes': changes})[0], 200)
        extra = '\n[projects."/tmp/example workspace"]\ntrust_level = "trusted"\n'
        config.write_text(config.read_text() + extra)
        _, review = self.request('/api/assignment/preview', {'changes': changes, 'recheck': True})
        status, result = self.request('/api/assignment/apply', {
            'changes': changes, 'revision': review['revision'], 'recheck': True, 'confirmed': True})
        self.assertEqual(status, 200, result)
        self.assertTrue(config.read_text().endswith(extra))
        _, refreshed = self.request('/api/table')
        self.assertNotIn('codex', next(s for s in refreshed['skills'] if s['name'] == 'sample')['assigned_agents'])

    def test_assignment_still_rejects_changed_skill_settings(self):
        config = self.home / '.codex/config.toml'
        config.write_text('[skills]\nconfig = []\n')
        _, table = self.request('/api/table')
        config.write_text('[skills]\nconfig = [{name = "sample", enabled = false}]\n')
        before = physical_state(self.home)
        status, result = self.request('/api/assignment/apply', {
            'changes': [{'skill': 'sample', 'agent': 'claude', 'enabled': True}],
            'revision': table['revision'], 'confirmed': True})
        self.assertEqual(status, 409)
        self.assertIn('刷新', result['error'])
        self.assertEqual(physical_state(self.home), before)

    def test_recheck_reports_race_and_allows_latest_plan(self):
        changes = [{'skill': 'sample', 'agent': 'claude', 'enabled': True}]
        status, review = self.request('/api/assignment/preview', {'changes': changes, 'recheck': True})
        self.assertEqual(status, 200, review)
        path = self.home / '.skill-pilot/store/sample/SKILL.md'
        path.write_text(path.read_text() + '\nexternal edit\n')
        status, result = self.request('/api/assignment/apply', {
            'changes': changes, 'revision': review['revision'], 'recheck': True, 'confirmed': True})
        self.assertEqual(status, 409)
        self.assertTrue(result['recheck'])
        _, latest = self.request('/api/assignment/preview', {'changes': changes, 'recheck': True})
        status, result = self.request('/api/assignment/apply', {
            'changes': changes, 'revision': latest['revision'], 'recheck': True, 'confirmed': True})
        self.assertEqual(status, 200, result)
        self.assertIn('external edit', path.read_text())

    def test_changed_choice_applies_and_confirmed_restore_recovers(self):
        from skill_pilot.sources import github
        _,table=self.request('/api/table')
        path=self.home/'repository'
        path.mkdir()
        skill=path/'new'
        skill.mkdir()
        (skill/'SKILL.md').write_text('---\nname: new\ndescription: demo\n---\nbody\n')
        github.git(path,'init','-q');github.git(path,'add','.')
        github.git(path,'-c','user.name=Test','-c','user.email=test@local','-c','commit.gpgsign=false','commit','-qm','fixture')
        status,review=self.request('/api/install/inspect',{'url':path.as_uri()})
        self.assertEqual(status,200,review)
        body={'review_id':review['id'],'skills':['new'],'agents':[],'revision':table['revision']}
        self.assertEqual(self.request('/api/install/apply',body)[0],400)
        status,result=self.request('/api/install/apply',{**body,'confirmed':True})
        self.assertEqual(status,200,result)
        _,table=self.request('/api/table')
        status,result=self.request('/api/install/apply',{**body,'revision':table['revision'],'confirmed':True})
        self.assertEqual(status,200,result)
        self.assertTrue(result['conflicts'])
        self.assertIn('comparison',self.request('/api/incoming/compare',{'name':'new','review_id':review['id']})[1])
        import shutil
        rmtree(review['temporary'])

    def test_readonly_target_write_denied(self):
        _,table=self.request('/api/table')
        with patch('skill_pilot.web.server.preview_allowed',return_value=False):
            self.assertEqual(self.request('/api/assignment/apply',{'changes':[],'revision':table['revision'],'confirmed':True})[0],403)
