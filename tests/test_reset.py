import os
import tempfile
import unittest


class ResetResultsTest(unittest.TestCase):
    def test_reset_is_authenticated_and_user_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            from app import db

            original_path = db.DB_PATH
            db.DB_PATH = os.path.join(tmp, "test.db")
            try:
                from fastapi.testclient import TestClient
                from app.main import app

                db.init()
                for uid in (1, 2):
                    db.execute("INSERT INTO users(id,email,pw) VALUES(?,?,?)",
                               (uid, f"{uid}@test.local", "unused"))
                    db.execute("INSERT INTO sessions(token,user_id) VALUES(?,?)",
                               (f"token{uid}", uid))
                    db.execute("INSERT INTO products(user_id,text) VALUES(?,?)",
                               (uid, "test product"))
                    db.execute(
                        "INSERT INTO results(user_id,text,stage,is_lead,tokens,cost,feedback) "
                        "VALUES(?,?,?,?,?,?,?)", (uid, "test", "draft", 1, 10, .1, 1))
                with TestClient(app) as client:
                    self.assertEqual(client.delete("/api/results").status_code, 401)
                    client.cookies.set("session", "token1")
                    self.assertEqual(client.delete("/api/results").json(),
                                     {"ok": True, "deleted": 1})
                    self.assertEqual(client.get("/api/results").json(), [])
                    stats = client.get("/api/stats").json()
                    for key in ("messages", "leads", "total_cost", "rated_leads",
                                "avg_cost_per_message", "cost_if_all_deep"):
                        self.assertEqual(stats[key], 0)
                    self.assertIsNone(stats["precision"])
                    self.assertEqual(stats["by_stage"], {})
                    self.assertEqual(client.get("/api/me").json()["product"], "test product")
                    self.assertEqual(len(db.query("SELECT * FROM results WHERE user_id=2")), 1)
                    self.assertEqual(client.delete("/api/results").json()["deleted"], 0)
            finally:
                db.DB_PATH = original_path
