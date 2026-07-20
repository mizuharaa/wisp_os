import os
import unittest


ROOT = os.path.dirname(os.path.abspath(__file__))


class PermissionGateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8") as handle:
            cls.html = handle.read()

    def test_persisted_permission_wait_is_not_presented_as_a_live_process(self):
        self.assertIn(
            'const PERMISSION_WAIT=new Set(["waiting_permission","waiting-permission"]);',
            self.html,
        )
        live_line = next(
            line for line in self.html.splitlines()
            if line.startswith("const CMD_LIVE=new Set")
        )
        self.assertNotIn("waiting_permission", live_line)
        self.assertIn("waitingPermission?false", self.html)
        self.assertIn(
            'data-open-mission="${esc(item.cid)}">Resolve permission</button>',
            self.html,
        )

    def test_waiting_role_renders_persisted_operator_decisions(self):
        self.assertIn("function permissionGatePanel(o,role)", self.html)
        self.assertIn(
            'const acts=PERMISSION_WAIT.has(rs)?permissionGatePanel(o,role)',
            self.html,
        )
        self.assertIn("?permissionGatePanel(o,{}):\"\"", self.html)
        self.assertIn("roleRows||missionPermission", self.html)
        self.assertIn('const reviewGated=rs==="review"&&o.live', self.html)
        self.assertIn(
            "String(role.permission_mode||o.permission_mode||",
            self.html,
        )
        for action, label in (
            ("allow", "Allow &amp; resume"),
            ("retry", "Retry after fixing"),
            ("deny", "Deny &amp; skip"),
        ):
            self.assertIn('data-ceo-act="%s"' % action, self.html)
            self.assertIn(label, self.html)

    def test_authorization_scope_is_server_derived_and_nonce_bound(self):
        self.assertIn('const requestId=String(request.request_id||"")', self.html)
        self.assertIn("request.can_authorize===true&&canDecide&&!isPlanner", self.html)
        self.assertIn('${canAuthorize?"":" disabled"}', self.html)
        self.assertIn("data-permission-label", self.html)
        self.assertEqual(self.html.count('data-permission-request-id="${requestIdAttr}"'), 3)
        self.assertGreaterEqual(self.html.count('${canDecide?"":" disabled"}'), 2)
        action_start = self.html.index('const ca=e.target.closest("[data-ceo-act]")')
        action_end = self.html.index('const cl=e.target.closest("[data-codex-login]")', action_start)
        handler = self.html[action_start:action_end]
        self.assertIn(
            'const actionPayload={cid:ca.dataset.cid,role:ca.dataset.role,action:act}',
            handler,
        )
        self.assertIn('post("/api/ceo-action",actionPayload)', handler)
        self.assertIn(
            "if(permissionAction)actionPayload.request_id=requestId;else actionPayload.feedback=feedback",
            handler,
        )
        self.assertIn("if(permissionAction&&!requestId)", handler)
        self.assertNotIn("permissionLabel,action", handler)
        self.assertNotIn("scope:", handler)
        self.assertIn("Rune does not grant blanket access", handler)

    def test_generic_continue_is_suppressed_while_any_permission_waits(self):
        self.assertIn("const hasPermissionWait=PERMISSION_WAIT.has(status)", self.html)
        self.assertIn("const resumable=!o.live&&!hasPermissionWait", self.html)

    def test_settling_and_planner_decisions_are_labeled_and_locked(self):
        self.assertIn("settling=!!o.live,canDecide=hasRequestId&&!settling", self.html)
        self.assertEqual(
            self.html.count('data-permission-settling="${settling}"'), 3
        )
        self.assertIn("Decisions unlock automatically", self.html)
        self.assertIn('isPlanner?"Deny &amp; stop":"Deny &amp; skip"', self.html)
        self.assertIn("Allow is unavailable for CEO planning", self.html)
        action_start = self.html.index('const ca=e.target.closest("[data-ceo-act]")')
        action_end = self.html.index(
            'const cl=e.target.closest("[data-codex-login]")', action_start
        )
        handler = self.html[action_start:action_end]
        self.assertIn("permissionAction&&settlingDecision", handler)
        self.assertIn("stop the planner mission", handler)
        self.assertIn("planner mission stopped", handler)

    def test_all_permission_decisions_are_confirmed_or_bounded(self):
        action_start = self.html.index('const ca=e.target.closest("[data-ceo-act]")')
        action_end = self.html.index('const cl=e.target.closest("[data-codex-login]")', action_start)
        handler = self.html[action_start:action_end]
        self.assertIn('act==="allow"&&!confirm', handler)
        self.assertIn('act==="retry"&&!confirm', handler)
        self.assertIn('act==="deny"&&!confirm', handler)
        self.assertIn("No permission is added", handler)
        self.assertIn("skip only this role", handler)


if __name__ == "__main__":
    unittest.main(verbosity=2)
