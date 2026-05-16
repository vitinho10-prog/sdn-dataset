from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet
from collections import defaultdict

class SimpleSwitch(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = defaultdict(dict)
        self.logger.info("SimpleSwitch iniciado")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp   = ev.msg.datapath
        ofp  = dp.ofproto
        ofpp = dp.ofproto_parser
        # Table-miss: envia ao controlador
        match = ofpp.OFPMatch()
        actions = [ofpp.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        inst = [ofpp.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = ofpp.OFPFlowMod(datapath=dp, priority=0, match=match, instructions=inst)
        dp.send_msg(mod)
        self.logger.info("Switch conectado: dpid=%s", dp.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg    = ev.msg
        dp     = msg.datapath
        ofp    = dp.ofproto
        ofpp   = dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        dst = eth.dst
        src = eth.src
        dpid = dp.id

        self.mac_to_port[dpid][src] = in_port
        self.logger.info("PacketIn dpid=%s src=%s dst=%s in_port=%s", dpid, src, dst, in_port)

        out_port = self.mac_to_port[dpid].get(dst, ofp.OFPP_FLOOD)
        actions  = [ofpp.OFPActionOutput(out_port)]

        if out_port != ofp.OFPP_FLOOD:
            match = ofpp.OFPMatch(in_port=in_port, eth_dst=dst)
            inst  = [ofpp.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            mod   = ofpp.OFPFlowMod(datapath=dp, priority=1, match=match,
                                     instructions=inst, idle_timeout=30)
            dp.send_msg(mod)

        data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
        out  = ofpp.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)
