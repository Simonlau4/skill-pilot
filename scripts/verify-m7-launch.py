"""LaunchServices + minimal environment + isolated browser + real-home read-only boundary."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from skill_pilot.web.server import lock_path

with tempfile.TemporaryDirectory(prefix='skill-pilot-m7-launch-') as directory:
    runtime=Path(directory).resolve()
    home=Path.home()
    command=['/usr/bin/open','-a',str(home/'Applications'/'Skill Pilot.app'),
             '--env','SKILL_PILOT_READ_ONLY=1','--env','PATH=/usr/bin:/bin',
             '--env','M7_BROWSER_RUNTIME='+str(runtime),
             '--env','BROWSER='+str(PROJECT/'scripts/m7-launch-browser.sh')+' %s']
    env={'HOME':str(home),'PATH':'/usr/bin:/bin'}
    def wait(predicate,timeout=25):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.05)
        raise AssertionError('等待验收状态超时')
    subprocess.run(command,env=env,check=True)
    wait(lambda:len(list(runtime.glob('*.json')))==1)
    first=json.loads(lock_path().read_text());pid=first['pid']
    def status():
        url=first['url'].replace('/?','/api/status?')
        return json.load(urllib.request.urlopen(url,timeout=1))
    assert status()=={'pid':pid,'read_only':True}
    subprocess.run(command,env=env,check=True)
    wait(lambda:len(list(runtime.glob('*.json')))==2)
    assert json.loads(lock_path().read_text())['pid']==pid
    pages=[json.loads(p.read_text()) for p in runtime.glob('*.json')]
    assert all(p['disabled'] and '技能' in p['text'] for p in pages)
    shutil.copyfile(runtime/'real-dashboard.png',PROJECT/'work/m7-real-dashboard.png')
    (runtime/'close').touch()
    wait(lambda:len(list(runtime.glob('*.closed')))==2)
    closed_at=max(int(p.read_text()) for p in runtime.glob('*.closed'))/1000
    def exited():
        try:os.kill(pid,0);return False
        except ProcessLookupError:return True
    wait(exited,31)
    elapsed=time.time()-closed_at
    assert elapsed<=30,elapsed
    result={'launchservices_minimal_environment':True,'read_only':True,'real_data_visible':True,
            'assignment_controls_disabled':True,'two_launches_one_server':True,'browser_pages':len(pages),
            'exit_after_last_page_seconds':round(elapsed,3),'pid':pid,
            'browser':'独立临时配置的 Chrome；默认 Chrome 已另行启动，但其整窗读取被审批拒绝'}
    (PROJECT/'work/m7-launch-acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))
