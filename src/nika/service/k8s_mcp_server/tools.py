"""MCP tool registrations for the in-node Kubernetes MCP server."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from nika.service.k8s_mcp_server._safe import safe_tool
from nika.service.k8s_mcp_server.client import get_client


def _json(payload: Any) -> str:
    return json.dumps(payload, default=str, indent=2)


def register_tools(mcp: FastMCP) -> None:
    @safe_tool
    @mcp.tool()
    def k8s_list_nodes() -> str:
        """List Kubernetes nodes with Ready status, InternalIP, and conditions."""
        return _json(get_client().list_nodes())

    @safe_tool
    @mcp.tool()
    def k8s_get_node(name: str) -> str:
        """Get a full Kubernetes Node object by name.

        Args:
            name: Node name (e.g. controller, worker1).
        """
        return _json(get_client().get_node(name))

    @safe_tool
    @mcp.tool()
    def k8s_list_pods(
        namespace: str = "",
        selector: str = "",
        field_selector: str = "",
        all_namespaces: bool = False,
    ) -> str:
        """List pods with phase, node, restarts, and pod IP.

        Args:
            namespace: Namespace to query (ignored when all_namespaces is true).
            selector: Label selector (e.g. app=word).
            field_selector: Field selector (e.g. spec.nodeName=worker1).
            all_namespaces: If true, list across all namespaces.
        """
        return _json(
            get_client().list_pods(
                namespace=namespace or None,
                selector=selector or None,
                field_selector=field_selector or None,
                all_namespaces=all_namespaces,
            )
        )

    @safe_tool
    @mcp.tool()
    def k8s_get_pod(name: str, namespace: str = "default") -> str:
        """Get a full Pod object.

        Args:
            name: Pod name.
            namespace: Pod namespace.
        """
        return _json(get_client().get_pod(name, namespace=namespace))

    @safe_tool
    @mcp.tool()
    def k8s_get_logs(
        name: str,
        namespace: str = "default",
        container: str = "",
        tail_lines: int = 200,
        since_seconds: int = 0,
        timeout_seconds: int = 30,
    ) -> str:
        """Fetch container logs for a pod.

        Args:
            name: Pod name.
            namespace: Pod namespace.
            container: Optional container name.
            tail_lines: Number of trailing log lines.
            since_seconds: Only logs newer than this many seconds (0 = all).
            timeout_seconds: API request timeout.
        """
        return get_client().get_logs(
            name,
            namespace=namespace,
            container=container or None,
            tail_lines=tail_lines,
            since_seconds=since_seconds or None,
            timeout_seconds=timeout_seconds,
        )

    @safe_tool
    @mcp.tool()
    def k8s_list_events(
        namespace: str = "",
        field_selector: str = "",
        all_namespaces: bool = False,
        limit: int = 100,
    ) -> str:
        """List Kubernetes events.

        Args:
            namespace: Namespace (ignored when all_namespaces is true).
            field_selector: Event field selector.
            all_namespaces: List cluster-wide events.
            limit: Max events to return.
        """
        return _json(
            get_client().list_events(
                namespace=namespace or None,
                field_selector=field_selector or None,
                all_namespaces=all_namespaces,
                limit=limit,
            )
        )

    @safe_tool
    @mcp.tool()
    def k8s_list_services(
        namespace: str = "",
        all_namespaces: bool = False,
    ) -> str:
        """List Services with ClusterIP, selectors, and ports.

        Args:
            namespace: Namespace (ignored when all_namespaces is true).
            all_namespaces: List across all namespaces.
        """
        return _json(
            get_client().list_services(
                namespace=namespace or None,
                all_namespaces=all_namespaces,
            )
        )

    @safe_tool
    @mcp.tool()
    def k8s_get_endpoints(service: str, namespace: str = "default") -> str:
        """Get EndpointSlice addresses and ClusterIP for a Service.

        Args:
            service: Service name.
            namespace: Service namespace.
        """
        return _json(get_client().get_endpoints(service, namespace=namespace))

    @safe_tool
    @mcp.tool()
    def k8s_get_network_policies(
        namespace: str = "",
        name: str = "",
        all_namespaces: bool = False,
    ) -> str:
        """List or get NetworkPolicy objects.

        Args:
            namespace: Namespace (ignored when all_namespaces is true).
            name: Optional policy name for a full get.
            all_namespaces: List cluster-wide policies.
        """
        return _json(
            get_client().get_network_policies(
                namespace=namespace or None,
                name=name or None,
                all_namespaces=all_namespaces,
            )
        )

    @safe_tool
    @mcp.tool()
    def k8s_dns_query(
        pod: str,
        query: str,
        namespace: str = "default",
        server: str = "",
        timeout_seconds: int = 20,
    ) -> str:
        """Run a DNS lookup from inside an existing pod.

        Args:
            pod: Source pod name.
            query: DNS name to resolve.
            namespace: Pod namespace.
            server: Optional DNS server IP (defaults to cluster DNS).
            timeout_seconds: Exec timeout.
        """
        return _json(
            get_client().dns_query(
                pod,
                query,
                namespace=namespace,
                server=server or None,
                timeout_seconds=timeout_seconds,
            )
        )

    @safe_tool
    @mcp.tool()
    def k8s_check_connectivity(
        pod: str,
        target: str,
        namespace: str = "default",
        port: int = 0,
        protocol: str = "tcp",
        timeout_seconds: int = 15,
    ) -> str:
        """Probe TCP or HTTP connectivity from a pod to a target.

        Args:
            pod: Source pod name.
            target: Destination host, ClusterIP, pod IP, or URL.
            namespace: Source pod namespace.
            port: Destination port (required for tcp).
            protocol: ``tcp`` or ``http``.
            timeout_seconds: Probe timeout.
        """
        return _json(
            get_client().check_connectivity(
                pod,
                target,
                namespace=namespace,
                port=port or None,
                protocol=protocol,
                timeout_seconds=timeout_seconds,
            )
        )
