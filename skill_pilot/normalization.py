from .platform_support import readlink
"""Keep nested example manifests out of recursive agent discovery."""
import os
import re
from pathlib import Path

from .scan import SKIP_CONTENT


def files(root, normalize=True):
    root = Path(root)
    result = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_CONTENT]
        for name in names + [d for d in dirs if (Path(base) / d).is_symlink()]:
            p = Path(base) / name
            if name not in SKIP_CONTENT:
                result[p.relative_to(root).as_posix()] = ((os.fsencode(readlink(p)), 0o120000) if p.is_symlink() else
                    (p.read_bytes(), 0o100755 if p.stat().st_mode & 0o111 else 0o100644))
    if not normalize:
        return result
    renames = {n: Path(n).with_name('SKILL.example.md').as_posix() for n in result
               if n != 'SKILL.md' and Path(n).name == 'SKILL.md'}
    for old, new in renames.items():
        if new in result:
            raise ValueError('子文件夹已经有 SKILL.example.md，请先核对，文件没有覆盖。')
    reference_names = {Path(n).with_name('SKILL.md').as_posix(): n for n in result if Path(n).name == 'SKILL.example.md' and '/' in n}
    reference_names.update(renames)
    output = {}
    for name, (data, mode) in result.items():
        if mode != 0o120000:
            try:
                text = data.decode('utf-8')
                def reference(match):
                    value = match[0]
                    relative = Path(os.path.normpath(str(Path(name).parent / value))).as_posix()
                    target = reference_names.get(value) or reference_names.get(relative)
                    return value[:-len('SKILL.md')]+'SKILL.example.md' if target else value
                text = re.sub(r'(?:[A-Za-z0-9_.-]+/)+SKILL\.md|(?<![A-Za-z0-9_.-])SKILL\.md',reference,text)
                data = text.encode('utf-8')
            except UnicodeError:
                pass
        output[renames.get(name, name)] = data, mode
    return output


def apply(root):
    root = Path(root)
    before, after = files(root, False), files(root)
    renamed = [{'from': n, 'to': Path(n).with_name('SKILL.example.md').as_posix()} for n in before if n not in after]
    renamed_names = {r['from']:r['to'] for r in renamed}
    references = [renamed_names.get(n,n) for n in before if before[n] != after[renamed_names.get(n,n)]]
    for row in renamed:
        (root / row['from']).rename(root / row['to'])
    for name, (data, mode) in after.items():
        original = next((r['from'] for r in renamed if r['to'] == name), name)
        if mode != 0o120000 and before[original][0] != data:
            (root / name).write_bytes(data)
    return {'renamed': renamed, 'references': sorted(references)}


def tree(root):
    """Git tree identity after standard processing, without writing source files."""
    from .store import git_object
    dirs = {}
    for name, (data, mode) in files(root).items():
        node = dirs
        for part in Path(name).parts[:-1]:
            node = node.setdefault(part, {})
        node[Path(name).name] = str(oct(mode)[2:]), git_object('blob', data)
    # Preserve empty directories as the existing git_tree implementation does.
    for base, names, _ in os.walk(root, followlinks=False):
        names[:] = [n for n in names if n not in SKIP_CONTENT and not (Path(base) / n).is_symlink()]
        node = dirs
        for part in Path(base).relative_to(root).parts:
            node = node.setdefault(part, {})
    def encode(node):
        rows = []
        for name, value in node.items():
            directory = isinstance(value, dict)
            mode, sha = ('40000', encode(value)) if directory else value
            rows.append((os.fsencode(name + ('/' if directory else '')), mode.encode() + b' ' + os.fsencode(name) + b'\0' + sha))
        return git_object('tree', b''.join(v for _, v in sorted(rows)))
    return encode(dirs).hex()
