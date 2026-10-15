import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dev_snapshot.common import sha
from dev_snapshot.local_summary import MAX_FILE_BYTES, describe
from dev_snapshot.snapshots import snapshot_summary


class LocalSummaryTests(unittest.TestCase):
    def summary(self, path, before, after):
        blobs = {}
        def entry(text):
            if text is None:
                return {"state": "absent"}
            data = text if isinstance(text, bytes) else text.encode("utf-8")
            key = sha(data)
            blobs[key] = data
            return {"state": "present", "hash": key, "size": len(data)}
        change = {"path": path, "before": entry(before), "after": entry(after),
                  "kind": "added" if before is None else "deleted" if after is None else "modified"}
        return snapshot_summary([change], SimpleNamespace(get=lambda key, size: blobs[key]))

    def test_python_labels_actual_changed_function_not_unchanged_neighbor(self):
        before = "def validate_login(value):\n    return True\n\ndef unrelated():\n    return 1\n"
        after = before.replace("return True", "return bool(value)")
        self.assertEqual(self.summary("src/auth.py", before, after), "로그인 검증 함수 수정")

    def test_python_add_delete_and_async_class_method(self):
        source = "class Store:\n    async def save_snapshot(self):\n        return 1\n"
        changed = source.replace("return 1", "return 2")
        self.assertEqual(self.summary("store.py", source, changed), "스냅샷 저장 함수 수정")
        self.assertEqual(self.summary("login.py", None, "def login():\n    pass\n"), "로그인 함수 추가")
        self.assertEqual(self.summary("login.py", "def login():\n    pass\n", None), "로그인 함수 삭제")

    def test_python_formatting_and_invalid_code_keep_file_summary(self):
        self.assertEqual(self.summary("helper.py", "def helper():\n return 1\n", "def helper():\n    return 1\n"), "helper.py 수정")
        self.assertEqual(self.summary("helper.py", "def helper():\n return 1\n", "def helper("), "helper.py 수정")

    def test_javascript_function_body_changes_and_literal_braces(self):
        before = 'export function validateLogin(value) { return "}"; }\nfunction untouched() { return 2; }\n'
        after = before.replace('return "}"', 'return "{"')
        self.assertEqual(self.summary("auth.ts", before, after), "로그인 검증 함수 수정")

    def test_javascript_class_method_and_arrow_function(self):
        source = "class Timeline {\n  renderTitle(): string { return 'old'; }\n  other() { return 1; }\n}\n"
        self.assertEqual(self.summary("timeline.ts", source, source.replace("'old'", "'new'")), "제목 표시 함수 수정")
        arrow = "export const validateLogin = (value: string): boolean => { return true; };"
        self.assertEqual(self.summary("auth.ts", None, arrow), "로그인 검증 함수 추가")
        self.assertEqual(self.summary("media/timeline.js", "function render() { return 1; }", "function render() { return 2; }"), "타임라인 표시 함수 수정")

    def test_comments_and_strings_cannot_invent_function_declarations(self):
        before = "const text = 'function fake() { return 1; }';\n// function other() {}\n"
        self.assertEqual(self.summary("index.js", before, before.replace("return 1", "return 2")), "index.js 수정")

    def test_regex_literal_declarations_are_not_treated_as_functions(self):
        before = "const pattern = /function fake() {}/;\nfunction validateLogin() { return /}/; }\n"
        self.assertEqual(self.summary("auth.js", before, before.replace("return /}/", "return /{/")), "로그인 검증 함수 수정")
        self.assertEqual(self.summary("index.js", "const x = /function fake() {}/;", "const x = /function fake() {x}/;"), "index.js 수정")

    def test_configuration_mentions_changed_key_without_storing_value(self):
        before = '{"scripts":{"build":"old command","test":"unchanged"}}'
        after = '{"scripts":{"build":"PRIVATE VALUE","test":"unchanged"}}'
        self.assertEqual(self.summary("package.json", before, after), "scripts.build 설정 수정")

    def test_css_mentions_changed_selector_not_property_values(self):
        before = ".repo-heading { color: red; }\n.other { color: blue; }"
        after = before.replace("red", "green")
        self.assertEqual(self.summary("timeline.css", before, after), ".repo-heading 스타일 수정")
        self.assertEqual(self.summary("timeline.css", '.title { content: "}"; }', '.title { content: "{"; }'), ".title 스타일 수정")

    def test_unsupported_encoding_binary_and_large_file_use_path_summary(self):
        for data in (b"\x00binary", b"\xff\xfe\x00", b"x" * (MAX_FILE_BYTES + 1)):
            with self.subTest(size=len(data)):
                self.assertEqual(self.summary("auth.py", None, data), "auth.py 추가")

    def test_limits_prevent_large_or_unbounded_object_reads(self):
        change = {"path": "auth.py", "kind": "modified",
                  "before": {"state": "present", "size": MAX_FILE_BYTES + 1, "hash": "old"},
                  "after": {"state": "present", "size": 1, "hash": "new"}}
        with patch("dev_snapshot.local_summary.MAX_TOTAL_BYTES", 0):
            self.assertIsNone(describe([change], lambda *_: self.fail("Unexpected object read")))
        change["before"]["size"] = 1
        with patch("dev_snapshot.local_summary.MAX_FILES", 0):
            self.assertIsNone(describe([change], lambda *_: self.fail("Unexpected object read")))

    def test_title_does_not_claim_untouched_files_were_analyzed(self):
        data = b"def login():\n    pass\n"
        change = {"path": "login.py", "kind": "added", "before": {"state": "absent"},
                  "after": {"state": "present", "size": len(data), "hash": "new"}}
        other = {"path": "notes.txt", "kind": "modified", "before": {}, "after": {}}
        self.assertEqual(describe([change, other], lambda *_: data), "로그인 함수 추가 외 1개 파일")

    def test_summary_has_no_network_or_process_calls(self):
        with patch("subprocess.Popen", side_effect=AssertionError("Process started")), patch("socket.socket", side_effect=AssertionError("Network opened")):
            self.assertEqual(self.summary("auth.py", None, "def login():\n    pass\n"), "로그인 함수 추가")
