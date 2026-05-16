from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet
from ryu.lib import hub
from collections import defaultdict
import time, json, socket, threading

STATS_INTERVAL = 1.0
IPC_HOST = "127.0.0.1"
IPC_PORT = 9999

class SDNController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = defaultdict(dict)
        self.datapaths = {}
        self.port_stats_prev = defaultdict(dict)
        self.port_stats_snapshot = []
        self.link_utilization = {}
        self._lock = threading.Lock()
        self._poll_thread = hub.spawn(self._stats_poll_loop)
        self._ipc_thread  = hub.spawn(self._ipc_publisher_loop)
        self.logger.info("SDNController iniciado")

    def _add_flow(self, dp, priority, match, actions, idle=30):
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
        # Drop IPv6
        self._add_flow(dp, 10, ofpp.OFPMatch(eth_type=0x86DD), [], idle=0)
        # Table-miss
        self._add_flow(dp, 0, ofpp.OFPMatch(),
                       [ofpp.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                             ofp.OFPCML_NO_BUFFER)], idle=0)
        with self._lock:
            self.datapaths[dp.id] = dp
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
        if eth is None or eth.ethertype == 0x86DD:
            return

        dst, src, dpid = eth.dst, eth.src, dp.id
        with self._lock:
            self.mac_to_port[dpid][src] = in_port

        out_port = self.mac_to_port[dpid].get(dst, ofp.OFPP_FLOOD)
        actions  = [ofpp.OFPActionOutput(out_port)]

        if out_port != ofp.OFPP_FLOOD:
            match = ofpp.OFPMatch(in_port=in_port, eth_dst=dst)
            self._add_flow(dp, 1, match, actions)

        data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
        dp.send_msg(ofpp.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        msg  = ev.msg
        dp   = msg.datapath
        dpid = dp.id
        now  = time.time()
        snapshot = []

        with self._lock:
            for stat in msg.body:
                port_no = stat.port_no
                if port_no >= dp.ofproto.OFPP_MAX:
                    continue
                tx_bytes = stat.tx_bytes
                rx_bytes = stat.rx_bytes
                tx_bps = rx_bps = 0.0
                prev = self.port_stats_prev[dpid].get(port_no)
                if prev:
                    dt = now - prev['ts']
                    if dt > 0:
                        tx_bps = (tx_bytes - prev['tx_bytes']) * 8 / dt
                        rx_bps = (rx_bytes - prev['rx_bytes']) * 8 / dt
                self.port_stats_prev[dpid][port_no] = {
                    'tx_bytes': tx_bytes, 'rx_bytes': rx_bytes, 'ts': now}
                cap = 1_000_000_000
                util = min(max(tx_bps, rx_bps) / cap * 100, 100.0)
                link_id = 'dp%d:p%d' % (dpid, port_no)
                entry = {
                    'timestamp': now, 'link_id': link_id,
                    'dpid': dpid, 'port_no': port_no,
                    'tx_mbps': tx_bps/1e6, 'rx_mbps': rx_bps/1e6,
                    'util_pct': util,
                    'tx_pkts': stat.tx_packets, 'rx_pkts': stat.rx_packets,
                    'tx_errors': stat.tx_errors, 'rx_errors': stat.rx_errors,
                }
                snapshot.append(entry)
                self.link_utilization[link_id] = {
                    'timestamp': now, 'util_pct': util,
                    'tx_mbps': tx_bps/1e6, 'rx_mbps': rx_bps/1e6}
            self.port_stats_snapshot = snapshot

    def _stats_poll_loop(self):
        while True:
            hub.sleep(STATS_INTERVAL)
            with self._lock:
                dps = list(self.datapaths.values())
            for dp in dps:
                try:
                    ofpp = dp.ofproto_parser
                    dp.send_msg(ofpp.OFPPortStatsRequest(
                        dp, 0, dp.ofproto.OFPP_ANY))
                except Exception as e:
                    self.logger.warning("Stats error: %s", e)

    def _ipc_publisher_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((IPC_HOST, IPC_PORT))
            srv.listen(5)
            srv.setblocking(False)
            self.logger.info("IPC listening on %s:%d", IPC_HOST, IPC_PORT)
        except Exception as e:
            self.logger.error("IPC bind error: %s", e)
            return
        clients = []
        while True:
            hub.sleep(STATS_INTERVAL)
            try:
                conn, addr = srv.accept()
                conn.setblocking(False)
                clients.append(conn)
            except BlockingIOError:
                pass
            with self._lock:
                snapshot = list(self.port_stats_snapshot)
            if not snapshot:
                continue
            payload = (json.dumps({'ts': time.time(), 'stats': snapshot}) + '\n').encode()
            dead = []
            for c in clients:
                try:
                    c.sendall(payload)
                except Exception:
                    dead.append(c)
            for c in dead:
                clients.remove(c)
