"""Bounded, deterministic labels from saved code; never execute or send its contents."""
from __future__ import annotations

import ast
import json
import re
from pathlib import PurePosixPath

from .common import SafetyError

VERSION = 4
MAX_FILES = 12
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 512 * 1024
ACTION = {"added": "추가", "modified": "수정", "deleted": "삭제"}
TOPICS = {"login": "로그인", "signup": "회원가입", "auth": "인증", "user": "사용자",
          "snapshot": "스냅샷", "title": "제목", "restore": "복원", "commit": "커밋",
          "config": "설정", "settings": "설정", "history": "기록", "cache": "캐시",
          "timeline": "타임라인", "summary": "요약", "record": "작업 기록", "report": "보고",
          "css": "스타일", "style": "스타일"}
OPERATIONS = {"validate": "검증", "render": "표시", "parse": "분석", "load": "불러오기",
              "save": "저장", "delete": "삭제", "remove": "삭제", "create": "생성",
              "update": "갱신", "collect": "수집", "link": "연결", "search": "검색", "get": "조회",
              "capture": "캡처", "sync": "동기화"}


def symbol_label(name: str, kind: str, path: str) -> str:
    leaf = name.rsplit(".", 1)[-1]
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", leaf).lower().split("_")
    topics = list(dict.fromkeys(TOPICS[w] for w in words if w in TOPICS))
    operations = list(dict.fromkeys(OPERATIONS[w] for w in words if w in OPERATIONS))
    if not topics and operations:
        topics = list(dict.fromkeys(TOPICS[word] for word in re.split(r"[/_.-]+", path.lower()) if word in TOPICS))
    # Keep the identifier when the small vocabulary cannot describe it.
    label = " ".join(topics[:2] + operations[:1]) if topics else name
    return f"{label} {kind}"


def python_symbols(text: str) -> dict[str, tuple[str, str]]:
    result = {}
    tree = ast.parse(text)
    def visit(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result[prefix + node.name] = ("함수", ast.dump(node, include_attributes=False))
            elif isinstance(node, ast.ClassDef):
                methods = [child for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                shell = ast.ClassDef(name=node.name, bases=node.bases, keywords=node.keywords,
                                     body=[child for child in node.body if child not in methods],
                                     decorator_list=node.decorator_list, type_params=node.type_params)
                result[prefix + node.name] = ("클래스", ast.dump(shell, include_attributes=False))
                visit(methods, prefix + node.name + ".")
    visit(tree.body)
    return result


# Mask comments and strings before looking for braces/declarations. Compare the
# original body afterwards so edits to strings inside a function still count.
REGEX_BODY = r"/(?![/*])(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\[\n])+/[a-z]*"
JS_LITERAL = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|"
                        + r"(?<=[=(:,!&|?;{}>])\s*" + REGEX_BODY + r"|\breturn\s+" + REGEX_BODY)
JS_FUNCTION = re.compile(r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^{};]*\)\s*(?::[^{};=]+)?\{")
JS_ARROW = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\([^{};]*\)\s*(?::[^{};=]+)?=>\s*\{")
JS_CLASS = re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)[^{};]*\{")
JS_METHOD = re.compile(r"^[ \t]*(?:(?:public|private|protected|static|async|override|readonly)\s+)*([A-Za-z_$][\w$]*)\s*\([^{};]*\)\s*(?::[^{};=]+)?\{", re.MULTILINE)


def javascript_symbols(text: str) -> dict[str, tuple[str, str]]:
    clean = JS_LITERAL.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), text)
    stack, ends, parents = [], {}, {}
    for offset, char in enumerate(clean):
        if char == "{":
            parents[offset] = stack[-1] if stack else None
            stack.append(offset)
        elif char == "}":
            if not stack:
                return {}
            ends[stack.pop()] = offset + 1
    if stack:
        return {}
    result = {}
    classes = {match.end() - 1: match for match in JS_CLASS.finditer(clean)}
    methods = {}
    for match in JS_METHOD.finditer(clean):
        start = match.end() - 1
        owner = classes.get(parents.get(start))
        if owner and start in ends:
            name = owner[1] + "." + match[1]
            result[name] = ("함수", text[match.start():ends[start]])
            methods.setdefault(owner.end() - 1, []).append((match.start(), ends[start]))
    for pattern in (JS_FUNCTION, JS_ARROW):
        for match in pattern.finditer(clean):
            start = match.end() - 1
            if start in ends:
                result[match[1]] = ("함수", text[match.start():ends[start]])
    for start, match in classes.items():
        if start not in ends:
            continue
        parts, cursor = [], match.start()
        for a, b in sorted(methods.get(start, [])):
            parts.append(text[cursor:a])
            cursor = b
        parts.append(text[cursor:ends[start]])
        result[match[1]] = ("클래스", " ".join("".join(parts).split()))
    return result


