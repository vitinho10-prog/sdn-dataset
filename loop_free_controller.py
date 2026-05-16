from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4
from collections import defaultdict

class LoopFreeSwitch(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = defaultdict(dict)

    def add_flow(self, dp, priority, match, actions, idle=60):
        ofp  = dp.ofproto
        ofpp = dp.ofproto_parser
        inst = [ofpp.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod  = ofpp.OFPFlowMod(datapath=dp, priority=priority,
                                match=match, instructions=inst,
                                idle_timeout=idle)
        dp.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp   = ev.msg.datapath
        ofp  = dp.ofproto
        ofpp = dp.ofproto_parser
        # Drop IPv6 multicast — evita loops de MLD
        match_ipv6 = ofpp.OFPMatch(eth_type=0x86DD)
        self.add_flow(dp, 10, match_ipv6, [], idle=0)
        # Table-miss: envia ao controlador
        match = ofpp.OFPMatch()
        actions = [ofpp.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, match, actions, idle=0)
        self.logger.info("Switch conectado: dpid=%s", dp.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg     = ev.msg
        dp      = msg.datapath
        ofp     = dp.ofproto
        ofpp    = dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        # Ignorar IPv6
        if eth.ethertype == 0x86DD:
            return

        dst = eth.dst
        src = eth.src
        dpid = dp.id

        self.mac_to_port[dpid][src] = in_port
        out_port = self.mac_to_port[dpid].get(dst, ofp.OFPP_FLOOD)
        actions  = [ofpp.OFPActionOutput(out_port)]

        if out_port != ofp.OFPP_FLOOD:
            match = ofpp.OFPMatch(in_port=in_port, eth_dst=dst)
            self.add_flow(dp, 1, match, actions)
            self.logger.info("Flow instalado: dpid=%s %s->%s porta=%s", dpid, src, dst, out_port)
        else:
            self.logger.info("PacketIn flood: dpid=%s src=%s dst=%s in_port=%s", dpid, src, dst, in_port)

        data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
        out  = ofpp.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)
