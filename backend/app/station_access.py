"""Вход складской станции без пароля: только с адресов сети склада."""

from __future__ import annotations

import ipaddress
import logging

STATION_USERNAME = "warehouse-station"
STATION_NETWORK_DENIED_CODE = "station_network_denied"
STATION_LOGIN_MAX_PER_MINUTE = 10

_FORBIDDEN_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)


def parse_warehouse_cidrs(values) -> tuple:
    """Разбирает список сетей склада; любое сомнительное значение выключает станцию целиком."""
    networks = []
    for raw in values or ():
        try:
            network = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            logging.error("TAKSKLAD_WAREHOUSE_CIDRS содержит битое значение, станция выключена")
            return ()
        if network.prefixlen == 0 or any(network.overlaps(blocked) for blocked in _FORBIDDEN_NETWORKS):
            logging.error("TAKSKLAD_WAREHOUSE_CIDRS содержит общую или частную сеть, станция выключена")
            return ()
        networks.append(network)
    return tuple(networks)


def is_warehouse_address(address: str, networks) -> bool:
    try:
        parsed = ipaddress.ip_address(str(address or "").strip())
    except ValueError:
        return False
    return any(parsed.version == network.version and parsed in network for network in networks)
