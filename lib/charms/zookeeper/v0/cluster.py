#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import re
from typing import Dict, Iterable, Set, Tuple, Union
from kazoo.handlers.threading import KazooTimeoutError
from ops.charm import CharmBase

from ops.model import (
    ActiveStatus,
    MaintenanceStatus,
    Relation,
    StatusBase,
    Unit,
)

from charms.zookeeper.v0.client import (
    MemberNotReadyError,
    MembersSyncingError,
    QuorumLeaderNotFoundError,
    ZooKeeperManager,
)

logger = logging.getLogger(__name__)

CHARM_KEY = "zookeeper"
PEER = "cluster"


class UnitNotFoundError(Exception):
    """a desired unit isn't yet found in the relation data."""

    pass


class NotUnitTurnError(Exception):
    """a desired unit isn't next in line to start safely."""

    pass


class ZooKeeperCluster:
    """Handler for managing the ZK peer-relation.

    Mainly for managing scale-up/down orchestration
    """

    def __init__(
        self,
        charm: CharmBase,
        client_port: int = 2181,
        server_port: int = 2888,
        election_port: int = 3888,
    ) -> None:
        self.charm = charm
        self.client_port = client_port
        self.server_port = server_port
        self.election_port = election_port
        self.status: StatusBase = MaintenanceStatus("performing cluster operation")

    @property
    def relation(self) -> Relation:
        """Relation property to be used by both the instance and charm.

        Returns:
            The peer relation instance
        """
        return self.charm.model.get_relation(PEER)

    @property
    def peer_units(self) -> Iterable[Unit]:
        """Grabs all units in the current peer relation, including the running unit.

        Returns:
            Iterable of units in the current peer relation, including the running unit
        """
        return set([self.charm.unit] + list(self.relation.units))

    @property
    def started_units(self) -> Set[Unit]:
        """Checks peer relation units for whether they've started the ZK service.

        Such units are ready to join the ZK quorum if they haven't already.

        Returns:
            Set of units with unit data "state" == "started". Shows only those units
                currently found related to the current unit.
        """
        started_units = set()
        for unit in self.peer_units:
            if self.relation.data[unit].get("state", None) == "started":
                started_units.add(unit)

        return started_units

    @staticmethod
    def get_unit_id(unit: Unit) -> int:
        """Grabs the unit's ID as definied by Juju.

        Args:
            unit: The target `Unit`

        Returns:
            The Juju unit ID for the unit.
                e.g `zookeeper/0` -> `0`
        """
        return int(unit.name.split("/")[1])

    def get_unit_from_id(self, unit_id: int) -> Unit:
        """Grabs the corresponding Unit for a given Juju unit ID

        Args:
            unit_id: The target unit id

        Returns:
            The target `Unit`

        Raises:
            UnitNotFoundError: The desired unit could not be found in the peer data
        """
        for unit in self.peer_units:
            if int(unit.name.split("/")[1]) == unit_id:
                return unit

        raise UnitNotFoundError

    def unit_config(
        self, unit: Union[Unit, int], state: str = "ready", role: str = "participant"
    ) -> Dict[str, str]:
        """Builds a collection of data useful for ZK for a given unit.

        Args:
            unit: The target `Unit`, either explicitly or from it's Juju unit ID
            state: The desired output state. "ready" or "started"
            role: The ZK role for the unit. Default = "participant"

        Returns:
            The generated config for the given unit.
                e.g for unit zookeeper/1:

                {
                    "host": 10.121.23.23,
                    "server_string": "server.1=host:server_port:election_port:role;localhost:clientport",
                    "server_id": "2",
                    "unit_id": "1",
                    "unit_name": "zookeeper/1",
                    "state": "ready",
                }

        Raises:
            UnitNotFoundError: When the target unit can't be found in the unit relation data, and/or cannot extract the private-address
        """
        unit_id = None
        server_id = None
        if isinstance(unit, Unit):
            unit = unit
            unit_id = self.get_unit_id(unit=unit)
            server_id = unit_id + 1
        if isinstance(unit, int):
            unit_id = unit
            server_id = unit + 1
            unit = self.get_unit_from_id(unit)

        try:
            host = self.relation.data[unit]["private-address"]
        except KeyError:
            raise UnitNotFoundError

        server_string = f"server.{server_id}={host}:{self.server_port}:{self.election_port}:{role};0.0.0.0:{self.client_port}"

        return {
            "host": host,
            "server_string": server_string,
            "server_id": str(server_id),
            "unit_id": str(unit_id),
            "unit_name": unit.name,
            "state": state,
        }

    def _get_updated_servers(self, server_strings: Iterable[str], updated_state: str):
        """Simple wrapper for building `updated_servers` for passing to app data updates."""
        updated_servers = {}
        for server in server_strings:
            unit_id = str(int(re.findall(r"server.([1-9]+)", server)[0]) - 1)
            updated_servers[unit_id] = updated_state

        return updated_servers

    def update_cluster(self) -> Dict[str, str]:
        """Adds and removes members from the current ZK quroum.

        To be ran by the Juju leader.

        After grabbing all the "started" units that the leader can see in the peer relation unit data.
        Removes members not in the quorum anymore (i.e `relation_departed` event)
        Adds new members to the quorum (i.e `relation_joined` event).

            Returns:
                A mapping of Juju unit IDs and updated state for changed units
                To be used in updating the app data
                    e.g {"0": "added", "1": "removed"}
        """
        active_hosts = []
        active_servers = set()

        # grabs all currently 'started' units from unit data
        # failed units will be absent
        for unit in self.started_units:
            active_hosts.append(self.unit_config(unit=unit)["host"])
            active_servers.add(self.unit_config(unit=unit)["server_string"])

        try:
            zk = ZooKeeperManager(hosts=active_hosts, client_port=self.client_port)
            zk_members = zk.server_members

            # remove units first, faster due to no startup/sync delay
            servers_to_remove = list(zk_members - active_servers)
            zk.remove_members(members=servers_to_remove)

            # sorting units to ensure units are added in id order
            servers_to_add = sorted(active_servers - zk_members)
            zk.add_members(members=servers_to_add)

            self.status = ActiveStatus()

            # extracts Juju unit ID from the changed servers
            removed_servers = self._get_updated_servers(
                server_strings=servers_to_remove, updated_state="removed"
            )
            added_servers = self._get_updated_servers(
                server_strings=servers_to_add, updated_state="added"
            )

            return {**added_servers, **removed_servers}

        # caught errors relate to a unit/zk_server not yet being ready to change
        except (
            MembersSyncingError,
            MemberNotReadyError,
            QuorumLeaderNotFoundError,
            KazooTimeoutError,
            UnitNotFoundError,
        ) as e:
            self.status = MaintenanceStatus(str(e))
            return {}

    def _is_unit_turn(self, unit_id: int) -> bool:
        """Checks if all units with a lower id than the current unit has been added/removed to the ZK quorum."""
        for peer_id in range(0, unit_id):
            if not self.relation.data[self.charm.app].get(str(peer_id), None):
                return False
        return True

    def _generate_units(self, unit_string: str) -> str:
        """Gets valid start-up server strings for current ZK quorum units found in the app data."""
        servers = ""
        for unit_id, state in self.relation.data[self.charm.app].items():
            if state == "added":
                server_string = self.unit_config(unit=int(unit_id))["server_string"]
                servers = servers + "\n" + server_string

        servers = servers + "\n" + unit_string
        return servers

    def ready_to_start(self, unit: Unit) -> Tuple[str, Dict]:
        """Decides whether a unit should start the ZK service, and with what configuration.

        Args:
            unit: the `Unit` to evaluate startability

        Returns:
            `servers`: a new-line delimited string of servers to add to a config file
            `unit_config`: a mapping of configuration for the given unit to be added to unit data

        Raises:
            `UnitNotFoundError`: if a lower ID unit is missing from the app/unit data
            `NotUnitTurnError`: if a lower ID unit has not yet been added to the ZK quorum
        """
        servers = ""
        unit_config = self.unit_config(unit=unit, state="ready", role="observer")
        unit_string = unit_config["server_string"]
        unit_id = unit_config["unit_id"]

        # double-checks all units are in the relation data
        total_units = len(self.relation.data[self.charm.app]) + 1
        if total_units < int(unit_id):
            raise UnitNotFoundError("can't find relation data")

        # i.e is the initial leader unit, always a participant to start quorum
        # in the case when 0 fails over, "0" will be in app data
        if int(unit_id) == 0 and not self.relation.data[self.charm.app].get("0", None):
            unit_string = unit_string.replace("observer", "participant")
            return unit_string.replace("observer", "participant"), unit_config

        if not self._is_unit_turn(unit_id=int(unit_id)):
            raise NotUnitTurnError("other units not yet added")

        servers = self._generate_units(unit_string=unit_string)

        return servers, unit_config
