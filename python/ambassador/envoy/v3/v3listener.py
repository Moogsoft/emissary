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
from typing import Any, Dict, List, Optional, Set, Tuple, Union, TYPE_CHECKING
from typing import cast as typecast

from os import environ

import logging

from ...ir.irlistener import IRListener
from ...ir.irtcpmappinggroup import IRTCPMappingGroup

from ...utils import dump_json, parse_bool

from .v3httpfilter import V3HTTPFilter
from .v3route import DictifiedV3Route, V3RouteVariants, v3prettyroute, hostglob_matches
from .v3tls import V3TLSContext
from .v3virtualhost import V3VirtualHost

if TYPE_CHECKING:
    from ...ir.irhost import IRHost             # pragma: no cover
    from ...ir.irtlscontext import IRTLSContext # pragma: no cover
    from . import V3Config                      # pragma: no cover


def route_host_match(route_hosts: Set[str], vhostname: str) -> bool:
    return any(hostglob_matches(route_glob, vhostname) for route_glob in route_hosts)    


class V3Listener(dict):
    def __init__(self, config: 'V3Config', irlistener: IRListener) -> None:
        super().__init__()

        self.config = config
        self.bind_address = irlistener.bind_address
        self.port = irlistener.port
        self.bind_to = f"{self.bind_address}-{self.port}"

        bindstr = f"-{self.bind_address}" if (self.bind_address != "0.0.0.0") else ""
        self.name = f"ambassador-listener{bindstr}-{self.port}"

        self.use_proxy_proto = False
        self.listener_filters: List[dict] = []
        self.traffic_direction: str = "UNSPECIFIED"
        self._security_model: str = irlistener.securityModel
        self._l7_depth: int = irlistener.get('l7Depth', 0)
        self._insecure_only: bool = False
        self._filter_chains: List[dict] = []
        self._base_http_config: Optional[Dict[str, Any]] = None
        self._vhosts: Dict[str, V3VirtualHost] = {}

        # It's important from a performance perspective to wrap debug log statements
        # with this check so we don't end up generating log strings (or even JSON
        # representations) that won't get logged anyway.
        self._log_debug = self.config.ir.logger.isEnabledFor(logging.DEBUG)
        if self._log_debug:
            self.config.ir.logger.debug(f"V3Listener {self.name} created -- {self._security_model}, l7Depth {self._l7_depth}")

        # If the IRListener is marked insecure-only, so are we.
        self._insecure_only = irlistener.insecure_only

        # Build out our listener filters, and figure out if we're an HTTP listener
        # in the process.
        for proto in irlistener.protocolStack:
            if proto == "HTTP":
                # Start by building our base HTTP config...
                self._base_http_config = self.base_http_config()

            if proto == "PROXY":
                self.listener_filters.append({
                    'name': 'envoy.filters.listener.proxy_protocol'
                })

            if proto == "TLS":
                self.listener_filters.append({
                    'name': 'envoy.filters.listener.tls_inspector'
                })

            if proto == "TCP":
                # TCP doesn't require any specific listener filters, but it
                # does require stuff in the filter chains. We can go ahead and
                # tackle that here.
                for irgroup in self.config.ir.ordered_groups():
                    if not isinstance(irgroup, IRTCPMappingGroup):
                        continue
                    
                    if irgroup.bind_to() != self.bind_to: 
                        # self.config.ir.logger.info("V3Listener %s: skip TCPMappingGroup on %s", self.bind_to, irgroup.bind_to())
                        continue
                    
                    self.add_tcp_group(irgroup)

    def add_tcp_group(self, irgroup: IRTCPMappingGroup) -> None:
        # self.config.ir.logger.info("V3Listener %s: take TCPMappingGroup on %s", self.bind_to, irgroup.bind_to())

        # First up, which clusters do we need to talk to?
        clusters = [{
            'name': mapping.cluster.envoy_name,
            'weight': mapping.weight
        } for mapping in irgroup.mappings]

        # From that, we can sort out a basic tcp_proxy filter config.
        tcp_filter = {
            'name': 'envoy.filters.network.tcp_proxy',
            'typed_config': {
                '@type': 'type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy',
                'stat_prefix': 'ingress_tcp_%d' % irgroup.port,
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

        # Start by assuming an empty filter match.
        filter_match = {}

        # Then, if SNI is a thing, update the chain entry with the appropriate chain match.
        if irgroup.get('tls_context', None):
            # # We need TLS for this chain...
            # filter_match['transport_protocol'] = 'tls'

            # ...and we need the correct context.
            envoy_ctx = V3TLSContext(irgroup.tls_context)

            if envoy_ctx:
                chain_entry['transport_socket'] = {
                    'name': 'envoy.transport_sockets.tls',
                    'typed_config': {
                        '@type': 'type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext',
                        **envoy_ctx
                    }
                }

            # Do we have a host match?
            host_wanted = irgroup.get('host') or '*'

            if host_wanted != '*':
                # Yup. Hook it in.
                filter_match['server_names'] = [ host_wanted ]

        # Once all of that is done, hook in the match...
        chain_entry['filter_chain_match'] = filter_match
     
        # ...and stick this chain into our filter.
        self._filter_chains.append(chain_entry)

    def add_vhost(self, name: str, host: 'IRHost', secure: bool) -> None:
        # None is OK for a secure action, though not for an insecure action. For real.
        secure_action = host.secure_action if secure else None
        insecure_action = host.insecure_action

        if self._log_debug:
            self.config.ir.logger.debug("V3Listener %s: adding %s '%s' %s)" %
                                       (self.name, "secure" if secure else "insecure", name, host))

        secure_key = 'secure' if secure else 'insecure'
        ctx_key = host.context.name if host.context else '<cleartext>'
        vhost_key = f"{host.hostname}-{secure_key}-{ctx_key}"

        vhost = self._vhosts.get(vhost_key)

        if vhost:
            if ((host.hostname != vhost._hostname) or
                (host.context != vhost._ctx) or
                (secure != vhost._secure) or
                (secure_action != vhost._action) or
                (insecure_action != vhost._insecure_action)):
                raise Exception("V3Listener %s: trying to make vhost %s for %s but one already exists" %
                                (self.name, name, host.hostname))
            else:
                return

        context = host.context if secure else None

        vhost = V3VirtualHost(config=self.config, listener=self,
                              name=name, hostname=host.hostname, ctx=context,
                              secure=secure, action=secure_action, insecure_action=insecure_action)
        self._vhosts[vhost_key] = vhost

    # access_log constructs the access_log configuration for this V3Listener
    def access_log(self) -> List[dict]:
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

            if self._log_debug:
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
    def base_http_config(self) -> Dict[str, Any]:
        base_http_config: Dict[str, Any] = {
            'stat_prefix': 'ingress_http',
            'access_log': self.access_log(),
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

    def finalize(self) -> None:
        # if self._log_debug:
        #     self.config.ir.logger.debug(f"V3Listener finalize {self}")
        self.config.ir.logger.info(f"V3Listener: ==== finalize {self}")

        # OK. Assemble the high-level stuff for Envoy.
        self.address = {
            "socket_address": {
                "address": self.bind_address,
                "port_value": self.port,
                "protocol": "TCP"
            }
        }

        # Next, deal with HTTP stuff if this is an HTTP Listener.
        if self._base_http_config:
            self.finalize_vhosts()
            self.finalize_routes()
            self.finalize_http()

    def finalize_vhosts(self) -> None:
        # Match up Hosts with this Listener, and create VHosts for them.
        for host in self.config.ir.get_hosts():
            # XXX Reject if labels don't match.

            # OK, if we're still here, then it's a question of matching the Listener's 
            # SecurityModel with the Host's requestPolicy.
            # 
            # First up, if the Listener is marked insecure-only, but the Listener's port
            # doesn't match the Host's insecure_addl_port, don't take this Host: this 
            # Listener was synthesized to handle some other Host. (This is a corner case that
            # will become less and less likely as more people hop on the Listener bandwagon.
            # Also, remember that Hosts don't specify bind addresses, so only the port matters
            # here.)

            if self._insecure_only and (self.port != host.insecure_addl_port):
                self.config.ir.logger.info("V3Listener %s (%s): drop %s, insecure-only port mismatch",
                                           self.name, self._security_model, host.name)
                continue

            # OK, we can't drop it for that, so we need to check the actions.

            vhostname = host.hostname or "*"

            security_model = self._security_model
            secure_action = host.secure_action
            insecure_action = host.insecure_action

            # If the Listener's securityModel is SECURE, but this host has a secure_action
            # of Reject (or empty), we'll skip this host, because the only requests this 
            # Listener can ever produce will be rejected. In any other case, we'll take
            # this Host on the secure side.

            will_reject_secure = ((not secure_action) or (secure_action == "Reject"))

            if not ((security_model == "SECURE") and will_reject_secure):
                self.config.ir.logger.info("V3Listener %s: take SECURE %s", self.name, host)
                self.add_vhost(name=vhostname, host=host, secure=True)

                # We can be done here because there's really only one VHost here -- we don't
                # need to check the insecure action separately if the secure side worked.
                continue

            # No hit on the secure side, so check the insecure side. Same idea here: only skip
            # the Host if the Listener's securityModel is INSECURE but the Host's insecure_action
            # is Reject.

            if not ((security_model == "INSECURE") and (insecure_action == "Reject")):
                self.config.ir.logger.info("V3Listener %s: take INSECURE %s", self.name, host)
                self.add_vhost(name=vhostname, host=host, secure=False)

    def finalize_routes(self) -> None:
        logger = self.config.ir.logger

        # Walk all the routes we know about, and figure out how routes match up to Hosts.
        #
        # Note that the data structure we're walking here is config.route_variants rather
        # than config.routes. There's a one-to-one correspondence between the two, but
        # we use the V3RouteVariants to lazily cache some of the work that we're doing
        # across Hosts.
        for rv in self.config.route_variants:
            logger.info("CHECK ROUTE: %s", v3prettyroute(dict(rv.route)))

            # For each route, go walk all our vhosts and match things up.
            for vhostkey, vhost in self._vhosts.items():
                logger.info(f"    {vhost.pretty()}")

                # For each vhost, we need to look at things for the secure world as well
                # as the insecure world, depending on what the action is exactly (and note
                # that, yes, we can have an action of None for an insecure_only listener).
                # 
                # "candidates" is matcher, action, V3RouteVariants
                candidates: List[Tuple[str, str, V3RouteVariants]] = []
                vhostname = vhost._hostname

                if (vhost._action is not None) and (self._security_model != "INSECURE"):
                    # We have a secure action, and we're willing to believe that at least some of
                    # our requests will be secure.
                    matcher = 'always' if (self._security_model == 'SECURE') else 'xfp-https'

                    candidates.append(( matcher, 'Route', rv ))
                
                if (vhost._insecure_action is not None) and (self._security_model != "SECURE"):
                    # We have an insecure action, and we're willing to believe that at least some of
                    # our requests will be insecure.
                    matcher = 'always' if (self._security_model == 'INSECURE') else 'xfp-http'
                    action = vhost._insecure_action

                    candidates.append(( matcher, action, rv ))

                for matcher, action, rv in candidates:
                    route_precedence = rv.route.get('_precedence', None)
                    route_hosts = rv.route['_host_constraints']
                    extra_info = ""

                    if rv.route["match"].get("prefix", None) == "/.well-known/acme-challenge/":
                        # We need to be sure to route ACME challenges, no matter what else is going
                        # on (this is the infamous ACME hole-puncher mentioned everywhere).
                        extra_info = " (force Route for ACME challenge)"
                        action = "Route"
                    elif ('*' not in route_hosts) and (vhostname != '*') and (not route_host_match(route_hosts, vhostname)):
                        # Drop this because the host is mismatched.
                        extra_info = f" (force Reject for mismatched host {sorted(route_hosts)})"
                        action = "Reject"
                    elif (self.config.ir.edge_stack_allowed and
                            (route_precedence == -1000000) and
                            (rv.route["match"].get("safe_regex", {}).get("regex", None) == "^/$")):
                        extra_info = " (force Route for fallback Mapping)"
                        action = "Route"

                    if action != 'Reject':
                        # Worth noting here that "Route" really means "do what the V3Route really says", which
                        # might be a host redirect. When we talk about "Redirect", we really mean "redirect to HTTPS".

                        if True or self._log_debug:
                            logger.info("      %s - %s: accept on %s %s%s",
                                        matcher, action, self.name, vhostname, extra_info)
                        vhost.routes.append(rv.get_variant(matcher, action.lower()))
                    else:
                        if True or self._log_debug:
                            logger.info("      %s - %s: drop from %s %s%s",
                                        matcher, action, self.name, vhostname, extra_info)

    def finalize_http(self) -> None:
        for vhostkey, vhost in self._vhosts.items():
            # Every VHost has a bunch of routes that need to be shoved into its filters.
            filter_chain_match: Dict[str, Any] = {}

            if vhost._ctx:
                filter_chain_match["transport_protocol"] = "tls"

            # Make sure we include a server name match if the hostname isn't "*".
            if vhost._hostname and (vhost._hostname != '*'):
                    filter_chain_match["server_names"] = [ vhost._hostname ]

            if vhost._hostname == "*":
                domains = [vhost._hostname]
            else:
                if vhost._ctx is not None and vhost._ctx.hosts is not None and len(vhost._ctx.hosts) > 0:
                    domains = vhost._ctx.hosts
                else:
                    domains = [vhost._hostname]

            # ...then build up the Envoy structures around it.
            filter_chain: Dict[str, Any] = {
                "filter_chain_match": filter_chain_match,
            }

            if vhost.tls_context:
                filter_chain['transport_socket'] = {
                    'name': 'envoy.transport_sockets.tls',
                    'typed_config': {
                        '@type': 'type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext',
                        **vhost.tls_context
                    }
                }

            http_config = dict(typecast(dict, self._base_http_config))

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

            self._filter_chains.append(filter_chain)

    def as_dict(self) -> dict:
        odict = {
            "name": self.name,
            "address": self.address,
            "filter_chains": self._filter_chains,
            "traffic_direction": self.traffic_direction
        }

        if self.listener_filters:
            odict["listener_filters"] = self.listener_filters

        return odict          

    def pretty(self) -> dict:
        return {
            "name": self.name,
            "bind_address": self.bind_address,
            "port": self.port,
            "vhosts": [ self._vhosts[k].verbose_dict() for k in sorted(self._vhosts.keys()) ],
            #  "use_proxy_proto": self.use_proxy_proto,
        }

    def __str__(self) -> str:
        return "<V3Listener %s %s on %s:%d [%s]>" % (
            "HTTP" if self._base_http_config else "TCP",
            self.name, self.bind_address, self.port, self._security_model
        )

    @classmethod
    def dump_listeners(cls, logger, listeners_by_port) -> None:
        pretty = { k: v.pretty() for k, v in listeners_by_port.items() }

        logger.debug(f"V3Listeners: {dump_json(pretty, pretty=True)}")

    @classmethod
    def generate(cls, config: 'V3Config') -> None:
        config.listeners = []
        logger = config.ir.logger

        for key in sorted(config.ir.listeners.keys()):
            irlistener = config.ir.listeners[key]
            v3listener = V3Listener(config, irlistener)
            v3listener.finalize()

            config.listeners.append(v3listener)
            config.ir.logger.info(f"V3Listener: ==== GENERATED {v3listener}")
            
            for k in sorted(v3listener._vhosts.keys()):
                config.ir.logger.info("    %s", v3listener._vhosts[k].pretty())
