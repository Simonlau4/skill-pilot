"""One real-home read-only check; retain timing and fingerprints in the project."""
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

os.environ['SKILL_PILOT_READ_ONLY'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skill_pilot import store, updates

project = Path(__file__).resolve().parents[1]
home = Path.home()
paths = ['.skill-pilot', '.skill-pilot/store', '.skill-pilot/bases', '.skill-pilot/registry.json', '.skill-pilot/agents.json',
         '.skill-pilot/logs', '.codex/skills', '.agents/skills', '.claude/skills', '.gemini/config/skills',
         '.dsh/skills', '.codex/config.toml', '.agents/.skill-lock.json']
before = {p: store.physical_state(home / p) for p in paths}
started = time.monotonic()
review = updates.create(home)
thread = threading.Thread(target=updates.check, kwargs={'review': review})
thread.start()
metrics = {'read_only': True, 'total': review['total'], 'repositories': review['repositories'],
           'first_result_seconds': None, 'first_github_result_seconds': None, 'first_update_seconds': None}
seen = set()
try:
    while thread.is_alive():
        snapshot = updates.public(review)
        for row in snapshot['skills']:
            if row['name'] in seen: continue
            seen.add(row['name'])
            elapsed = round(time.monotonic() - started, 3)
            if metrics['first_result_seconds'] is None: metrics['first_result_seconds'] = elapsed
            if row['source']['kind'] == 'github' and metrics['first_github_result_seconds'] is None:
                metrics['first_github_result_seconds'] = elapsed
            if row['has_update'] and metrics['first_update_seconds'] is None: metrics['first_update_seconds'] = elapsed
            print(json.dumps({'seconds': elapsed, 'name': row['name'], 'status': row['status'],
                              'error': row.get('error')}, ensure_ascii=False), flush=True)
        thread.join(.05)
    metrics.update(total_seconds=round(time.monotonic() - started, 3), done=review['done'], remaining=review['remaining'],
                   update_count=review['update_count'], latest_count=review['latest_count'], errors=review['errors'],
                   api_calls=review['api_calls'], lark=review['lark'], running=review['running'])
    after = {p: store.physical_state(home / p) for p in paths}
    metrics['changed_paths'] = [p for p in paths if before[p] != after[p]]
    metrics['fingerprints'] = {'before': before, 'after': after}
    metrics['rows'] = [{'name': r['name'], 'status': r['status'], 'source': r['source'].get('label'),
                       'error': r.get('error'), 'has_update': r['has_update'],
                       'baseline_uncertain': r.get('baseline_uncertain'), 'local_modified': r['local_modified']} for r in review['skills']]
    output = sys.argv[1] if len(sys.argv) > 1 else 'fix-update-check-readonly'
    (project / 'work' / (output + '.json')).write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in metrics.items() if k not in {'fingerprints', 'rows'}}, ensure_ascii=False), flush=True)
finally:
    thread.join()
    shutil.rmtree(review['temporary'], ignore_errors=True)
