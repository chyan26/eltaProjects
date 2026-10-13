import copy
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from highlight_builder import server
from tests.test_job import REQUEST


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.Handler.manager = server.JobManager(demo=True, demo_delay=0.05)
        server.Handler.renders = server.RenderManager(server.now)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")

    def wait(self, job_id, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            _, job = self.call("GET", f"/api/v1/jobs/{job_id}")
            if job["status"] in ("succeeded", "failed", "cancelled"):
                return job
            time.sleep(0.1)
        self.fail("job did not finish")

    def test_pages_are_served(self):
        for path in ("/", "/app", "/static/app.js", "/static/theme.css"):
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                self.assertEqual(resp.status, 200, path)

    def test_static_cannot_escape_web_root(self):
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/static/../highlight_builder/server.py", timeout=10)

    def test_preview_and_invalid_body(self):
        status, body = self.call("POST", "/api/v1/jobs/preview", REQUEST)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["commands"]), 3)
        bad = copy.deepcopy(REQUEST)
        bad["steps"][1]["args"]["out_dir"] = "../x"
        status, body = self.call("POST", "/api/v1/jobs", bad)
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], 50035)
        field = body["errors"]["steps"]["1"]["args"]["out_dir"]["_errors"][0]
        self.assertEqual(field["code"], 50036)

    def test_job_runs_and_is_idempotent(self):
        req = copy.deepcopy(REQUEST)
        req["request_id"] = "11111111-2222-4333-8444-555555555555"
        req["steps"][1]["args"].pop("innings")
        status, job = self.call("POST", "/api/v1/jobs", req)
        self.assertEqual(status, 200)
        self.assertTrue(job["id"].startswith("job_"))
        _, again = self.call("POST", "/api/v1/jobs", req)
        self.assertEqual(again["id"], job["id"])
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "succeeded", done)
        self.assertEqual(len(done["outputs"]["clips"]), 9)
        self.assertTrue(all(s["status"] == "succeeded" for s in done["steps"]))

    def test_cancel(self):
        req = copy.deepcopy(REQUEST)
        req["request_id"] = "99999999-2222-4333-8444-555555555555"
        req["steps"][1]["args"].pop("innings")
        _, job = self.call("POST", "/api/v1/jobs", req)
        time.sleep(0.2)
        status, cancelled = self.call("POST", f"/api/v1/jobs/{job['id']}/cancel")
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["status"], "cancelled")

    def test_media_supports_ranges_and_blocks_other_paths(self):
        import shutil
        from highlight_builder import REPO_ROOT

        folder = REPO_ROOT / "sport_hightlight" / "dataset" / "_test_media"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            (folder / "clip one.mp4").write_bytes(bytes(range(256)) * 16)  # 4096 bytes
            (folder / "placeholder.mp4").write_text("version https://git-lfs.github.com/spec/v1\n")
            url = self.base + "/media/sport_hightlight/dataset/_test_media/clip%20one.mp4"
            with urllib.request.urlopen(url, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers["Content-Type"], "video/mp4")
                self.assertEqual(len(resp.read()), 4096)
            req = urllib.request.Request(url, headers={"Range": "bytes=100-199"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 206)
                self.assertEqual(resp.headers["Content-Range"], "bytes 100-199/4096")
                self.assertEqual(resp.read(), (bytes(range(256)) * 16)[100:200])
            for path, code in (("/media/sport_hightlight/dataset/nope/x.mp4", 404),
                               ("/media/highlight_builder/server.py", 403),
                               ("/media/sport_hightlight/csv_data/full_game_ground_truth.csv", 403),
                               ("/media/sport_hightlight/dataset/_test_media/placeholder.mp4", 404)):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(self.base + path, timeout=10)
                self.assertEqual(ctx.exception.code, code, path)
                ctx.exception.close()
        finally:
            shutil.rmtree(folder)

    def test_media_handles_aborted_client_connection(self):
        import shutil
        from highlight_builder import REPO_ROOT

        folder = REPO_ROOT / "sport_hightlight" / "dataset" / "_test_media"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            (folder / "aborted.mp4").write_bytes(bytes(range(256)) * 16)
            handler = mock.Mock()
            handler.headers.get.return_value = ""
            handler.wfile.write.side_effect = ConnectionAbortedError

            server.Handler.serve_media(handler, "sport_hightlight/dataset/_test_media/aborted.mp4")

            handler.wfile.write.assert_called_once()
        finally:
            shutil.rmtree(folder)

    def test_events_handles_aborted_client_connection(self):
        handler = mock.Mock()
        handler.wfile.write.side_effect = ConnectionAbortedError
        job = mock.MagicMock()
        job.status = "running"
        job.version = 1
        job.to_json.return_value = {}

        server.Handler.stream(handler, job)

        handler.wfile.write.assert_called_once()

    def test_unknown_job(self):
        status, body = self.call("GET", "/api/v1/jobs/job_NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], 10001)

    def test_events_stream_ends_with_job_done(self):
        req = copy.deepcopy(REQUEST)
        req["request_id"] = "77777777-2222-4333-8444-555555555555"
        req["steps"][1]["args"].pop("innings")
        _, job = self.call("POST", "/api/v1/jobs", req)
        with urllib.request.urlopen(f"{self.base}/api/v1/jobs/{job['id']}/events", timeout=30) as resp:
            text = resp.read().decode()
        self.assertIn("event: JOB_UPDATE", text)
        self.assertTrue(text.rstrip().split("\n\n")[-1].startswith("event: JOB_DONE"))


if __name__ == "__main__":
    unittest.main()
