# Copyright 2018 Datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License
from typing import Any, Dict, List, Optional, Tuple, Union, TYPE_CHECKING
from typing import cast as typecast

from os import environ

import logging

from ...ir.irlistener import IRListener
from ...ir.irtcpmappinggroup import IRTCPMappingGroup

from ...utils import dump_json, parse_bool

from .v3httpfilter import V3HTTPFilter
from .v3route import DictifiedV3Route, v3prettyroute
from .v3tls import V3TLSContext
from .v3virtualhost import V3VirtualHost

if TYPE_CHECKING:
    from ...ir.irtlscontext import IRTLSContext # pragma: no cover
    from . import V3Config                      # pragma: no cover


class V3TCPListener(dict):
    def __init__(self, config: 'V3Config', group: IRTCPMappingGroup) -> None:
        super().__init__()

        # Use the actual listener name & port number
        self.bind_address = group.get('address') or '0.0.0.0'
        self.name = "listener-%s-%s" % (self.bind_address, group.port)

        self.tls_context: Optional[V3TLSContext] = None

        # Set the basics like our name and listening address.
        self.update({
            'name': self.name,
            'address': {
                'socket_address': {
                    'address': self.bind_address,
                    'port_value': group.port,
                    'protocol': 'TCP'
                }
            },
            'filter_chains': []
        })

        # Next: is SNI a thing?
        if group.get('tls_context', None):
            # Yup. We need the TLS inspector here...
            self['listener_filters'] = [ {
                'name': 'envoy.filters.listener.tls_inspector'
            } ]

            # ...and we need to save the TLS context we'll be using.
            self.tls_context = V3TLSContext(group.tls_context)

    def add_group(self, config: 'V3Config', group: IRTCPMappingGroup) -> None:
        # First up, which clusters do we need to talk to?
        clusters = [{
            'name': mapping.cluster.envoy_name,
            'weight': mapping.weight
        } for mapping in group.mappings]

        # From that, we can sort out a basic tcp_proxy filter config.
        tcp_filter = {
            'name': 'envoy.filters.network.tcp_proxy',
            'typed_config': {
                '@type': 'type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy',
                'stat_prefix': 'ingress_tcp_%d' % group.port,
                'weighted_clusters': {
                    'clusters': clusters
                }
            }
        }

        # OK. Basic filter chain entry next.
        chain_entry: Dict[str, Any] = {
            'filters': [
                tcp_filter
            ]
        }

        # Then, if SNI is a thing, update the chain entry with the appropriate chain match.
        if self.tls_context:
            # Apply the context to the chain...
            chain_entry['transport_socket'] = {
                    'name': 'envoy.transport_sockets.tls',
                    'typed_config': {
                        '@type': 'type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext',
                        **self.tls_context,
                    }
            }

            # Do we have a host match?
            host_wanted = group.get('host') or '*'

            if host_wanted != '*':
                # Yup. Hook it in.
                chain_entry['filter_chain_match'] = {
                    'server_names': [ host_wanted ]
                }

        # OK, once that's done, stick this into our filter chains.
        self['filter_chains'].append(chain_entry)


class V3ListenerCollection:
    def __init__(self, config: 'V3Config') -> None:
        self.listeners: Dict[int, 'V3Listener'] = {}
        self.config = config

    def __getitem__(self, port: int) -> 'V3Listener':
        listener = self.listeners.get(port, None)

        if listener is None:
            listener = V3Listener(self.config, port)
            self.listeners[port] = listener

        return listener

    def __contains__(self, port: int) -> bool:
        return port in self.listeners

    def items(self):
        return self.listeners.items()

    def get(self, port: int, use_proxy_proto: bool) -> 'V3Listener':
        set_upp = (not port in self)

        v3listener = self[port]

        if set_upp:
            v3listener.use_proxy_proto = use_proxy_proto
        elif v3listener.use_proxy_proto != use_proxy_proto:
            raise Exception("listener for port %d has use_proxy_proto %s, requester wants upp %s" %
                            (v3listener.port, v3listener.use_proxy_proto, use_proxy_proto))

        return v3listener


