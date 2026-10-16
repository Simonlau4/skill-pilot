#!/usr/bin/env python3
"""Real pinned upstream content; all management and Codex writes in a temporary home."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(PROJECT))
from skill_pilot import operations,store,updates
from skill_pilot.sources.update_github import Reader
from skill_pilot.scan import content_digest

home=Path(tempfile.mkdtemp(prefix='skill-pilot-m5-upstream-home-')).resolve()
for name,alias in [('huashu-nuwa','nuwa-skill'),('interview-skills','interview-skills')]:
 path=home/'.codex/skills'/alias;path.parent.mkdir(parents=True,exist_ok=True)
 shutil.copytree(Path.home()/'.skill-pilot'/'store'/name,path,symlinks=True)
(home/'.codex/config.toml').write_text('[skills]\nconfig=[{name="interview-skills",enabled=false},{name="plugin:original",enabled=false}]\n')
store.apply(store.preview(home)['plan_id'],home)
registry=operations.load(home)[1]
operations._commit(home,registry,list(registry['skills']),[],'starting fixture state')
point=operations.revision(home,operations.load(home)[2])
# Exercise the real CLI with a confirmed source correction. Existing gh credentials are only read.
env={**os.environ,'SKILL_PILOT_HOME':str(home)}
process=subprocess.run([sys.executable,'-m','skill_pilot','set-source','interview-skills','--repo','jennifer88huang/interview-skills','--path','.','--yes','--json'],cwd=PROJECT,env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=120,check=True)
source=json.loads(process.stdout)
assert source['source']['source_path']=='.' and not source['local_modified']
operations.undo(source['id'],home=home)
assert point==operations.revision(home,operations.load(home)[2])
verified=json.loads((PROJECT/'work/m5-upstream-verification.json').read_text())
# Reuse the already verified shallow objects; no source substitutes or merge mocks.
class CachedReader(Reader):
 def repository(self,source):
  row=next(v for v in verified.values() if v['repository']==source['repository'])
  self.root=Path(row['_upstream_path']).parent
  return next(self.root.glob('repo-*')),row['commit']
with patch.object(updates,'Reader',CachedReader):review=updates.check(home)
assert not review['errors'],review['errors']
row=next(r for r in review['skills'] if r['name']=='huashu-nuwa')
assert row['has_update']
result=updates.apply(review,['huashu-nuwa'],{'huashu-nuwa':'upstream'},True)
assert not list((home/'.skill-pilot/store/huashu-nuwa').glob('**/*/SKILL.md'))
assert len(list((home/'.skill-pilot/store/huashu-nuwa/examples').rglob('SKILL.example.md')))==15
operations.undo(result['id'],home=home)
assert point==operations.revision(home,operations.load(home)[2])
with patch.object(updates,'Reader',CachedReader):review=updates.check(home)
row=next(r for r in review['skills'] if r['name']=='huashu-nuwa')
result=updates.apply(review,['huashu-nuwa'],{'huashu-nuwa':'upstream'},True)
codex_env={**env,'HOME':str(home),'CODEX_HOME':str(home/'.codex'),'CFFIXED_USER_HOME':str(home),
           'XDG_CONFIG_HOME':str(home/'.config'),'XDG_CACHE_HOME':str(home/'.cache'),'XDG_STATE_HOME':str(home/'.state')}
p=subprocess.run(['/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex','debug','prompt-input','probe'],cwd=home,env=codex_env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=60,check=True)
messages=json.loads(p.stdout)
text='\n'.join(c.get('text','') for m in messages for c in m.get('content',[]) if isinstance(c,dict))
# CLI debug output remains private; only detected names are retained as evidence.
names=sorted(set(re.findall(r'^- ([^:\n]+): .*?\(file: ([^)]+)\)$',text,re.M)))
visible=[n for n,path in names if path.startswith('r0/') or 'skill-pilot-m5-upstream-home-' in path]
assert 'huashu-nuwa' in visible,visible
assert not any(n.endswith('-perspective') for n in visible)
assert 'x-mastery-mentor' not in visible
assert not next(s for s in operations.table(home)['skills'] if s['name']=='huashu-nuwa')['local_modified']
output={'home':str(home),'huashu_nuwa':{'upstream_commit':row['commit'],'renamed':result['normalization']['huashu-nuwa'],
        'update_succeeded':True,'codex_visible_managed_skills':visible,'nested_perspective_skills_visible':False,'undo_exact':True},
        'set_source':{'cli_succeeded':True,'repository':'jennifer88huang/interview-skills','path':'.','calibrated_commit':source['source']['commit'],'undo_exact':True}}
(PROJECT/'work/m5-upstream-acceptance.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(output,ensure_ascii=False))
shutil.rmtree(review['temporary'],ignore_errors=True)
