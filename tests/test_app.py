"""App-level security behaviour: auth, limits, cleanup, health, deletion."""

import json
import logging
import threading
import time
import unittest
from unittest import mock

import app as appmod


def _reset_state():
    appmod.RATE_BUCKETS.clear()
    appmod.ACCESS_TOKEN = ""
    appmod.PROXY = ""
    appmod.RATE_LIMIT_JOBS = 0
    appmod.RATE_LIMIT_GLOBAL = 0
    appmod.MAX_CONCURRENT_JOBS = 3
    appmod.MIN_FREE_MB = 0
    appmod.MAX_FILESIZE_MB = 0
    appmod.REQUIRE_GUARD = False
    appmod.DIRECT_DOWNLOAD = False
    appmod._GUARD_WARNED.clear()
    appmod.app.config["MAX_CONTENT_LENGTH"] = 16 * 1024


class Base(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.client = appmod.app.test_client()

    def tearDown(self):
        _reset_state()
        for job_id in list(appmod.JOBS):
            appmod.drop_job(job_id)


class AuthTest(Base):
    def test_requires_token_and_does_not_leak_it(self):
        appmod.ACCESS_TOKEN = "s3cr3t-token"
        anon = self.client.get("/")
        self.assertEqual(anon.status_code, 401)
        self.assertNotIn(b"s3cr3t-token", anon.data)

        ok = self.client.get("/", headers={"Authorization": "Basic eS1kb3dubG9hZDpzM2NyM3QtdG9rZW4="})
        self.assertEqual(ok.status_code, 200)
        self.assertNotIn("s3cr3t-token", ok.get_data(as_text=True))

    def test_health_is_exempt(self):
        appmod.ACCESS_TOKEN = "s3cr3t-token"
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_wrong_token_is_rejected(self):
        appmod.ACCESS_TOKEN = "s3cr3t-token"
        bad = self.client.get("/", headers={"Authorization": "Basic eS1kb3dubG9hZDp3cm9uZw=="})
        self.assertEqual(bad.status_code, 401)


class RateLimitOrderTest(Base):
    def test_rate_limit_runs_before_url_validation(self):
        appmod.RATE_LIMIT_JOBS = 1
        appmod.rate_limited("download", "127.0.0.1", 1)  # consume the only slot
        with mock.patch.object(appmod, "validate_url") as validate:
            response = self.client.post("/download", json={"url": "http://example.com"})
        self.assertEqual(response.status_code, 429)
        validate.assert_not_called()

    def test_spoofed_xff_prefix_cannot_forge_identity(self):
        appmod.RATE_LIMIT_JOBS = 1
        first = self.client.post(
            "/download", json={"url": "http://169.254.169.254/"},
            headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
        second = self.client.post(
            "/download", json={"url": "http://169.254.169.254/"},
            headers={"X-Forwarded-For": "2.2.2.2, 9.9.9.9"})
        self.assertNotEqual(first.status_code, 429)
        self.assertEqual(second.status_code, 429)


class LimitsTest(Base):
    def test_oversized_body_is_rejected(self):
        appmod.app.config["MAX_CONTENT_LENGTH"] = 512
        response = self.client.post("/info", data=json.dumps({"url": "x" * 4000}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 413)

    def test_download_size_cap_is_applied(self):
        appmod.MAX_FILESIZE_MB = 1
        job = {"id": "a" * 12}
        opts = appmod.build_opts(job, "best", None)
        self.assertEqual(opts["max_filesize"], 1024 * 1024)

    def test_low_disk_refuses_job(self):
        appmod.MIN_FREE_MB = 10 ** 12  # absurdly high, so free space looks too low
        with mock.patch.object(appmod, "validate_url", return_value=None):
            response = self.client.post("/download", json={"url": "http://example.com/x"})
        self.assertEqual(response.status_code, 503)

    def test_concurrency_cap_refuses_extra_downloads(self):
        appmod.MAX_CONCURRENT_JOBS = 2
        started = []
        for i in range(2):
            appmod.JOBS[f"{i:012x}"] = {"id": f"{i:012x}", "status": "downloading"}
        with mock.patch.object(appmod, "validate_url", return_value=None), \
                mock.patch.object(appmod, "start_job", side_effect=lambda *a: started.append(a) or "x"):
            response = self.client.post("/download", json={"url": "http://example.com/x"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(started, [])


class CleanupTest(Base):
    def _job(self, job_id, **extra):
        job = {"id": job_id, "status": "queued", "created_at": time.time(),
               "cancel_event": threading.Event()}
        job.update(extra)
        appmod.JOBS[job_id] = job
        return job

    def test_failed_job_leaves_no_files(self):
        job_id = "b" * 12
        self._job(job_id)

        class Boom:
            def __enter__(self):
                raise RuntimeError("boom")

            def __exit__(self, *a):
                return False

        with mock.patch.object(appmod, "make_ydl", lambda opts: Boom()):
            appmod.run_job(job_id, "http://example.com", "best", None)

        self.assertEqual(appmod.JOBS[job_id]["status"], "error")
        self.assertFalse((appmod.DOWNLOAD_DIR / job_id).exists())

    def test_cancelled_job_is_removed(self):
        job_id = "c" * 12
        job = self._job(job_id)
        job["cancel_event"].set()

        class Cancelled:
            def __enter__(self):
                raise appmod.JobCancelled()

            def __exit__(self, *a):
                return False

        with mock.patch.object(appmod, "make_ydl", lambda opts: Cancelled()):
            appmod.run_job(job_id, "http://example.com", "best", None)

        self.assertNotIn(job_id, appmod.JOBS)
        self.assertFalse((appmod.DOWNLOAD_DIR / job_id).exists())

    def test_successful_job_keeps_file_until_ttl(self):
        job_id = "d" * 12
        job = self._job(job_id)
        outdir = appmod.DOWNLOAD_DIR / job_id
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "video.mp4").write_bytes(b"data")

        info = {"title": "T", "duration": 5}

        class FakeYDL:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=True):
                return info

        with mock.patch.object(appmod, "make_ydl", lambda opts: FakeYDL()):
            appmod.run_job(job_id, "http://example.com", "best", None)

        self.assertEqual(appmod.JOBS[job_id]["status"], "done")
        self.assertTrue((outdir / "video.mp4").exists())
        appmod.drop_job(job_id)
        self.assertFalse(outdir.exists())


class HealthTest(Base):
    def test_concurrent_health_checks_all_succeed(self):
        results = []

        def worker():
            client = appmod.app.test_client()
            results.append(client.get("/health").status_code)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [200] * 16)


class DeleteJobTest(Base):
    def test_invalid_id_is_not_found(self):
        self.assertEqual(self.client.delete("/job/not-a-job").status_code, 404)

    def test_active_job_is_cancelled_not_deleted(self):
        job_id = "e" * 12
        event = threading.Event()
        appmod.JOBS[job_id] = {"id": job_id, "status": "downloading", "cancel_event": event}
        response = self.client.delete(f"/job/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json().get("cancelled"))
        self.assertTrue(event.is_set())
        self.assertIn(job_id, appmod.JOBS)

    def test_finished_job_is_deleted_with_its_files(self):
        job_id = "f" * 12
        outdir = appmod.DOWNLOAD_DIR / job_id
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "video.mp4").write_bytes(b"data")
        appmod.JOBS[job_id] = {"id": job_id, "status": "done", "dir": str(outdir)}
        response = self.client.delete(f"/job/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(job_id, appmod.JOBS)
        self.assertFalse(outdir.exists())


class GuardWiringTest(Base):
    def test_urllib_guard_success_is_silent(self):
        with mock.patch.object(appmod, "install_urllib_redirect_guard", return_value=True), \
                mock.patch.object(appmod, "_guard_warning") as warn:
            appmod.ensure_urllib_guard()
        warn.assert_not_called()

    def test_urllib_guard_failure_warns_once(self):
        with mock.patch.object(appmod, "install_urllib_redirect_guard", return_value=False), \
                mock.patch.object(appmod, "_guard_warning") as warn:
            appmod.ensure_urllib_guard()
            appmod.ensure_urllib_guard()
        warn.assert_called_once()

    def test_urllib_guard_failure_is_fatal_when_required(self):
        appmod.REQUIRE_GUARD = True
        with mock.patch.object(appmod, "install_urllib_redirect_guard", return_value=False), \
                self.assertRaises(SystemExit):
            appmod.ensure_urllib_guard()

    def test_make_ydl_drops_unguarded_curl_handler(self):
        opts = {"quiet": True, "logger": logging.getLogger("t")}
        with mock.patch.object(appmod, "install_ydl_redirect_guard", return_value=False), \
                mock.patch.object(appmod, "_guard_warning") as warn:
            ydl = appmod.make_ydl(opts)
        self.assertNotIn("CurlCFFI", ydl._request_director.handlers)
        warn.assert_called_once()

    def test_make_ydl_is_silent_when_there_is_no_curl_handler(self):
        class FakeDirector:
            handlers = {}

        class FakeYDL:
            _request_director = FakeDirector()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(appmod.yt_dlp, "YoutubeDL", return_value=FakeYDL()), \
                mock.patch.object(appmod, "install_ydl_redirect_guard", return_value=False), \
                mock.patch.object(appmod, "_guard_warning") as warn:
            appmod.make_ydl({"quiet": True})
        warn.assert_not_called()


class DirectDownloadTest(Base):
    """Direct-to-browser candidates and the /info branch that exposes them."""

    def _muxed(self, url, height, ext="mp4", vcodec="avc1", acodec="mp4a"):
        return {"url": url, "ext": ext, "protocol": "https",
                "vcodec": vcodec, "acodec": acodec, "height": height}

    # ---- unit: direct_candidate ------------------------------------------

    def test_single_direct_file_is_a_candidate(self):
        meta = {"formats": [{"url": "https://example.com/v.mp4", "ext": "mp4", "protocol": "https"}]}
        with mock.patch.object(appmod, "validate_url", return_value=None):
            cand = appmod.direct_candidate(meta, "best")
        self.assertIsNotNone(cand)
        self.assertEqual(cand["url"], "https://example.com/v.mp4")
        self.assertEqual(cand["ext"], "mp4")

    def test_separate_video_and_audio_is_not_a_candidate(self):
        meta = {"formats": [
            self._muxed("https://example.com/v.mp4", 1080, vcodec="avc1", acodec="none"),
            {"url": "https://example.com/a.m4a", "ext": "m4a", "protocol": "https",
             "vcodec": "none", "acodec": "mp4a"},
        ]}
        self.assertIsNone(appmod.direct_candidate(meta, "best"))

    def test_manifest_is_not_a_candidate(self):
        meta = {"formats": [{"url": "https://example.com/x.m3u8", "ext": "mp4", "protocol": "m3u8_native"}]}
        self.assertIsNone(appmod.direct_candidate(meta, "best"))

    def test_fragmented_format_is_not_a_candidate(self):
        meta = {"formats": [{"url": "https://example.com/v.mp4", "ext": "mp4",
                             "protocol": "https", "fragments": [{"url": "https://example.com/seg"}]}]}
        self.assertIsNone(appmod.direct_candidate(meta, "best"))

    def test_audio_quality_never_uses_a_muxed_file(self):
        meta = {"formats": [self._muxed("https://example.com/v.mp4", 720)]}
        self.assertIsNone(appmod.direct_candidate(meta, "audio"))

    def test_quality_cap_picks_a_format_within_the_cap(self):
        meta = {"formats": [self._muxed("https://example.com/1080.mp4", 1080),
                            self._muxed("https://example.com/480.mp4", 480)]}
        with mock.patch.object(appmod, "validate_url", return_value=None):
            cand = appmod.direct_candidate(meta, "480")
        self.assertEqual(cand["url"], "https://example.com/480.mp4")

    def test_nothing_within_the_cap_is_not_a_candidate(self):
        meta = {"formats": [self._muxed("https://example.com/1080.mp4", 1080)]}
        self.assertIsNone(appmod.direct_candidate(meta, "480"))

    def test_internal_candidate_url_is_refused_with_the_real_guard(self):
        # Real validate_url (no patch). An IP literal needs no DNS, and
        # 169.254.169.254 is link-local, so the guard must reject it.
        meta = {"formats": [{"url": "http://169.254.169.254/v.mp4", "ext": "mp4", "protocol": "http"}]}
        self.assertIsNone(appmod.direct_candidate(meta, "best"))

    def test_candidate_url_is_revalidated(self):
        meta = {"formats": [{"url": "https://evil.internal/v.mp4", "ext": "mp4", "protocol": "https"}]}
        with mock.patch.object(appmod, "validate_url", return_value="That host is not allowed."):
            self.assertIsNone(appmod.direct_candidate(meta, "best"))

    # ---- route: /info ----------------------------------------------------

    def _post_info(self, url="https://example.com/page", quality="best"):
        return self.client.post("/info", json={"url": url, "quality": quality})

    def _fake_ydl(self, meta):
        class FakeYDL:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return meta

        return FakeYDL()

    def test_info_exposes_direct_when_enabled_and_eligible(self):
        appmod.DIRECT_DOWNLOAD = True
        meta = {"title": "T", "formats": [
            {"url": "https://example.com/v.mp4", "ext": "mp4", "protocol": "https"}]}
        with mock.patch.object(appmod, "validate_url", return_value=None), \
                mock.patch.object(appmod, "make_ydl", lambda opts: self._fake_ydl(meta)):
            response = self._post_info()
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(body["direct"])
        self.assertEqual(body["direct"]["url"], "https://example.com/v.mp4")

    def test_info_returns_direct_null_when_disabled(self):
        appmod.DIRECT_DOWNLOAD = False
        meta = {"title": "T", "formats": [
            {"url": "https://example.com/v.mp4", "ext": "mp4", "protocol": "https"}]}
        with mock.patch.object(appmod, "validate_url", return_value=None), \
                mock.patch.object(appmod, "make_ydl", lambda opts: self._fake_ydl(meta)):
            response = self._post_info()
        self.assertIsNone(response.get_json()["direct"])

    def test_info_returns_direct_null_when_ineligible(self):
        appmod.DIRECT_DOWNLOAD = True
        meta = {"title": "T", "formats": [
            {"url": "https://example.com/v.mp4", "ext": "mp4", "protocol": "https",
             "vcodec": "avc1", "acodec": "none", "height": 1080},
            {"url": "https://example.com/a.m4a", "ext": "m4a", "protocol": "https",
             "vcodec": "none", "acodec": "mp4a"},
        ]}
        with mock.patch.object(appmod, "validate_url", return_value=None), \
                mock.patch.object(appmod, "make_ydl", lambda opts: self._fake_ydl(meta)):
            response = self._post_info()
        self.assertIsNone(response.get_json()["direct"])

    def test_info_drops_a_candidate_that_fails_revalidation(self):
        appmod.DIRECT_DOWNLOAD = True
        meta = {"title": "T", "formats": [
            {"url": "https://cdn.example.net/v.mp4", "ext": "mp4", "protocol": "https"}]}

        def only_source_allowed(u):
            return None if u == "https://example.com/page" else "That host is not allowed."

        with mock.patch.object(appmod, "validate_url", side_effect=only_source_allowed), \
                mock.patch.object(appmod, "make_ydl", lambda opts: self._fake_ydl(meta)):
            response = self._post_info()
        self.assertIsNone(response.get_json()["direct"])

    # ---- auth / unchanged flow ------------------------------------------

    def test_info_and_download_still_require_auth(self):
        appmod.ACCESS_TOKEN = "tok"
        appmod.DIRECT_DOWNLOAD = True
        self.assertEqual(self._post_info().status_code, 401)
        self.assertEqual(
            self.client.post("/download", json={"url": "https://example.com/page"}).status_code, 401)

    def test_download_endpoint_is_unchanged_when_direct_is_enabled(self):
        appmod.DIRECT_DOWNLOAD = True
        started = []
        with mock.patch.object(appmod, "validate_url", return_value=None), \
                mock.patch.object(appmod, "start_job",
                                  side_effect=lambda *a: started.append(a) or "jobid"):
            response = self.client.post("/download", json={"url": "https://example.com/page"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["job_id"], "jobid")
        self.assertTrue(started)
        self.assertNotIn("direct", response.get_json())


class RedactionTest(Base):
    def test_proxy_credentials_are_redacted(self):
        appmod.PROXY = "http://bob:hunter2@proxy.example:8080"
        message = appmod.clean_error(
            RuntimeError("Unable to connect to proxy http://bob:hunter2@proxy.example:8080"))
        self.assertNotIn("hunter2", message)
        self.assertNotIn("bob", message)

    def test_access_token_is_redacted(self):
        appmod.ACCESS_TOKEN = "top-secret"
        self.assertNotIn("top-secret", appmod.clean_error(RuntimeError("failed: top-secret")))


if __name__ == "__main__":
    unittest.main()
