"""Pure source contract: backend audit writes must have explicit UI translations.

Only stdlib AST/text parsing is used. No app imports, dotenv, database or models.
"""
import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "backend" / "app" / "api"
VIEW = ROOT / "frontend" / "src" / "views" / "AuditView.vue"


def backend_audit_actions():
    actions = {}
    for path in sorted(API.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        names = {"write_audit"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.api.deps":
                names.update(alias.asname or alias.name for alias in node.names
                             if alias.name == "write_audit")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_writer = (
                isinstance(node.func, ast.Name) and node.func.id in names
            ) or (
                isinstance(node.func, ast.Attribute) and node.func.attr == "write_audit"
            )
            if not is_writer:
                continue
            action = next((item.value for item in node.keywords if item.arg == "action"), None)
            if action is None and len(node.args) > 3:
                action = node.args[3]
            location = f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
            if not isinstance(action, ast.Constant) or not isinstance(action.value, str):
                raise AssertionError(f"Audit action must remain statically inspectable: {location}")
            if not action.value.strip():
                raise AssertionError(f"Empty audit action: {location}")
            actions.setdefault(action.value, []).append(location)
    return actions


def frontend_action_mappings():
    source = VIEW.read_text(encoding="utf-8")
    match = re.search(
        r"const ACTION_TEXT = computed<Record<string, string>>\(\(\) => \(\{(.*?)\}\)\)",
        source, re.DOTALL,
    )
    if match is None:
        raise AssertionError("Expected the explicit computed ACTION_TEXT map in AuditView.vue")
    mappings = {}
    for line in match[1].splitlines():
        if not line.strip():
            continue
        entry = re.fullmatch(r"\s*([a-z][a-z0-9_]*)\s*:\s*(t|aa)\('([\w.]+)'\),?\s*", line)
        if entry is None:
            raise AssertionError(f"Audit map must keep explicit action and translation keys: {line!r}")
        action, translator, key = entry.groups()
        if action in mappings:
            raise AssertionError(f"Duplicate frontend audit action: {action}")
        mappings[action] = f"auditActions.{key}" if translator == "aa" else key
    return mappings


class AuditActionCatalogTests(unittest.TestCase):
    def test_actual_backend_ast_and_frontend_map_cover_the_same_34_actions(self):
        backend = backend_audit_actions()
        frontend = frontend_action_mappings()
        self.assertEqual(len(backend), 34)
        self.assertEqual(
            set(backend), set(frontend),
            f"Missing UI labels: { {key: backend[key] for key in backend.keys() - frontend.keys()} }; "
            f"UI actions without backend writes: {sorted(frontend.keys() - backend.keys())}",
        )
        self.assertEqual(len(set(frontend.values())), 34)

    def test_seven_previously_missing_events_use_their_own_lazy_translation_keys(self):
        expected = {
            "kb_member_update": "auditActions.kbMemberUpdate",
            "kb_member_remove": "auditActions.kbMemberRemove",
            "feedback_regression_job_create": "auditActions.feedbackRegressionJobCreate",
            "feedback_regression_batch_create": "auditActions.feedbackRegressionBatchCreate",
            "feedback_regression_job_cancel": "auditActions.feedbackRegressionJobCancel",
            "feedback_regression_job_review": "auditActions.feedbackRegressionJobReview",
            "feedback_regression_batch_cancel": "auditActions.feedbackRegressionBatchCancel",
        }
        actual = frontend_action_mappings()
        backend = backend_audit_actions()
        for action, key in expected.items():
            with self.subTest(action=action):
                self.assertIn(action, backend)
                self.assertEqual(actual[action], key)


if __name__ == "__main__":
    unittest.main()