def json_symbols(text: str) -> dict[str, tuple[str, str]]:
    data = json.loads(text) if text.strip() else {}
    result = {}
    def visit(value, prefix="", depth=0):
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if not re.fullmatch(r"[\w.-]{1,64}", key):
                continue
            name = prefix + key
            if isinstance(item, dict) and depth < 2:
                visit(item, name + ".", depth + 1)
            else:
                result[name] = ("설정", json.dumps(item, sort_keys=True, ensure_ascii=False))
    visit(data)
    return result


def css_symbols(text: str) -> dict[str, tuple[str, str]]:
    literals = r"/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\""
    clean = re.sub(literals, lambda match: re.sub(r"[^\n]", " ", match.group()), text)
    result = {}
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", clean):
        selector = " ".join(text[match.start(1):match.end(1)].split())
        if 0 < len(selector) <= 60 and re.fullmatch(r"[\w\s.#,:>+~*\[\]=()%\-]+", selector):
            result[selector] = ("스타일", text[match.start(2):match.end(2)].strip())
    return result


PARSERS = {".py": python_symbols, ".js": javascript_symbols, ".jsx": javascript_symbols,
           ".ts": javascript_symbols, ".tsx": javascript_symbols, ".mjs": javascript_symbols,
           ".cjs": javascript_symbols, ".json": json_symbols, ".css": css_symbols, ".scss": css_symbols}


def describe(changes: list[dict], read_blob) -> str | None:
    """Read bounded stored objects only. Unrecognized or unavailable data has no label."""
    labels, remaining = [], MAX_TOTAL_BYTES
    for change in changes[:MAX_FILES]:
        parser = PARSERS.get(PurePosixPath(change["path"]).suffix.lower())
        if not parser:
            continue
        entries = [change.get(side, {}) for side in ("before", "after")]
        sizes = [entry.get("size", 0) for entry in entries]
        if any(type(size) is not int or not 0 <= size <= MAX_FILE_BYTES for size in sizes) or sum(sizes) > remaining:
            continue
        remaining -= sum(sizes)
        try:
            texts = [read_blob(entry["hash"], entry["size"]).decode("utf-8-sig")
                     if entry.get("state") == "present" else "" for entry in entries]
            if any("\0" in text or text.count("\n") > 2000 for text in texts):
                continue
            before, after = (parser(text) for text in texts)
        except (OSError, UnicodeError, ValueError, SyntaxError, RecursionError, SafetyError):
            continue
        for name in sorted(set(before) | set(after)):
            a, b = before.get(name), after.get(name)
            if a == b:
                continue
            kind = (b or a)[0]
            action = "added" if a is None else "deleted" if b is None else "modified"
            label = symbol_label(name, kind, change["path"]) if kind in ("함수", "클래스") else f"{name} {kind}"
            labels.append((change["path"], f"{label} {ACTION[action]}"))
    if not labels:
        return None
    selected = labels[:2]
    covered = {path for path, _ in selected}
    extra = len(changes) - len(covered)
    title = " · ".join(label for _, label in selected)
    if len(labels) > len(selected):
        title += " 등"
    if extra:
        title += f" 외 {extra}개 파일"
    return title if len(title) <= 100 else title[:99] + "…"
