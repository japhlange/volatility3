# This file is Copyright 2019 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#

import logging
import datetime
from typing import Iterable, List

from volatility.framework import constants, exceptions, interfaces, renderers, symbols, layers
from volatility.framework.automagic.pdbscan import scan
from volatility.framework.configuration import requirements
from volatility.framework.renderers import format_hints
from volatility.framework.symbols import intermed
from volatility.framework.symbols.windows.extensions import network
from volatility.plugins import timeliner
from volatility.plugins.windows import poolscanner

vollog = logging.getLogger(__name__)

class NetScan(interfaces.plugins.PluginInterface):
    """Scans for network objects present in a particular windows memory image."""

    _version = (1, 0, 0)
    CORRUPT_DEFAULT = False

    @classmethod
    def get_requirements(cls):
        return [
            requirements.TranslationLayerRequirement(name = 'primary',
                                                     description = 'Memory layer for the kernel',
                                                     architectures = ["Intel32", "Intel64"]),
            requirements.SymbolTableRequirement(name = "nt_symbols", description = "Windows kernel symbols"),
            requirements.PluginRequirement(name = 'poolscanner', plugin = poolscanner.PoolScanner, version = (1, 0, 0)),
            requirements.BooleanRequirement(name = 'include-corrupt',
                description = "Radically eases result validation. This will show partially overwritten data. WARNING: the results are likely to include garbage and/or corrupt data. Be cautious!",
                default = cls.CORRUPT_DEFAULT,
                optional = True
            ),
        ]

    @staticmethod
    def create_netscan_constraints(context, symbol_table: str) -> List[poolscanner.PoolConstraint]:
        """Creates a list of Pool Tag Constraints for network objects.

        Args:
            context: The context to retrieve required elements (layers, symbol tables) from
            symbol_table: The name of an existing symbol table containing the symbols / types

        Returns:
            The list containing the built constraints.
        """

        tcpl_size = context.symbol_space.get_type(symbol_table + constants.BANG + "_TCP_LISTENER").size
        tcpe_size = context.symbol_space.get_type(symbol_table + constants.BANG + "_TCP_ENDPOINT").size
        udpa_size = context.symbol_space.get_type(symbol_table + constants.BANG + "_UDP_ENDPOINT").size

        # ~ vollog.debug("Using pool size constraints: TcpL {}, TcpE {}, UdpA {}".format(tcpl_size, tcpe_size, udpa_size))

        return [
            # TCP listener
            poolscanner.PoolConstraint(b'TcpL',
                                       type_name = symbol_table + constants.BANG + "_TCP_LISTENER",
                                       size = (tcpl_size, None),
                                       page_type = poolscanner.PoolType.NONPAGED | poolscanner.PoolType.FREE),
            # TCP Endpoint
            poolscanner.PoolConstraint(b'TcpE',
                                       type_name = symbol_table + constants.BANG + "_TCP_ENDPOINT",
                                       size = (tcpe_size, None),
                                       page_type = poolscanner.PoolType.NONPAGED | poolscanner.PoolType.FREE),
            # UDP Endpoint
            poolscanner.PoolConstraint(b'UdpA',
                                       type_name = symbol_table + constants.BANG + "_UDP_ENDPOINT",
                                       size = (udpa_size, None),
                                       page_type = poolscanner.PoolType.NONPAGED | poolscanner.PoolType.FREE)
        ]

    def determine_tcpip_version(self) -> str:
        """Tries to determine which symbol filename to use for the image's tcpip driver. The logic is partially taken from the info plugin.

        Args:

        Returns:
            The filename of the symbol table to use.
        """

        # while the failsafe way to determine the version of tcpip.sys would be to
        # extract the driver and parse its PE header containing the versionstring,
        # unfortunately that header is not guaranteed to persist within memory.
        # therefore we determine the version based on the kernel version as testing
        # with several windows versions has showed this to work out correctly.

        is_64bit = symbols.symbol_table_is_64bit(self.context, self.config['nt_symbols'])

        if is_64bit:
            arch = "x64"
        else:
            arch = "x86"

        # the following code is taken from the windows.info plugin.

        virtual_layer_name = self.config["primary"]
        virtual_layer = self.context.layers[virtual_layer_name]
        if not isinstance(virtual_layer, layers.intel.Intel):
            raise TypeError("Virtual Layer is not an intel layer")

        kvo = virtual_layer.config["kernel_virtual_offset"]

        ntkrnlmp = self.context.module(self.config["nt_symbols"], layer_name = virtual_layer_name, offset = kvo)

        vers_offset = ntkrnlmp.get_symbol("KdVersionBlock").address

        vers = ntkrnlmp.object(object_type = "_DBGKD_GET_VERSION64",
                               layer_name = virtual_layer_name,
                               offset = vers_offset)

        vollog.debug("Determined OS Major/Minor Version: {}.{}".format(vers.MajorVersion, vers.MinorVersion))

        vers_minor_version = int(vers.MinorVersion)

        # this is a hard-coded address in the Windows OS
        if virtual_layer.bits_per_register == 32:
            kuser_addr = 0xFFDF0000
        else:
            kuser_addr = 0xFFFFF78000000000

        kuser = ntkrnlmp.object(object_type = "_KUSER_SHARED_DATA",
                                layer_name = virtual_layer_name,
                                offset = kuser_addr,
                                absolute = True)

        nt_major_version = str(kuser.NtMajorVersion)
        nt_minor_version = str(kuser.NtMinorVersion)

        # default to general class types, may be overwritten later.
        class_types = network.class_types
        if nt_major_version == "10":
            if arch == "x64":
                # win10 x64 has an additional class type we have to include.
                class_types = network.win10_x64_class_types

            if vers_minor_version < 14393:
                # all win10 below 14393 have the same structs for our needs.
                filename = "netscan-win10-{arch}".format(arch=arch)
            elif vers_minor_version < 15063:
                if arch == "x64":
                    filename = "netscan-win10-x64"
                else:
                    # 14393 x86 is special.
                    filename = "netscan-win10-14393-x86"
            else:
                # for now all newer windows versions share the same structs.
                filename = "netscan-win10-15063-{arch}".format(arch=arch)

        elif nt_major_version == "6":
            # win between vista and 8.1
            if nt_minor_version == "0":
                # vista
                # if vista sp 12 x64 then:
                # filename = "netscan-vista-sp12-x64"
                filename = "netscan-vista-{arch}".format(arch=arch)
            elif nt_minor_version == "1":
                # 7
                filename = "netscan-win7-{arch}".format(arch=arch)
            elif nt_minor_version == "2":
                # 8
                filename = "netscan-win8-{arch}".format(arch=arch)
            elif nt_minor_version == "3":
                # 8.1
                filename = "netscan-win81-{arch}".format(arch=arch)
        else:
            # default to a fallback, but this *should* not happen.
            filename = "netscan-win{vers}-{arch}".format(vers=major_version, arch=arch)

        vollog.debug("Determined symbol filename: {}".format(filename))

        return filename, class_types

    def create_netscan_symbol_table(self) -> str:
        """Creates a symbol table for TCP Listeners and TCP/UDP Endpoints.

        Returns:
            The name of the constructed symbol table
        """
        table_mapping = {"nt_symbols": self.config["nt_symbols"]}

        symbol_filename, class_types = self.determine_tcpip_version()

        return intermed.IntermediateSymbolTable.create(self.context,
                                                       self.config_path,
                                                       "windows",
                                                       symbol_filename,
                                                       class_types = class_types,
                                                       table_mapping = table_mapping)

    @classmethod
    def scan(cls,
             context: interfaces.context.ContextInterface,
             layer_name: str,
             nt_symbol_table: str,
             netscan_symbol_table: str) -> \
        Iterable[interfaces.objects.ObjectInterface]:
        """Scans for network objects using the poolscanner module and constraints.

        Args:
            context: The context to retrieve required elements (layers, symbol tables) from
            layer_name: The name of the layer on which to operate
            nt_symbol_table: The name of the table containing the kernel symbols
            netscan_symbol_table: The name of the table containing the network object symbols (_TCP_LISTENER etc.)

        Returns:
            A list of network objects found by scanning the `layer_name` layer for network pool signatures
        """

        constraints = cls.create_netscan_constraints(context, netscan_symbol_table)

        for result in poolscanner.PoolScanner.generate_pool_scan(context, layer_name, nt_symbol_table, constraints):

            _constraint, mem_object, _header = result
            yield mem_object

    def _generator(self, show_corrupt_results = None):
        """ Generates the network objects for use in rendering. """

        netscan_symbol_table = self.create_netscan_symbol_table()

        for netw_obj in self.scan(self.context, self.config['primary'], self.config['nt_symbols'],
                                  netscan_symbol_table):

            vollog.debug("Found netw obj @ 0x{:2x} of assumed type {}".format(netw_obj.vol.offset, type(netw_obj)))
            # objects passed pool header constraints. check for additional constraints if strict flag is set.
            if not show_corrupt_results:
                if not netw_obj.is_valid():
                    continue

            if isinstance(netw_obj, network._UDP_ENDPOINT):
                vollog.debug("Found UDP_ENDPOINT @ 0x{:2x}".format(netw_obj.vol.offset))

                # For UdpA, the state is always blank and the remote end is asterisks
                for ver, laddr, _ in netw_obj.dual_stack_sockets():
                    yield (0, (format_hints.Hex(netw_obj.vol.offset),
                               "UDP" + ver,
                               laddr,
                               netw_obj.Port,
                               "*", 0, "",
                               netw_obj.get_owner_pid() or renderers.UnreadableValue(),
                               netw_obj.get_owner_procname() or renderers.UnreadableValue(),
                               netw_obj.get_create_time() or renderers.UnreadableValue()))

            elif isinstance(netw_obj, network._TCP_ENDPOINT):
                vollog.debug("Found _TCP_ENDPOINT @ 0x{:2x}".format(netw_obj.vol.offset))
                if netw_obj.get_address_family() == network.AF_INET:
                    proto = "TCPv4"
                elif netw_obj.get_address_family() == network.AF_INET6:
                    proto = "TCPv6"
                else:
                    proto = "TCPv?"

                if netw_obj.State in network.TCP_STATE_ENUM:
                    state = network.TCP_STATE_ENUM[netw_obj.State]
                else:
                    state = renderers.UnreadableValue()

                yield (0, (format_hints.Hex(netw_obj.vol.offset), proto,
                           netw_obj.get_local_address() or renderers.UnreadableValue(),
                           netw_obj.LocalPort,
                           netw_obj.get_remote_address() or renderers.UnreadableValue(),
                           netw_obj.RemotePort,
                           state,
                           netw_obj.get_owner_pid() or renderers.UnreadableValue(),
                           netw_obj.get_owner_procname() or renderers.UnreadableValue(),
                           netw_obj.get_create_time() or renderers.UnreadableValue()))

            # check for isinstance of tcp listener last, because all other objects are inherited from here
            elif isinstance(netw_obj, network._TCP_LISTENER):
                vollog.debug("Found _TCP_LISTENER @ 0x{:2x}".format(netw_obj.vol.offset))

                # For TcpL, the state is always listening and the remote port is zero
                for ver, laddr, raddr in netw_obj.dual_stack_sockets():
                    yield (0, (format_hints.Hex(netw_obj.vol.offset), "TCP" + ver,
                               laddr,
                               netw_obj.Port,
                               raddr,
                               0,
                               "LISTENING",
                               netw_obj.get_owner_pid() or renderers.UnreadableValue(),
                               netw_obj.get_owner_procname() or renderers.UnreadableValue(),
                               netw_obj.get_create_time() or renderers.UnreadableValue()))
            else:
                # this should not happen therefore we log it.
                vollog.debug("Found network object unsure of its type: {} of type {}".format(netw_obj, type(netw_obj)))

    def run(self):
        show_corrupt_results = self.config.get('include-corrupt', None)

        return renderers.TreeGrid([
            ("Offset", format_hints.Hex),
            ("Proto", str),
            ("LocalAddr", str),
            ("LocalPort", int),
            ("ForeignAddr", str),
            ("ForeignPort", int),
            ("State", str),
            ("PID", int),
            ("Owner", str),
            ("Created", datetime.datetime),
        ], self._generator(show_corrupt_results=show_corrupt_results))
