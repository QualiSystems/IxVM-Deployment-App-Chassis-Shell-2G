import copy
import json

from cloudshell.api.cloudshell_api import SetConnectorRequest
from cloudshell.core.context.error_handling_context import ErrorHandlingContext
from cloudshell.devices.driver_helper import get_api
from cloudshell.devices.autoload.autoload_builder import AutoloadDetailsBuilder
from cloudshell.devices.driver_helper import get_logger_with_thread_id
from cloudshell.shell.core.driver_context import AutoLoadDetails
from cloudshell.shell.core.resource_driver_interface import ResourceDriverInterface

from traffic.ixvm.vchassis.configuration_attributes_structure import TrafficGeneratorVChassisResource
from traffic.ixvm.vchassis.client import IxVMChassisHTTPClient
from traffic.ixvm.vchassis.autoload import models


ATTR_REQUESTED_SOURCE_VNIC = "Requested Source vNIC Name"
ATTR_REQUESTED_TARGET_VNIC = "Requested Target vNIC Name"
MODEL_PORT = "IxVM Virtual Traffic Generator Port"


class IxVMVchassisDriver(ResourceDriverInterface):
    def __init__(self):
        """Constructor must be without arguments, it is created with reflection at run time"""
        pass

    def initialize(self, context):
        """Initialize the driver session, this function is called everytime a new instance of the driver is created.

        This is a good place to load and cache the driver configuration, initiate sessions etc.
        :param InitCommandContext context: the context the command runs on
        """
        pass

    @staticmethod
    def _get_resource_attribute_value(resource, attribute_name):
        """

        :param resource cloudshell.api.cloudshell_api.ResourceInfo:
        :param str attribute_name:
        """
        for attribute in resource.ResourceAttributes:
            if attribute.Name == attribute_name:
                return attribute.Value

    def get_inventory(self, context):
        """Discovers the resource structure and attributes.

        :param AutoLoadCommandContext context: the context the command runs on
        :return Attribute and sub-resource information for the Shell resource you can return an AutoLoadDetails object
        :rtype: AutoLoadDetails
        """
        logger = get_logger_with_thread_id(context)
        logger.info("Autoload")

        with ErrorHandlingContext(logger):

            vchassis_resource = TrafficGeneratorVChassisResource.from_context(context)

            if not vchassis_resource.address or vchassis_resource.address.upper() == "NA":
                return AutoLoadDetails([], [])

            # cs_api = get_api(context)

            # api_client = IxVMChassisHTTPClient(address=vchassis_resource.address,
            #                                    user=vchassis_resource.user,
            #                                    password=vchassis_resource.password)  # todo: decrypt password !!!!!

            api_client = IxVMChassisHTTPClient(address=vchassis_resource.address,
                                               user="admin",
                                               password="admin")  # todo: decrypt password !!!!!


            api_client.login()


            # todo: clarify if there always will be only one chassis
            chassis_data = api_client.get_chassis()[0]
            chassis_id = chassis_data["id"]

            chassis_res = models.Chassis(shell_name="",
                                         name="IxVm Virtual Chassis {}".format(chassis_id),
                                         unique_id=chassis_id)

            port_resources = {}
            for port_data in api_client.get_ports():
                port_id = port_data["id"]

                parent_id = port_data["parentId"]
                port_res = models.Port(shell_name="",
                                       name="Port {}".format(port_data["portNumber"]),
                                       unique_id=port_id)
                ports_by_module = port_resources.setdefault(parent_id, [])
                ports_by_module.append(port_res)

            for module_data in api_client.get_cards():
                module_id = module_data["id"]
                module_res = models.Module(shell_name="",
                                           name="IxVm Virtual Module {}".format(module_data["cardNumber"]),
                                           unique_id=module_id)

                chassis_res.add_sub_resource(module_id, module_res)

                for port_res in port_resources.get(module_id, []):
                    module_res.add_sub_resource(port_res.unique_id, port_res)

            return AutoloadDetailsBuilder(chassis_res).autoload_details()

    def cleanup(self):
        """ Destroy the driver session, this function is called everytime a driver instance is destroyed
        This is a good place to close any open sessions, finish writing to log files
        """

        pass

    def connect_child_resources(self, context):
        """

        :type context: cloudshell.shell.core.driver_context.ResourceCommandContext
        :rtype: str
        """
        logger = get_logger_with_thread_id(context)
        logger.info("Connect child resources command started")

        with ErrorHandlingContext(logger):
            api = get_api(context)

            resource_name = context.resource.fullname
            reservation_id = context.reservation.reservation_id
            connectors = context.connectors

            if not context.connectors:
                return "Success"

            resource = api.GetResourceDetails(resource_name)

            to_disconnect = []
            to_connect = []
            temp_connectors = []
            ports = self._get_ports(resource)

            for connector in connectors:
                me, other = self._set_remap_connector_details(connector, resource_name, temp_connectors)
                to_disconnect.extend([me, other])

            connectors = temp_connectors

            # these are connectors from app to vlan where user marked to which interface the connector should be connected
            connectors_with_predefined_target = [connector for connector in connectors if connector.vnic_id != ""]

            # these are connectors from app to vlan where user left the target interface unspecified
            connectors_without_target = [connector for connector in connectors if connector.vnic_id == ""]

            for connector in connectors_with_predefined_target:
                if connector.vnic_id not in ports.keys():
                    raise Exception("Tried to connect an interface that is not on reservation - " + connector.vnic_id)

                else:
                    if hasattr(ports[connector.vnic_id], "allocated"):
                        raise Exception(
                            "Tried to connect several connections to same interface: " + ports[connector.vnic_id])

                    else:
                        to_connect.append(SetConnectorRequest(SourceResourceFullName=ports[connector.vnic_id].Name,
                                                              TargetResourceFullName=connector.other,
                                                              Direction=connector.direction,
                                                              Alias=connector.alias))
                        ports[connector.vnic_id].allocated = True

            unallocated_ports = [port for key, port in ports.items() if not hasattr(port, "allocated")]

            if len(unallocated_ports) < len(connectors_without_target):
                raise Exception("There were more connections to TeraVM than available interfaces after deployment.")
            else:
                for port in unallocated_ports:
                    if connectors_without_target:
                        connector = connectors_without_target.pop()
                        to_connect.append(SetConnectorRequest(SourceResourceFullName=port.Name,
                                                              TargetResourceFullName=connector.other,
                                                              Direction=connector.direction,
                                                              Alias=connector.alias))

            if connectors_without_target:
                raise Exception("There were more connections to TeraVM than available interfaces after deployment.")

            api.RemoveConnectorsFromReservation(reservation_id, to_disconnect)
            api.SetConnectorsInReservation(reservation_id, to_connect)

            return "Success"

    @staticmethod
    def _set_remap_connector_details(connector, resource_name, connectors):
        attribs = connector.attributes
        if resource_name in connector.source.split("/"):
            remap_requests = attribs.get(ATTR_REQUESTED_SOURCE_VNIC, "").split(",")

            me = connector.source
            other = connector.target

            for vnic_id in remap_requests:
                new_con = copy.deepcopy(connector)
                TeraVMVbladeDriver._update_connector(new_con, me, other, vnic_id)
                connectors.append(new_con)

        elif resource_name in connector.target.split("/"):
            remap_requests = attribs.get(ATTR_REQUESTED_TARGET_VNIC, "").split(",")

            me = connector.target
            other = connector.source

            for vnic_id in remap_requests:
                new_con = copy.deepcopy(connector)
                TeraVMVbladeDriver._update_connector(new_con, me, other, vnic_id)
                connectors.append(new_con)
        else:
            raise Exception("Oops, a connector doesn't have required details:\n Connector source: {0}\n"
                            "Connector target: {1}\nPlease contact your admin".format(connector.source,
                                                                                      connector.target))

        return me, other

    @staticmethod
    def _update_connector(connector, me, other, vnic_id):
        connector.vnic_id = vnic_id
        connector.me = me
        connector.other = other

    @staticmethod
    def _get_ports(resource):
        ports = {str(idx): port for idx, port in enumerate(resource.ChildResources)
                 if port.ResourceModelName == MODEL_PORT}
        return ports

