"""Deterministic publishing suggestions; skill files are only read as text."""
import os
import re
from pathlib import Path

from . import labels, team_hub
from .sources import normalize

MUST_WORDS = ('必须', '务必', '先读', '先阅读', '先调用', '先加载', '前置', '统一交由',
              'must', 'required', 'prerequisite', 'depends on', 'read first', 'first read')
FIRST_WORDS = ('之前先读', '先读这个', '先读本', '都要先读', '前置', 'read this before', 'read before',
               'load this before', 'before using', 'read this first', 'prerequisite')
ROUTING_WORDS = ('不负责', '不适用', '不要用', '勿用', '改用', '不走', 'instead', 'not for', "don't use", 'do not use',
                 'as needed', 'if needed', 'optional', '可选', '按需', '如需')
BOUNDARY = r'[A-Za-z0-9_-]'
SENTENCES = re.compile(r'[\r\n。；;！？!?]|\.\s+')
CODE_SPANS = re.compile(r'`+([^`]+)`+')
SKILL_BEFORE = re.compile(r'(?:技能|\bskills?)\s*$', re.I)
SKILL_AFTER = re.compile(r'^\s*(?:技能|skills?\b)', re.I)
ROUTED_BEFORE = re.compile(r'(?:走|转|交给)\s*[`\"\']*$')
WILDCARDS = re.compile(r'(?<!' + BOUNDARY + r')([A-Za-z0-9_.-]+-)\*(?!' + BOUNDARY + ')')
GROUPS = re.compile(r'(?<!' + BOUNDARY + r')([A-Za-z0-9_.-]+-)([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)')


def _texts(home, names):
    root = Path(home) / '.skill-pilot/store'
    result = {}
    for name in sorted(names):
        path = root / name / 'SKILL.md'
        try:
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            result[name] = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            continue
    return result


def _mentions(sentence, pattern):
    spans = [m.span(1) for m in CODE_SPANS.finditer(sentence)]
    result, routed = set(), set()
    for match in pattern.finditer(sentence):
        name, before, after = match[0], sentence[:match.start()], sentence[match.end():]
        if ('-' not in name and not (
                any(start <= match.start() and match.end() <= end for start, end in spans) or
                (before.endswith('../') and (after[:1] in ('', '/', '`', '"', "'", ')', ']', '>') or after[:1].isspace())) or
                SKILL_BEFORE.search(before) or SKILL_AFTER.search(after))):
            continue
        result.add(name)
        if ROUTED_BEFORE.search(before):
            routed.add(name)
    return result - routed


def detect_requires(home, registry, names):
    skills = registry['skills']
    targets = set(names) & skills.keys()
    if not targets:
        return {}
    pattern = re.compile(r'(?<!' + BOUNDARY + ')(?:' + '|'.join(re.escape(n) for n in sorted(skills, key=lambda n: (-len(n), n))) +
                         r')(?!' + BOUNDARY + ')')
    prefixes = {}
    for name in targets:
        for index, char in enumerate(name):
            if char == '-':
                prefixes.setdefault(name[:index + 1], set()).add(name)
    forward, reverse = {}, {}
    for owner, body in _texts(home, skills).items():
        for sentence in SENTENCES.split(body):
            sentence = sentence.strip()
            lower = sentence.lower()
            direct = owner in targets and any(w in lower for w in MUST_WORDS)
            backward = len(owner) >= 3 and any(w in lower for w in FIRST_WORDS)
            if not (direct or backward) or any(w in lower for w in ROUTING_WORDS):
                continue
            mentioned = _mentions(sentence, pattern)
            if direct:
                for dependency in mentioned:
                    forward.setdefault(owner, {}).setdefault(dependency, sentence[:120])
            if backward:
                referenced = mentioned & targets
                for match in WILDCARDS.finditer(sentence):
                    if not ROUTED_BEFORE.search(sentence[:match.start()]):
                        referenced.update(prefixes.get(match[1], set()))
                for match in GROUPS.finditer(sentence):
                    if not ROUTED_BEFORE.search(sentence[:match.start()]):
                        referenced.update({match[1] + s for s in match[2].split('/')} & targets)
                for name in referenced:
                    reverse.setdefault(name, {}).setdefault(owner, sentence[:120])
    graph = {n: set(s.get('requires', [])) for n, s in skills.items()}
    def reaches(start, end):
        pending, seen = [start], set()
        while pending:
            current = pending.pop()
            if current == end:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(graph.get(current, set()))
        return False
    result = {}
    for name in sorted(targets):
        evidence = {**reverse.get(name, {}), **forward.get(name, {})}
        for dependency in sorted(evidence):
            if (name == dependency or len(dependency) < 3 or dependency in graph[name] or
                    skills[name].get('dependency_decisions', {}).get(dependency) == 'rejected'):
                continue
            if not reaches(dependency, name):
                graph[name].add(dependency)
                result.setdefault(name, []).append({'requires': dependency, 'evidence': evidence[dependency]})
    return result


def suggest_packs(home, registry, names, existing=None):
    existing = existing or []
    names = set(names)
    used = {n for p in existing for n in p['skills']}
    skills = registry['skills']
    origins = {n: normalize(s.get('source', {})) for n, s in skills.items() if n in names}
    candidates = sorted(n for n in origins.keys() - used if origins[n]['kind'] != 'lark')
    texts = _texts(home, candidates)
    parent = {n: n for n in candidates}
    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    for index, a in enumerate(candidates):
        for b in candidates[index + 1:]:
            different_repos = (origins[a]['kind'] == origins[b]['kind'] == 'github' and
                               origins[a]['repository'].lower() != origins[b]['repository'].lower())
            if ((a.split('-', 1)[0] == b.split('-', 1)[0] and not different_repos) or
                    '../' + b + '/' in texts.get(a, '') or '../' + a + '/' in texts.get(b, '')):
                parent[find(b)] = find(a)
    groups = {}
    for name in candidates:
        groups.setdefault(find(name), []).append(name)
    titles = {n: s['title'] for g in labels.read(home) for n, s in g['skills'].items()}
    identities, result = {p['id'] for p in existing}, []
    for group in groups.values():
        if len(group) < 2:
            continue
        # Keep even large families within the same limits as manually edited packs.
        chunks = [group[i:i + 50] for i in range(0, len(group), 50)]
        if len(chunks) > 1 and len(chunks[-1]) == 1:
            chunks[-1].insert(0, chunks[-2].pop())
        for members in chunks:
            if len(result) + len(existing) >= 50:
                return result
            base = os.path.commonprefix(members).rstrip('-')[:64]
            if not re.fullmatch(team_hub.NAME, base):
                base = members[0].split('-', 1)[0][:64]
            identity, suffix = base, 2
            while identity in identities:
                ending = '-' + str(suffix)
                identity, suffix = base[:64 - len(ending)] + ending, suffix + 1
            words = [re.match(r'([A-Za-z][A-Za-z0-9]*)(?=\b)', titles.get(n, '')) for n in members]
            if all(words) and len({w[1] for w in words}) == 1:
                word = words[0][1]
            else:
                word = identity.split('-', 1)[0]
                word = word[0].upper() + word[1:]
            title = word + ' 技能包' if len(word) + 4 <= 20 else word[:20]
            pack = team_hub.clean_pack({'id': identity, 'title': title, 'skills': members})
            repositories = {origins[n]['repository'].lower() for n in members if origins[n]['kind'] == 'github'}
            pack['why'] = '名字开头相同' if len({n.split('-', 1)[0] for n in members}) == 1 and len(repositories) <= 1 else '互相链接'
            result.append(pack)
            identities.add(identity)
    return result