class V3Listener(dict):
    def __init__(self, config: 'V3Config', port: int) -> None:
        super().__init__()

        self.config = config
        self.port = port
        self.name = f"ambassador-listener-{self.port}"
        self.use_proxy_proto = False
        self.vhosts: Dict[str, V3VirtualHost] = {}
        self.first_vhost: Optional[V3VirtualHost] = None
        self.listener_filters: List[dict] = []
        self.traffic_direction: str = "UNSPECIFIED"

        self._base_http_config: Optional[Dict[str, Any]] = None

        # It's important from a performance perspective to wrap debug log statements
        # with this check so we don't end up generating log strings (or even JSON
        # representations) that won't get logged anyway.
        log_debug = self.config.ir.logger.isEnabledFor(logging.DEBUG)
        if log_debug:
            self.config.ir.logger.debug(f"V3Listener {self.name} created")

        # Start by building our base HTTP config...
        self._base_http_config = self.base_http_config(log_debug)

    # access_log constructs the access_log configuration for this V3Listener
    def access_log(self, log_debug: bool) -> List[dict]:
        access_log: List[dict] = []

        for al in self.config.ir.log_services.values():
            access_log_obj: Dict[str, Any] = { "common_config": al.get_common_config() }
            req_headers = []
            resp_headers = []
            trailer_headers = []

            for additional_header in al.get_additional_headers():
                if additional_header.get('during_request', True):
                    req_headers.append(additional_header.get('header_name'))
                if additional_header.get('during_response', True):
                    resp_headers.append(additional_header.get('header_name'))
                if additional_header.get('during_trailer', True):
                    trailer_headers.append(additional_header.get('header_name'))

            if al.driver == 'http':
                access_log_obj['additional_request_headers_to_log'] = req_headers
                access_log_obj['additional_response_headers_to_log'] = resp_headers
                access_log_obj['additional_response_trailers_to_log'] = trailer_headers
                access_log_obj['@type'] = 'type.googleapis.com/envoy.extensions.access_loggers.grpc.v3.HttpGrpcAccessLogConfig'
                access_log.append({
                    "name": "envoy.access_loggers.http_grpc",
                    "typed_config": access_log_obj
                })
            else:
                # inherently TCP right now
                # tcp loggers do not support additional headers
                access_log_obj['@type'] = 'type.googleapis.com/envoy.extensions.access_loggers.grpc.v3.TcpGrpcAccessLogConfig'
                access_log.append({
                    "name": "envoy.access_loggers.tcp_grpc",
                    "typed_config": access_log_obj
                })

        # Use sane access log spec in JSON
        if self.config.ir.ambassador_module.envoy_log_type.lower() == "json":
            log_format = self.config.ir.ambassador_module.get('envoy_log_format', None)
            if log_format is None:
                log_format = {
                    'start_time': '%START_TIME%',
                    'method': '%REQ(:METHOD)%',
                    'path': '%REQ(X-ENVOY-ORIGINAL-PATH?:PATH)%',
                    'protocol': '%PROTOCOL%',
                    'response_code': '%RESPONSE_CODE%',
                    'response_flags': '%RESPONSE_FLAGS%',
                    'bytes_received': '%BYTES_RECEIVED%',
                    'bytes_sent': '%BYTES_SENT%',
                    'duration': '%DURATION%',
                    'upstream_service_time': '%RESP(X-ENVOY-UPSTREAM-SERVICE-TIME)%',
                    'x_forwarded_for': '%REQ(X-FORWARDED-FOR)%',
                    'user_agent': '%REQ(USER-AGENT)%',
                    'request_id': '%REQ(X-REQUEST-ID)%',
                    'authority': '%REQ(:AUTHORITY)%',
                    'upstream_host': '%UPSTREAM_HOST%',
                    'upstream_cluster': '%UPSTREAM_CLUSTER%',
                    'upstream_local_address': '%UPSTREAM_LOCAL_ADDRESS%',
                    'downstream_local_address': '%DOWNSTREAM_LOCAL_ADDRESS%',
                    'downstream_remote_address': '%DOWNSTREAM_REMOTE_ADDRESS%',
                    'requested_server_name': '%REQUESTED_SERVER_NAME%',
                    'istio_policy_status': '%DYNAMIC_METADATA(istio.mixer:status)%',
                    'upstream_transport_failure_reason': '%UPSTREAM_TRANSPORT_FAILURE_REASON%'
                }

                tracing_config = self.config.ir.tracing
                if tracing_config and tracing_config.driver == 'envoy.tracers.datadog':
                    log_format['dd.trace_id'] = '%REQ(X-DATADOG-TRACE-ID)%'
                    log_format['dd.span_id'] = '%REQ(X-DATADOG-PARENT-ID)%'

            access_log.append({
                'name': 'envoy.access_loggers.file',
                'typed_config': {
                    '@type': 'type.googleapis.com/envoy.extensions.access_loggers.file.v3.FileAccessLog',
                    'path': self.config.ir.ambassador_module.envoy_log_path,
                    'json_format': log_format
                }
            })
        else:
            # Use a sane access log spec
            log_format = self.config.ir.ambassador_module.get('envoy_log_format', None)

            if not log_format:
                log_format = 'ACCESS [%START_TIME%] \"%REQ(:METHOD)% %REQ(X-ENVOY-ORIGINAL-PATH?:PATH)% %PROTOCOL%\" %RESPONSE_CODE% %RESPONSE_FLAGS% %BYTES_RECEIVED% %BYTES_SENT% %DURATION% %RESP(X-ENVOY-UPSTREAM-SERVICE-TIME)% \"%REQ(X-FORWARDED-FOR)%\" \"%REQ(USER-AGENT)%\" \"%REQ(X-REQUEST-ID)%\" \"%REQ(:AUTHORITY)%\" \"%UPSTREAM_HOST%\"'

            if log_debug:
                self.config.ir.logger.debug("V3Listener: Using log_format '%s'" % log_format)
            access_log.append({
                'name': 'envoy.access_loggers.file',
                'typed_config': {
                    '@type': 'type.googleapis.com/envoy.extensions.access_loggers.file.v3.FileAccessLog',
                    'path': self.config.ir.ambassador_module.envoy_log_path,
                    'format': log_format + '\n'
                }
            })

        return access_log

    # base_http_config constructs the starting configuration for this
    # V3Listener's http_connection_manager filter.
    def base_http_config(self, log_debug: bool) -> Dict[str, Any]:
        base_http_config: Dict[str, Any] = {
            'stat_prefix': 'ingress_http',
            'access_log': self.access_log(log_debug),
            'http_filters': [],
            'normalize_path': True
        }

        # Assemble base HTTP filters
        for f in self.config.ir.filters:
            v3hf: dict = V3HTTPFilter(f, self.config)

            # V3HTTPFilter can return None to indicate that the filter config
            # should be omitted from the final envoy config. This is the
            # uncommon case, but it can happen if a filter waits utnil the
            # v3config is generated before deciding if it needs to be
            # instantiated. See IRErrorResponse for an example.
            if v3hf:
                base_http_config['http_filters'].append(v3hf)

        if 'use_remote_address' in self.config.ir.ambassador_module:
            base_http_config["use_remote_address"] = self.config.ir.ambassador_module.use_remote_address

        if 'xff_num_trusted_hops' in self.config.ir.ambassador_module:
            base_http_config["xff_num_trusted_hops"] = self.config.ir.ambassador_module.xff_num_trusted_hops

        if 'server_name' in self.config.ir.ambassador_module:
            base_http_config["server_name"] = self.config.ir.ambassador_module.server_name

        listener_idle_timeout_ms = self.config.ir.ambassador_module.get('listener_idle_timeout_ms', None)
        if listener_idle_timeout_ms:
            if 'common_http_protocol_options' in base_http_config:
                base_http_config["common_http_protocol_options"]["idle_timeout"] = "%0.3fs" % (float(listener_idle_timeout_ms) / 1000.0)
            else:
                base_http_config["common_http_protocol_options"] = { 'idle_timeout': "%0.3fs" % (float(listener_idle_timeout_ms) / 1000.0) }

        if 'headers_with_underscores_action' in self.config.ir.ambassador_module:
            if 'common_http_protocol_options' in base_http_config:
                base_http_config["common_http_protocol_options"]["headers_with_underscores_action"] = self.config.ir.ambassador_module.headers_with_underscores_action
            else:
                base_http_config["common_http_protocol_options"] = { 'headers_with_underscores_action': self.config.ir.ambassador_module.headers_with_underscores_action }

        max_request_headers_kb = self.config.ir.ambassador_module.get('max_request_headers_kb', None)
        if max_request_headers_kb:
            base_http_config["max_request_headers_kb"] = max_request_headers_kb

        if 'enable_http10' in self.config.ir.ambassador_module:
            http_options = base_http_config.setdefault("http_protocol_options", {})
            http_options['accept_http_10'] = self.config.ir.ambassador_module.enable_http10

        if 'preserve_external_request_id' in self.config.ir.ambassador_module:
            base_http_config["preserve_external_request_id"] = self.config.ir.ambassador_module.preserve_external_request_id

        if 'forward_client_cert_details' in self.config.ir.ambassador_module:
            base_http_config["forward_client_cert_details"] = self.config.ir.ambassador_module.forward_client_cert_details

        if 'set_current_client_cert_details' in self.config.ir.ambassador_module:
            base_http_config["set_current_client_cert_details"] = self.config.ir.ambassador_module.set_current_client_cert_details

        if self.config.ir.tracing:
            base_http_config["generate_request_id"] = True

            base_http_config["tracing"] = {}
            self.traffic_direction = "OUTBOUND"

            req_hdrs = self.config.ir.tracing.get('tag_headers', [])

            if req_hdrs:
                base_http_config["tracing"]["custom_tags"] = []
                for hdr in req_hdrs:
                    custom_tag = {
                        "request_header": {
                            "name": hdr,
                            },
                        "tag": hdr,
                    }
                    base_http_config["tracing"]["custom_tags"].append(custom_tag)


            sampling = self.config.ir.tracing.get('sampling', {})
            if sampling:
                client_sampling = sampling.get('client', None)
                if client_sampling is not None:
                    base_http_config["tracing"]["client_sampling"] = {
                        "value": client_sampling
                    }

                random_sampling = sampling.get('random', None)
                if random_sampling is not None:
                    base_http_config["tracing"]["random_sampling"] = {
                        "value": random_sampling
                    }

                overall_sampling = sampling.get('overall', None)
                if overall_sampling is not None:
                    base_http_config["tracing"]["overall_sampling"] = {
                        "value": overall_sampling
                    }

        proper_case: bool = self.config.ir.ambassador_module['proper_case']

        # Get the list of downstream headers whose casing should be overriden
        # from the Ambassador module. We configure the upstream side of this
        # in v3cluster.py
        header_case_overrides = self.config.ir.ambassador_module.get('header_case_overrides', None)
        if header_case_overrides:
            if proper_case:
                self.config.ir.post_error(
                    "Only one of 'proper_case' or 'header_case_overrides' fields may be set on " +\
                    "the Ambassador module. Honoring proper_case and ignoring " +\
                    "header_case_overrides.")
                header_case_overrides = None
            if not isinstance(header_case_overrides, list):
                # The header_case_overrides field must be an array.
                self.config.ir.post_error("Ambassador module config 'header_case_overrides' must be an array")
                header_case_overrides = None
            elif len(header_case_overrides) == 0:
                # Allow an empty list to mean "do nothing".
                header_case_overrides = None

        if header_case_overrides:
            # We have this config validation here because the Ambassador module is
            # still an untyped config. That is, we aren't yet using a CRD or a
            # python schema to constrain the configuration that can be present.
            rules = []
            for hdr in header_case_overrides:
                if not isinstance(hdr, str):
                    self.config.ir.post_error("Skipping non-string header in 'header_case_overrides': {hdr}")
                    continue
                rules.append(hdr)

            if len(rules) == 0:
                self.config.ir.post_error(f"Could not parse any valid string headers in 'header_case_overrides': {header_case_overrides}")
            else:
                # Create custom header rules that map the lowercase version of every element in
                # `header_case_overrides` to the the respective original casing.
                #
                # For example the input array [ X-HELLO-There, X-COOL ] would create rules:
                # { 'x-hello-there': 'X-HELLO-There', 'x-cool': 'X-COOL' }. In envoy, this effectively
                # overrides the response header case by remapping the lowercased version (the default
                # casing in envoy) back to the casing provided in the config.
                custom_header_rules: Dict[str, Dict[str, dict]] = {
                    'custom': {
                        'rules': {
                            header.lower() : header for header in rules
                        }
                    }
                }
                http_options = base_http_config.setdefault("http_protocol_options", {})
                http_options["header_key_format"] = custom_header_rules

        if proper_case:
            proper_case_header: Dict[str, Dict[str, dict]] = {'header_key_format': {'proper_case_words': {}}}
            if 'http_protocol_options' in base_http_config:
                base_http_config["http_protocol_options"].update(proper_case_header)
            else:
                base_http_config["http_protocol_options"] = proper_case_header

        return base_http_config

    def add_irlistener(self, listener: IRListener) -> None:
        if listener.service_port != self.port:
            # This is a problem.
            raise Exception("V3Listener %s: trying to add listener %s on %s:%d??" %
                            (self.name, listener.name, listener.hostname, listener.service_port))

        # OK, make sure we don't somehow have a VHost collision.
        if listener.hostname in self.vhosts:
            raise Exception("V3Listener %s: listener %s on %s:%d already has a vhost??" %
                            (self.name, listener.name, listener.hostname, listener.service_port))

    # Weirdly, the action is optional but the insecure_action is not. This is not a typo.
    def make_vhost(self, name: str, hostname: str, context: Optional['IRTLSContext'], secure: bool,
                   action: Optional[str], insecure_action: str) -> None:
        if self.config.ir.logger.isEnabledFor(logging.DEBUG):
            self.config.ir.logger.debug("V3Listener %s: adding VHost %s for host %s, secure %s, insecure %s)" %
                                       (self.name, name, hostname, action, insecure_action))

        vhost = self.vhosts.get(hostname)

        if vhost:
            if ((hostname != vhost._hostname) or
                (context != vhost._ctx) or
                (secure != vhost._secure) or
                (action != vhost._action) or
                (insecure_action != vhost._insecure_action)):
                raise Exception("V3Listener %s: trying to make vhost %s for %s but one already exists" %
                                (self.name, name, hostname))
            else:
                return

        vhost = V3VirtualHost(config=self.config, listener=self,
                              name=name, hostname=hostname, ctx=context,
                              secure=secure, action=action, insecure_action=insecure_action)
        self.vhosts[hostname] = vhost

        if not self.first_vhost:
            self.first_vhost = vhost

    def finalize(self) -> None:
        if self.config.ir.logger.isEnabledFor(logging.DEBUG):
            self.config.ir.logger.debug(f"V3Listener finalize {self.pretty()}")

        # Check if AMBASSADOR_ENVOY_BIND_ADDRESS is set, and if so, bind Envoy to that external address.
        if "AMBASSADOR_ENVOY_BIND_ADDRESS" in environ:
            envoy_bind_address = environ.get("AMBASSADOR_ENVOY_BIND_ADDRESS")
        else:
            envoy_bind_address = "0.0.0.0"

        # OK. Assemble the high-level stuff for Envoy.
        self.address = {
            "socket_address": {
                "address": envoy_bind_address,
                "port_value": self.port,
                "protocol": "TCP"
            }
        }

        self.filter_chains: List[dict] = []
        need_tcp_inspector = False

        for vhostname, vhost in self.vhosts.items():
            # Finalize this VirtualHost...
            vhost.finalize()

            if vhost._hostname == "*":
                domains = [vhost._hostname]
            else:
                if vhost._ctx is not None and vhost._ctx.hosts is not None and len(vhost._ctx.hosts) > 0:
                    domains = vhost._ctx.hosts
                else:
                    domains = [vhost._hostname]

            # ...then build up the Envoy structures around it.
            filter_chain: Dict[str, Any] = {
                "filter_chain_match": vhost.filter_chain_match,
            }

            if vhost.tls_context:
                filter_chain['transport_socket'] = {
                        'name': 'envoy.transport_sockets.tls',
                        'typed_config': {
                            '@type': 'type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext',
                            **vhost.tls_context,
                        }
                }
                need_tcp_inspector = True

            http_config = dict(self._base_http_config or {})
            http_config["route_config"] = {
                "virtual_hosts": [
                    {
                        "name": f"{self.name}-{vhost._name}",
                        "domains": domains,
                        "routes": vhost.routes
                    }
                ]
            }

            if parse_bool(self.config.ir.ambassador_module.get("strip_matching_host_port", "false")):
                http_config["strip_matching_host_port"] = True

            if parse_bool(self.config.ir.ambassador_module.get("merge_slashes", "false")):
                http_config["merge_slashes"] = True

            if parse_bool(self.config.ir.ambassador_module.get("reject_requests_with_escaped_slashes", "false")):
                http_config["path_with_escaped_slashes_action"] = "REJECT_REQUEST"

            filter_chain["filters"] = [
                {
                    "name": "envoy.filters.network.http_connection_manager",
                    "typed_config": {
                        "@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
                        **http_config
                    }
                }
            ]

            self.filter_chains.append(filter_chain)

        if self.use_proxy_proto:
            self.listener_filters.append({
                'name': 'envoy.filters.listener.proxy_protocol'
            })

        if need_tcp_inspector:
            self.listener_filters.append({
                'name': 'envoy.filters.listener.tls_inspector'
            })

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "address": self.address,
            "filter_chains": self.filter_chains,
            "listener_filters": self.listener_filters,
            "traffic_direction": self.traffic_direction
        }

    def pretty(self) -> dict:
        return { "name": self.name,
                 "port": self.port,
                 "use_proxy_proto": self.use_proxy_proto,
                 "vhosts": { k: v.pretty() for k, v in self.vhosts.items() } }

    @classmethod
    def dump_listeners(cls, logger, listeners_by_port) -> None:
        pretty = { k: v.pretty() for k, v in listeners_by_port.items() }

        logger.debug(f"V3Listeners: {dump_json(pretty, pretty=True)}")

    @classmethod
    def generate(cls, config: 'V3Config') -> None:
        config.listeners = []
        logger = config.ir.logger

        # It's important from a performance perspective to wrap debug log statements
        # with this check so we don't end up generating log strings (or even JSON
        # representations) that won't get logged anyway.
        log_debug = logger.isEnabledFor(logging.DEBUG)

        # OK, so we need to construct one or more V3Listeners, based on our IRListeners.
        # The highest-level thing that defines an Envoy listener is a port, so start
        # with that.

        listeners_by_port = V3ListenerCollection(config)

        # Also, in Edge Stack, the magic extremely-low-precedence / Mapping is always routed,
        # rather than being redirected. If a user doesn't want this behavior, they can override
        # the Mapping.

        first_irlistener_by_port: Dict[int, IRListener] = {}

        for irlistener in config.ir.listeners:
            if irlistener.service_port not in first_irlistener_by_port:
                first_irlistener_by_port[irlistener.service_port] = irlistener

            if log_debug:
                logger.debug(f"V3Listeners: working on {irlistener.pretty()}")

            # Grab a new V3Listener for this IRListener...
            listener = listeners_by_port.get(irlistener.service_port, irlistener.use_proxy_proto)
            listener.add_irlistener(irlistener)

            # What VirtualHost hostname are we trying to work with here?
            vhostname = irlistener.hostname or "*"

            listener.make_vhost(name=vhostname,
                                hostname=vhostname,
                                context=irlistener.context,
                                secure=True,
                                action=irlistener.secure_action,
                                insecure_action=irlistener.insecure_action)

            if (irlistener.insecure_addl_port is not None) and (irlistener.insecure_addl_port > 0):
                # Make sure we have a listener on the right port for this.
                listener = listeners_by_port.get(irlistener.insecure_addl_port, irlistener.use_proxy_proto)

                if irlistener.insecure_addl_port not in first_irlistener_by_port:
                    first_irlistener_by_port[irlistener.insecure_addl_port] = irlistener

                # Do we already have a VHost for this hostname?
                if vhostname not in listener.vhosts:
                    # Nope, add one. Also, no, it is not a bug to have action=None.
                    # There is no secure action for this vhost.
                    listener.make_vhost(name=vhostname,
                                        hostname=vhostname,
                                        context=None,
                                        secure=False,
                                        action=None,
                                        insecure_action=irlistener.insecure_action)

        if log_debug:
            logger.debug(f"V3Listeners: after IRListeners")
            cls.dump_listeners(logger, listeners_by_port)

        # Make sure that each listener has a '*' vhost.
        for port, listener in listeners_by_port.items():
            if not '*' in listener.vhosts:
                # Force the first VHost to '*'. I know, this is a little weird, but it's arguably
                # the least surprising thing to do in most situations.
                assert listener.first_vhost
                first_vhost = listener.first_vhost
                first_vhost._hostname = '*'
                first_vhost._name = f"{first_vhost._name}-forced-star"

        if config.ir.edge_stack_allowed and not config.ir.agent_active:
            # If we're running Edge Stack, and we're not an intercept agent, make sure we have
            # a listener on port 8080, so that we have a place to stand for ACME.

            if 8080 not in listeners_by_port:
                # Check for a listener on the main service port to see if the proxy proto
                # is enabled.
                main_listener = first_irlistener_by_port.get(config.ir.ambassador_module.service_port, None)
                use_proxy_proto = main_listener.use_proxy_proto if main_listener else False

                # Force a listener on 8080 with a VHost for '*' that rejects everything. The ACME
                # hole-puncher will override the reject for ACME, and nothing else will get through.
                if log_debug:
                    logger.debug(f"V3Listeners: listeners_by_port has no 8080, forcing Edge Stack listener on 8080")
                listener = listeners_by_port.get(8080, use_proxy_proto)

                # Remember, it is not a bug to have action=None. There is no secure action
                # for this vhost.
                listener.make_vhost(name="forced-8080",
                                    hostname="*",
                                    context=None,
                                    secure=False,
                                    action=None,
                                    insecure_action='Reject')

        prune_unreachable_routes = config.ir.ambassador_module['prune_unreachable_routes']

        # OK. We have all the listeners. Time to walk the routes (note that they are already ordered).
        for c_route in config.routes:
            # Remember which hosts this can apply to
            route_hosts = c_route.host_constraints(prune_unreachable_routes)

            # Remember, also, if a precedence was set.
            route_precedence = c_route.get('_precedence', None)

            if log_debug:
                logger.debug(f"V3Listeners: route {v3prettyroute(c_route)}...")

            # Build a cleaned-up version of this route without the '_sni' and '_precedence' elements...
            insecure_route: DictifiedV3Route = dict(c_route)
            insecure_route.pop('_sni', None)
            insecure_route.pop('_precedence', None)

            # ...then copy _that_ so we can make a secured version with an explicit XFP check.
            #
            # (Obviously the user may have put in an XFP check by hand here, in which case the
            # insecure_route isn't really insecure, but that's not actually up to us to mess with.)
            #
            # But wait, I hear you cry! Can't we use use require_tls: True in a VirtualHost?? Well,
            # no, not if we want to allow ACME challenges to flow through as cleartext at the same
            # time...
            secure_route = dict(insecure_route)

            found_xfp = False
            for header in secure_route["match"].get("headers", []):
                if header.get("name", "").lower() == "x-forwarded-proto":
                    found_xfp = True
                    break

            if not found_xfp:
                # Ew.
                match_copy = dict(secure_route["match"])
                secure_route["match"] = match_copy

                headers_copy = list(match_copy.get("headers") or [])
                match_copy["headers"] = headers_copy

                headers_copy.append({
                    "name": "x-forwarded-proto",
                    "exact_match": "https"
                })

            # Also gen up a redirecting route.
            redirect_route = dict(insecure_route)
            redirect_route.pop("route", None)
            redirect_route["redirect"] = {
                "https_redirect": True
            }

            # We now have a secure route and an insecure route, so we need to walk all listeners
            # and all vhosts, and match up the routes with the vhosts.

            for port, listener in listeners_by_port.items():
                for vhostkey, vhost in listener.vhosts.items():
                    # For each vhost, we need to look at things for the secure world as well
                    # as the insecure world, depending on what the action is exactly (and note
                    # that we can have an action of None if we're looking at a vhost created
                    # by an insecure_addl_port).

                    candidates: List[Tuple[bool, DictifiedV3Route, str]] = []
                    vhostname = vhost._hostname

                    if vhost._action is not None:
                        candidates.append(( True, secure_route, vhost._action ))

                    if vhost._insecure_action == "Redirect":
                        candidates.append(( False, redirect_route, "Redirect" ))
                    elif vhost._insecure_action is not None:
                        candidates.append((False, insecure_route, vhost._insecure_action))

                    for secure, route, action in candidates:
                        variant = "secure" if secure else "insecure"

                        if route["match"].get("prefix", None) == "/.well-known/acme-challenge/":
                            # We need to be sure to route ACME challenges, no matter what else is going
                            # on (this is the infamous ACME hole-puncher mentioned everywhere).
                            if log_debug:
                                logger.debug(f"V3Listeners: {listener.name} {vhostname} force Route for ACME challenge")
                            action = "Route"

                            # We have to force the correct route entry, too, just in case. (Note that right now,
                            # the user can't create a Mapping that forces redirection. When they can do this
                            # per-Mapping, well, really, we can't force them to not redirect if they explicitly
                            # ask for it, and that'll be OK.)

                            if secure:
                                route = secure_route
                            else:
                                route = insecure_route
                        elif ('*' not in route_hosts) and (vhostname != '*') and (vhostname not in route_hosts):
                            # Drop this because the host is mismatched.
                            if log_debug:
                                logger.debug(
                                    f"V3Listeners: {listener.name} {vhostname} {variant}: force Reject (rhosts {sorted(route_hosts)}, vhost {vhostname})")
                            action = "Reject"
                        elif (config.ir.edge_stack_allowed and
                              (route_precedence == -1000000) and
                              (route["match"].get("safe_regex", {}).get("regex", None) == "^/$")):
                            if log_debug:
                                logger.debug(
                                    f"V3Listeners: {listener.name} {vhostname} {variant}: force Route for fallback Mapping")
                            action = "Route"

                            # Force the actual route entry, instead of using the redirect_route, too.
                            # (If the user overrides the fallback with their own route at precedence -1000000,
                            # uh.... y'know what, on their own head be it.)
                            route = insecure_route

                        if action != 'Reject':
                            if log_debug:
                                logger.debug(
                                    f"V3Listeners: {listener.name} {vhostname} {variant}: Accept as {action}")
                            vhost.routes.append(route)
                        else:
                            if log_debug:
                                logger.debug(
                                    f"V3Listeners: {listener.name} {vhostname} {variant}: Drop")

        # OK. Finalize the world.
        for port, listener in listeners_by_port.items():
            listener.finalize()

        if log_debug:
            logger.debug("V3Listeners: after finalize")
            cls.dump_listeners(logger, listeners_by_port)

        for k, v in listeners_by_port.items():
            config.listeners.append(v.as_dict())

        # logger.info(f"==== ENVOY LISTENERS ====: {dump_json(config.listeners, pretty=True)}")

        # We need listeners for the TCPMappingGroups too.
        tcplisteners: Dict[str, V3TCPListener] = {}

        for irgroup in config.ir.ordered_groups():
            if not isinstance(irgroup, IRTCPMappingGroup):
                continue

            # OK, good to go. Do we already have a TCP listener binding where this one does?
            group_key = irgroup.bind_to()
            tcplistener = tcplisteners.get(group_key, None)

            if log_debug:
                config.ir.logger.debug("V3TCPListener: group at %s found %s listener" %
                                       (group_key, "extant" if tcplistener else "no"))

            if not tcplistener:
                # Nope. Make a new one and save it.
                tcplistener = config.save_element('listener', irgroup, V3TCPListener(config, irgroup))
                assert tcplistener
                config.listeners.append(tcplistener)
                tcplisteners[group_key] = tcplistener

            # Whether we just created this listener or not, add this irgroup to it.
            tcplistener.add_group(config, irgroup)