if __name__ == "__main__":
    import mock
    from cloudshell.shell.core.context import ResourceCommandContext, ResourceContextDetails, ReservationContextDetails

    address = '192.168.42.191'

    user = 'admin'
    password = 'admin'
    port = 443
    scheme = "https"
    auth_key = 'h8WRxvHoWkmH8rLQz+Z/pg=='
    api_port = 8029

    context = ResourceCommandContext()
    context.resource = ResourceContextDetails()
    context.resource.name = 'tvm_m_2_fec7-7c42'
    context.resource.fullname = 'tvm_m_2_fec7-7c42'
    context.reservation = ReservationContextDetails()
    context.reservation.reservation_id = '0cc17f8c-75ba-495f-aeb5-df5f0f9a0e97'
    context.resource.attributes = {}
    context.resource.attributes['User'] = user
    context.resource.attributes['Password'] = password
    context.resource.attributes['TVM Comms Network'] = "TVM_Comms_VLAN_99"
    context.resource.attributes['TVM MGMT Network'] = "TMV_Mgmt"
    context.resource.address = address
    context.resource.app_context = mock.MagicMock(app_request_json=json.dumps(
        {
            "deploymentService": {
                "cloudProviderName": "vcenter_333"
            }
        }))

    context.connectivity = mock.MagicMock()
    context.connectivity.server_address = "192.168.85.23"

    dr = IxVMVchassisDriver()
    dr.initialize(context)

    result = dr.get_inventory(context)

    for resource in result.resources:
        print resource.__dict__