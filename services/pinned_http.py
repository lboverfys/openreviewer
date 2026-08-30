"""把 HTTPS 主机解析结果固定到实际 TCP 连接，阻断 DNS 重绑定。"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable
from ipaddress import ip_address
from typing import Any

import httpcore
import httpx

Resolver = Callable[..., list[tuple[Any, ...]]]


def _public_addresses(
    resolver: Resolver,
    host: str,
    port: int,
) -> tuple[str, ...]:
    """解析并校验所有答案；混入一个非公网地址时整体拒绝。"""

    try:
        records = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise httpcore.ConnectError("public DNS resolution failed") from exc

    addresses: list[str] = []
    seen: set[str] = set()
    for record in records:
        if len(record) < 5:
            continue
        socket_address = record[4]
        if (
            not isinstance(socket_address, tuple)
            or not socket_address
            or not isinstance(socket_address[0], str)
        ):
            continue
        raw_address = socket_address[0].split("%", 1)[0]
        try:
            address = ip_address(raw_address)
        except ValueError as exc:
            raise httpcore.ConnectError("DNS returned an invalid address") from exc
        if not address.is_global:
            raise httpcore.ConnectError("DNS resolved to a non-public address")
        canonical = address.compressed
        if canonical not in seen:
            seen.add(canonical)
            addresses.append(canonical)
    if not addresses:
        raise httpcore.ConnectError("DNS returned no public address")
    return tuple(addresses)


class PublicDnsPinnedNetworkBackend(httpcore.NetworkBackend):
    """每次建立新连接时解析一次，并用 IP 字面量连接。"""

    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver = resolver or socket.getaddrinfo
        self._delegate = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        # 关键点是把解析出的字面量传给 socket；不能再把 hostname 交回
        # socket.create_connection，否则校验和实际连接之间仍存在 TOCTOU 窗口。
        addresses = _public_addresses(self._resolver, host, port)
        last_error: httpcore.NetworkError | None = None
        for address in addresses:
            try:
                return self._delegate.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.NetworkError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("public address could not be connected")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        # 当前模型客户端不启用 UDS；保留委托实现以满足 NetworkBackend 契约。
        return self._delegate.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    def sleep(self, seconds: float) -> None:
        self._delegate.sleep(seconds)


class PublicDnsPinnedHTTPTransport(httpx.HTTPTransport):
    """HTTPX 传输层：TLS 仍使用原 hostname，TCP 固定到已校验 IP。"""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        **kwargs: Any,
    ) -> None:
        # HTTPTransport 的公开构造函数不暴露 network_backend；先让它建立
        # 完整的 SSL/连接池配置，再替换尚未使用的连接池后端。httpx 版本由
        # 项目依赖约束在 0.28.x、httpcore 1.0.x；若底层契约改变就立即失败，
        # 避免安全传输悄悄退回到未固定 DNS 的实现。
        kwargs["trust_env"] = False
        super().__init__(**kwargs)
        pool = self._pool
        if not hasattr(pool, "_network_backend"):
            self.close()
            raise RuntimeError(
                "installed httpx/httpcore versions do not support DNS-pinned transport"
            )
        pool._network_backend = PublicDnsPinnedNetworkBackend(resolver)
