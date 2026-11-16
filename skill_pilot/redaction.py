"""Shared path and credential redaction; never resolves diagnostic paths."""
import re
from pathlib import Path

_PATH_STOP = set('\r\n\'"<>:;,)]，。；：、）】')
_HIDDEN_PATH = '[路径已隐藏]'
_PATH_START = r'(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/]|/)'
_ABSOLUTE_PATH = re.compile(r'(?<![~\w.])' + _PATH_START)
_PATH_PROSE = {'and', 'or', 'to', 'from', 'because', 'is', 'are', 'was', 'were', 'in', 'at', 'with'}


def _path_token_end(detail, pos):
    while pos < len(detail) and detail[pos] not in ' \t' and detail[pos] not in _PATH_STOP:
        pos += 1
    return pos


def _next_word(detail, pos):
    """Return the next word after spaces, or None when the path should stop."""
    if pos >= len(detail) or detail[pos] != ' ':
        return None
    nxt = pos + 1
    while nxt < len(detail) and detail[nxt] == ' ':
        nxt += 1
    if nxt >= len(detail) or detail[nxt] in _PATH_STOP or detail[nxt] == '\t':
        return None
    word_end = _path_token_end(detail, nxt)
    if word_end == nxt:
        return None
    return nxt, word_end


def _through_continued_path(detail, pos):
    """Allow short spaced directory names, without scanning across a clause."""
    scan = pos
    # Three plain words cover names such as "Skill Pilot copy 2/repo".
    for _ in range(4):
        word_end = _path_token_end(detail, scan)
        word = detail[scan:word_end]
        if (_ABSOLUTE_PATH.match(detail, scan) or word.lower() in _PATH_PROSE or
                word.startswith(('~/', '~\\', './', '../', '.\\', '..\\'))):
            return None
        if '/' in word or '\\' in word:
            return word_end
        found = _next_word(detail, word_end)
        if not found:
            return None
        scan = found[0]
    return None


def _absolute_path_end(detail, start):
    """Stop at the next independent path or explanatory clause."""
    end = _path_token_end(detail, start)
    while True:
        found = _next_word(detail, end)
        if not found:
            break
        continued = _through_continued_path(detail, found[0])
        if continued is None:
            break
        end = continued
    return end


def _hide_unquoted_absolute_paths(detail):
    pieces = []
    cursor = 0
    for match in _ABSOLUTE_PATH.finditer(detail):
        start = match.start()
        if start < cursor:
            continue
        end = _absolute_path_end(detail, match.end())
        if end <= start:
            continue
        pieces.append(detail[cursor:start])
        pieces.append(_HIDDEN_PATH)
        cursor = end
    pieces.append(detail[cursor:])
    return ''.join(pieces)


def _hide_paths_outside_home(detail):
    """Keep home-relative paths, and hide every other absolute path."""
    def quoted(match):
        return match.group(1) + _HIDDEN_PATH + match.group(1)

    detail = re.sub(r'(?<![~\w.])(["\'])' + _PATH_START + r'[^\r\n]*?\1', quoted, detail)
    return _hide_unquoted_absolute_paths(detail)


def _home_ends(detail, index):
    """True when the home directory is not just the prefix of a longer name."""
    if index >= len(detail):
        return True
    character = detail[index]
    if character in '/\\' or character.isspace() or character in _PATH_STOP:
        return True
    if character == '.' and (index + 1 >= len(detail) or not (detail[index + 1].isalnum() or detail[index + 1] == '_')):
        return True
    return False


def _extended_path(text):
    if text.startswith('\\\\?\\UNC\\'):
        return '\\\\' + text[8:]
    return text.removeprefix('\\\\?\\')


def _path_parts(text):
    return [part for part in _extended_path(text).replace('\\', '/').split('/') if part not in ('', '.')]


def _windows_style(text):
    bare = _extended_path(text)
    return bare.startswith('\\\\') or bare.startswith('//') or bool(re.match(r'^[A-Za-z]:[\\/]', bare))


def _strictly_inside(parent, child):
    """True when child is a directory inside parent, not parent itself or a longer name."""
    parent_parts, child_parts = _path_parts(parent), _path_parts(child)
    if not parent_parts or len(child_parts) <= len(parent_parts):
        return False
    pairs = zip(parent_parts, child_parts)
    if _windows_style(parent) or _windows_style(child):
        return all(left.casefold() == right.casefold() for left, right in pairs)
    return all(left == right for left, right in pairs)


def _abbreviate_home(detail, home):
    """Shorten a home directory only where that directory actually ends.

    The managed home can live inside the user profile (Windows keeps temporary
    folders there). Abbreviating that shorter profile would turn every other
    folder into a tilde path and leave its name in the log.
    """
    managed, profile = str(home), str(Path.home())
    directories = {managed}
    if profile and not _strictly_inside(profile, managed):
        directories.add(profile)
    for directory in sorted(directories, key=len, reverse=True):
        if not directory:
            continue
        pattern, flags = re.escape(directory), 0
        if '\\' in directory or _windows_style(directory):
            pattern, flags = pattern.replace(r'\\', r'[\\/]'), re.IGNORECASE
        detail = re.sub(pattern, lambda match: '~' if _home_ends(match.string, match.end()) else match[0],
                        detail, flags=flags)
    return detail


def hide_credentials(detail):
    detail = re.sub(r"\b(?:https?|file|ssh|git)://[^\s'\"<>]+", '[链接已隐藏]', detail)
    detail = re.sub(r'(?im)\b(authorization|proxy-authorization|api[_-]?key|access[_-]?token|token|password|secret)\s*[\"\']?\s*[:=][^\r\n]*', '[凭据已隐藏]', detail)
    return re.sub(r'\b(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]+', '[凭据已隐藏]', detail)
