"""Built-in knowledge of New Relic metric names and event attributes and
what produces the equivalent data in an LGTM stack.

Two tables:

* :data:`INFRA` — (event type, attribute) for the infrastructure sample
  events (``SystemSample``, ``ProcessSample``, ``NetworkSample``,
  ``StorageSample``, ``ContainerSample``, every ``K8s*Sample``) mapped
  onto node_exporter, process-exporter, cAdvisor, kube-state-metrics and
  kubelet metrics.
* :data:`METRICS` — ``FROM Metric`` names New Relic itself produces
  (``apm.service.*``, ``newrelic.goldenmetrics.*``), OpenTelemetry
  semantic-convention names as they arrive in New Relic, and the
  infrastructure agent's dimensional metrics (``host.*``, ``k8s.*``),
  which alias into :data:`INFRA`. AWS CloudWatch metric streams
  (``aws.<namespace>.<Metric>``) follow the YACE exporter convention.

Every entry is a :class:`Spec`. Kinds:

``gauge`` / ``counter`` / ``histogram``
    a Prometheus metric family the normal aggregation rules apply to
    (``name`` + fixed ``matchers`` + ``unit``).
``rate``
    New Relic reports a per-second rate that Prometheus stores as a
    counter: ``average(x)`` becomes ``avg(rate(counter[W]))``.
``expr``
    a full PromQL template. Placeholders: ``<W>`` window, ``<BY>``
    grouping (``by (a, b)`` or empty), ``<AGG>`` the outer aggregation
    (avg/max/min/sum from the NRQL function), ``<AGGINV>`` its inverse
    for ``1 - x`` shapes (max<->min), ``<SEL>`` ``,k=v`` matcher tail,
    ``<SELBARE>`` ``k=v`` matchers, ``<HTTP>`` the configured HTTP server
    histogram base name.
``count``
    the metric whose series count the entity population
    (``uniqueCount(podName)`` -> ``count(kube_pod_info)``).
``none``
    explicitly untranslatable, with the reason and the LGTM equivalent
    the user would need to build.

Everything here is a convention, not a fact about a particular cluster,
so specs default to ``needs-review`` unless the mapping is canonical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .common import APPROXIMATE, EXACT, NEEDS_REVIEW


@dataclass
class Spec:
    kind: str
    name: str = ""
    expr: str = ""
    matchers: List[Tuple[str, str, str]] = field(default_factory=list)
    unit: str = ""
    conf: str = NEEDS_REVIEW
    note: str = ""
    reason: str = ""
    # Attribute names whose uniqueCount() maps to the entity count.
    entity_attrs: List[str] = field(default_factory=list)


def _g(name: str, unit: str = "short", matchers=None, note: str = "",
       conf: str = NEEDS_REVIEW) -> Spec:
    return Spec("gauge", name=name, unit=unit, matchers=list(matchers or []),
                note=note, conf=conf)


def _r(name: str, unit: str = "short", matchers=None, note: str = "",
       conf: str = NEEDS_REVIEW) -> Spec:
    return Spec("rate", name=name, unit=unit, matchers=list(matchers or []),
                note=note, conf=conf)


def _h(name: str, unit: str = "s", note: str = "",
       conf: str = NEEDS_REVIEW) -> Spec:
    return Spec("histogram", name=name, unit=unit, note=note, conf=conf)


def _c(name: str, unit: str = "short", matchers=None, note: str = "",
       conf: str = NEEDS_REVIEW) -> Spec:
    return Spec("counter", name=name, unit=unit,
                matchers=list(matchers or []), note=note, conf=conf)


def _e(expr: str, unit: str = "short", note: str = "",
       conf: str = NEEDS_REVIEW) -> Spec:
    return Spec("expr", expr=expr, unit=unit, note=note, conf=conf)


def _cnt(name: str, matchers=None, note: str = "") -> Spec:
    return Spec("count", name=name, matchers=list(matchers or []),
                unit="short", note=note)


def _no(reason: str) -> Spec:
    return Spec("none", reason=reason)


_FS = [("fstype", "!~", "tmpfs|overlay|squashfs")]
_NONLO = [("device", "!=", "lo")]
_CONT = [("container", "!=", "")]
_ROOT = [("id", "=", "/")]

NODE_NOTE = ("node_exporter metric; the host label is `instance` unless your "
             "scrape relabels it")
KSM_NOTE = "kube-state-metrics"
CADV_NOTE = "cAdvisor (kubelet) metric"
PROC_NOTE = ("process-exporter (namedprocess_namegroup_*) metric grouped by "
             "`groupname`; map processDisplayName/commandName to your "
             "process-exporter groups")
KUBELET_NOTE = "kubelet volume-stats metric"


# ---------------------------------------------------------------------------
# Infrastructure sample events
# ---------------------------------------------------------------------------

INFRA: Dict[Tuple[str, str], Spec] = {}


def _norm_attr(attr: str) -> str:
    if attr == "__count__":
        return attr
    return attr.lower().replace("_", "").replace(".", "")


def _add(event: str, table: Dict[str, Spec]) -> None:
    for attr, spec in table.items():
        INFRA[(event, _norm_attr(attr))] = spec


def _cpu_mode(mode: str) -> Spec:
    return _e("100 * <AGG> <BY>(rate(node_cpu_seconds_total{mode=\"%s\"<SEL>}"
              "[<W>]))" % mode, "percent", NODE_NOTE, APPROXIMATE)


_add("systemsample", {
    "cpuPercent": _e(
        "100 * (1 - <AGGINV> <BY>(rate(node_cpu_seconds_total{mode=\"idle\""
        "<SEL>}[<W>])))", "percent", "node_exporter CPU busy % = 100 - idle; "
        + NODE_NOTE, APPROXIMATE),
    "cpuUserPercent": _cpu_mode("user"),
    "cpuSystemPercent": _cpu_mode("system"),
    "cpuIoWaitPercent": _cpu_mode("iowait"),
    "cpuIOWaitPercent": _cpu_mode("iowait"),
    "cpuStealPercent": _cpu_mode("steal"),
    "cpuIdlePercent": _cpu_mode("idle"),
    "cpuNicePercent": _cpu_mode("nice"),
    "memoryUsedPercent": _e(
        "100 * (1 - <AGGINV> <BY>(node_memory_MemAvailable_bytes{<SELBARE>} / "
        "node_memory_MemTotal_bytes{<SELBARE>}))", "percent",
        "used = total - available; " + NODE_NOTE, APPROXIMATE),
    "memoryFreePercent": _e(
        "100 * <AGG> <BY>(node_memory_MemAvailable_bytes{<SELBARE>} / "
        "node_memory_MemTotal_bytes{<SELBARE>})", "percent", NODE_NOTE,
        APPROXIMATE),
    "memoryUsedBytes": _e(
        "<AGG> <BY>(node_memory_MemTotal_bytes{<SELBARE>} - "
        "node_memory_MemAvailable_bytes{<SELBARE>})", "bytes", NODE_NOTE,
        APPROXIMATE),
    "memoryFreeBytes": _g("node_memory_MemAvailable_bytes", "bytes",
                          note=NODE_NOTE, conf=APPROXIMATE),
    "memoryTotalBytes": _g("node_memory_MemTotal_bytes", "bytes",
                           note=NODE_NOTE, conf=EXACT),
    "systemMemoryBytes": _g("node_memory_MemTotal_bytes", "bytes",
                            note=NODE_NOTE, conf=EXACT),
    "memoryCachedBytes": _g("node_memory_Cached_bytes", "bytes",
                            note=NODE_NOTE, conf=EXACT),
    "memorySlabBytes": _g("node_memory_Slab_bytes", "bytes", note=NODE_NOTE,
                          conf=EXACT),
    "memorySharedBytes": _g("node_memory_Shmem_bytes", "bytes",
                            note=NODE_NOTE, conf=EXACT),
    "memoryBuffersBytes": _g("node_memory_Buffers_bytes", "bytes",
                             note=NODE_NOTE, conf=EXACT),
    "swapUsedBytes": _e(
        "<AGG> <BY>(node_memory_SwapTotal_bytes{<SELBARE>} - "
        "node_memory_SwapFree_bytes{<SELBARE>})", "bytes", NODE_NOTE,
        EXACT),
    "swapFreeBytes": _g("node_memory_SwapFree_bytes", "bytes",
                        note=NODE_NOTE, conf=EXACT),
    "swapTotalBytes": _g("node_memory_SwapTotal_bytes", "bytes",
                         note=NODE_NOTE, conf=EXACT),
    "diskUsedPercent": _e(
        "100 * (1 - <AGGINV> <BY>(node_filesystem_avail_bytes{fstype!~"
        "\"tmpfs|overlay|squashfs\"<SEL>} / node_filesystem_size_bytes{fstype"
        "!~\"tmpfs|overlay|squashfs\"<SEL>}))", "percent",
        "per-filesystem; NR reports the host aggregate; " + NODE_NOTE,
        APPROXIMATE),
    "diskFreePercent": _e(
        "100 * <AGG> <BY>(node_filesystem_avail_bytes{fstype!~"
        "\"tmpfs|overlay|squashfs\"<SEL>} / node_filesystem_size_bytes{fstype"
        "!~\"tmpfs|overlay|squashfs\"<SEL>})", "percent", NODE_NOTE,
        APPROXIMATE),
    "diskUsedBytes": _e(
        "<AGG> <BY>(node_filesystem_size_bytes{fstype!~\"tmpfs|overlay|"
        "squashfs\"<SEL>} - node_filesystem_avail_bytes{fstype!~\"tmpfs|"
        "overlay|squashfs\"<SEL>})", "bytes", NODE_NOTE, APPROXIMATE),
    "diskFreeBytes": _g("node_filesystem_avail_bytes", "bytes", _FS,
                        NODE_NOTE, APPROXIMATE),
    "diskTotalBytes": _g("node_filesystem_size_bytes", "bytes", _FS,
                         NODE_NOTE, APPROXIMATE),
    "diskUtilizationPercent": _e(
        "100 * <AGG> <BY>(rate(node_disk_io_time_seconds_total{<SELBARE>}"
        "[<W>]))", "percent", NODE_NOTE, APPROXIMATE),
    "diskReadUtilizationPercent": _e(
        "100 * <AGG> <BY>(rate(node_disk_read_time_seconds_total{<SELBARE>}"
        "[<W>]))", "percent", NODE_NOTE, APPROXIMATE),
    "diskWriteUtilizationPercent": _e(
        "100 * <AGG> <BY>(rate(node_disk_write_time_seconds_total{<SELBARE>}"
        "[<W>]))", "percent", NODE_NOTE, APPROXIMATE),
    "diskReadsPerSecond": _r("node_disk_reads_completed_total", "iops",
                             note=NODE_NOTE, conf=EXACT),
    "diskWritesPerSecond": _r("node_disk_writes_completed_total", "iops",
                              note=NODE_NOTE, conf=EXACT),
    "diskReadBytesPerSecond": _r("node_disk_read_bytes_total", "Bps",
                                 note=NODE_NOTE, conf=EXACT),
    "diskWriteBytesPerSecond": _r("node_disk_written_bytes_total", "Bps",
                                  note=NODE_NOTE, conf=EXACT),
    "loadAverageOneMinute": _g("node_load1", "short", note=NODE_NOTE,
                               conf=EXACT),
    "loadAverageFiveMinute": _g("node_load5", "short", note=NODE_NOTE,
                                conf=EXACT),
    "loadAverageFifteenMinute": _g("node_load15", "short", note=NODE_NOTE,
                                   conf=EXACT),
    "uptime": _e("<AGG> <BY>(time() - node_boot_time_seconds{<SELBARE>})",
                 "s", NODE_NOTE, EXACT),
    "coreCount": _e("<AGG> <BY>(count by (instance)(node_cpu_seconds_total"
                    "{mode=\"idle\"<SEL>}))", "short", NODE_NOTE, EXACT),
    "processorCount": _e("<AGG> <BY>(count by (instance)("
                         "node_cpu_seconds_total{mode=\"idle\"<SEL>}))",
                         "short", NODE_NOTE, EXACT),
    "__count__": Spec("count", name="node_uname_info",
                      entity_attrs=["hostname", "fullHostname", "entityGuid",
                                    "entityName", "entityId", "entityKey",
                                    "displayName", "host"],
                      note="one node_uname_info series per host; "
                           + NODE_NOTE),
})

_add("networksample", {
    "receiveBytesPerSecond": _r("node_network_receive_bytes_total", "Bps",
                                _NONLO, NODE_NOTE, EXACT),
    "transmitBytesPerSecond": _r("node_network_transmit_bytes_total", "Bps",
                                 _NONLO, NODE_NOTE, EXACT),
    "receivePacketsPerSecond": _r("node_network_receive_packets_total", "pps",
                                  _NONLO, NODE_NOTE, EXACT),
    "transmitPacketsPerSecond": _r("node_network_transmit_packets_total",
                                   "pps", _NONLO, NODE_NOTE, EXACT),
    "receiveErrorsPerSecond": _r("node_network_receive_errs_total", "short",
                                 _NONLO, NODE_NOTE, EXACT),
    "transmitErrorsPerSecond": _r("node_network_transmit_errs_total", "short",
                                  _NONLO, NODE_NOTE, EXACT),
    "receiveDroppedPerSecond": _r("node_network_receive_drop_total", "short",
                                  _NONLO, NODE_NOTE, EXACT),
    "transmitDroppedPerSecond": _r("node_network_transmit_drop_total", "short",
                                   _NONLO, NODE_NOTE, EXACT),
    "__count__": Spec("count", name="node_network_info",
                      entity_attrs=["interfaceName", "device", "entityGuid",
                                    "hostname"],
                      note=NODE_NOTE),
})

_add("storagesample", {
    "readBytesPerSecond": _r("node_disk_read_bytes_total", "Bps",
                             note=NODE_NOTE, conf=EXACT),
    "writeBytesPerSecond": _r("node_disk_written_bytes_total", "Bps",
                              note=NODE_NOTE, conf=EXACT),
    "readsPerSecond": _r("node_disk_reads_completed_total", "iops",
                         note=NODE_NOTE, conf=EXACT),
    "writesPerSecond": _r("node_disk_writes_completed_total", "iops",
                          note=NODE_NOTE, conf=EXACT),
    "totalUtilizationPercent": _e(
        "100 * <AGG> <BY>(rate(node_disk_io_time_seconds_total{<SELBARE>}"
        "[<W>]))", "percent", NODE_NOTE, APPROXIMATE),
    "readUtilizationPercent": _e(
        "100 * <AGG> <BY>(rate(node_disk_read_time_seconds_total{<SELBARE>}"
        "[<W>]))", "percent", NODE_NOTE, APPROXIMATE),
    "writeUtilizationPercent": _e(
        "100 * <AGG> <BY>(rate(node_disk_write_time_seconds_total{<SELBARE>}"
        "[<W>]))", "percent", NODE_NOTE, APPROXIMATE),
    "diskUsedPercent": INFRA[("systemsample", "diskusedpercent")],
    "diskFreePercent": INFRA[("systemsample", "diskfreepercent")],
    "diskUsedBytes": INFRA[("systemsample", "diskusedbytes")],
    "diskFreeBytes": INFRA[("systemsample", "diskfreebytes")],
    "diskTotalBytes": INFRA[("systemsample", "disktotalbytes")],
    "inodesUsedPercent": _e(
        "100 * (1 - <AGGINV> <BY>(node_filesystem_files_free{fstype!~"
        "\"tmpfs|overlay|squashfs\"<SEL>} / node_filesystem_files{fstype!~"
        "\"tmpfs|overlay|squashfs\"<SEL>}))", "percent", NODE_NOTE,
        APPROXIMATE),
    "inodesFree": _g("node_filesystem_files_free", "short", _FS, NODE_NOTE,
                     EXACT),
    "inodesTotal": _g("node_filesystem_files", "short", _FS, NODE_NOTE,
                      EXACT),
    "avgQueueLen": _r("node_disk_io_time_weighted_seconds_total", "short",
                      note=NODE_NOTE, conf=APPROXIMATE),
    "currentQueueLen": _g("node_disk_io_now", "short", note=NODE_NOTE,
                          conf=EXACT),
    "__count__": Spec("count", name="node_filesystem_size_bytes",
                      matchers=list(_FS),
                      entity_attrs=["mountPoint", "device", "entityGuid",
                                    "hostname", "filesystemType"],
                      note=NODE_NOTE),
})

_add("processsample", {
    "cpuPercent": _e(
        "100 * <AGG> <BY>(rate(namedprocess_namegroup_cpu_seconds_total"
        "{<SELBARE>}[<W>]))", "percent", PROC_NOTE, APPROXIMATE),
    "cpuUserPercent": _e(
        "100 * <AGG> <BY>(rate(namedprocess_namegroup_cpu_seconds_total"
        "{mode=\"user\"<SEL>}[<W>]))", "percent", PROC_NOTE, APPROXIMATE),
    "cpuSystemPercent": _e(
        "100 * <AGG> <BY>(rate(namedprocess_namegroup_cpu_seconds_total"
        "{mode=\"system\"<SEL>}[<W>]))", "percent", PROC_NOTE, APPROXIMATE),
    "memoryResidentSizeBytes": _g("namedprocess_namegroup_memory_bytes",
                                  "bytes", [("memtype", "=", "resident")],
                                  PROC_NOTE, APPROXIMATE),
    "memoryVirtualSizeBytes": _g("namedprocess_namegroup_memory_bytes",
                                 "bytes", [("memtype", "=", "virtual")],
                                 PROC_NOTE, APPROXIMATE),
    "threadCount": _g("namedprocess_namegroup_num_threads", "short",
                      note=PROC_NOTE, conf=APPROXIMATE),
    "fdCount": _g("namedprocess_namegroup_open_filedesc", "short",
                  note=PROC_NOTE, conf=APPROXIMATE),
    "fileDescriptorCount": _g("namedprocess_namegroup_open_filedesc", "short",
                              note=PROC_NOTE, conf=APPROXIMATE),
    "ioReadBytesPerSecond": _r("namedprocess_namegroup_read_bytes_total",
                               "Bps", note=PROC_NOTE, conf=APPROXIMATE),
    "ioWriteBytesPerSecond": _r("namedprocess_namegroup_write_bytes_total",
                                "Bps", note=PROC_NOTE, conf=APPROXIMATE),
    "ioReadCountPerSecond": _r("namedprocess_namegroup_read_bytes_total",
                               "short", note=PROC_NOTE),
    "__count__": Spec("expr", expr="<AGG> <BY>(namedprocess_namegroup_num_procs"
                                    "{<SELBARE>})",
                      unit="short",
                      entity_attrs=["processId", "pid", "entityGuid",
                                    "processDisplayName", "commandName",
                                    "commandLine"],
                      note=PROC_NOTE),
})

_add("containersample", {
    "cpuPercent": _e(
        "100 * <AGG> <BY>(rate(container_cpu_usage_seconds_total{container!="
        "\"\"<SEL>}[<W>]))", "percent", CADV_NOTE, APPROXIMATE),
    "cpuUsedCores": _e(
        "<AGG> <BY>(rate(container_cpu_usage_seconds_total{container!=\"\""
        "<SEL>}[<W>]))", "short", CADV_NOTE, EXACT),
    "cpuKernelPercent": _e(
        "100 * <AGG> <BY>(rate(container_cpu_system_seconds_total{container"
        "!=\"\"<SEL>}[<W>]))", "percent", CADV_NOTE, APPROXIMATE),
    "cpuUserPercent": _e(
        "100 * <AGG> <BY>(rate(container_cpu_user_seconds_total{container!="
        "\"\"<SEL>}[<W>]))", "percent", CADV_NOTE, APPROXIMATE),
    "cpuLimitCores": _g("container_spec_cpu_quota", "short", _CONT,
                        "cAdvisor quota in microseconds per period; divide "
                        "by container_spec_cpu_period for cores",
                        NEEDS_REVIEW),
    "memoryUsageBytes": _g("container_memory_usage_bytes", "bytes", _CONT,
                           CADV_NOTE, EXACT),
    "memoryResidentSizeBytes": _g("container_memory_rss", "bytes", _CONT,
                                  CADV_NOTE, EXACT),
    "memoryCacheBytes": _g("container_memory_cache", "bytes", _CONT,
                           CADV_NOTE, EXACT),
    "memorySizeLimitBytes": _g("container_spec_memory_limit_bytes", "bytes",
                               _CONT, CADV_NOTE, EXACT),
    "memoryUsageLimitPercent": _e(
        "100 * <AGG> <BY>(container_memory_usage_bytes{container!=\"\"<SEL>} "
        "/ (container_spec_memory_limit_bytes{container!=\"\"<SEL>} > 0))",
        "percent", "unlimited containers (limit 0) are excluded; "
        + CADV_NOTE, APPROXIMATE),
    "networkRxBytesPerSecond": _r("container_network_receive_bytes_total",
                                  "Bps", note=CADV_NOTE, conf=EXACT),
    "networkTxBytesPerSecond": _r("container_network_transmit_bytes_total",
                                  "Bps", note=CADV_NOTE, conf=EXACT),
    "networkRxDroppedPerSecond": _r("container_network_receive_packets_dropped_total",
                                    "short", note=CADV_NOTE, conf=EXACT),
    "networkTxDroppedPerSecond": _r("container_network_transmit_packets_dropped_total",
                                    "short", note=CADV_NOTE, conf=EXACT),
    "networkRxErrorsPerSecond": _r("container_network_receive_errors_total",
                                   "short", note=CADV_NOTE, conf=EXACT),
    "networkTxErrorsPerSecond": _r("container_network_transmit_errors_total",
                                   "short", note=CADV_NOTE, conf=EXACT),
    "restartCount": _e("<AGG> <BY>(kube_pod_container_status_restarts_total"
                       "{<SELBARE>})", "short",
                       KSM_NOTE + " (Kubernetes only)", APPROXIMATE),
    "__count__": Spec("count", name="container_last_seen",
                      matchers=list(_CONT),
                      entity_attrs=["containerId", "containerName", "name",
                                    "entityGuid", "entityName", "image",
                                    "imageName"],
                      note="cAdvisor only reports running containers, so a "
                           "`state = 'running'` filter is implicit"),
})

_add("k8scontainersample", {
    "restartCount": _e("<AGG> <BY>(kube_pod_container_status_restarts_total"
                       "{<SELBARE>})", "short",
                       "kube-state-metrics cumulative restart count (current "
                       "value, as New Relic reports it)", EXACT),
    "cpuUsedCores": _e(
        "<AGG> <BY>(rate(container_cpu_usage_seconds_total{container!=\"\""
        "<SEL>}[<W>]))", "short", CADV_NOTE, EXACT),
    "cpuCoresUtilization": _e(
        "100 * <AGG> <BY>(sum by (namespace, pod, container)(rate("
        "container_cpu_usage_seconds_total{container!=\"\"<SEL>}[<W>])) / on "
        "(namespace, pod, container) max by (namespace, pod, container)("
        "kube_pod_container_resource_limits{"
        "resource=\"cpu\"<SEL>}))", "percent",
        "used / limit; containers without a CPU limit drop out",
        APPROXIMATE),
    "cpuRequestedCoresUtilization": _e(
        "100 * <AGG> <BY>(sum by (namespace, pod, container)(rate("
        "container_cpu_usage_seconds_total{container!=\"\"<SEL>}[<W>])) / on "
        "(namespace, pod, container) max by (namespace, pod, container)("
        "kube_pod_container_resource_requests{"
        "resource=\"cpu\"<SEL>}))", "percent", "used / request", APPROXIMATE),
    "cpuLimitCores": _g("kube_pod_container_resource_limits", "short",
                        [("resource", "=", "cpu")], KSM_NOTE, EXACT),
    "cpuRequestedCores": _g("kube_pod_container_resource_requests", "short",
                            [("resource", "=", "cpu")], KSM_NOTE, EXACT),
    "memoryLimitBytes": _g("kube_pod_container_resource_limits", "bytes",
                           [("resource", "=", "memory")], KSM_NOTE, EXACT),
    "memoryRequestedBytes": _g("kube_pod_container_resource_requests", "bytes",
                               [("resource", "=", "memory")], KSM_NOTE,
                               EXACT),
    "memoryWorkingSetBytes": _g("container_memory_working_set_bytes", "bytes",
                                _CONT, CADV_NOTE, EXACT),
    "memoryUsedBytes": _g("container_memory_usage_bytes", "bytes", _CONT,
                          CADV_NOTE, EXACT),
    "memoryWorkingSetUtilization": _e(
        "100 * <AGG> <BY>(sum by (namespace, pod, container)("
        "container_memory_working_set_bytes{container!=\"\"<SEL>}) / on "
        "(namespace, pod, container) max by (namespace, pod, container)("
        "kube_pod_container_resource_limits{"
        "resource=\"memory\"<SEL>}))", "percent",
        "working set / limit; containers without a memory limit drop out",
        APPROXIMATE),
    "memoryUtilization": _e(
        "100 * <AGG> <BY>(sum by (namespace, pod, container)("
        "container_memory_usage_bytes{container!=\"\"<SEL>}) / on "
        "(namespace, pod, container) max by (namespace, pod, container)("
        "kube_pod_container_resource_limits{"
        "resource=\"memory\"<SEL>}))", "percent", "usage / limit",
        APPROXIMATE),
    "memoryRequestedUtilization": _e(
        "100 * <AGG> <BY>(sum by (namespace, pod, container)("
        "container_memory_working_set_bytes{container!=\"\"<SEL>}) / on "
        "(namespace, pod, container) max by (namespace, pod, container)("
        "kube_pod_container_resource_requests{"
        "resource=\"memory\"<SEL>}))", "percent", "working set / request",
        APPROXIMATE),
    "isReady": _g("kube_pod_container_status_ready", "short", note=KSM_NOTE,
                  conf=EXACT),
    "fsUsedBytes": _g("container_fs_usage_bytes", "bytes", _CONT, CADV_NOTE,
                      APPROXIMATE),
    "__count__": Spec("count", name="kube_pod_container_info",
                      entity_attrs=["containerName", "containerID",
                                    "containerId", "entityGuid", "entityName",
                                    "displayName"],
                      note=KSM_NOTE),
})

_add("k8spodsample", {
    "isReady": _g("kube_pod_status_ready", "short",
                  [("condition", "=", "true")], KSM_NOTE, EXACT),
    "isScheduled": _g("kube_pod_status_scheduled", "short",
                      [("condition", "=", "true")], KSM_NOTE, EXACT),
    "restartCount": _e(
        "<AGG> <BY>(sum by (namespace, pod)("
        "kube_pod_container_status_restarts_total{<SELBARE>}))", "short",
        KSM_NOTE, EXACT),
    "cpuUsedCores": _e(
        "<AGG> <BY>(sum by (namespace, pod)(rate("
        "container_cpu_usage_seconds_total{container!=\"\"<SEL>}[<W>])))",
        "short", CADV_NOTE, EXACT),
    "cpuRequestedCores": _e(
        "<AGG> <BY>(sum by (namespace, pod)(kube_pod_container_resource_requests"
        "{resource=\"cpu\"<SEL>}))", "short", KSM_NOTE, EXACT),
    "cpuLimitCores": _e(
        "<AGG> <BY>(sum by (namespace, pod)(kube_pod_container_resource_limits"
        "{resource=\"cpu\"<SEL>}))", "short", KSM_NOTE, EXACT),
    "memoryWorkingSetBytes": _e(
        "<AGG> <BY>(sum by (namespace, pod)(container_memory_working_set_bytes"
        "{container!=\"\"<SEL>}))", "bytes", CADV_NOTE, EXACT),
    "memoryUsedBytes": _e(
        "<AGG> <BY>(sum by (namespace, pod)(container_memory_usage_bytes"
        "{container!=\"\"<SEL>}))", "bytes", CADV_NOTE, EXACT),
    "memoryRequestedBytes": _e(
        "<AGG> <BY>(sum by (namespace, pod)(kube_pod_container_resource_requests"
        "{resource=\"memory\"<SEL>}))", "bytes", KSM_NOTE, EXACT),
    "memoryLimitBytes": _e(
        "<AGG> <BY>(sum by (namespace, pod)(kube_pod_container_resource_limits"
        "{resource=\"memory\"<SEL>}))", "bytes", KSM_NOTE, EXACT),
    "net.rxBytesPerSecond": _e(
        "<AGG> <BY>(sum by (namespace, pod)(rate("
        "container_network_receive_bytes_total{<SELBARE>}[<W>])))", "Bps",
        CADV_NOTE, EXACT),
    "net.txBytesPerSecond": _e(
        "<AGG> <BY>(sum by (namespace, pod)(rate("
        "container_network_transmit_bytes_total{<SELBARE>}[<W>])))", "Bps",
        CADV_NOTE, EXACT),
    "net.errorsPerSecond": _e(
        "<AGG> <BY>(sum by (namespace, pod)(rate("
        "container_network_receive_errors_total{<SELBARE>}[<W>]) + rate("
        "container_network_transmit_errors_total{<SELBARE>}[<W>])))", "short",
        CADV_NOTE, EXACT),
    "createdAt": _e("<AGG> <BY>(kube_pod_created{<SELBARE>}) * 1000",
                    "dateTimeAsIso", KSM_NOTE, EXACT),
    "startTime": _e("<AGG> <BY>(kube_pod_start_time{<SELBARE>}) * 1000",
                    "dateTimeAsIso", KSM_NOTE, EXACT),
    "__count__": Spec("count", name="kube_pod_info",
                      entity_attrs=["podName", "podGuid", "entityGuid",
                                    "entityName", "displayName", "podId"],
                      note=KSM_NOTE + "; a status/phase filter selects "
                                      "kube_pod_status_phase instead"),
})

_add("k8snodesample", {
    "cpuUsedCores": _e(
        "<AGG> <BY>(rate(container_cpu_usage_seconds_total{id=\"/\"<SEL>}"
        "[<W>]))", "short",
        "root-cgroup cAdvisor series; node identity is the `node` label "
        "when kube-prometheus relabeling is in place", APPROXIMATE),
    "cpuUsedCoresUtilization": _e(
        "100 * <AGG> <BY>(sum by (node)(rate(container_cpu_usage_seconds_total"
        "{id=\"/\"<SEL>}[<W>])) / on (node) max by (node)(kube_node_status_allocatable{"
        "resource=\"cpu\"<SEL>}))", "percent", "used / allocatable",
        APPROXIMATE),
    "memoryUsedBytes": _g("container_memory_usage_bytes", "bytes", _ROOT,
                          "root-cgroup cAdvisor series", APPROXIMATE),
    "memoryWorkingSetBytes": _g("container_memory_working_set_bytes", "bytes",
                                _ROOT, "root-cgroup cAdvisor series",
                                APPROXIMATE),
    "memoryUsedBytesUtilization": _e(
        "100 * <AGG> <BY>(sum by (node)(container_memory_working_set_bytes"
        "{id=\"/\"<SEL>}) / on (node) max by (node)(kube_node_status_allocatable{"
        "resource=\"memory\"<SEL>}))", "percent", "working set / allocatable",
        APPROXIMATE),
    "allocatableCpuCores": _g("kube_node_status_allocatable", "short",
                              [("resource", "=", "cpu")], KSM_NOTE, EXACT),
    "allocatableMemoryBytes": _g("kube_node_status_allocatable", "bytes",
                                 [("resource", "=", "memory")], KSM_NOTE,
                                 EXACT),
    "allocatablePods": _g("kube_node_status_allocatable", "short",
                          [("resource", "=", "pods")], KSM_NOTE, EXACT),
    "capacityCpuCores": _g("kube_node_status_capacity", "short",
                           [("resource", "=", "cpu")], KSM_NOTE, EXACT),
    "capacityMemoryBytes": _g("kube_node_status_capacity", "bytes",
                              [("resource", "=", "memory")], KSM_NOTE, EXACT),
    "capacityPods": _g("kube_node_status_capacity", "short",
                       [("resource", "=", "pods")], KSM_NOTE, EXACT),
    "cpuRequestedCores": _e(
        "<AGG> <BY>(sum by (node)(kube_pod_container_resource_requests{"
        "resource=\"cpu\"<SEL>}))", "short", KSM_NOTE, EXACT),
    "cpuLimitCores": _e(
        "<AGG> <BY>(sum by (node)(kube_pod_container_resource_limits{"
        "resource=\"cpu\"<SEL>}))", "short", KSM_NOTE, EXACT),
    "memoryRequestedBytes": _e(
        "<AGG> <BY>(sum by (node)(kube_pod_container_resource_requests{"
        "resource=\"memory\"<SEL>}))", "bytes", KSM_NOTE, EXACT),
    "memoryLimitBytes": _e(
        "<AGG> <BY>(sum by (node)(kube_pod_container_resource_limits{"
        "resource=\"memory\"<SEL>}))", "bytes", KSM_NOTE, EXACT),
    "condition.Ready": _g("kube_node_status_condition", "short",
                          [("condition", "=", "Ready"), ("status", "=", "true")],
                          KSM_NOTE, EXACT),
    "condition.DiskPressure": _g("kube_node_status_condition", "short",
                                 [("condition", "=", "DiskPressure"),
                                  ("status", "=", "true")], KSM_NOTE, EXACT),
    "condition.MemoryPressure": _g("kube_node_status_condition", "short",
                                   [("condition", "=", "MemoryPressure"),
                                    ("status", "=", "true")], KSM_NOTE, EXACT),
    "condition.PIDPressure": _g("kube_node_status_condition", "short",
                                [("condition", "=", "PIDPressure"),
                                 ("status", "=", "true")], KSM_NOTE, EXACT),
    "condition.NetworkUnavailable": _g("kube_node_status_condition", "short",
                                       [("condition", "=", "NetworkUnavailable"),
                                        ("status", "=", "true")], KSM_NOTE,
                                       EXACT),
    "unschedulable": _g("kube_node_spec_unschedulable", "short",
                        note=KSM_NOTE, conf=EXACT),
    "fsUsedPercent": _e(
        "100 * (1 - <AGGINV> <BY>(node_filesystem_avail_bytes{mountpoint=\"/\""
        "<SEL>} / node_filesystem_size_bytes{mountpoint=\"/\"<SEL>}))",
        "percent", "root filesystem via node_exporter; " + NODE_NOTE,
        APPROXIMATE),
    "fsUsedBytes": _e(
        "<AGG> <BY>(node_filesystem_size_bytes{mountpoint=\"/\"<SEL>} - "
        "node_filesystem_avail_bytes{mountpoint=\"/\"<SEL>})", "bytes",
        NODE_NOTE, APPROXIMATE),
    "fsCapacityBytes": _g("node_filesystem_size_bytes", "bytes",
                          [("mountpoint", "=", "/")], NODE_NOTE, APPROXIMATE),
    "fsAvailableBytes": _g("node_filesystem_avail_bytes", "bytes",
                           [("mountpoint", "=", "/")], NODE_NOTE, APPROXIMATE),
    "net.rxBytesPerSecond": _r("node_network_receive_bytes_total", "Bps",
                               _NONLO, NODE_NOTE, APPROXIMATE),
    "net.txBytesPerSecond": _r("node_network_transmit_bytes_total", "Bps",
                               _NONLO, NODE_NOTE, APPROXIMATE),
    "__count__": Spec("count", name="kube_node_info",
                      entity_attrs=["nodeName", "entityGuid", "entityName",
                                    "displayName"],
                      note=KSM_NOTE),
})

_add("k8sdeploymentsample", {
    "podsDesired": _g("kube_deployment_spec_replicas", "short", note=KSM_NOTE,
                      conf=EXACT),
    "podsAvailable": _g("kube_deployment_status_replicas_available", "short",
                        note=KSM_NOTE, conf=EXACT),
    "podsUnavailable": _g("kube_deployment_status_replicas_unavailable",
                          "short", note=KSM_NOTE, conf=EXACT),
    "podsUpdated": _g("kube_deployment_status_replicas_updated", "short",
                      note=KSM_NOTE, conf=EXACT),
    "podsTotal": _g("kube_deployment_status_replicas", "short", note=KSM_NOTE,
                    conf=EXACT),
    "podsReady": _g("kube_deployment_status_replicas_ready", "short",
                    note=KSM_NOTE, conf=EXACT),
    "podsMissing": _e(
        "<AGG> <BY>(kube_deployment_spec_replicas{<SELBARE>} - "
        "kube_deployment_status_replicas_available{<SELBARE>})", "short",
        KSM_NOTE, EXACT),
    "__count__": Spec("count", name="kube_deployment_created",
                      entity_attrs=["deploymentName", "entityGuid",
                                    "entityName", "displayName"],
                      note=KSM_NOTE),
})

_add("k8sdaemonsetsample", {
    "podsDesired": _g("kube_daemonset_status_desired_number_scheduled", "short",
                      note=KSM_NOTE, conf=EXACT),
    "podsScheduled": _g("kube_daemonset_status_current_number_scheduled",
                        "short", note=KSM_NOTE, conf=EXACT),
    "podsReady": _g("kube_daemonset_status_number_ready", "short",
                    note=KSM_NOTE, conf=EXACT),
    "podsAvailable": _g("kube_daemonset_status_number_available", "short",
                        note=KSM_NOTE, conf=EXACT),
    "podsUnavailable": _g("kube_daemonset_status_number_unavailable", "short",
                          note=KSM_NOTE, conf=EXACT),
    "podsMisscheduled": _g("kube_daemonset_status_number_misscheduled", "short",
                           note=KSM_NOTE, conf=EXACT),
    "podsUpdatedScheduled": _g("kube_daemonset_status_updated_number_scheduled",
                               "short", note=KSM_NOTE, conf=EXACT),
    "podsMissing": _e(
        "<AGG> <BY>(kube_daemonset_status_desired_number_scheduled{<SELBARE>} "
        "- kube_daemonset_status_number_ready{<SELBARE>})", "short", KSM_NOTE,
        EXACT),
    "__count__": Spec("count", name="kube_daemonset_created",
                      entity_attrs=["daemonsetName", "entityGuid",
                                    "entityName", "displayName"],
                      note=KSM_NOTE),
})

_add("k8sstatefulsetsample", {
    "podsDesired": _g("kube_statefulset_replicas", "short", note=KSM_NOTE,
                      conf=EXACT),
    "podsReady": _g("kube_statefulset_status_replicas_ready", "short",
                    note=KSM_NOTE, conf=EXACT),
    "podsCurrent": _g("kube_statefulset_status_replicas_current", "short",
                      note=KSM_NOTE, conf=EXACT),
    "podsTotal": _g("kube_statefulset_status_replicas", "short", note=KSM_NOTE,
                    conf=EXACT),
    "podsUpdated": _g("kube_statefulset_status_replicas_updated", "short",
                      note=KSM_NOTE, conf=EXACT),
    "podsMissing": _e(
        "<AGG> <BY>(kube_statefulset_replicas{<SELBARE>} - "
        "kube_statefulset_status_replicas_ready{<SELBARE>})", "short",
        KSM_NOTE, EXACT),
    "__count__": Spec("count", name="kube_statefulset_created",
                      entity_attrs=["statefulsetName", "entityGuid",
                                    "entityName", "displayName"],
                      note=KSM_NOTE),
})

_add("k8sreplicasetsample", {
    "podsDesired": _g("kube_replicaset_spec_replicas", "short", note=KSM_NOTE,
                      conf=EXACT),
    "podsReady": _g("kube_replicaset_status_ready_replicas", "short",
                    note=KSM_NOTE, conf=EXACT),
    "podsTotal": _g("kube_replicaset_status_replicas", "short", note=KSM_NOTE,
                    conf=EXACT),
    "podsFullyLabeled": _g("kube_replicaset_status_fully_labeled_replicas",
                           "short", note=KSM_NOTE, conf=EXACT),
    "podsMissing": _e(
        "<AGG> <BY>(kube_replicaset_spec_replicas{<SELBARE>} - "
        "kube_replicaset_status_ready_replicas{<SELBARE>})", "short",
        KSM_NOTE, EXACT),
    "__count__": Spec("count", name="kube_replicaset_created",
                      entity_attrs=["replicasetName", "entityGuid",
                                    "entityName", "displayName"],
                      note=KSM_NOTE),
})

_add("k8shpasample", {
    "currentReplicas": _g("kube_horizontalpodautoscaler_status_current_replicas",
                          "short", note=KSM_NOTE, conf=EXACT),
    "desiredReplicas": _g("kube_horizontalpodautoscaler_status_desired_replicas",
                          "short", note=KSM_NOTE, conf=EXACT),
    "minReplicas": _g("kube_horizontalpodautoscaler_spec_min_replicas", "short",
                      note=KSM_NOTE, conf=EXACT),
    "maxReplicas": _g("kube_horizontalpodautoscaler_spec_max_replicas", "short",
                      note=KSM_NOTE, conf=EXACT),
    "targetMetric": _g("kube_horizontalpodautoscaler_spec_target_metric",
                       "short", note=KSM_NOTE, conf=APPROXIMATE),
    "currentMetric": _g("kube_horizontalpodautoscaler_status_target_metric",
                        "short", note=KSM_NOTE, conf=APPROXIMATE),
    "isAble": _g("kube_horizontalpodautoscaler_status_condition", "short",
                 [("condition", "=", "AbleToScale"), ("status", "=", "true")],
                 KSM_NOTE, EXACT),
    "isActive": _g("kube_horizontalpodautoscaler_status_condition", "short",
                   [("condition", "=", "ScalingActive"), ("status", "=", "true")],
                   KSM_NOTE, EXACT),
    "isLimited": _g("kube_horizontalpodautoscaler_status_condition", "short",
                    [("condition", "=", "ScalingLimited"),
                     ("status", "=", "true")], KSM_NOTE, EXACT),
    "__count__": Spec("count", name="kube_horizontalpodautoscaler_info",
                      entity_attrs=["hpaName", "displayName", "entityGuid",
                                    "entityName"],
                      note=KSM_NOTE),
})

_add("k8snamespacesample", {
    "__count__": Spec("count", name="kube_namespace_created",
                      entity_attrs=["namespaceName", "namespace", "entityGuid",
                                    "entityName", "displayName"],
                      note=KSM_NOTE),
})

_add("k8sclustersample", {
    "__count__": Spec("expr", expr="<AGG> <BY>(count by (cluster)("
                                    "kube_node_info{<SELBARE>}))",
                      unit="short",
                      entity_attrs=["clusterName", "entityGuid", "entityName",
                                    "displayName"],
                      note="clusters are counted as distinct `cluster` "
                           "label values on kube_node_info"),
})

_add("k8sservicesample", {
    "__count__": Spec("count", name="kube_service_info",
                      entity_attrs=["serviceName", "entityGuid", "entityName",
                                    "displayName"],
                      note=KSM_NOTE),
})

_add("k8svolumesample", {
    "fsUsedBytes": _g("kubelet_volume_stats_used_bytes", "bytes",
                      note=KUBELET_NOTE, conf=EXACT),
    "fsCapacityBytes": _g("kubelet_volume_stats_capacity_bytes", "bytes",
                          note=KUBELET_NOTE, conf=EXACT),
    "fsAvailableBytes": _g("kubelet_volume_stats_available_bytes", "bytes",
                           note=KUBELET_NOTE, conf=EXACT),
    "fsUsedPercent": _e(
        "100 * <AGG> <BY>(kubelet_volume_stats_used_bytes{<SELBARE>} / "
        "kubelet_volume_stats_capacity_bytes{<SELBARE>})", "percent",
        KUBELET_NOTE, EXACT),
    "fsInodesUsed": _g("kubelet_volume_stats_inodes_used", "short",
                       note=KUBELET_NOTE, conf=EXACT),
    "fsInodes": _g("kubelet_volume_stats_inodes", "short", note=KUBELET_NOTE,
                   conf=EXACT),
    "fsInodesFree": _g("kubelet_volume_stats_inodes_free", "short",
                       note=KUBELET_NOTE, conf=EXACT),
    "__count__": Spec("count", name="kube_persistentvolumeclaim_info",
                      entity_attrs=["pvcName", "volumeName", "entityGuid",
                                    "entityName", "displayName"],
                      note=KSM_NOTE),
})

for _et in ("k8sapiserversample", "k8sschedulersample",
            "k8scontrollermanagersample", "k8setcdsample"):
    INFRA[(_et, "*")] = _no(
        "Kubernetes control-plane sample events map onto the control "
        "plane's own Prometheus metrics (apiserver_*, scheduler_*, "
        "workqueue_*, etcd_*); rebuild this panel on those metrics")

INFRA[("k8sevent", "*")] = _no(
    "Kubernetes events are not metrics; ship them to Loki (Alloy "
    "loki.source.kubernetes_events or the eventrouter) and rewrite the "
    "widget as FROM Log with a {job=\"kubernetes-events\"} selector — or "
    "add an event_map entry for K8sEvent in the config")
INFRA[("k8seventssample", "*")] = INFRA[("k8sevent", "*")]
INFRA[("infrastructureevent", "*")] = _no(
    "InfrastructureEvent (agent lifecycle / inventory change events) has "
    "no metric equivalent; the closest LGTM source is Loki (agent logs "
    "or Kubernetes events) — rewrite as FROM Log or add an event_map entry")
INFRA[("deployment", "*")] = _no(
    "Deployment markers are Grafana annotations, not panel data: post them "
    "to /api/annotations from your deploy pipeline (or use the Grafana "
    "annotations datasource) and enable them on this dashboard")
INFRA[("nraiincident", "*")] = _no(
    "New Relic alert incidents map onto Grafana Alerting: use an Alert "
    "list panel or the ALERTS{alertstate=\"firing\"} metric in Mimir")
INFRA[("nraiissue", "*")] = INFRA[("nraiincident", "*")]
INFRA[("nraisignal", "*")] = INFRA[("nraiincident", "*")]

# Per-event attribute -> exporter label conventions. These override the
# generic label_map only while translating that event type (the same NR
# attribute name means different things on different events: `name` is
# the container name on ContainerSample but the transaction name on
# Transaction).
EVENT_LABELS: Dict[str, Dict[str, str]] = {
    "systemsample": {"hostname": "instance", "fullHostname": "instance",
                     "displayName": "instance", "entityName": "instance",
                     "host": "instance"},
    "networksample": {"interfaceName": "device", "hostname": "instance",
                      "fullHostname": "instance", "entityName": "instance"},
    "storagesample": {"mountPoint": "mountpoint", "device": "device",
                      "filesystemType": "fstype", "hostname": "instance",
                      "fullHostname": "instance", "entityName": "instance"},
    "processsample": {"processDisplayName": "groupname",
                      "commandName": "groupname", "hostname": "instance",
                      "fullHostname": "instance", "entityName": "instance"},
    "containersample": {"name": "name", "containerName": "name",
                        "containerId": "id", "image": "image",
                        "imageName": "image", "hostname": "instance",
                        "entityName": "name"},
    "k8scontainersample": {"containerName": "container", "podName": "pod",
                           "namespaceName": "namespace", "nodeName": "node",
                           "clusterName": "cluster", "displayName":
                           "container", "entityName": "container",
                           "deploymentName": "deployment", "image": "image",
                           "containerImage": "image"},
    "k8spodsample": {"podName": "pod", "namespaceName": "namespace",
                     "nodeName": "node", "clusterName": "cluster",
                     "displayName": "pod", "entityName": "pod",
                     "deploymentName": "deployment", "status": "phase",
                     "createdKind": "created_by_kind",
                     "createdBy": "created_by_name",
                     "label.app": "label_app"},
    "k8snodesample": {"nodeName": "node", "clusterName": "cluster",
                      "displayName": "node", "entityName": "node"},
    "k8sdeploymentsample": {"deploymentName": "deployment",
                            "namespaceName": "namespace",
                            "clusterName": "cluster",
                            "displayName": "deployment",
                            "entityName": "deployment"},
    "k8sdaemonsetsample": {"daemonsetName": "daemonset",
                           "namespaceName": "namespace",
                           "clusterName": "cluster",
                           "displayName": "daemonset",
                           "entityName": "daemonset"},
    "k8sstatefulsetsample": {"statefulsetName": "statefulset",
                             "namespaceName": "namespace",
                             "clusterName": "cluster",
                             "displayName": "statefulset",
                             "entityName": "statefulset"},
    "k8sreplicasetsample": {"replicasetName": "replicaset",
                            "namespaceName": "namespace",
                            "clusterName": "cluster",
                            "displayName": "replicaset",
                            "entityName": "replicaset",
                            "deploymentName": "deployment"},
    "k8shpasample": {"hpaName": "horizontalpodautoscaler",
                     "displayName": "horizontalpodautoscaler",
                     "entityName": "horizontalpodautoscaler",
                     "namespaceName": "namespace", "clusterName": "cluster"},
    "k8snamespacesample": {"namespaceName": "namespace",
                           "displayName": "namespace",
                           "entityName": "namespace",
                           "clusterName": "cluster", "status": "phase"},
    "k8sclustersample": {"clusterName": "cluster", "displayName": "cluster",
                         "entityName": "cluster"},
    "k8svolumesample": {"pvcName": "persistentvolumeclaim",
                        "volumeName": "persistentvolumeclaim",
                        "pvcNamespace": "namespace",
                        "namespaceName": "namespace", "podName": "pod",
                        "clusterName": "cluster"},
    "k8sservicesample": {"serviceName": "service",
                         "namespaceName": "namespace",
                         "clusterName": "cluster", "displayName": "service"},
}

# Pod-phase style attributes: WHERE status = 'X' / FACET status on these
# events select a labelled boolean metric instead of a label matcher.
PHASE_ATTRS: Dict[str, Tuple[str, str, List[str]]] = {
    # event -> (attribute names, metric, label)
    "k8spodsample": ("kube_pod_status_phase", "phase", ["status", "phase"]),
    "k8snamespacesample": ("kube_namespace_status_phase", "phase",
                           ["status", "phase"]),
    "k8scontainersample": ("kube_pod_container_status_%s", "",
                           ["status", "state"]),
}

# Attributes on infra events that are filters with no label equivalent
# because the exporter only ever reports one state.
IMPLICIT_FILTERS: Dict[Tuple[str, str], str] = {
    ("containersample", "state"): "cAdvisor only reports running containers",
    ("containersample", "status"): "cAdvisor only reports running containers",
    ("k8spodsample", "status"): "",  # handled via PHASE_ATTRS
}


def infra_lookup(event: str, attr: str) -> Optional[Spec]:
    """Spec for (event type, attribute) or None."""
    key = (event.lower(), _norm_attr(attr))
    spec = INFRA.get(key)
    if spec is not None:
        return spec
    return INFRA.get((event.lower(), "*"))


def infra_count_spec(event: str) -> Optional[Spec]:
    return INFRA.get((event.lower(), "__count__"))


def is_infra_event(event: str) -> bool:
    e = event.lower()
    return any(k[0] == e for k in INFRA)


# ---------------------------------------------------------------------------
# FROM Metric names
# ---------------------------------------------------------------------------

# OTel semantic-convention metrics as New Relic ingests them (dotted).
# Prometheus names follow the OTel collector's prometheus exporters with
# default suffixing (unit + _total / _ratio).
_OTEL_NOTE = ("OTel semantic-convention metric; the Prometheus name assumes "
              "the collector's default unit/_total suffixing")
METRICS: Dict[str, Spec] = {
    "http.server.request.duration": _h("http_server_request_duration_seconds",
                                       "s", _OTEL_NOTE, APPROXIMATE),
    "http.server.duration": _h("http_server_duration_milliseconds", "ms",
                               _OTEL_NOTE, APPROXIMATE),
    "http.client.request.duration": _h("http_client_request_duration_seconds",
                                       "s", _OTEL_NOTE, APPROXIMATE),
    "http.client.duration": _h("http_client_duration_milliseconds", "ms",
                               _OTEL_NOTE, APPROXIMATE),
    "http.server.request.body.size": _h("http_server_request_body_size_bytes",
                                        "bytes", _OTEL_NOTE),
    "http.server.response.body.size": _h("http_server_response_body_size_bytes",
                                         "bytes", _OTEL_NOTE),
    "http.server.active_requests": _g("http_server_active_requests", "short",
                                      note=_OTEL_NOTE, conf=APPROXIMATE),
    "rpc.server.duration": _h("rpc_server_duration_milliseconds", "ms",
                              _OTEL_NOTE, APPROXIMATE),
    "rpc.client.duration": _h("rpc_client_duration_milliseconds", "ms",
                              _OTEL_NOTE, APPROXIMATE),
    "db.client.operation.duration": _h("db_client_operation_duration_seconds",
                                       "s", _OTEL_NOTE, APPROXIMATE),
    "db.client.connections.usage": _g("db_client_connections_usage", "short",
                                      note=_OTEL_NOTE),
    "db.client.connection.count": _g("db_client_connection_count", "short",
                                     note=_OTEL_NOTE),
    "messaging.publish.duration": _h("messaging_publish_duration_seconds", "s",
                                     _OTEL_NOTE),
    "messaging.receive.duration": _h("messaging_receive_duration_seconds", "s",
                                     _OTEL_NOTE),
    "messaging.process.duration": _h("messaging_process_duration_seconds", "s",
                                     _OTEL_NOTE),
    "messaging.client.operation.duration": _h(
        "messaging_client_operation_duration_seconds", "s", _OTEL_NOTE),
    "system.cpu.utilization": _g("system_cpu_utilization_ratio", "percentunit",
                                 note="hostmetrics receiver; " + _OTEL_NOTE),
    "system.cpu.time": _c("system_cpu_time_seconds_total", "s",
                          note="hostmetrics receiver; " + _OTEL_NOTE),
    "system.cpu.load_average.1m": _g("system_cpu_load_average_1m_ratio",
                                     "short", note=_OTEL_NOTE),
    "system.cpu.load_average.5m": _g("system_cpu_load_average_5m_ratio",
                                     "short", note=_OTEL_NOTE),
    "system.cpu.load_average.15m": _g("system_cpu_load_average_15m_ratio",
                                      "short", note=_OTEL_NOTE),
    "system.memory.utilization": _g("system_memory_utilization_ratio",
                                    "percentunit", note=_OTEL_NOTE),
    "system.memory.usage": _g("system_memory_usage_bytes", "bytes",
                              note=_OTEL_NOTE),
    "system.filesystem.utilization": _g("system_filesystem_utilization_ratio",
                                        "percentunit", note=_OTEL_NOTE),
    "system.filesystem.usage": _g("system_filesystem_usage_bytes", "bytes",
                                  note=_OTEL_NOTE),
    "system.network.io": _c("system_network_io_bytes_total", "bytes",
                            note=_OTEL_NOTE),
    "system.network.packets": _c("system_network_packets_total", "short",
                                 note=_OTEL_NOTE),
    "system.network.errors": _c("system_network_errors_total", "short",
                                note=_OTEL_NOTE),
    "system.disk.io": _c("system_disk_io_bytes_total", "bytes", note=_OTEL_NOTE),
    "system.disk.operations": _c("system_disk_operations_total", "short",
                                 note=_OTEL_NOTE),
    "process.cpu.utilization": _g("process_cpu_utilization_ratio",
                                  "percentunit", note=_OTEL_NOTE),
    "process.cpu.time": _c("process_cpu_time_seconds_total", "s",
                           note=_OTEL_NOTE),
    "process.memory.usage": _g("process_memory_usage_bytes", "bytes",
                               note=_OTEL_NOTE),
    "process.memory.virtual": _g("process_memory_virtual_bytes", "bytes",
                                 note=_OTEL_NOTE),
    "process.runtime.jvm.memory.usage": _g("process_runtime_jvm_memory_usage_bytes",
                                           "bytes", note=_OTEL_NOTE),
    "process.runtime.jvm.gc.duration": _h("process_runtime_jvm_gc_duration_seconds",
                                          "s", _OTEL_NOTE),
    "jvm.memory.used": _g("jvm_memory_used_bytes", "bytes", note=_OTEL_NOTE),
    "jvm.memory.committed": _g("jvm_memory_committed_bytes", "bytes",
                               note=_OTEL_NOTE),
    "jvm.memory.limit": _g("jvm_memory_limit_bytes", "bytes", note=_OTEL_NOTE),
    "jvm.gc.duration": _h("jvm_gc_duration_seconds", "s", _OTEL_NOTE),
    "jvm.thread.count": _g("jvm_thread_count", "short", note=_OTEL_NOTE),
    "jvm.cpu.recent_utilization": _g("jvm_cpu_recent_utilization_ratio",
                                     "percentunit", note=_OTEL_NOTE),
    "jvm.cpu.time": _c("jvm_cpu_time_seconds_total", "s", note=_OTEL_NOTE),
    "jvm.class.count": _g("jvm_class_count", "short", note=_OTEL_NOTE),
    "kafka.consumer.records-lag-max": _g("kafka_consumer_records_lag_max",
                                         "short", note=_OTEL_NOTE),
    "kafka.consumer.fetch-rate": _g("kafka_consumer_fetch_rate", "short",
                                    note=_OTEL_NOTE),
    "kafka.consumer.records-consumed-rate": _g(
        "kafka_consumer_records_consumed_rate", "short", note=_OTEL_NOTE),
    "kafka.producer.record-send-rate": _g("kafka_producer_record_send_rate",
                                          "short", note=_OTEL_NOTE),
}

_APM_NOTE = ("New Relic APM agent metric mapped onto OTel instrumentation "
             "of the same service; requires the service to emit OTel metrics")
_GOLD_NOTE = ("New Relic golden metric rebuilt from OTel HTTP server "
              "metrics")

# NR agent-produced APM metrics (the ones "Add to dashboard" emits).
METRICS.update({
    "apm.service.transaction.duration": Spec(
        "http", note=_APM_NOTE, conf=APPROXIMATE),
    "apm.service.transaction.apdex": Spec(
        "http", note=_APM_NOTE, conf=APPROXIMATE),
    "apm.service.apdex": Spec("http", note=_APM_NOTE, conf=APPROXIMATE),
    "apm.service.error.count": Spec(
        "http-errors", unit="short",
        note=_APM_NOTE + "; errors approximated as HTTP 5xx responses",
        conf=NEEDS_REVIEW),
    "apm.service.transaction.error.count": Spec(
        "http-errors", unit="short",
        note=_APM_NOTE + "; errors approximated as HTTP 5xx responses",
        conf=NEEDS_REVIEW),
    "apm.service.overview.web": _no(
        "apm.service.overview.web is the per-segment (WebTransaction "
        "breakdown by tier: application/database/external/...) time "
        "split; OTel metrics have no such breakdown — use "
        "http_server_request_duration_seconds for total time and "
        "db_client_operation_duration_seconds / "
        "http_client_request_duration_seconds for the tiers"),
    "apm.service.overview.other": _no(
        "apm.service.overview.other (non-web transaction segment breakdown) "
        "has no OTel metric equivalent"),
    "apm.service.datastore.operation.duration": _h(
        "db_client_operation_duration_seconds", "s",
        _APM_NOTE + " (OTel db.client.operation.duration)", NEEDS_REVIEW),
    "apm.service.external.host.duration": _h(
        "http_client_request_duration_seconds", "s",
        _APM_NOTE + " (OTel http.client.request.duration)", NEEDS_REVIEW),
    "apm.service.cpu.usertime.utilization": _e(
        "<AGG> <BY>(rate(process_cpu_time_seconds_total{state=\"user\"<SEL>}"
        "[<W>]))", "percentunit",
        _APM_NOTE + " (OTel process.cpu.time, user state)", NEEDS_REVIEW),
    "apm.service.cpu.systemtime.utilization": _e(
        "<AGG> <BY>(rate(process_cpu_time_seconds_total{state=\"system\"<SEL>}"
        "[<W>]))", "percentunit", _APM_NOTE, NEEDS_REVIEW),
    "apm.service.memory.physical": _g(
        "process_memory_usage_bytes", "bytes",
        note=_APM_NOTE + " (OTel process.memory.usage, bytes — NR reported "
                         "MB)", conf=NEEDS_REVIEW),
    "apm.service.memory.heap.used": _g(
        "jvm_memory_used_bytes", "bytes", [("jvm_memory_type", "=", "heap")],
        _APM_NOTE + " (OTel jvm.memory.used)", NEEDS_REVIEW),
    "apm.service.memory.heap.max": _g(
        "jvm_memory_limit_bytes", "bytes", [("jvm_memory_type", "=", "heap")],
        _APM_NOTE, NEEDS_REVIEW),
    "apm.service.instance.count": _e(
        "<AGG> <BY>(count by (service_name)(count by (service_name, "
        "service_instance_id)(target_info{<SELBARE>})))", "short",
        _APM_NOTE + " (distinct service.instance.id on target_info)",
        NEEDS_REVIEW),
    "apm.service.thread.count": _g("jvm_thread_count", "short",
                                   note=_APM_NOTE, conf=NEEDS_REVIEW),
    "apm.service.gc.time": _e(
        "<AGG> <BY>(rate(jvm_gc_duration_seconds_sum{<SELBARE>}[<W>]))", "s",
        _APM_NOTE + " (OTel jvm.gc.duration)", NEEDS_REVIEW),
    "newrelic.goldenmetrics.apm.application.throughput": _e(
        "sum <BY>(rate(<HTTP>_count{<SELBARE>}[<W>])) * 60", "short",
        _GOLD_NOTE + " (requests per minute)", APPROXIMATE),
    "newrelic.goldenmetrics.apm.application.responseTimeMs": _e(
        "1000 * (sum <BY>(rate(<HTTP>_sum{<SELBARE>}[<W>])) / sum <BY>("
        "rate(<HTTP>_count{<SELBARE>}[<W>])))", "ms", _GOLD_NOTE, APPROXIMATE),
    "newrelic.goldenmetrics.apm.application.errorRate": _e(
        "100 * (sum <BY>(rate(<HTTP>_count{http_response_status_code=~"
        "\"5..\"<SEL>}[<W>])) / sum <BY>(rate(<HTTP>_count{<SELBARE>}[<W>])))",
        "percent", _GOLD_NOTE + "; errors approximated as HTTP 5xx",
        NEEDS_REVIEW),
    "newrelic.goldenmetrics.apm.application.nonWebThroughput": _no(
        "non-web (background) transaction throughput has no HTTP-server "
        "metric equivalent; use the messaging.* / rpc.* OTel metrics your "
        "workers emit"),
    "newrelic.goldenmetrics.apm.application.nonWebResponseTimeMs": _no(
        "non-web transaction response time has no HTTP-server metric "
        "equivalent; use the messaging.* / rpc.* OTel metrics your workers "
        "emit"),
    "newrelic.goldenmetrics.infra.host.cpuUtilization": INFRA[
        ("systemsample", "cpupercent")],
    "newrelic.goldenmetrics.infra.host.memoryUsage": INFRA[
        ("systemsample", "memoryusedpercent")],
    "newrelic.goldenmetrics.infra.host.diskUtilization": INFRA[
        ("systemsample", "diskusedpercent")],
    "newrelic.goldenmetrics.infra.host.networkTrafficRx": INFRA[
        ("networksample", "receivebytespersecond")],
    "newrelic.goldenmetrics.infra.host.networkTrafficTx": INFRA[
        ("networksample", "transmitbytespersecond")],
    "newrelic.goldenmetrics.infra.host.loadAverageOneMinute": INFRA[
        ("systemsample", "loadaverageoneminute")],
    "newrelic.timeslice.value": _no(
        "newrelic.timeslice.value is the legacy agent timeslice metric "
        "store (selected by metricTimesliceName); map each timeslice name "
        "to a Prometheus metric in metric_map, e.g. "
        "\"Custom/checkout/orders\": {\"name\": \"checkout_orders_total\", "
        "\"type\": \"counter\"}"),
})

# Infra agent dimensional metrics alias the sample-event attributes.
_HOST_EVENT_FOR = {"disk": "storagesample", "net": "networksample",
                   "network": "networksample"}
_K8S_EVENT_FOR = {
    "container": "k8scontainersample", "pod": "k8spodsample",
    "node": "k8snodesample", "deployment": "k8sdeploymentsample",
    "daemonset": "k8sdaemonsetsample", "statefulset": "k8sstatefulsetsample",
    "replicaset": "k8sreplicasetsample", "hpa": "k8shpasample",
    "namespace": "k8snamespacesample", "cluster": "k8sclustersample",
    "volume": "k8svolumesample", "service": "k8sservicesample",
}


def _alias_lookup(name_lower: str) -> Optional[Spec]:
    if name_lower.startswith("host."):
        rest = name_lower[len("host."):]
        parts = rest.split(".", 1)
        if len(parts) == 2 and parts[0] in _HOST_EVENT_FOR:
            spec = infra_lookup(_HOST_EVENT_FOR[parts[0]], parts[1])
            if spec is not None and spec.kind != "none":
                return spec
        return infra_lookup("systemsample", rest.replace(".", ""))
    if name_lower.startswith("k8s."):
        parts = name_lower[len("k8s."):].split(".", 1)
        if len(parts) == 2 and parts[0] in _K8S_EVENT_FOR:
            return infra_lookup(_K8S_EVENT_FOR[parts[0]], parts[1])
    return None


_STAT_FOR_AGG = {"average": "average", "avg": "average", "latest": "average",
                 "sum": "sum", "max": "maximum", "min": "minimum",
                 "count": "sample_count"}


def _yace_snake(metric: str) -> str:
    import re as _re
    s = _re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", metric)
    s = _re.sub(r"[^A-Za-z0-9_]", "_", s)
    return s.lower()


def aws_spec(name: str, agg: str) -> Optional[Spec]:
    """aws.<namespace>.<Metric> -> YACE-style aws_<ns>_<metric>_<stat>."""
    parts = name.split(".")
    if len(parts) < 3 or parts[0].lower() != "aws":
        return None
    ns = parts[1].lower()
    metric_orig = ".".join(parts[2:])
    stat = _STAT_FOR_AGG.get(agg, "average")
    return Spec(
        "gauge",
        name="aws_%s_%s_%s" % (ns, _yace_snake(metric_orig), stat),
        unit="short", conf=NEEDS_REVIEW,
        note="CloudWatch metric via YACE (yet-another-cloudwatch-exporter) "
             "naming aws_<namespace>_<metric>_<statistic>; dimensions are "
             "dimension_<Name> labels. If Grafana queries CloudWatch "
             "directly instead, replace this panel's query with a "
             "CloudWatch datasource query for AWS/%s %s" % (ns.upper(),
                                                            metric_orig))


def metric_spec(name: str, agg: str = "") -> Optional[Spec]:
    """Built-in spec for a FROM Metric name (exact, case-insensitive)."""
    low = name.lower()
    spec = METRICS.get(low)
    if spec is not None:
        return spec
    for k, v in METRICS.items():
        if k.lower() == low:
            return v
    spec = _alias_lookup(low)
    if spec is not None:
        return spec
    if low.startswith("aws."):
        return aws_spec(name, agg)
    return None


def aws_attr_label(name: str) -> Optional[str]:
    """aws.ec2.InstanceId -> dimension_InstanceId; aws.accountId ->
    account_id; aws.region -> region (YACE label conventions)."""
    if not name.lower().startswith("aws."):
        return None
    low = name.lower()
    if low in ("aws.accountid", "aws.account.id"):
        return "account_id"
    if low in ("aws.region", "aws.awsregion"):
        return "region"
    parts = name.split(".")
    if len(parts) >= 3:
        return "dimension_" + parts[-1]
    return None


# Transaction event attributes -> which OTel metric carries them.
TRANSACTION_ATTRS: Dict[str, Tuple[str, str]] = {
    # attr (lower) -> (source kind, note)
    "duration": ("http", ""),
    "totaltime": ("http", "totalTime approximated by request duration"),
    "webduration": ("http", ""),
    "queueduration": ("none", "request queue time is not instrumented by "
                              "OTel HTTP server metrics"),
    "databaseduration": ("db", "databaseDuration approximated by the OTel "
                               "db.client.operation.duration histogram "
                               "(per DB call, not per transaction)"),
    "databasecallcount": ("db-count", "databaseCallCount approximated by "
                                      "the count of db.client.operation "
                                      "calls"),
    "externalduration": ("ext", "externalDuration approximated by the OTel "
                                "http.client.request.duration histogram "
                                "(per outbound call, not per transaction)"),
    "externalcallcount": ("ext-count", "externalCallCount approximated by "
                                       "the count of http.client calls"),
    "gccumulative": ("none", "GC time per transaction is not an OTel HTTP "
                             "metric; use jvm.gc.duration"),
    "apdexperfzone": ("none", "apdexPerfZone is an NR-computed bucket; "
                              "use apdex() on the duration histogram"),
    "timestamp": ("timestamp", ""),
}


# ---------------------------------------------------------------------------
# Legacy AWS integration sample events (ComputeSample, DatastoreSample, ...)
# ---------------------------------------------------------------------------
#
# New Relic's API-polling AWS integrations write one sample event per
# resource, with attributes named provider.<CloudWatchMetric>.<Statistic>
# and a `provider` attribute naming the resource type. YACE exports the same
# CloudWatch metrics as aws_<namespace>_<metric>_<statistic> with
# dimension_<Name> labels, plus one aws_<namespace>_info series per
# discovered resource (tags as tag_<Key> labels).

LEGACY_AWS_EVENTS: Dict[str, Dict[str, str]] = {
    "computesample": {
        "ec2instance": "ec2", "lambdafunction": "lambda",
        "ecscluster": "ecs", "ecsservice": "ecs",
        "ebinstance": "elasticbeanstalk",
        "elasticbeanstalkenvironment": "elasticbeanstalk",
        "emrcluster": "elasticmapreduce", "autoscalinggroup": "autoscaling",
    },
    "datastoresample": {
        "rdsdbinstance": "rds", "rdsdbcluster": "rds",
        "dynamodbtable": "dynamodb", "dynamodbregion": "dynamodb",
        "dynamodbglobalsecondaryindex": "dynamodb",
        "elasticacheredisnode": "elasticache",
        "elasticacherediscluster": "elasticache",
        "elasticachememcachednode": "elasticache",
        "elasticachememcachedcluster": "elasticache",
        "redshiftcluster": "redshift", "redshiftnode": "redshift",
        "elasticsearchcluster": "es", "elasticsearchnode": "es",
        "documentdbcluster": "docdb", "documentdbinstance": "docdb",
        "neptuneinstance": "neptune", "neptunecluster": "neptune",
        "efsfilesystem": "efs", "s3bucket": "s3",
    },
    "queuesample": {"sqsqueue": "sqs"},
    "loadbalancersample": {
        "elb": "elb", "alb": "applicationelb",
        "albtargetgroup": "applicationelb", "nlb": "networkelb",
        "nlbtargetgroup": "networkelb",
    },
    "blockdevicesample": {"ebsvolume": "ebs"},
    "serverlesssample": {"lambdafunction": "lambda",
                         "lambdafunctionalias": "lambda",
                         "lambdaregion": "lambda"},
    "streamsample": {"kinesisstream": "kinesis",
                     "kinesisstreamshard": "kinesis",
                     "kinesisdeliverystream": "firehose"},
    "cdnsample": {"cloudfrontdistribution": "cloudfront"},
    "dnssample": {"route53healthcheck": "route53",
                  "route53hostedzone": "route53"},
    "apigatewaysample": {"apigatewayapi": "apigateway",
                         "apigatewaystage": "apigateway",
                         "apigatewayresourcewithmetrics": "apigateway"},
}

# Sample attributes -> YACE labels (dimensions, account, region, identity).
LEGACY_AWS_LABELS: Dict[str, str] = {
    "provider": "provider",  # consumed by the translator, never a label
    "awsRegion": "region", "providerAccountId": "account_id",
    "ec2InstanceId": "dimension_InstanceId",
    "instanceId": "dimension_InstanceId",
    "dbInstanceIdentifier": "dimension_DBInstanceIdentifier",
    "dbClusterIdentifier": "dimension_DBClusterIdentifier",
    "queueName": "dimension_QueueName",
    "functionName": "dimension_FunctionName",
    "loadBalancerName": "dimension_LoadBalancerName",
    "tableName": "dimension_TableName",
    "cacheClusterId": "dimension_CacheClusterId",
    "cacheNodeId": "dimension_CacheNodeId",
    "volumeId": "dimension_VolumeId",
    "clusterIdentifier": "dimension_ClusterIdentifier",
    "streamName": "dimension_StreamName",
    "autoScalingGroupName": "dimension_AutoScalingGroupName",
    "distributionId": "dimension_DistributionId",
    "apiName": "dimension_ApiName", "stage": "dimension_Stage",
    "fileSystemId": "dimension_FileSystemId",
    "bucketName": "dimension_BucketName",
    "domainName": "dimension_DomainName",
    "entityName": "name", "displayName": "name",
}

for _event in LEGACY_AWS_EVENTS:
    EVENT_LABELS.setdefault(_event, {}).update(LEGACY_AWS_LABELS)

_AWS_STATS = {"average": "average", "sum": "sum", "maximum": "maximum",
              "minimum": "minimum", "samplecount": "sample_count"}


def _aws_unit(metric: str) -> str:
    low = metric.lower()
    if "bytes" in low:
        return "bytes"
    if "utilization" in low or "percent" in low:
        return "percent"
    if "latency" in low or "responsetime" in low:
        return "s"
    if "duration" in low:
        return "ms"
    return "short"


def legacy_aws_providers(event: str) -> List[str]:
    return sorted(LEGACY_AWS_EVENTS.get(event.lower(), {}))


def legacy_aws_namespace(event: str, provider: str) -> Optional[str]:
    return LEGACY_AWS_EVENTS.get(event.lower(), {}).get(
        (provider or "").lower())


def legacy_aws_spec(event: str, provider: str, attr: str,
                    agg: str) -> Optional[Spec]:
    """provider.<Metric>.<Statistic> on a legacy AWS sample event ->
    aws_<namespace>_<metric>_<statistic> under YACE naming."""
    ns = legacy_aws_namespace(event, provider)
    parts = attr.split(".")
    if ns is None or len(parts) < 2 or parts[0].lower() != "provider":
        return None
    if len(parts) >= 3 and parts[-1].lower() in _AWS_STATS:
        stat = _AWS_STATS[parts[-1].lower()]
        metric = ".".join(parts[1:-1])
    else:
        stat = _STAT_FOR_AGG.get(agg, "average")
        metric = ".".join(parts[1:])
    name = "aws_%s_%s_%s" % (ns, _yace_snake(metric), stat)
    return Spec(
        "gauge", name=name, unit=_aws_unit(metric), conf=NEEDS_REVIEW,
        note="legacy AWS %s (provider %s) mapped to the CloudWatch metric "
             "under YACE naming (aws_<namespace>_<metric>_<statistic>, "
             "dimension_<Name> labels). New Relic camel-cases the "
             "CloudWatch name, so verify %s against your metric names "
             "(YACE keeps acronyms together: CPUUtilization is "
             "aws_%s_cpuutilization_average) and pin it with metric_map "
             "(key \"%s.%s\") if it differs"
             % (event, provider, name, ns, event, attr))
