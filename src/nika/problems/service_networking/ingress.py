"""Ingress controller rate-policy failures."""

from __future__ import annotations

from urllib.parse import urlsplit

from nika.problems.base import FailureDomain
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import k8s_control_node
from nika.problems.support.kubernetes.base import K8sParams, K8sProblemBase
from nika.problems.support.kubernetes.objects import (
    LAST_APPLIED,
    edit_spec,
    restore_spec,
)

RATE_ANNOTATION = "nginx.ingress.kubernetes.io/limit-rpm"


def workload_routes(net_env):
    """Declared API-client routes and conservative per-path request rates."""
    routes = {}
    for process in net_env.business_workload().processes:
        if process.app != "api_client" or "--body" in process.args:
            continue
        args = process.args
        workers = int(args[args.index("--workers") + 1]) if "--workers" in args else 1
        every = args[args.index("--every") + 1] if "--every" in args else "2-6"
        interval = max(float(value) for value in every.split("-"))
        for url in args:
            if url.startswith("http://"):
                parsed = urlsplit(url)
                routes[(parsed.netloc, parsed.path)] = (
                    process.node,
                    url,
                    60 * workers / interval,
                )
    return routes


class IngressRateLimitMisconfig(K8sProblemBase):
    """An ingress-nginx request-rate annotation undersizes application demand.

    The default burst multiplier is five; requests beyond the per-client
    bucket return 503. Minute limits reproduce this for low-rate workloads.
    Sources: ingress-nginx, Rate Limiting annotations,
    https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/#rate-limiting
    and NGINX's leaky-bucket request limiter,
    https://nginx.org/en/docs/http/ngx_http_limit_req_module.html
    """

    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name = "k8s_ingress_rate_limit_misconfig"
    description = "A Kubernetes Ingress request-rate limit is below application demand."
    symptom_desc = "Application requests intermittently return HTTP 503."
    symptom_probe = "custom"
    symptom_class = "degradation"
    TAGS = ["kubernetes", "k3s", "ingress"]
    COMPATIBLE_COLUMNS = frozenset({"k8s_lab"})
    INCOMPATIBLE_SCENARIOS = {
        "llmd_lab": "Business traffic uses AgentGateway, not ingress-nginx."
    }
    Params = K8sParams
    BENCHMARK_TARGETS = "canonical"
    RECORDED_ATTRS = (*K8sProblemBase.RECORDED_ATTRS, "ingress_ref", "limit_rpm")

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"control_node": k8s_control_node(ctx)}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        control = inject.get("control_node")
        if control and control not in ctx.devices:
            raise ValueError(f"Unknown Kubernetes control node {control!r}.")
        if not workload_routes(ctx.net_env):
            raise ValueError("No declared HTTP API workload traverses an Ingress.")

    def select_target(self):
        k8s = self.runtime.lab_api
        control = self.control_node()
        classes = {
            obj["metadata"]["name"]
            for obj in k8s.kubectl_json(control, "get ingressclass").get("items", [])
            if obj["spec"].get("controller") == "k8s.io/ingress-nginx"
        }
        routes = workload_routes(self.net_env)
        items = k8s.kubectl_json(control, "get ingress -A").get("items", [])
        for obj in sorted(
            items,
            key=lambda item: (item["metadata"]["namespace"], item["metadata"]["name"]),
        ):
            if obj["spec"].get("ingressClassName") not in classes:
                continue
            for rule in obj["spec"].get("rules", []):
                for path in rule.get("http", {}).get("paths", []):
                    route = routes.get((rule.get("host", ""), path.get("path", "/")))
                    if route:
                        obj["kind"] = "Ingress"
                        return obj, route
        raise ValueError("No ingress-nginx route carries the declared HTTP workload.")

    def root_cause_resources(self, params):
        return [node_resource(self.control_node(params))]

    def inject_fault(self, params):
        self.control_node(params)
        obj, (_, _, requests_per_minute) = self.select_target()
        annotations = obj["metadata"].get("annotations") or {}
        if any(
            key.startswith("nginx.ingress.kubernetes.io/limit-") for key in annotations
        ):
            raise ValueError("The selected Ingress already has a rate policy.")
        self._original = obj
        self.ingress_ref = f"{obj['metadata']['namespace']}/{obj['metadata']['name']}"
        # Four times below the declared rate at the longest inter-round interval.
        self.limit_rpm = max(1, int(requests_per_minute / 4))
        edit_spec(
            self.runtime.lab_api,
            self.control_node(params),
            obj,
            lambda spec: None,
            annotations={RATE_ANNOTATION: str(self.limit_rpm)},
        )
        self.record_k8s_object(
            "ingress", obj["metadata"]["name"], namespace=obj["metadata"]["namespace"]
        )

    def verify_fault(self, params):
        import json

        def check():
            ns, name = self.ingress_ref.split("/", 1)
            obj = self.runtime.lab_api.kubectl_json(
                self.control_node(params), f"get ingress {name} -n {ns}"
            )
            annotations = obj["metadata"].get("annotations") or {}
            applied = annotations.get(LAST_APPLIED)
            consistent = not applied or json.loads(applied)["metadata"][
                "annotations"
            ].get(RATE_ANNOTATION) == str(self.limit_rpm)
            return annotations.get(RATE_ANNOTATION) == str(
                self.limit_rpm
            ) and consistent, {
                "ingress": self.ingress_ref,
                "limit_rpm": self.limit_rpm,
                "last_applied_consistent": consistent,
            }

        return self.poll_verify(check)

    def recover_fault(self, params):
        from nika.validation.symptom.ingress_probes import ingress_rate_baseline

        original = getattr(self, "_original", None)
        if original is None:
            return {"verified": False, "details": {"error": "No saved Ingress."}}
        restore_spec(
            self.runtime.lab_api,
            self.control_node(params),
            original,
            annotation_keys=(RATE_ANNOTATION,),
        )
        ok, details = ingress_rate_baseline(self, params)
        ns, name = self.ingress_ref.split("/", 1)
        restored = (
            self.runtime.lab_api.kubectl_json(
                self.control_node(params), f"get ingress {name} -n {ns}"
            )["metadata"].get("annotations")
            or {}
        )
        original_annotations = original["metadata"].get("annotations") or {}
        annotations_restored = RATE_ANNOTATION not in restored and restored.get(
            LAST_APPLIED
        ) == original_annotations.get(LAST_APPLIED)
        return {
            "verified": ok and annotations_restored,
            "details": {**details, "annotations_restored": annotations_restored},
        }
